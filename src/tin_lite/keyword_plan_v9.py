"""Keyword research that screens before it selects, with credible competitors and honest priorities.

A review of all 36 production runs (October 6, 2026) found three faults in the research v8
inherited, none of them a failure:

- Competitor discovery returned the target's search neighbours, not its rivals: youtube.com,
  facebook.com, reddit.com and linkedin.com in most runs, and namesakes or lookalikes for
  small sites (another TLD of the same name, or a similar-looking one). Each became a source with an
  equal share of the 300 candidate slots.
- Candidates were capped at 300 in provider order before buyer screening, so a run that
  collected 817 keywords left 517 unscreened and kept 2 direct ones. In 9 of 31 collections
  fewer than one candidate in ten fit the buyer.
- 22 of 69 high-priority groups had no measured demand in any member: a proposed seed with
  no database row was rated high and became a planned article.

v9 keeps v8's lookups, seeds contract, samples, review cap and schemas, and changes:

- Discovery asks for twenty competitor domains instead of five and drops general platforms,
  the target's namesakes and domains sharing fewer than three ranked keywords with it, then
  keeps the first three as before. Dropped domains stay in the evidence with their reason.
- Up to 600 collected candidates are screened (round-robin by source, as before), then the
  300 for review are chosen direct first, then adjacent, then the rest.
- A group may be high priority only when a member has measured demand: provider search
  volume or Search Console impressions above zero. The review is told so, and a high group
  without it is kept at medium priority rather than failing the run.

Screening twelve batches instead of six fits the $2 floor because its caps are sized to the
largest output seen in production (2,320 tokens for 50 keywords): 12,000 tokens first and
24,000 on the one retry. The largest possible batch request is about 49,800 bytes, so the
bound is 52,000; Tin's usage recorder counts 56,096 input tokens for it. At
`service_pricing.CARD` standard-band rates ($0.125 per million input or cache writes, $0.50
per million output) the worst first attempt costs $0.013012 and the worst retry $0.019012;
they reserve $0.014 and $0.02. A full run reserves at most seeds $0.10, review $0.15,
screening 12 x ($0.014 + $0.02) = $0.408, 22 lookups $1.10 and 40 samples $0.20: $1.958.
"""

from __future__ import annotations

from collections import defaultdict, deque

from tin_lite import keyword_plan as v1
from tin_lite import keyword_plan_v2 as v2
from tin_lite import keyword_plan_v8 as v8

POLICY = {
    **v8.POLICY,
    "version": "keyword-plan-v9",
    "screen_candidates": 600,
    "triage_output_tokens": 12_000,
    "triage_retry_output_tokens": 24_000,
    "triage_max_request_bytes": 52_000,
    "triage_reservation_usd": "0.014",
    "triage_retry_reservation_usd": "0.02",
    "competitor_discovery_rows": 20,
    "competitor_min_shared_keywords": 3,
    "selection": "screen_then_select_by_fit.v1",
    "high_priority": "measured_demand.v1",
}

# General platforms that rank for nearly anything. Matched on the domain's first label, so
# regional storefronts (amazon.co.uk, capterra.co.uk) are covered too.
PLATFORMS = frozenset(
    {
        "amazon",
        "apple",
        "bing",
        "capterra",
        "ebay",
        "etsy",
        "facebook",
        "g2",
        "gartner",
        "github",
        "google",
        "instagram",
        "linkedin",
        "medium",
        "microsoft",
        "pinterest",
        "quora",
        "reddit",
        "stackexchange",
        "stackoverflow",
        "substack",
        "tiktok",
        "trustpilot",
        "twitter",
        "wikihow",
        "wikipedia",
        "wiktionary",
        "x",
        "yahoo",
        "yelp",
        "youtube",
    }
)

INSTRUCTIONS = {
    **v8.INSTRUCTIONS,
    "seeds": v8.INSTRUCTIONS["seeds"]
    + """
Each core phrase must still name what this product is, in the words its buyer uses when
looking for this kind of product. A phrase whose typical searcher wants a different kind of
product (a generic 'browser app' for a marketing tool, 'project management' for a narrow
tracker) spends a lookup on keywords screening will reject.""",
    "review": v8.INSTRUCTIONS["review"].replace(
        "Explicit niche integrations can be high priority with unknown volume. ", ""
    )
    + """
High priority also requires measured demand: at least one member with measured_demand true
(provider search volume or Search Console impressions above zero). A group with none stays
medium or low however well it fits; say in its rationale that demand is unmeasured. Weigh
keyword difficulty against the site: a very difficult head term is rarely high priority for
a site that ranks for little.""",
}
SCHEMAS = v8.SCHEMAS
seed_values = v8.seed_values

