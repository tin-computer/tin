# Scoring, classification and state

Extract the single Python block below into a scratch module and use it unchanged. The model
decides the evidence (which competitors to research, how features map, what the pricing is);
this code decides the priority order, validates feature classifications, and tracks what
earlier runs already drafted. Never score or classify by hand.

## Feature classification

Every row in a feature comparison must be one of these categories. The model assigns the
category from evidence; the validator rejects anything else.

- `stronger`: the product does this and adds a capability the competitor lacks. Cite both
  the Feature map line and the extra capability.
- `direct`: the product does the same thing, possibly under a different name. Cite the
  Feature map line.
- `partial`: the product covers part of this. Cite the Feature map line and note what is
  missing.
- `absent`: the product does not do this. No Feature map citation needed.

An uncited `stronger`, `direct` or `partial` classification is invalid. The validator rejects
it. When in doubt, choose the less favourable classification: `partial` over `direct`,
`direct` over `stronger`. Honesty builds trust; exaggeration kills it.

## Competitor prioritisation

When there are more competitors than `max_pages`, pick the ones most worth drafting. The
score balances search evidence (do people search for "X alternative"?), feature overlap
(is there enough to compare?) and pricing gap (is there a clear cost story?).

## State tracking

Every report ends with a `tin-migration-state` block. Read it from every earlier report in
the output folder. A competitor an earlier run already drafted is not drafted again in a
later run unless the `focus` input explicitly asks for a new angle.

