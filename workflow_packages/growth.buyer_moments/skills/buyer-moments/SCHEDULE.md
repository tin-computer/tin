# Backward scheduling, exclusions and run-to-run memory

Extract the single Python block below into a scratch module and use it unchanged. You decide the
evidence for each moment (its date, the source, what the buyer has to do then, and how well the
product fits). This code decides which moments are kept and why, their order, which channels are
still possible in time, the dated run sheet, the verdict and what later runs must not prepare
again. Never rank, exclude or schedule by hand.

## Why schedule backwards

A moment is a fixed date the business can't move. Each channel needs a different lead time
before that date: a search page has to be live weeks early for search engines to find and rank
it, while a forwardable message works best a couple of days before. So each channel gets a
`ship_by` date (the moment's date minus the channel's lead time) and a `start_by` date (`ship_by`
minus the days the work takes). A channel whose `ship_by` has already passed is dropped for this
year instead of being rushed, and a moment with no channel left in time waits for next year.

`LEAD_DAYS` holds conservative starting defaults, not measured facts. Change them in a reviewed
package version, not during a run.

## Moment records

One record per candidate moment you verified or rejected, with these fields:

- `name`: the moment's own name without the year (`Diwali`, `CBSE Class 12 results`,
  `Income tax return deadline`).
- `market`: the country or region it applies to, as written in the report (`India`, `Brazil`,
  `Global`).
- `kind`: `festival`, `deadline`, `results`, `season` or `industry_event`.
- `date`: this year's date of the moment as `YYYY-MM-DD`: the day the buyer acts, which for a
  multi-day season is its first day.
- `date_source`: the primary page that states this year's date (`https://...`).
- `date_verified`: true only when that page states this year's date. A date copied from last
  year, estimated from a pattern, or found only on an aggregator is false.
- `buyer_job`: one sentence naming what the buyer has to get done at this moment that the
  product helps with.
- `fit`: 3 when a named feature does that job directly, 2 when it helps with part of it, 1 when
  the link is only a greeting or a vague theme.
- `channels`: the channels worth preparing for this moment, from `LEAD_DAYS`.
- `uses_offer`: true when the plan for this moment depends on a discount or special price.

## Memory between runs

Every report ends with a `tin-moments-state` block. Read the block of every earlier report in
the output folder, pass each parsed value to `read_state()`, and merge the trusted ones with
`merge_states()`. A block that fails `read_state()` is ignored, and the report says so.