assert "can be high priority with unknown volume" not in INSTRUCTIONS["review"]


def competitor_domains(items: list[dict], *, target: str, limit: int) -> tuple[list, list]:
    """Credible search competitors in provider order, and the ones dropped with a reason."""
    target = target.removeprefix("www.")
    name = target.split(".")[0]
    kept, dropped = [], []
    for item in items:
        try:
            domain = v1.host(item["domain"]).removeprefix("www.")
        except (KeyError, TypeError, ValueError):
            continue
        if domain == target or domain in kept or any(row["domain"] == domain for row in dropped):
            continue
        label = domain.split(".")[0]
        shared = item.get("intersections")
        if label in PLATFORMS:
            reason = "general_platform"
        elif label == name:
            reason = "namesake"
        elif (
            isinstance(shared, int)
            and not isinstance(shared, bool)
            and shared < POLICY["competitor_min_shared_keywords"]
        ):
            reason = "few_shared_keywords"
        else:
            kept.append(domain)
            continue
        dropped.append({"domain": domain, "reason": reason})
    return kept[:limit], dropped[: POLICY["competitor_discovery_rows"]]


def screening_pool(sources: dict[str, list[dict]], *, limit: int) -> tuple[list, int]:
    """Up to `limit` distinct keywords taken one per source in turn, and how many exist."""
    queues = [deque(rows) for rows in sources.values()]
    pool: dict[str, dict] = {}
    while any(queues) and len(pool) < limit:
        for queue in queues:
            while queue:
                row = queue.popleft()
                if row["id"] not in pool:
                    pool[row["id"]] = {"id": row["id"], "keyword": row["keyword"]}
                    break
            if len(pool) == limit:
                break
    return list(pool.values()), len({row["id"] for rows in sources.values() for row in rows})


FIT_ORDER = ("direct", "adjacent", "unclear", "generic", "wrong_buyer")


def select_screened(sources: dict[str, list[dict]], fits: dict[str, str], *, limit: int):
    """The `limit` screened candidates to review, best buyer fit first.

    Within a fit, one keyword per source in turn, as before. Observations and byte bounds are
    `select_candidates`'s. Returns candidates labelled with their fit, and coverage.
    """
    chosen: list[str] = []
    for fit in FIT_ORDER:
        if len(chosen) == limit:
            break
        tier = {
            name: [row for row in rows if fits.get(row["id"]) == fit]
            for name, rows in sources.items()
        }
        picked, _ = screening_pool(tier, limit=limit - len(chosen))
        chosen.extend(row["id"] for row in picked if row["id"] not in chosen)
    keep = set(chosen)
    candidates, coverage = v1.select_candidates(
        {name: [row for row in rows if row["id"] in keep] for name, rows in sources.items()},
        limit=len(chosen),
    )
    by_fit = defaultdict(int)
    for row in candidates:
        row["buyer_fit"] = fits[row["id"]]
        by_fit[row["buyer_fit"]] += 1
    collected = len({row["id"] for rows in sources.values() for row in rows})
    return candidates, {
        "unique_collected": collected,
        "screened": len(fits),
        "selected": len(candidates),
        "omitted": collected - len(candidates),
        "selection": "round_robin_by_source; screened before selection; best buyer fit first; "
        "candidate and byte caps",
        "observations_omitted": coverage["observations_omitted"],
        "buyer_fit": {fit: sum(value == fit for value in fits.values()) for fit in v2.FIT},
        "selected_buyer_fit": {fit: by_fit[fit] for fit in v2.FIT},
    }


def measured_demand(candidate: dict) -> bool:
    return any(
        (row.get("search_volume") or 0) > 0 or (row.get("impressions") or 0) > 0
        for row in candidate["observations"]
    )


def review_input(*, scope: dict, candidates: list, samples: dict, coverage: dict) -> dict:
    data = v2.review_input(scope=scope, candidates=candidates, samples=samples, coverage=coverage)
    eligible = [row for row in candidates if row["buyer_fit"] in v2.ELIGIBLE]
    for row, original in zip(data["candidates"], eligible, strict=True):
        row["measured_demand"] = measured_demand(original)
    return data


def expand_review(value: dict, candidates: list) -> tuple[dict, list[str]]:
    """v2's validated review, with high groups lacking measured demand kept at medium.

    Returns the review and the primary keyword IDs of the groups it lowered.
    """
    review = v2.expand_review(value, candidates)
    by_id = {row["id"]: row for row in candidates}
    lowered = []
    for group in review["groups"]:
        if group["priority"] == "high" and not any(
            measured_demand(by_id[key]) for key in group["keyword_ids"]
        ):
            group["priority"] = "medium"
            lowered.append(group["primary_keyword_id"])
    return review, lowered
