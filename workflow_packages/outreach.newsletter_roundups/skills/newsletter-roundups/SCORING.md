# Ranking, exclusions and run-to-run memory

Extract the single Python block below into a scratch module and use it unchanged. You decide the
evidence for each newsletter (its section, requirements, route and fit); this code decides what
is excluded and why, the order, the verdict, and which newsletters later runs must not pitch
again. Never rank or exclude by hand.

## Newsletter records

One record per candidate you verified or rejected, with these fields:

- `name`: the newsletter's own name (`TLDR`, not `TLDR Newsletter 2026`).
- `url`: the newsletter's own archive, submission or about page (`https://...`).
- `newsletter_type`: `tools_roundup`, `industry_digest` or `community_digest`.
- `open_verified`: true only when the newsletter's own page, or a recent issue, shows it
  currently takes outside suggestions for its roundup section.
- `requirements_confirmed`: true only when the length limit, format and submission mechanism
  were read on the newsletter's own page or a recent issue.
- `route`: `form` (a suggestion or tip form), `published_address` (the newsletter's own page
  asks for tips at a stated address), or `direct_message` (no stated intake: writing to the
  writer unprompted, which is cold outreach).
- `fit`: 3 when the newsletter's stated audience and recent picks match the pitch closely, 2
  when they overlap, 1 when the link is loose.

## Memory between runs

Every report ends with a `tin-newsletter-state` block. Read the block of every earlier report in
the output folder, pass each parsed value to `read_state()`, and merge the trusted ones with
`merge_states()`. A block that fails `read_state()` is ignored, and the report says so.

