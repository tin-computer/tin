"""Paid ads monitor: the pure rules that keep a launched Google Search campaign healthy.

Code normalises what the account reports, decides every automatic change (negatives, pausing
disapproved ads, pausing wasteful keywords) and every proposal (budget, bidding strategy) and
renders the day's report; two bounded model steps label search terms and explain the day in plain
words. Nothing here performs I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

from tin_lite import paid_ads
from tin_lite.domain import PAID_ADS_MONITOR_WORKFLOW_NAME
from tin_lite.model_providers import ModelRoute

KEY = PAID_ADS_MONITOR_WORKFLOW_NAME
PREFIX = "paid_ads_monitor"
CAMPAIGN_DIR = "ads/google"
ASSETS = Path(__file__).parent / "paid_ads_monitor_assets"
SKILL = (ASSETS / "RULES.md").read_text()
ROUTES: tuple[ModelRoute, ...] = (paid_ads.JUDGMENT_ROUTE, paid_ads.DRAFTING_ROUTE)
JUDGMENT_STEPS = frozenset({"brief"})
POLICY = {
    "version": 1,
    "quiet_days": 3,
    "search_terms_min_clicks": 2,
    "keyword_pause_multiple": 3.0,
    "keyword_pause_min_days": 30,
    "budget_step": 0.2,
    "budget_cpa_window_days": 14,
    "max_conv_threshold": 15,
    "tcpa_threshold": 30,
    "tcpa_multiplier": 1.1,
    "landing_page_ctr": 0.02,
    "landing_page_cpa_multiple": 1.5,
    "max_model_input_bytes": 120_000,
    "max_negatives_default": 20,
    "classify_reservation_usd": "0.30",
    "brief_reservation_usd": "0.80",
    "repair_reservation_usd": "0.30",
    "google_ads_reservation_usd": "0",
}
LIMITS = {"MONITOR.md": 120_000, "monitor.json": 600_000}
MAX_OUT = {"classify": 6000, "brief": 4000, "repair": 3000}
LABELS = ("irrelevant", "competitor", "job_or_free", "relevant", "unsure")
AUTO_NEGATIVE_LABELS = frozenset({"irrelevant", "job_or_free"})
PROPOSAL_KINDS = ("bid_strategy_change", "budget_change")
EXCLUDED_TERM_STATES = frozenset({"EXCLUDED", "ADDED_EXCLUDED"})
ALERT_REASONS = ("HAS_ADS_DISAPPROVED", "MISCONFIGURED", "NOT_ELIGIBLE")

INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project_id"],
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "launch_run_id": {
            "type": "string",
            "maxLength": 36,
            "default": "",
            "title": "Campaign launch run",
            "description": "The successful Google Ads launch in this project whose campaign Tin watches.",
            "x-tin-ui": {"control": "text", "order": 10},
        },
        "auto_negatives_per_run": {
            "type": "integer",
            "minimum": 0,
            "maximum": 50,
            "default": POLICY["max_negatives_default"],
            "title": "Negatives Tin may add per check",
            "description": "Search terms that wasted clicks are blocked without asking, up to this many each run.",
            "x-tin-ui": {"control": "counter", "order": 20},
        },
        "notes": {
            "type": "string",
            "maxLength": 600,
            "default": "",
            "title": "Anything else",
            "x-tin-ui": {"control": "textarea", "order": 30},
        },
        "max_cost_usd": {
            "type": "number",
            "minimum": 1,
            "maximum": 5,
            "default": 2,
            "title": "Maximum model spend per check (USD)",
            "description": "The Google Ads API itself costs nothing; this bounds the two model steps.",
            "x-tin-ui": {"control": "number", "order": 40},
        },
    },
}


# ---------------------------------------------------------------- contract


def route_definitions():
    return [
        {
            "key": route.key,
            "provider": route.provider.value,
            "model": route.model,
            "capabilities": sorted(c.value for c in route.capabilities),
        }
        for route in ROUTES
    ]


def contract_digest():
    """Runs pin the rules and the code they were admitted under."""
    h = hashlib.sha256()
    for path in (*sorted(ASSETS.glob("*.*")), Path(__file__)):
        if path.suffix in {".md", ".json", ".py"}:
            h.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


def schema_name(step):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"paid_ads_monitor_{step}")[:64]


def route_for(step):
    return (
        paid_ads.JUDGMENT_ROUTE if step.split(":")[0] in JUDGMENT_STEPS else paid_ads.DRAFTING_ROUTE
    )


def section(start, end=None):
    i = SKILL.index(start)
    return SKILL[i : SKILL.index(end) if end else len(SKILL)].strip()


WRITING = section("## How to write", "## 1. Classify")
CLASSIFY_RULES = section("## 1. Classify", "## 2. Brief")
BRIEF_RULES = section("## 2. Brief")


def paths(launch_run_id: str, run_id: str) -> dict[str, str]:
    launch, run = UUID(str(launch_run_id)), UUID(str(run_id))
    return {
        "MONITOR.md": f"{CAMPAIGN_DIR}/{launch}/monitor/{run}.md",
        "monitor.json": f"{CAMPAIGN_DIR}/{launch}/monitor/{run}.json",
    }


def proposal_path(launch_run_id: str, number: int) -> str:
    if type(number) is not int or number < 1:
        raise ValueError("Proposal numbers start at one.")
    return f"{CAMPAIGN_DIR}/{UUID(str(launch_run_id))}/proposals/{number:03d}.md"


def check_inputs(inputs: dict) -> None:
    """Refuse what the schema cannot express: the launch run must be a UUID."""
    launch = inputs.get("launch_run_id") or ""
    if not launch:
        raise ValueError("Choose the Google Ads launch this check should watch.")
    UUID(str(launch))
    cap = inputs.get("auto_negatives_per_run", POLICY["max_negatives_default"])
    if isinstance(cap, bool) or type(cap) is not int or not 0 <= cap <= 50:
        raise ValueError("Negatives per check must be a whole number between 0 and 50.")
    maximum = inputs.get("max_cost_usd", 2)
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or not 1 <= maximum <= 5:
        raise ValueError("The model spend ceiling must be between $1 and $5.")


# ---------------------------------------------------------------- normalising reads


def _usd(micros) -> float:
    try:
        value = float(micros or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(value / 1_000_000, 2) if value == value else 0.0


def _int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number else 0.0


def _share(value):
    if value in (None, "", "< 0.1", "> 0.9"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 4) if number == number else None


def _first(rows) -> dict:
    for row in rows or []:
        if isinstance(row, dict):
            return row
    return {}


def _window(row: dict) -> dict:
    metrics = row.get("metrics") or {}
    clicks = _int(metrics.get("clicks"))
    cost = _usd(metrics.get("costMicros"))
    conversions = _float(metrics.get("conversions"))
    impressions = _int(metrics.get("impressions"))
    return {
        "impressions": impressions,
        "clicks": clicks,
        "cost_usd": cost,
        "conversions": round(conversions, 2),
        "ctr": round(clicks / impressions, 4) if impressions else 0.0,
        "cpc_usd": round(cost / clicks, 2) if clicks else 0.0,
        "cpa_usd": round(cost / conversions, 2) if conversions else None,
        "impression_share": _share(metrics.get("searchImpressionShare")),
        "budget_lost_share": _share(metrics.get("searchBudgetLostImpressionShare")),
        "rank_lost_share": _share(metrics.get("searchRankLostImpressionShare")),
    }


def _campaign(row: dict) -> dict:
    campaign = row.get("campaign") or {}
    budget = row.get("campaignBudget") or {}
    ceiling = (campaign.get("targetSpend") or {}).get("cpcBidCeilingMicros")
    tcpa = (campaign.get("maximizeConversions") or {}).get("targetCpaMicros")
    dates = [
        campaign[key]
        for key in ("acaMigrationDateTime", "broadMatchMigrationDateTime")
        if campaign.get(key)
    ]
    return {
        "id": str(campaign.get("id") or ""),
        "name": str(campaign.get("name") or ""),
        "status": str(campaign.get("status") or "UNKNOWN"),
        "primary_status": str(campaign.get("primaryStatus") or "UNKNOWN"),
        "primary_status_reasons": [str(r) for r in campaign.get("primaryStatusReasons") or []],
        "bidding_strategy_type": str(campaign.get("biddingStrategyType") or "UNKNOWN"),
        "cpc_ceiling_usd": _usd(ceiling) if ceiling is not None else None,
        "target_cpa_usd": _usd(tcpa) if tcpa is not None else None,
        "ai_max_enabled": bool((campaign.get("aiMaxSetting") or {}).get("enableAiMax")),
        "migration_dates": dates,
        "budget_usd": _usd(budget.get("amountMicros")),
        "recommended_budget_usd": (
            _usd(budget["recommendedBudgetAmountMicros"])
            if budget.get("recommendedBudgetAmountMicros") is not None
            else None
        ),
    }


def _search_term(row: dict) -> dict:
    view = row.get("searchTermView") or {}
    keyword = ((row.get("segments") or {}).get("keyword") or {}).get("info") or {}
    metrics = row.get("metrics") or {}
    return {
        "term": str(view.get("searchTerm") or "").strip().casefold(),
        "status": str(view.get("status") or "NONE"),
        "matched_keyword": str(keyword.get("text") or ""),
        "match_type": str(keyword.get("matchType") or ""),
        "source": str((row.get("segments") or {}).get("searchTermMatchSource") or ""),
        "ad_group_id": str((row.get("adGroup") or {}).get("id") or ""),
        "clicks": _int(metrics.get("clicks")),
        "cost_usd": _usd(metrics.get("costMicros")),
        "conversions": round(_float(metrics.get("conversions")), 2),
    }


def _keyword(row: dict) -> dict:
    criterion = row.get("adGroupCriterion") or {}
    keyword = criterion.get("keyword") or {}
    quality = (criterion.get("qualityInfo") or {}).get("qualityScore")
    metrics = row.get("metrics") or {}
    return {
        "resource_name": str(criterion.get("resourceName") or ""),
        "criterion_id": str(criterion.get("criterionId") or ""),
        "text": str(keyword.get("text") or ""),
        "match_type": str(keyword.get("matchType") or ""),
        "status": str(criterion.get("status") or "UNKNOWN"),
        "quality_score": _int(quality) if quality is not None else None,
        "clicks": _int(metrics.get("clicks")),
        "cost_usd": _usd(metrics.get("costMicros")),
        "conversions": round(_float(metrics.get("conversions")), 2),
        "impressions": _int(metrics.get("impressions")),
    }


def _ad(row: dict) -> dict:
    ad_group_ad = row.get("adGroupAd") or {}
    policy = ad_group_ad.get("policySummary") or {}
    topics = [
        str(entry.get("topic") or "")
        for entry in policy.get("policyTopicEntries") or []
        if isinstance(entry, dict)
    ]
    return {
        "resource_name": str(ad_group_ad.get("resourceName") or ""),
        "ad_id": str((ad_group_ad.get("ad") or {}).get("id") or ""),
        "status": str(ad_group_ad.get("status") or "UNKNOWN"),
        "ad_strength": str(ad_group_ad.get("adStrength") or "UNKNOWN"),
        "approval_status": str(policy.get("approvalStatus") or "UNKNOWN"),
        "review_status": str(policy.get("reviewStatus") or "UNKNOWN"),
        "topics": [t for t in topics if t],
        "reasons": [str(r) for r in ad_group_ad.get("primaryStatusReasons") or []],
    }


def _conversion_action(row: dict) -> dict:
    action = row.get("conversionAction") or {}
    metrics = row.get("metrics") or {}
    return {
        "name": str(action.get("name") or ""),
        "category": str(action.get("category") or ""),
        "conversions_30d": round(_float(metrics.get("allConversions")), 2),
    }


def _subscription(row: dict) -> dict:
    sub = row.get("recommendationSubscription") or {}
    return {"type": str(sub.get("type") or ""), "status": str(sub.get("status") or "")}


def normalize_reads(reads: dict) -> dict:
    """Turn the REST rows the activity gathered into one plain, bounded picture."""
    reads = reads or {}
    rows = lambda name: [r for r in (reads.get(name) or []) if isinstance(r, dict)]  # noqa: E731
    head = _first(reads.get("campaign_7d")) or _first(reads.get("campaign_30d"))
    terms = [t for t in map(_search_term, rows("search_terms")) if t["term"]]
    terms.sort(key=lambda t: (-t["cost_usd"], -t["clicks"], t["term"]))
    return {
        "campaign": _campaign(head),
        "windows": {
            "7d": _window(_first(reads.get("campaign_7d"))),
            "14d": _window(_first(reads.get("campaign_14d"))),
            "30d": _window(_first(reads.get("campaign_30d"))),
        },
        "search_terms": terms,
        "keywords": [k for k in map(_keyword, rows("keywords")) if k["resource_name"]],
        "ads": [a for a in map(_ad, rows("ads")) if a["resource_name"]],
        "conversion_actions": [c for c in map(_conversion_action, rows("conversion_actions"))],
        "subscriptions": [s for s in map(_subscription, rows("subscriptions")) if s["type"]],
    }


# ---------------------------------------------------------------- schemas and prompts


def classify_schema(terms):
    values = sorted({str(t) for t in terms})
    if not values:
        raise ValueError("Nothing to classify.")
    return paid_ads.obj(
        {
            "labels": {
                "type": "array",
                "items": paid_ads.obj(
                    {"term": paid_ads.enum(values), "label": paid_ads.enum(LABELS)}
                ),
            }
        }
    )


def brief_schema():
    return paid_ads.obj(
        {
            "summary": paid_ads.STR,
            "changes_explained": {"type": "array", "items": paid_ads.STR},
            "watch_for": {"type": "array", "items": paid_ads.STR, "maxItems": 3},
        }
    )


REPAIR_SCHEMA = paid_ads.REPAIR_SCHEMA


def _json(value, limit=None):
    text = json.dumps(value, indent=1, ensure_ascii=False, default=str)
    return text if limit is None or len(text) <= limit else text[:limit] + "\n…(truncated)"


def classify_prompt(business_summary: str, terms_with_metrics: list[dict]):
    system = f"{WRITING}\n\n{CLASSIFY_RULES}"
    user = (
        f"Business summary:\n{str(business_summary or '').strip()[:2000]}\n\n"
        "Search terms with their clicks, cost and conversions over the last seven days:\n"
        f"{_json(terms_with_metrics, 60_000)}\n\n"
        "Label every term."
    )
    return system, user


def brief_prompt(decision: dict, campaign: dict):
    system = f"{WRITING}\n\n{BRIEF_RULES}"
    user = (
        f"Campaign facts:\n{_json(campaign, 6000)}\n\n"
        f"Today's decision (already made by code):\n{_json(decision, 40_000)}\n\n"
        "Write the summary, the changes explained and what to watch for."
    )
    return system, user


def validate_labels(result: dict, terms: list[str]) -> dict[str, str]:
    """Every listed term labelled once; unknown terms or labels are unusable."""
    rows = result.get("labels") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise paid_ads.UnusableModelResult("labels are not a list")
    wanted, labels = set(terms), {}
    for row in rows:
        if not isinstance(row, dict) or row.get("term") not in wanted:
            raise paid_ads.UnusableModelResult("label refers to a term that was not asked")
        if row.get("label") not in LABELS:
            raise paid_ads.UnusableModelResult("label is outside the fixed vocabulary")
        if row["term"] in labels and labels[row["term"]] != row["label"]:
            raise paid_ads.UnusableModelResult("a term received two labels")
        labels[row["term"]] = row["label"]
    missing = wanted - set(labels)
    if missing:
        raise paid_ads.UnusableModelResult(f"{len(missing)} terms were not labelled")
    return labels


def validate_brief(result: dict) -> dict:
    if not isinstance(result, dict):
        raise paid_ads.UnusableModelResult("brief is not an object")
    summary = result.get("summary")
    changes = result.get("changes_explained")
    watch = result.get("watch_for")
    if not isinstance(summary, str) or not 20 <= len(summary.strip()) <= 1200:
        raise paid_ads.UnusableModelResult("brief summary is missing or out of bounds")
    for name, items in (("changes_explained", changes), ("watch_for", watch)):
        if (
            not isinstance(items, list)
            or len(items) > 12
            or any(not isinstance(i, str) or not i.strip() or len(i) > 400 for i in items)
        ):
            raise paid_ads.UnusableModelResult(f"brief {name} is malformed")
    return {
        "summary": summary.strip(),
        "changes_explained": [i.strip() for i in changes],
        "watch_for": [i.strip() for i in watch][:3],
    }


# ---------------------------------------------------------------- the decision


def _iso_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _money(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def _allowable(campaign: dict) -> float | None:
    value = campaign.get("allowable_cpa_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _term_totals(search_terms: list[dict]) -> list[dict]:
    """One row per search term: Google splits a term by ad group and matched keyword, and a
    campaign-wide negative blocks it in all of them."""
    totals: dict[str, dict] = {}
    for row in search_terms:
        total = totals.setdefault(
            row["term"],
            {
                "term": row["term"],
                "clicks": 0,
                "cost_usd": 0.0,
                "conversions": 0.0,
                "excluded": False,
            },
        )
        total["clicks"] += row["clicks"]
        total["cost_usd"] = _money(total["cost_usd"] + row["cost_usd"])
        total["conversions"] += row["conversions"]
        total["excluded"] = total["excluded"] or row["status"] in EXCLUDED_TERM_STATES
    return sorted(totals.values(), key=lambda t: (-t["cost_usd"], -t["clicks"], t["term"]))


def _negatives(reads: dict, labels: dict, cap: int) -> list[dict]:
    chosen = []
    for term in _term_totals(reads["search_terms"]):
        if len(chosen) >= cap:
            break
        label = labels.get(term["term"])
        if label not in AUTO_NEGATIVE_LABELS:
            continue
        if term["conversions"] > 0 or term["excluded"]:
            continue
        if term["clicks"] < POLICY["search_terms_min_clicks"]:
            continue
        chosen.append(
            {
                "text": term["term"],
                "match_type": "PHRASE",
                "clicks": term["clicks"],
                "cost_usd": term["cost_usd"],
                "why": (
                    f"{term['clicks']} clicks and ${term['cost_usd']:.2f} with no conversion; "
                    + (
                        "the searcher wanted something else."
                        if label == "irrelevant"
                        else "the searcher wanted a job, a lesson or a free copy."
                    )
                ),
            }
        )
    return chosen


def _pause_ads(reads: dict) -> list[dict]:
    return [
        {
            "resource_name": ad["resource_name"],
            "ad_id": ad["ad_id"],
            "why": "Google disapproved this ad"
            + (f" ({', '.join(ad['topics'][:3])})" if ad["topics"] else "")
            + "; it cannot serve and a disapproved ad can hold up the campaign.",
        }
        for ad in reads["ads"]
        if ad["approval_status"] == "DISAPPROVED" and ad["status"] == "ENABLED"
    ]


def _pause_keywords(reads: dict, allowable: float | None, days_live: int) -> list[dict]:
    if allowable is None or days_live < POLICY["keyword_pause_min_days"]:
        return []
    threshold = allowable * POLICY["keyword_pause_multiple"]
    return [
        {
            "resource_name": kw["resource_name"],
            "text": kw["text"],
            "cost_usd": kw["cost_usd"],
            "why": (
                f"${kw['cost_usd']:.2f} spent over thirty days with no conversion, more than "
                f"{POLICY['keyword_pause_multiple']:.0f} times the ${allowable:.2f} you can pay for one."
            ),
        }
        for kw in reads["keywords"]
        if kw["status"] == "ENABLED" and kw["conversions"] == 0 and kw["cost_usd"] >= threshold
    ]


def _bid_proposal(reads: dict, allowable: float | None) -> dict | None:
    campaign, month = reads["campaign"], reads["windows"]["30d"]
    strategy, conversions = campaign["bidding_strategy_type"], month["conversions"]
    if strategy == "TARGET_SPEND" and conversions >= POLICY["max_conv_threshold"]:
        return {
            "kind": "bid_strategy_change",
            "previous": {
                "strategy": "maximize_clicks",
                "cpc_ceiling_usd": campaign["cpc_ceiling_usd"],
            },
            "proposed": {"strategy": "maximize_conversions", "target_cpa_usd": None},
            "rationale": (
                f"{conversions:g} conversions in thirty days is enough for Google to bid for "
                "conversions instead of clicks. Nothing else changes; the daily budget still caps spend."
            ),
        }
    if (
        strategy == "MAXIMIZE_CONVERSIONS"
        and campaign["target_cpa_usd"] is None
        and conversions >= POLICY["tcpa_threshold"]
        and month["cpa_usd"] is not None
    ):
        target = month["cpa_usd"] * POLICY["tcpa_multiplier"]
        if allowable is not None:
            target = min(target, allowable)
        return {
            "kind": "bid_strategy_change",
            "previous": {"strategy": "maximize_conversions", "target_cpa_usd": None},
            "proposed": {"strategy": "target_cpa", "target_cpa_usd": _money(target)},
            "rationale": (
                f"{conversions:g} conversions in thirty days at ${month['cpa_usd']:.2f} each. "
                f"A target of ${_money(target):.2f} tells Google what a conversion may cost"
                + (
                    f", never above the ${allowable:.2f} you can pay for one."
                    if allowable is not None
                    else "."
                )
            ),
        }
    return None


def _budget_proposal(reads: dict, allowable: float | None) -> dict | None:
    campaign, fortnight = reads["campaign"], reads["windows"]["14d"]
    budget = campaign["budget_usd"]
    if allowable is None or budget <= 0:
        return None
    step = POLICY["budget_step"]
    if (
        "BUDGET_CONSTRAINED" in campaign["primary_status_reasons"]
        and fortnight["cpa_usd"] is not None
        and fortnight["cpa_usd"] <= allowable
    ):
        proposed = _money(budget * (1 + step))
        return {
            "kind": "budget_change",
            "previous": {"daily_budget_usd": budget},
            "proposed": {"daily_budget_usd": proposed},
            "rationale": (
                f"The budget ran out on most days while a conversion cost ${fortnight['cpa_usd']:.2f}, "
                f"under the ${allowable:.2f} you can pay. A {step:.0%} step from ${budget:.2f} to "
                f"${proposed:.2f} a day buys more of the same."
            ),
        }
    if (
        fortnight["conversions"] == 0
        and fortnight["cost_usd"] >= allowable * POLICY["keyword_pause_multiple"]
    ):
        proposed = max(1.0, _money(budget * (1 - step)))
        return {
            "kind": "budget_change",
            "previous": {"daily_budget_usd": budget},
            "proposed": {"daily_budget_usd": proposed},
            "rationale": (
                f"${fortnight['cost_usd']:.2f} in two weeks brought no conversion, more than "
                f"{POLICY['keyword_pause_multiple']:.0f} times the ${allowable:.2f} you can pay for one. "
                f"A {step:.0%} step down from ${budget:.2f} to ${proposed:.2f} a day slows the loss while "
                "the negatives and keyword pauses take effect."
            ),
        }
    return None


def _alerts_and_findings(reads: dict, days_live: int, labels: dict) -> tuple[list, list]:
    campaign, week, month = reads["campaign"], reads["windows"]["7d"], reads["windows"]["30d"]
    alerts, findings = [], []
    if campaign["ai_max_enabled"]:
        alerts.append(
            {
                "code": "ai_max_on",
                "text": "AI Max is switched on. Tin created the campaign with it off; Google may have changed it. Turn it off in Google Ads or the campaign will buy searches Tin never chose.",
            }
        )
    if campaign["migration_dates"]:
        alerts.append(
            {
                "code": "migrated",
                "text": "Google has scheduled or applied an automatic migration on this campaign. Check its settings in Google Ads.",
            }
        )
    for reason in campaign["primary_status_reasons"]:
        if reason in ALERT_REASONS:
            alerts.append(
                {
                    "code": reason.lower(),
                    "text": {
                        "HAS_ADS_DISAPPROVED": "Google disapproved at least one ad.",
                        "MISCONFIGURED": "Google reports the campaign as misconfigured.",
                        "NOT_ELIGIBLE": "Google reports the campaign as not eligible to serve.",
                    }[reason],
                }
            )
    if campaign["status"] == "PAUSED":
        alerts.append(
            {
                "code": "paused_externally",
                "text": "The campaign is paused in Google Ads. Tin did not pause it; nothing is spending.",
            }
        )
    if reads["conversion_actions"] and days_live > 7:
        total = sum(c["conversions_30d"] for c in reads["conversion_actions"])
        if total == 0:
            alerts.append(
                {
                    "code": "no_conversion_data",
                    "text": "No conversion action recorded anything in thirty days. Either nobody converted or the tag stopped firing; check the tag before trusting any cost per conversion.",
                }
            )
    if (week["budget_lost_share"] or 0) >= 0.3:
        findings.append(
            {
                "code": "budget_limited",
                "text": f"The campaign missed about {week['budget_lost_share']:.0%} of its possible impressions last week because the daily budget ran out.",
            }
        )
    enabled = [s["type"] for s in reads["subscriptions"] if s["status"] == "ENABLED"]
    if enabled:
        findings.append(
            {
                "code": "auto_apply_on",
                "text": "Google is allowed to apply its own recommendations automatically: "
                + ", ".join(sorted(enabled)[:6])
                + ". Tin pauses these at launch; turn them off in Google Ads if they came back.",
            }
        )
    allowable = None
    if labels:
        for label, code, what in (
            ("competitor", "competitor_terms", "competitor names"),
            ("unsure", "unsure_terms", "terms Tin could not judge"),
        ):
            terms = [t["term"] for t in reads["search_terms"] if labels.get(t["term"]) == label]
            if terms:
                findings.append(
                    {
                        "code": code,
                        "text": f"Searches for {what} brought clicks: "
                        + ", ".join(terms[:8])
                        + ". Tin never blocks these on its own; add them as negatives in Google Ads if you want them gone.",
                    }
                )
    del allowable, month
    return alerts, findings


def decide(
    *,
    campaign: dict,
    reads: dict,
    labels: dict,
    today: date,
    auto_negatives_cap: int,
) -> dict:
    """Everything Tin will do or propose today, decided by the rules alone.

    Order of the single proposal: the bidding strategy first because it is the larger lever;
    a budget change is proposed only when no bidding change is due.
    """
    enabled_on = _iso_date(campaign.get("enabled_at"))
    days_live = max(0, (today - enabled_on).days) if enabled_on else 0
    allowable = _allowable(campaign)
    quiet = enabled_on is None or days_live < POLICY["quiet_days"]
    cap = max(0, min(50, int(auto_negatives_cap)))
    alerts, findings = _alerts_and_findings(reads, days_live, labels)
    week = reads["windows"]["7d"]
    if (
        allowable is not None
        and week["cpa_usd"] is not None
        and week["ctr"] >= POLICY["landing_page_ctr"]
        and week["cpa_usd"] >= allowable * POLICY["landing_page_cpa_multiple"]
    ):
        findings.append(
            {
                "code": "landing_page",
                "text": (
                    f"Click-through is {week['ctr']:.1%} but a conversion costs ${week['cpa_usd']:.2f}, "
                    f"well above the ${allowable:.2f} you can pay: the ad works and the page does not. "
                    "The landing page, not the campaign, is the thing to fix."
                ),
            }
        )
    if quiet:
        findings.insert(
            0,
            {
                "code": "quiet_period",
                "text": f"Day {days_live + 1} of the first three: Tin only checks that ads were approved, spend is registering and the tag records data. No changes yet.",
            },
        )
        auto, proposals = {"negatives": [], "pause_ads": [], "pause_keywords": []}, []
    else:
        auto = {
            "negatives": _negatives(reads, labels, cap),
            "pause_ads": _pause_ads(reads),
            "pause_keywords": _pause_keywords(reads, allowable, days_live),
        }
        proposal = _bid_proposal(reads, allowable) or _budget_proposal(reads, allowable)
        proposals = [proposal] if proposal else []
    return {
        "quiet": quiet,
        "days_live": days_live,
        "auto": auto,
        "proposals": proposals,
        "alerts": alerts,
        "findings": findings,
        "metrics": {
            "allowable_cpa_usd": allowable,
            "windows": reads["windows"],
            "campaign": reads["campaign"],
            "search_terms": len(reads["search_terms"]),
            "keywords": len(reads["keywords"]),
            "ads": len(reads["ads"]),
        },
    }


# ---------------------------------------------------------------- rendering


def _md(value) -> str:
    return re.sub(r"[\r\n]+", " ", str(value)).strip()


def _plural(count, word) -> str:
    count = int(count)
    return f"{count} {word}{'' if count == 1 else 's'}"


def _window_line(name: str, window: dict) -> str:
    cpa = (
        f"${window['cpa_usd']:.2f} per conversion"
        if window["cpa_usd"] is not None
        else "no conversion yet"
    )
    return (
        f"- {name}: {_plural(window['clicks'], 'click')} for ${window['cost_usd']:.2f} "
        f"({window['ctr']:.1%} of {window['impressions']} impressions clicked, "
        f"${window['cpc_usd']:.2f} a click), {window['conversions']:g} conversions, {cpa}."
    )


def _applied_lines(applied: dict) -> list[str]:
    lines = []
    for item in applied.get("negatives") or []:
        lines.append(
            f'- Blocked the search "{_md(item["text"])}" ({item.get("status", "applied")}): {_md(item.get("why", ""))}'
        )
    for item in applied.get("pause_ads") or []:
        lines.append(
            f"- Paused ad {item.get('ad_id', '')} ({item.get('status', 'applied')}): {_md(item.get('why', ''))}"
        )
    for item in applied.get("pause_keywords") or []:
        lines.append(
            f'- Paused the keyword "{_md(item["text"])}" ({item.get("status", "applied")}): {_md(item.get("why", ""))}'
        )
    return lines


def proposal_document(proposal: dict, campaign: dict, number: int, today: date) -> str:
    kind = proposal["kind"]
    previous, proposed = proposal["previous"], proposal["proposed"]
    if kind == "budget_change":
        title = "Change the daily budget"
        change = f"From ${previous['daily_budget_usd']:.2f} a day to ${proposed['daily_budget_usd']:.2f} a day."
    else:
        title = "Change how Google bids"
        names = {
            "maximize_clicks": "as many clicks as the budget buys, with a cost-per-click ceiling",
            "maximize_conversions": "as many conversions as the budget buys",
            "target_cpa": "conversions at a target cost each",
        }
        change = f"From {names.get(previous.get('strategy'), previous.get('strategy'))} to {names.get(proposed.get('strategy'), proposed.get('strategy'))}"
        if proposed.get("target_cpa_usd") is not None:
            change += f", target ${proposed['target_cpa_usd']:.2f} per conversion"
        change += "."
    return (
        f"# Proposal {number}: {title}\n\n"
        f"Campaign: {_md(campaign.get('name') or campaign.get('campaign_name') or 'your Google Search campaign')}. "
        f"Written on {today.isoformat()}.\n\n"
        f"## What would change\n\n{change}\n\n"
        f"## Why\n\n{_md(proposal['rationale'])}\n\n"
        "## What happens next\n\n"
        "Nothing changes until you approve. Approve it from the Activity feed, the dashboard or by "
        "asking Tin, and Tin makes exactly this one change in your Google Ads account. Discard it "
        "and Tin leaves the campaign as it is; the next check may propose it again if the numbers "
        "still say so.\n"
    )


def render(
    *,
    decision: dict,
    campaign: dict,
    reads_normalized: dict,
    brief: dict,
    applied: dict,
    proposals_saved: list,
    run_id: str,
    today: date,
) -> dict[str, str]:
    campaign_name = (
        campaign.get("name") or campaign.get("campaign_name") or "your Google Search campaign"
    )
    windows = reads_normalized["windows"]
    allowable = decision["metrics"].get("allowable_cpa_usd")
    lines = [
        f"# Google Ads check, {today.isoformat()}",
        "",
        f"Campaign: {_md(campaign_name)}. Live for {_plural(decision['days_live'], 'day')}.",
        "",
        _md(brief.get("summary", "")),
        "",
        "## The numbers",
        "",
        _window_line("Last 7 days", windows["7d"]),
        _window_line("Last 30 days", windows["30d"]),
    ]
    if allowable is not None:
        lines.append(
            f"- The most you can pay for one conversion, from the assessment: ${allowable:.2f}."
        )
    campaign_facts = reads_normalized["campaign"]
    lines.append(
        f"- Google reports the campaign as {campaign_facts['primary_status'].lower().replace('_', ' ')}"
        + (
            f" ({', '.join(r.lower().replace('_', ' ') for r in campaign_facts['primary_status_reasons'][:3])})"
            if campaign_facts["primary_status_reasons"]
            else ""
        )
        + f"; daily budget ${campaign_facts['budget_usd']:.2f}."
    )
    lines += ["", "## What Tin did"]
    applied_lines = _applied_lines(applied)
    lines += [""] + (applied_lines or ["- Nothing changed today."])
    for explanation in brief.get("changes_explained") or []:
        lines.append(f"- {_md(explanation)}")
    lines += ["", "## Waiting for you"]
    if proposals_saved:
        for item in proposals_saved:
            proposal = item.get("proposal") or {}
            lines.append(
                f"- Proposal {item.get('number', '?')}: {_md(proposal.get('rationale', ''))} "
                f"Saved as {item.get('path', '')}."
            )
    else:
        lines.append("- No proposals today.")
    if decision["alerts"]:
        lines += ["", "## Needs your attention", ""]
        lines += [f"- {_md(alert['text'])}" for alert in decision["alerts"]]
    if decision["findings"]:
        lines += ["", "## Worth knowing", ""]
        lines += [f"- {_md(finding['text'])}" for finding in decision["findings"]]
    if brief.get("watch_for"):
        lines += ["", "## Watch for", ""]
        lines += [f"- {_md(item)}" for item in brief["watch_for"]]
    lines.append("")
    report = "\n".join(lines)
    payload = {
        "schema_version": POLICY["version"],
        "run_id": str(UUID(str(run_id))),
        "date": today.isoformat(),
        "campaign": campaign,
        "decision": decision,
        "applied": applied,
        "proposals": proposals_saved,
        "brief": brief,
        "reads": reads_normalized,
    }
    documents = {"MONITOR.md": report, "monitor.json": json.dumps(payload, indent=1, default=str)}
    for name, text in documents.items():
        if len(text.encode()) > LIMITS[name]:
            raise ValueError(f"{name} exceeds its size limit.")
    return documents


def summary_line(decision: dict, applied: dict, proposals_saved: list) -> str:
    parts = []
    negatives = sum(
        1 for i in applied.get("negatives") or [] if i.get("status", "applied") == "applied"
    )
    paused = sum(
        1
        for key in ("pause_ads", "pause_keywords")
        for i in applied.get(key) or []
        if i.get("status", "applied") == "applied"
    )
    if negatives:
        parts.append(_plural(negatives, "negative") + " added")
    if paused:
        parts.append(_plural(paused, "pause") + " applied")
    if proposals_saved:
        parts.append(_plural(len(proposals_saved), "proposal") + " awaiting you")
    if decision.get("alerts"):
        parts.append(_plural(len(decision["alerts"]), "alert"))
    if not parts:
        return (
            "Quiet period, nothing changed" if decision.get("quiet") else "Checked, nothing changed"
        )
    return ", ".join(parts)
