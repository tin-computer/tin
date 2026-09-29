"""Search Console evidence for organic-audit-v10: page and query rows, and what they show.

Rows describe the connected property for the saved date window; they are appearance
evidence, not an indexing verdict for pages that are absent. Pure functions only.
"""

from __future__ import annotations

import math
import re
from urllib.parse import urlsplit

from tin_lite.organic_audit_format import count, site_finding
from tin_lite.organic_audit_site import is_utility_url, language_prefix, segments, url_key

# A conservative planning curve for organic click-through by rounded position. It is
# used only to say "far fewer clicks than usual", never as a forecast.
EXPECTED_CTR = {
    1: 0.20,
    2: 0.12,
    3: 0.08,
    4: 0.06,
    5: 0.045,
    6: 0.035,
    7: 0.03,
    8: 0.025,
    9: 0.02,
    10: 0.017,
}
LOW_CTR_SHARE = 0.35
LOW_CTR_MIN_EXPECTED_CLICKS = 3
TOPIC_MARKERS = frozenset({"alternatives", "alternative", "competitors", "competitor"})


def _metric(value, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Invalid Search Console {name}")
    if value < 0:
        raise ValueError(f"Invalid Search Console {name}")
    return value


def search_console_rows(raw: dict, dimensions: tuple[str, ...], *, in_scope, max_rows: int):
    """Validated rows for the requested dimensions, filtered to the audited hosts."""
    rows = raw.get("rows", [])
    if not isinstance(rows, list) or len(rows) > max_rows:
        raise ValueError("Search Console exceeded its row contract")
    result = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("keys"), list)
            or len(row["keys"]) != len(dimensions)
            or not all(isinstance(key, str) for key in row["keys"])
        ):
            raise ValueError("Invalid Search Console row")
        values = dict(zip(dimensions, row["keys"], strict=True))
        if not in_scope(values["page"]):
            continue
        clicks = _metric(row.get("clicks"), name="clicks")
        impressions = _metric(row.get("impressions"), name="impressions")
        position = _metric(row.get("position", 0), name="position")
        if clicks > impressions:
            raise ValueError("Search Console clicks exceed impressions")
        item = {
            "url": values["page"][:2000],
            "clicks": clicks,
            "impressions": impressions,
            "position": round(position, 2),
        }
        if "query" in values:
            item["query"] = " ".join(values["query"].split())[:300]
        result.append(item)
    return result, len(rows)


def expected_ctr(position: float) -> float | None:
    rounded = max(1, math.floor(position))
    return EXPECTED_CTR.get(rounded)


def page_topic(url: str) -> tuple[str, str] | None:
    """URL template and topic: /compare/semrush-alternatives and /alternatives/semrush
    both have topic "semrush" under templates /compare/{x}-alternatives and /alternatives/{x}.
    """
    parts = [part.lower() for part in segments(url)]
    if not parts:
        return None
    prefix = "".join(f"/{part}" for part in parts[:-1])
    tokens = parts[-1].split("-")
    if len(tokens) > 1 and tokens[-1] in TOPIC_MARKERS:
        return f"{prefix}/{{x}}-{tokens[-1]}", "-".join(tokens[:-1])
    if len(tokens) > 2 and tokens[0] in TOPIC_MARKERS and tokens[1] in {"to", "for"}:
        return f"{prefix}/{tokens[0]}-{tokens[1]}-{{x}}", "-".join(tokens[2:])
    return f"{prefix}/{{x}}", parts[-1]


def brand_terms(host: str, panel: dict | None) -> list[str]:
    base = host.removeprefix("www.")
    label = base.split(".")[0]
    terms = {base, base.replace(".", " ")}
    if len(label) >= 5:
        terms.add(label)
    if panel:
        terms.update([panel.get("name", ""), *panel.get("aliases", [])])
    return sorted({" ".join(t.casefold().split()) for t in terms if len(t.strip()) >= 2})


def _contains(text: str, term: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text.casefold()))