```python
import re
import unicodedata
from datetime import date, datetime, timezone

TYPES = ("tools_roundup", "industry_digest", "community_digest")
PREFERENCES = ("no_preference",) + TYPES
ROUTES = ("form", "published_address", "direct_message")
HARD_NOS = (
    "no_cold_email",
    "no_paid_ads",
    "no_founder_posting",
    "no_discounting",
    "no_unbacked_claims",
)
FIT_MIN_SHORTLIST = 3
MAX_STATE_NEWSLETTERS = 500
REQUIRED = (
    "name",
    "url",
    "newsletter_type",
    "open_verified",
    "requirements_confirmed",
    "route",
    "fit",
)


def slug(name):
    """Normalise a newsletter name for matching: case, accents and punctuation."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be non-empty text")
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    if not text:
        raise ValueError("name must contain letters or digits")
    return text


def check(newsletter):
    missing = [field for field in REQUIRED if field not in newsletter]
    if missing:
        raise ValueError(f"newsletter record is missing {', '.join(missing)}")
    slug(newsletter["name"])
    if not isinstance(newsletter["url"], str) or not newsletter["url"].startswith(
        ("https://", "http://")
    ):
        raise ValueError("url must be the newsletter's own page")
    if newsletter["newsletter_type"] not in TYPES:
        raise ValueError(f"newsletter_type must be one of {', '.join(TYPES)}")
    for field in ("open_verified", "requirements_confirmed"):
        if not isinstance(newsletter[field], bool):
            raise ValueError(f"{field} must be true or false")
    if newsletter["route"] not in ROUTES:
        raise ValueError(f"route must be one of {', '.join(ROUTES)}")
    fit = newsletter["fit"]
    if isinstance(fit, bool) or not isinstance(fit, int) or not 1 <= fit <= 3:
        raise ValueError("fit must be 1, 2 or 3")


def read_state(value):
    """Validate one earlier report's state block. Returns None when it cannot be trusted."""
    try:
        if not isinstance(value, dict) or value.get("version") != 1:
            return None
        newsletters = value.get("newsletters")
        if not isinstance(newsletters, dict) or len(newsletters) > MAX_STATE_NEWSLETTERS:
            return None
        clean = {}
        for key, item in newsletters.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(item, dict):
                return None
            name = item.get("name")
            slug(name)
            if item.get("newsletter_type") not in TYPES:
                return None
            date.fromisoformat(item.get("first_drafted"))
            report = item.get("report")
            if not isinstance(report, str) or not report.strip():
                return None
            clean[key] = {
                "name": name,
                "newsletter_type": item["newsletter_type"],
                "first_drafted": item["first_drafted"],
                "report": report,
            }
        return {"version": 1, "newsletters": clean}
    except (AttributeError, TypeError, ValueError):
        return None


def merge_states(states):
    """Union trusted earlier states; a newsletter keeps the report that first drafted it."""
    merged = {}
    for state in states:
        if state is None:
            continue
        for key, item in state["newsletters"].items():
            known = merged.get(key)
            if known is None or item["first_drafted"] < known["first_drafted"]:
                merged[key] = dict(item)
    return {"version": 1, "newsletters": merged}


def plan(
    newsletters,
    max_newsletters=10,
    newsletter_type_preference="no_preference",
    already_pitched=(),
    previous=None,
    hard_nos=(),
    report_path="",
    as_of=None,
):
    """Exclude, rank and cap verified newsletters; return the shortlist, exclusions, next state."""
    as_of = as_of or datetime.now(timezone.utc).date().isoformat()
    date.fromisoformat(as_of)
    if (
        isinstance(max_newsletters, bool)
        or not isinstance(max_newsletters, int)
        or not 3 <= max_newsletters <= 20
    ):
        raise ValueError("max_newsletters must be 3-20")
    if newsletter_type_preference not in PREFERENCES:
        raise ValueError(f"newsletter_type_preference must be one of {', '.join(PREFERENCES)}")
    for item in hard_nos:
        if item not in HARD_NOS:
            raise ValueError(f"hard no must be one of {', '.join(HARD_NOS)}")
    skipped = {slug(name) for name in already_pitched if isinstance(name, str) and name.strip()}
    earlier = (previous or {}).get("newsletters", {})
    seen, eligible, excluded, duplicates = set(), [], [], 0
    for newsletter in newsletters:
        check(newsletter)
        key = slug(newsletter["name"])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        row = dict(newsletter, key=key)
        reason = ""
        if (
            newsletter_type_preference != "no_preference"
            and newsletter["newsletter_type"] != newsletter_type_preference
        ):
            reason = f"type: {newsletter['newsletter_type']} was not requested"
        elif not newsletter["open_verified"]:
            reason = "open submissions not verified on the newsletter's own page"
        elif not newsletter["requirements_confirmed"]:
            reason = "submission requirements not confirmed on the newsletter's own page"
        elif key in skipped:
            reason = "named in already_pitched"
        elif key in earlier:
            prior = earlier[key]
            reason = f"pitch drafted on {prior['first_drafted']} in {prior['report']}"
        elif newsletter["route"] == "direct_message" and "no_cold_email" in hard_nos:
            reason = "hard no: no cold email (no stated intake for pitches)"
        if reason:
            excluded.append(dict(row, reason=reason))
        else:
            eligible.append(row)

    def rank(row):
        return (-row["fit"], slug(row["name"]))

    eligible.sort(key=rank)
    shortlist = eligible[:max_newsletters]
    for row in eligible[max_newsletters:]:
        excluded.append(dict(row, reason="over max_newsletters; eligible next run"))
    count = len(shortlist)
    verdict = "fit" if count >= FIT_MIN_SHORTLIST else "thin" if count else "not a fit"
    state = {key: dict(item) for key, item in earlier.items()}
    for row in shortlist:
        state[row["key"]] = {
            "name": row["name"],
            "newsletter_type": row["newsletter_type"],
            "first_drafted": as_of,
            "report": report_path,
        }
    if len(state) > MAX_STATE_NEWSLETTERS:
        newest = sorted(state.items(), key=lambda kv: kv[1]["first_drafted"], reverse=True)
        state = dict(newest[:MAX_STATE_NEWSLETTERS])
    return {
        "status": "complete" if count else "no newsletters verified",
        "verdict": verdict,
        "shortlist": shortlist,
        "excluded": excluded,
        "duplicates_dropped": duplicates,
        "state": {"version": 1, "newsletters": state},
    }
```

Pass `already_pitched` as a list of the names in the input (split on commas and new lines),
`hard_nos` as the ids of the hard no's the growth plan records (`no cold email` is
`no_cold_email`), `report_path` as the declared output path of this run, and `as_of` as today's
UTC date.