```python
from datetime import date

CATEGORIES = ("stronger", "direct", "partial", "absent")
REQUIRES_CITATION = ("stronger", "direct", "partial")


def _choice(value, allowed, name):
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}")
    return value


def _whole(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be a whole number from {low} to {high}")
    return value


def _day(value, name):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f"{name} must be YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def validate_feature(row):
    """Validate one feature comparison row.

    A row is a dict with:
      feature: str          — the feature name as the competitor uses it
      category: str         — one of CATEGORIES
      citation: str | None  — Feature map line for stronger/direct/partial; None for absent
      note: str             — short explanation of the mapping

    Returns the validated row. Raises ValueError on invalid input.
    """
    if not isinstance(row, dict):
        raise ValueError("feature row must be a dict")
    feature = row.get("feature", "").strip()
    if not feature:
        raise ValueError("feature must be non-empty")
    category = _choice(row.get("category"), CATEGORIES, "category")
    citation = row.get("citation")
    if category in REQUIRES_CITATION:
        if not isinstance(citation, str) or not citation.strip():
            raise ValueError(
                f"{category} classification for '{feature}' requires a Feature map citation"
            )
    note = row.get("note", "").strip()
    return {
        "feature": feature,
        "category": category,
        "citation": citation.strip() if citation else None,
        "note": note,
    }


def validate_comparison(features):
    """Validate a full feature comparison table. Returns the list of validated rows."""
    if not isinstance(features, list) or len(features) == 0:
        raise ValueError("features must be a non-empty list")
    if len(features) > 100:
        raise ValueError("too many features; cap at 100 rows")
    return [validate_feature(row) for row in features]


def score_competitor(competitor):
    """Score one competitor for prioritisation.

    A competitor dict has:
      name: str
      search_evidence: int  — 3 high, 2 medium, 1 low, 0 none
      feature_overlap: int  — number of direct + stronger + partial features found
      pricing_public: bool  — whether pricing is publicly visible
      already_drafted: bool — whether an earlier run already drafted this competitor

    Returns the competitor dict with a score and reason added.
    """
    if not isinstance(competitor, dict):
        raise ValueError("competitor must be a dict")
    name = competitor.get("name", "").strip()
    if not name:
        raise ValueError("competitor name must be non-empty")
    search = _whole(competitor.get("search_evidence", 0), "search_evidence", 0, 3)
    overlap = _whole(competitor.get("feature_overlap", 0), "feature_overlap", 0, 200)
    pricing = bool(competitor.get("pricing_public", False))
    drafted = bool(competitor.get("already_drafted", False))

    result = dict(competitor, name=name, score=None)
    if drafted:
        result["reason"] = "already drafted in an earlier run"
        return result

    value = (search + 1) * (overlap + 1) * (2 if pricing else 1)
    result["score"] = value
    parts = []
    if search >= 2:
        parts.append("strong search signal")
    elif search == 1:
        parts.append("some search signal")
    else:
        parts.append("no search evidence found")
    parts.append(f"{overlap} feature{'s' if overlap != 1 else ''} overlap")
    if pricing:
        parts.append("pricing is public")
    else:
        parts.append("pricing not public")
    result["reason"] = "; ".join(parts)
    return result


def prioritise(competitors, max_pages):
    """Score and pick which competitors to draft pages for.

    Returns (picks, ranked) where picks has at most max_pages entries and
    ranked is the full sorted list.
    """
    _whole(max_pages, "max_pages", 1, 5)
    scored = [score_competitor(c) for c in competitors]
    seen = set()
    for c in scored:
        lower = c["name"].lower()
        if lower in seen:
            raise ValueError(f"competitor '{c['name']}' appears twice")
        seen.add(lower)
    ranked = sorted(
        scored,
        key=lambda c: (
            c["score"] is None,
            -(c["score"] or 0),
            c["name"].lower(),
        ),
    )
    picks = [c for c in ranked if c["score"] is not None][:max_pages]
    return picks, ranked


def read_state(value):
    """Validate an earlier run's state block. Returns None when it cannot be trusted."""
    try:
        if not isinstance(value, dict) or value.get("version") != 1:
            return None
        competitors = value.get("competitors")
        if not isinstance(competitors, dict) or len(competitors) > 50:
            return None
        clean = {}
        for name, item in competitors.items():
            if not isinstance(name, str) or not name.strip():
                continue
            clean[name.lower()] = {
                "name": name,
                "drafted": bool(item.get("drafted", False)),
                "focus": item.get("focus", ""),
            }
            if item.get("drafted_on") is not None:
                clean[name.lower()]["drafted_on"] = _day(item["drafted_on"], "drafted_on")
            if item.get("drafted_in") is not None:
                if isinstance(item["drafted_in"], str) and item["drafted_in"].strip():
                    clean[name.lower()]["drafted_in"] = item["drafted_in"]
        state = {"version": 1, "competitors": clean}
        if value.get("checked") is not None:
            state["checked"] = _day(value["checked"], "checked")
        return state
    except (AttributeError, TypeError, ValueError):
        return None


def merge_states(states):
    """Fold every trusted earlier state into one, oldest first.

    A competitor keeps the earliest report that drafted it.
    """
    trusted = sorted((s for s in states if s), key=lambda s: s.get("checked", ""))
    if not trusted:
        return None
    competitors = {}
    for state in trusted:
        for key, item in state["competitors"].items():
            known = competitors.get(key)
            if known is None:
                competitors[key] = dict(item)
            elif item.get("drafted") and not known.get("drafted"):
                competitors[key] = dict(item)
    merged = {"version": 1, "competitors": competitors}
    if trusted[-1].get("checked"):
        merged["checked"] = trusted[-1]["checked"]
    return merged


def next_state(previous, ranked, picks, checked=None, report=None):
    """Build this run's state block.

    Returns (state, changes) where changes summarises what is new since the previous run.
    """
    before = (previous or {}).get("competitors", {})
    drafted_names = {c["name"].lower() for c in picks}
    competitors = {}
    new_drafts, skipped, already = [], [], []
    for c in ranked:
        key = c["name"].lower()
        prior = before.get(key)
        is_drafted = key in drafted_names or (prior and prior.get("drafted"))
        entry = {"name": c["name"], "drafted": is_drafted}
        if is_drafted and key in drafted_names:
            if checked is not None:
                entry["drafted_on"] = _day(checked, "checked")
            if report is not None:
                entry["drafted_in"] = report
            if not (prior and prior.get("drafted")):
                new_drafts.append(c["name"])
        elif is_drafted and prior:
            for field in ("drafted_on", "drafted_in", "focus"):
                if field in prior:
                    entry[field] = prior[field]
            already.append(c["name"])
        else:
            skipped.append(c["name"])
        competitors[key] = entry
    state = {"version": 1, "competitors": competitors}
    if checked is not None:
        state["checked"] = checked
    changes = {
        "new_drafts": new_drafts,
        "already_drafted": already,
        "skipped": skipped,
    }
    return state, changes
```