def _fmt(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def _path(url: str, host: str) -> str:
    """The path (and query) alone; the host is named once at the top of the report."""
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return path if parts.hostname == host else f"{parts.hostname}{path}"


def _aggregate_queries(queries: list[dict]) -> dict[str, dict[str, dict]]:
    """query → page key → summed clicks/impressions and impression-weighted position."""
    groups: dict[str, dict[str, dict]] = {}
    for row in queries:
        query = row["query"].casefold()
        key = url_key(row["url"])
        entry = groups.setdefault(query, {}).setdefault(
            key,
            {"url": row["url"], "clicks": 0, "impressions": 0, "weighted": 0.0, "query": query},
        )
        entry["clicks"] += row["clicks"]
        entry["impressions"] += row["impressions"]
        entry["weighted"] += row["position"] * row["impressions"]
    for pages in groups.values():
        for entry in pages.values():
            entry["position"] = (
                round(entry["weighted"] / entry["impressions"], 1) if entry["impressions"] else 0
            )
    return groups


def search_findings(
    *,
    host: str,
    pages: list[dict],
    queries: list[dict],
    window: str,
    titles: dict[str, str],
    policy: dict,
    brand: list[str],
) -> list[dict]:
    """Cannibalization, near-page-one queries, low CTR, and brand searches on the wrong page."""
    findings = []
    total_impressions = sum(row["impressions"] for row in pages) or sum(
        row["impressions"] for row in queries
    )
    groups = _aggregate_queries(queries)
    page_totals: dict[str, dict] = {}
    for row in pages:
        entry = page_totals.setdefault(
            url_key(row["url"]),
            {"url": row["url"], "clicks": 0, "impressions": 0, "position": row["position"]},
        )
        entry["clicks"] += row["clicks"]
        entry["impressions"] += row["impressions"]

    # (a) Two or more pages competing for the same search.
    competing = {
        query: sorted(entries.values(), key=lambda e: (-e["impressions"], e["url"]))
        for query, entries in groups.items()
        if len([e for e in entries.values() if e["impressions"] > 0]) >= 2
    }
    if competing:
        ranked = sorted(
            competing.items(),
            key=lambda item: (-sum(e["impressions"] for e in item[1]), item[0]),
        )
        involved: dict[str, float] = {}
        for _, entries in ranked:
            for entry in entries:
                involved[entry["url"]] = involved.get(entry["url"], 0) + entry["impressions"]
        templates: dict[str, dict[str, str]] = {}
        for row in pages:
            # Translations are linked with hreflang, not merged; keep them out of patterns.
            topic = None if language_prefix(row["url"]) else page_topic(row["url"])
            if topic:
                templates.setdefault(topic[0], {})[topic[1]] = url_key(row["url"])
        pairs = []
        names = sorted(templates)
        competing_keys = {
            frozenset(url_key(e["url"]) for e in entries) for entries in competing.values()
        }
        for index, first in enumerate(names):
            for second in names[index + 1 :]:
                shared = sorted(set(templates[first]) & set(templates[second]))
                if not shared:
                    continue
                overlapping = sum(
                    any(
                        {templates[first][t], templates[second][t]} <= keys
                        for keys in competing_keys
                    )
                    for t in shared
                )
                if len(shared) >= 2 or overlapping:
                    pairs.append((first, second, shared, overlapping))
        pairs.sort(key=lambda p: (-p[3], -len(p[2]), p[0], p[1]))
        lines = [
            f"{count(len(competing), 'search', 'searches')} sent impressions to two or more "
            f"of your pages ({window})."
        ]
        pattern_stats = []
        for first, second, shared, _ in pairs[:3]:
            keys = {
                url_key(row["url"])
                for row in pages
                if (page_topic(row["url"]) or ("", ""))[0] in {first, second}
            }
            impressions = sum(page_totals[k]["impressions"] for k in keys if k in page_totals)
            clicks = sum(page_totals[k]["clicks"] for k in keys if k in page_totals)
            share = round(100 * impressions / total_impressions) if total_impressions else 0
            pattern_stats.append(
                {
                    "templates": [first, second],
                    "shared_topics": len(shared),
                    "pages": len(keys),
                    "impressions": impressions,
                    "clicks": clicks,
                    "share_percent": share,
                }
            )
            lines.append(
                f"URL patterns {first} and {second} both cover {len(shared)} "
                f"topic{'s' if len(shared) != 1 else ''} ({', '.join(shared[:5])}). "
                f"The {len(keys)} pages following these patterns got {share}% of all "
                f"impressions ({_fmt(impressions)} of {_fmt(total_impressions)}) and "
                f"{_fmt(clicks)} clicks."
            )
        for query, entries in ranked[:5]:
            lines.append(
                f'"{query}": '
                + "; ".join(
                    f"{_path(e['url'], host)} (position {e['position']:g}, "
                    f"{_fmt(e['impressions'])} impressions, {_fmt(e['clicks'])} clicks)"
                    for e in entries[:3]
                )
            )
        share = (
            100
            * sum(page_totals.get(url_key(u), {}).get("impressions", 0) for u in involved)
            / total_impressions
            if total_impressions
            else 0
        )
        finding = site_finding(
            host=host,
            check_id="search.cannibalization",
            category="search",
            area="content",
            issue="Two or more of your pages compete for the same searches",
            impact="high" if share >= 20 or len(competing) >= 5 else "medium",
            evidence=lines,
            fix=(
                "Choose one page per topic and search. Merge the overlapping pages into it, "
                "301-redirect the others to it, and update internal links and the sitemap. "
                "Where both pages must stay, give them clearly different intents and titles."
            ),
            priority="high_impact",
            evidence_kind="search_console",
            urls=sorted(involved, key=lambda u: (-involved[u], u)),
            affected_count=len(involved),
            next_action="content_plan",
            ownership="content_owner",
            evidence_refs=["search_console_queries.value.queries"],
        )
        finding["verification"] = {
            "kind": "recheck_search_console",
            "competing_queries": len(competing),
            "url_patterns": pattern_stats,
        }
        findings.append(finding)

    # (b) Searches just outside the top results.
    low, high = policy["near_page_one_positions"]
    near = []
    for entries in groups.values():
        best = max(entries.values(), key=lambda e: (e["impressions"], e["url"]))
        if (
            low <= best["position"] <= high
            and best["impressions"] >= policy["near_page_one_min_impressions"]
        ):
            near.append(best)
    near.sort(key=lambda e: (-e["impressions"], e["query"]))
    if near:
        findings.append(
            site_finding(
                host=host,
                check_id="search.near_page_one",
                category="search",
                area="content",
                issue="Searches rank just below the top results",
                impact="high" if sum(e["impressions"] for e in near) >= 1000 else "medium",
                evidence=[
                    f"{count(len(near), 'search', 'searches')} at average position {low} to "
                    f"{high} with at least {policy['near_page_one_min_impressions']} impressions "
                    f"({window}): {_fmt(sum(e['impressions'] for e in near))} impressions and "
                    f"{_fmt(sum(e['clicks'] for e in near))} clicks in all.",
                    *(
                        f'"{e["query"]}" → {_path(e["url"], host)}: position {e["position"]:g}, '
                        f"{_fmt(e['impressions'])} impressions, {_fmt(e['clicks'])} clicks"
                        for e in near[:10]
                    ),
                ],
                fix=(
                    "Improve the ranking page for each search: answer it directly near the top, "
                    "use the searcher's words in the title and H1, add internal links from "
                    "related pages, and keep one page per search."
                ),
                priority="high_impact",
                evidence_kind="search_console",
                urls=list(dict.fromkeys(e["url"] for e in near)),
                affected_count=len(near),
                next_action="content_plan",
                ownership="content_owner",
                evidence_refs=["search_console_queries.value.queries"],
            )
        )

    # (c) High impressions, few clicks, already near the top: a title or snippet problem.
    low_ctr = []
    for entry in page_totals.values():
        position = entry["position"]
        rate = expected_ctr(position) if position else None
        if (
            rate is None
            or position > policy["low_ctr_max_position"]
            or entry["impressions"] < policy["low_ctr_min_impressions"]
        ):
            continue
        expected = entry["impressions"] * rate
        if expected >= LOW_CTR_MIN_EXPECTED_CLICKS and entry["clicks"] < LOW_CTR_SHARE * expected:
            low_ctr.append({**entry, "expected": expected})
    low_ctr.sort(key=lambda e: (-(e["expected"] - e["clicks"]), e["url"]))
    if low_ctr:
        lines = [
            f"{count(len(low_ctr), 'page')} at average position "
            f"{policy['low_ctr_max_position']} or better with at least "
            f"{policy['low_ctr_min_impressions']} impressions got under a third of the clicks "
            f"a typical result gets at that position ({window})."
        ]
        for entry in low_ctr[:10]:
            key = url_key(entry["url"])
            top = sorted(
                (e for g in groups.values() for k, e in g.items() if k == key),
                key=lambda e: -e["impressions"],
            )[:2]
            title = titles.get(key)
            lines.append(
                f"{_path(entry['url'], host)}: position {entry['position']:g}, "
                f"{_fmt(entry['impressions'])} impressions, {_fmt(entry['clicks'])} clicks "
                f"(about {entry['expected']:.0f} expected)"
                + (f'; title "{title}"' if title else "")
                + ("; top searches: " + ", ".join(f'"{e["query"]}"' for e in top) if top else "")
            )
        findings.append(
            site_finding(
                host=host,
                check_id="search.low_ctr",
                category="search",
                area="on_page",
                issue="Pages shown near the top of results get few clicks",
                impact="medium",
                evidence=lines,
                fix=(
                    "Rewrite the title and meta description of each page to match its top "
                    "searches and say why to click; check that the page's canonical and "
                    "structured data give Google an accurate snippet."
                ),
                priority="quick_win",
                evidence_kind="search_console",
                urls=[e["url"] for e in low_ctr],
                next_action="technical_fix",
                evidence_refs=["search_console.value.pages"],
            )
        )

    # (d) Brand searches that land on a page whose title does not name the brand.
    label = host.removeprefix("www.").split(".")[0].casefold()
    brand_rows = []
    for query, entries in groups.items():
        if not any(_contains(query, term) for term in brand):
            continue
        best = max(entries.values(), key=lambda e: (e["impressions"], e["url"]))
        key = url_key(best["url"])
        title = titles.get(key) or ""
        named = any(_contains(title, term) for term in [*brand, label])
        if key != "/" and (not named or is_utility_url(best["url"])):
            brand_rows.append({**best, "title": title, "named": named})
    brand_rows.sort(key=lambda e: (-e["impressions"], e["query"]))
    if brand_rows:
        findings.append(
            site_finding(
                host=host,
                check_id="search.brand_landing_page",
                category="search",
                area="on_page",
                issue="Searches for your brand land on a page that does not present it",
                impact="high" if any(e["position"] <= 3 for e in brand_rows) else "medium",
                evidence=[
                    f'"{e["query"]}" mostly shows {_path(e["url"], host)} (position '
                    f"{e['position']:g}, {_fmt(e['impressions'])} impressions, "
                    f"{_fmt(e['clicks'])} clicks)"
                    + (f', titled "{e["title"]}"' if e["title"] else ", title unknown")
                    + ("" if e["named"] else ", which does not name the brand")
                    + (" and is a sign-in or account page" if is_utility_url(e["url"]) else "")
                    for e in brand_rows[:8]
                ],
                fix=(
                    "Make the homepage the result for brand searches: give it a title that "
                    "starts with the current brand name and link to it prominently. Update "
                    "stale titles that still use an old name, and add noindex to sign-in and "
                    "account pages."
                ),
                priority="quick_win",
                evidence_kind="search_console",
                urls=list(dict.fromkeys(e["url"] for e in brand_rows)),
                next_action="technical_fix",
                evidence_refs=["search_console_queries.value.queries"],
            )
        )
    return findings