```python
import re
import unicodedata
from datetime import date, timedelta

KINDS = ("festival", "deadline", "results", "season", "industry_event")
# channel: (days before the moment it must be live, days the work takes)
LEAD_DAYS = {
    "search_page": (42, 5),
    "partner_pitch": (28, 3),
    "site_banner": (10, 1),
    "email": (7, 2),
    "social_post": (4, 1),
    "forward_message": (2, 1),
}
HARD_NOS = (
    "no_cold_email",
    "no_paid_ads",
    "no_founder_posting",
    "no_discounting",
    "no_unbacked_claims",
)
BLOCKED_BY = {
    "no_cold_email": ("partner_pitch",),
    "no_founder_posting": ("social_post", "forward_message"),
}
REVIEW_WINDOW_DAYS = 60
MAX_STATE_MOMENTS = 500
REQUIRED = (
    "name",
    "market",
    "kind",
    "date",
    "date_source",
    "date_verified",
    "buyer_job",
    "fit",
    "channels",
    "uses_offer",
)


def _day(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be YYYY-MM-DD") from error


def slug(text):
    """Normalise a name for matching: case, accents, punctuation and a trailing year."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("name must be non-empty text")
    clean = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    clean = re.sub(r"[^a-z0-9]+", " ", clean).strip()
    clean = re.sub(r"\s+(19|20)\d{2}$", "", clean)
    if not clean:
        raise ValueError("name must contain letters or digits")
    return clean


def moment_key(moment):
    """One moment per market per year: next year's Diwali is a new moment."""
    year = _day(moment["date"], "date").year
    return f"{slug(moment['name'])}@{slug(moment['market'])}@{year}"


def utm_campaign(moment):
    return f"{slug(moment['name']).replace(' ', '-')}-{_day(moment['date'], 'date').year}"


def check(moment):
    missing = [field for field in REQUIRED if field not in moment]
    if missing:
        raise ValueError(f"moment record is missing {', '.join(missing)}")
    slug(moment["name"])
    slug(moment["market"])
    if moment["kind"] not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    _day(moment["date"], "date")
    source = moment["date_source"]
    if not isinstance(source, str) or not source.startswith(("https://", "http://")):
        raise ValueError("date_source must be the page that states the date")
    for field in ("date_verified", "uses_offer"):
        if not isinstance(moment[field], bool):
            raise ValueError(f"{field} must be true or false")
    if not isinstance(moment["buyer_job"], str) or not moment["buyer_job"].strip():
        raise ValueError("buyer_job must say what the buyer has to get done")
    fit = moment["fit"]
    if isinstance(fit, bool) or not isinstance(fit, int) or not 1 <= fit <= 3:
        raise ValueError("fit must be 1, 2 or 3")
    channels = moment["channels"]
    if not isinstance(channels, list) or not channels:
        raise ValueError("channels must be a non-empty list")
    for channel in channels:
        if channel not in LEAD_DAYS:
            raise ValueError(f"channel must be one of {', '.join(LEAD_DAYS)}")


def schedule(moment, today, blocked=()):
    """Work back from the moment's date to each channel's ship_by and start_by."""
    when = _day(moment["date"], "date")
    kept, missed = [], []
    for channel in dict.fromkeys(moment["channels"]):
        if channel in blocked:
            missed.append({"channel": channel, "reason": "blocked by a hard no"})
            continue
        live, work = LEAD_DAYS[channel]
        ship_by = when - timedelta(days=live)
        start_by = ship_by - timedelta(days=work)
        if ship_by < today:
            missed.append({"channel": channel, "reason": f"needed to ship by {ship_by.isoformat()}"})
            continue
        kept.append(
            {
                "channel": channel,
                "start_by": start_by.isoformat(),
                "ship_by": ship_by.isoformat(),
                "timing": "on time" if start_by >= today else "tight",
            }
        )
    kept.sort(key=lambda row: (row["ship_by"], row["channel"]))
    return kept, missed


def read_state(value):
    """Validate one earlier report's state block. Returns None when it cannot be trusted."""
    try:
        if not isinstance(value, dict) or value.get("version") != 1:
            return None
        moments = value.get("moments")
        if not isinstance(moments, dict) or len(moments) > MAX_STATE_MOMENTS:
            return None
        clean = {}
        for key, item in moments.items():
            if not isinstance(key, str) or key.count("@") != 2 or not isinstance(item, dict):
                return None
            slug(item.get("name"))
            slug(item.get("market"))
            _day(item.get("date"), "date")
            _day(item.get("first_prepared"), "first_prepared")
            report = item.get("report")
            campaign = item.get("utm_campaign")
            if not isinstance(report, str) or not report.strip():
                return None
            if not isinstance(campaign, str) or not campaign.strip():
                return None
            clean[key] = {
                "name": item["name"],
                "market": item["market"],
                "date": item["date"],
                "first_prepared": item["first_prepared"],
                "report": report,
                "utm_campaign": campaign,
            }
        return {"version": 1, "moments": clean}
    except (AttributeError, TypeError, ValueError):
        return None


def merge_states(states):
    """Union trusted earlier states; a moment keeps the report that first prepared it."""
    merged = {}
    for state in states:
        if state is None:
            continue
        for key, item in state["moments"].items():
            known = merged.get(key)
            if known is None or item["first_prepared"] < known["first_prepared"]:
                merged[key] = dict(item)
    return {"version": 1, "moments": merged}


def plan(
    moments,
    as_of,
    horizon_days=90,
    max_moments=4,
    previous=None,
    hard_nos=(),
    skip=(),
    report_path="",
):
    """Keep, rank, schedule and cap verified moments; return the plan and the next state."""
    today = _day(as_of, "as_of")
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int):
        raise ValueError("horizon_days must be 30-180")
    if not 30 <= horizon_days <= 180:
        raise ValueError("horizon_days must be 30-180")
    if isinstance(max_moments, bool) or not isinstance(max_moments, int):
        raise ValueError("max_moments must be 1-8")
    if not 1 <= max_moments <= 8:
        raise ValueError("max_moments must be 1-8")
    for item in hard_nos:
        if item not in HARD_NOS:
            raise ValueError(f"hard no must be one of {', '.join(HARD_NOS)}")
    blocked = {channel for item in hard_nos for channel in BLOCKED_BY.get(item, ())}
    skipped = {slug(name) for name in skip if isinstance(name, str) and name.strip()}
    earlier = (previous or {}).get("moments", {})
    horizon = today + timedelta(days=horizon_days)
    seen, eligible, excluded, prepared_before, duplicates = set(), [], [], [], 0
    for moment in moments:
        check(moment)
        key = moment_key(moment)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        when = _day(moment["date"], "date")
        row = dict(moment, key=key, utm_campaign=utm_campaign(moment))
        reason = ""
        if slug(moment["name"]) in skipped:
            reason = "skipped on request"
        elif not moment["date_verified"]:
            reason = "this year's date not verified on a primary source"
        elif when < today:
            reason = "already passed this year"
        elif when > horizon:
            reason = "after the horizon; a later run will prepare it"
        elif moment["fit"] < 2:
            reason = "loose fit: no feature does the buyer's job at this moment"
        elif "no_discounting" in hard_nos and moment["uses_offer"]:
            reason = "depends on a discount, which the growth plan rules out"
        elif key in earlier:
            item = earlier[key]
            prepared_before.append(
                dict(row, first_prepared=item["first_prepared"], report=item["report"])
            )
            continue
        if not reason:
            kept, missed = schedule(moment, today, blocked)
            row["plan"], row["missed"] = kept, missed
            if not kept:
                reason = "too late for every channel this year"
        if reason:
            excluded.append({"key": key, "name": moment["name"], "reason": reason})
            continue
        eligible.append(row)
    eligible.sort(
        key=lambda row: (-row["fit"], row["plan"][0]["start_by"], row["date"], row["key"])
    )
    shortlist = eligible[:max_moments]
    for row in eligible[max_moments:]:
        excluded.append({"key": row["key"], "name": row["name"], "reason": "over max_moments"})
    run_sheet = sorted(
        (
            {
                "start_by": step["start_by"],
                "ship_by": step["ship_by"],
                "timing": step["timing"],
                "moment": row["name"],
                "channel": step["channel"],
            }
            for row in shortlist
            for step in row["plan"]
        ),
        key=lambda step: (step["start_by"], step["ship_by"], step["moment"], step["channel"]),
    )
    review_from = today - timedelta(days=REVIEW_WINDOW_DAYS)
    review = sorted(
        (
            dict(item, key=key)
            for key, item in earlier.items()
            if review_from <= _day(item["date"], "date") < today
        ),
        key=lambda item: (item["date"], item["key"]),
    )
    state = {"version": 1, "moments": {key: dict(item) for key, item in earlier.items()}}
    for row in shortlist:
        state["moments"][row["key"]] = {
            "name": row["name"],
            "market": row["market"],
            "date": row["date"],
            "first_prepared": today.isoformat(),
            "report": report_path,
            "utm_campaign": row["utm_campaign"],
        }
    if len(state["moments"]) > MAX_STATE_MOMENTS:
        oldest = sorted(state["moments"].items(), key=lambda pair: pair[1]["date"])
        state["moments"] = dict(oldest[-MAX_STATE_MOMENTS:])
    if not shortlist:
        verdict = "not a fit"
    elif len(shortlist) >= min(3, max_moments):
        verdict = "fit"
    else:
        verdict = "thin"
    return {
        "status": "complete" if shortlist else "no moments verified",
        "verdict": verdict,
        "shortlist": shortlist,
        "run_sheet": run_sheet,
        "prepared_before": prepared_before,
        "review": review,
        "excluded": excluded,
        "duplicates": duplicates,
        "state": state,
    }
```
