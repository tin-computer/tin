"""Pure Google Ads REST request builders and the fixed GAQL set (API v25).

Everything here is deterministic and free of I/O so the exact bodies a launch or a monitor
sends can be pinned in tests. Money is expressed as USD and converted to micros strings; enums
are the REST string forms; temporary resource ids are negative and unique per bulk request.
"""

# GAQL literals are escaped and ids digit-checked above each query; this is not SQL.
# ruff: noqa: S608
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from uuid import UUID

GEO: dict[str, int] = {
    "US": 2840,
    "GB": 2826,
    "CA": 2124,
    "AU": 2036,
    "DE": 2276,
    "FR": 2250,
    "NL": 2528,
    "IE": 2372,
    "ES": 2724,
    "IT": 2380,
    "SE": 2752,
    "DK": 2208,
    "NO": 2578,
    "CH": 2756,
    "AT": 2040,
    "BE": 2056,
    "NZ": 2554,
    "IN": 2356,
    "SG": 2702,
    "JP": 2392,
    "BR": 2076,
    "MX": 2484,
    "PT": 2620,
    "FI": 2246,
    "PL": 2616,
}

SUBSCRIPTION_TYPES = (
    "KEYWORD",
    "KEYWORD_MATCH_TYPE",
    "USE_BROAD_MATCH_KEYWORD",
    "RESPONSIVE_SEARCH_AD",
    "RESPONSIVE_SEARCH_AD_IMPROVE_AD_STRENGTH",
    "SEARCH_PARTNERS_OPT_IN",
    "MAXIMIZE_CLICKS_OPT_IN",
    "TARGET_CPA_OPT_IN",
    "SET_TARGET_CPA",
    "RAISE_TARGET_CPA",
    "ENHANCED_CPC_OPT_IN",
    "OPTIMIZE_AD_ROTATION",
)
MATCH_TYPES = ("EXACT", "PHRASE")
CONVERSION_CATEGORIES = (
    "SIGNUP",
    "PURCHASE",
    "SUBMIT_LEAD_FORM",
    "BEGIN_CHECKOUT",
    "SUBSCRIBE_PAID",
    "BOOK_APPOINTMENT",
    "LEAD",
    "DOWNLOAD",
    "CONTACT",
    "REQUEST_QUOTE",
)
BOUNDS = {
    "headlines": (3, 15, 30),
    "descriptions": (2, 4, 90),
    "path": 15,
    "sitelink_text": 25,
    "sitelink_description": 35,
    "callout": 25,
    "keyword_chars": 80,
    "keyword_words": 10,
    "ad_groups": (1, 20),
    "keywords_per_group": (1, 50),
    "negatives": 300,
    "sitelinks": 4,
    "callouts": 6,
    "min_budget_usd": Decimal("1"),
    "min_ceiling_usd": Decimal("0.05"),
    "search_rows": 5000,
}
CUSTOMER_ID = re.compile(r"^\d{10}$")
DIGITS = re.compile(r"^\d{1,20}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RESOURCE = re.compile(r"^customers/\d{10}/[A-Za-z]+/[0-9~A-Za-z_-]+$")
_MICROS = Decimal(1_000_000)


def customer_id(value) -> str:
    """Return the ten digit customer id from any dashed or spaced form."""
    if not isinstance(value, str):
        raise ValueError("Google Ads customer ids are ten digits.")
    digits = re.sub(r"[\s-]", "", value)
    if not CUSTOMER_ID.fullmatch(digits):
        raise ValueError("Google Ads customer ids are ten digits.")
    return digits


def micros(usd) -> str:
    """USD to a micros int64 string; the API rejects fractions of a micro."""
    if isinstance(usd, bool):
        raise ValueError("Amounts must be numbers.")
    try:
        amount = Decimal(str(usd))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Amounts must be numbers.") from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000"):
        raise ValueError("Amounts must be finite, non-negative and below one million dollars.")
    return str(int((amount * _MICROS).quantize(Decimal(1))))


def usd(value) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("Micros must be an integer string.")
    return float(Decimal(str(value)) / _MICROS)


def marker(run_id) -> str:
    digest = hashlib.sha256(str(UUID(str(run_id))).encode()).hexdigest()
    return f"[tin:{digest[:12]}]"


def _text(value, *, limit: int, label: str, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    cleaned = " ".join(value.split())
    if not minimum <= len(cleaned) <= limit:
        raise ValueError(f"{label} must be between {minimum} and {limit} characters.")
    return cleaned


def _keyword(item) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Keywords are objects with text and match_type.")
    text = _text(item.get("text"), limit=BOUNDS["keyword_chars"], label="Keyword text")
    if len(text.split()) > BOUNDS["keyword_words"]:
        raise ValueError("Keywords carry at most ten words.")
    match_type = item.get("match_type")
    if not isinstance(match_type, str) or match_type.upper() not in MATCH_TYPES:
        raise ValueError("Keywords use exact or phrase match; broad match is never sent.")
    return {"text": text, "matchType": match_type.upper()}


def _url(value, *, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("https://") or len(value) > 2000:
        raise ValueError(f"{label} must be a public https address.")
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError(f"{label} must be a public https address.")
    return value


def _amount(value, *, minimum: Decimal, label: str) -> str:
    result = micros(value)
    if Decimal(result) < minimum * _MICROS:
        raise ValueError(f"{label} must be at least ${minimum}.")
    return result


class _Ids:
    """Negative temporary ids, unique across one bulk request."""

    def __init__(self):
        self._next = -1

    def take(self) -> int:
        value = self._next
        self._next -= 1
        return value


def _list(value, *, label: str, maximum: int, minimum: int = 0) -> list:
    if value is None:
        value = []
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{label} must list between {minimum} and {maximum} items.")
    return value


def bundle_names(plan: dict) -> dict[str, str]:
    """The names campaign_bundle sends, marked once; lookups after an uncertain create use them."""
    mark = _text(plan.get("marker"), limit=40, label="Marker")
    name = _text(plan.get("campaign_name"), limit=200, label="Campaign name")
    name = name.removesuffix(f" {mark}")
    return {
        "campaign": f"{name} {mark}",
        "budget": f"{name} budget {mark}",
        "shared_set": f"{name} negatives {mark}",
    }


def campaign_bundle(plan: dict, *, customer_id: str) -> list[dict]:
    """The whole paused campaign as one ordered, all-or-nothing `mutateOperations` list."""
    cid = globals()["customer_id"](customer_id)
    if not isinstance(plan, dict):
        raise ValueError("A campaign plan is an object.")
    names = bundle_names(plan)
    budget = _amount(plan.get("daily_budget_usd"), minimum=BOUNDS["min_budget_usd"], label="Budget")
    ceiling = _amount(
        plan.get("cpc_ceiling_usd"), minimum=BOUNDS["min_ceiling_usd"], label="CPC ceiling"
    )
    markets = _list(plan.get("markets"), label="Markets", maximum=len(GEO), minimum=1)
    if any(not isinstance(m, str) or m not in GEO for m in markets) or len(set(markets)) != len(
        markets
    ):
        raise ValueError("Markets must be distinct supported country codes.")
    landing = _url(plan.get("landing_page"), label="Landing page")
    start_date = plan.get("start_date")
    if not isinstance(start_date, str) or not DATE.fullmatch(start_date):
        raise ValueError("Start date must be YYYY-MM-DD.")
    template = plan.get("tracking_template")
    if template is not None:
        template = _text(template, limit=500, label="Tracking template")
        if not template.startswith("{lpurl}"):
            raise ValueError("Tracking templates must start with {lpurl}.")
    groups = _list(
        plan.get("ad_groups"),
        label="Ad groups",
        minimum=BOUNDS["ad_groups"][0],
        maximum=BOUNDS["ad_groups"][1],
    )
    negatives = [
        _keyword(item)
        for item in _list(plan.get("negatives"), label="Negatives", maximum=BOUNDS["negatives"])
    ]
    sitelinks = _list(plan.get("sitelinks"), label="Sitelinks", maximum=BOUNDS["sitelinks"])
    callouts = _list(plan.get("callouts"), label="Callouts", maximum=BOUNDS["callouts"])

    ids = _Ids()
    ops: list[dict] = []

    def resource(kind: str, temp: int) -> str:
        return f"customers/{cid}/{kind}/{temp}"

    budget_rn = resource("campaignBudgets", ids.take())
    ops.append(
        {
            "campaignBudgetOperation": {
                "create": {
                    "resourceName": budget_rn,
                    "name": names["budget"],
                    "amountMicros": budget,
                    "deliveryMethod": "STANDARD",
                    "explicitlyShared": False,
                }
            }
        }
    )
    campaign_rn = resource("campaigns", ids.take())
    campaign = {
        "resourceName": campaign_rn,
        "name": names["campaign"],
        "status": "PAUSED",
        "advertisingChannelType": "SEARCH",
        "campaignBudget": budget_rn,
        "targetSpend": {"cpcBidCeilingMicros": ceiling},
        "networkSettings": {
            "targetGoogleSearch": True,
            "targetSearchNetwork": False,
            "targetContentNetwork": False,
            "targetPartnerSearchNetwork": False,
        },
        "geoTargetTypeSetting": {
            "positiveGeoTargetType": "PRESENCE",
            "negativeGeoTargetType": "PRESENCE",
        },
        "aiMaxSetting": {"enableAiMax": False},
        "assetAutomationSettings": [
            {
                "assetAutomationType": "TEXT_ASSET_AUTOMATION",
                "assetAutomationStatus": "OPTED_OUT",
            },
            {
                "assetAutomationType": "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
                "assetAutomationStatus": "OPTED_OUT",
            },
        ],
        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    }
    if template:
        campaign["trackingUrlTemplate"] = template
    ops.append({"campaignOperation": {"create": campaign}})
    for market in markets:
        ops.append(
            {
                "campaignCriterionOperation": {
                    "create": {
                        "campaign": campaign_rn,
                        "location": {"geoTargetConstant": f"geoTargetConstants/{GEO[market]}"},
                    }
                }
            }
        )
    shared_rn = resource("sharedSets", ids.take())
    ops.append(
        {
            "sharedSetOperation": {
                "create": {
                    "resourceName": shared_rn,
                    "name": names["shared_set"],
                    "type": "NEGATIVE_KEYWORDS",
                }
            }
        }
    )
    for negative in negatives:
        ops.append(
            {"sharedCriterionOperation": {"create": {"sharedSet": shared_rn, "keyword": negative}}}
        )
    ops.append(
        {
            "campaignSharedSetOperation": {
                "create": {"campaign": campaign_rn, "sharedSet": shared_rn}
            }
        }
    )
    group_rns: list[tuple[str, dict]] = []
    seen_groups: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Ad groups are objects.")
        group_name = _text(group.get("name"), limit=200, label="Ad group name")
        if group_name in seen_groups:
            raise ValueError("Ad group names must be unique.")
        seen_groups.add(group_name)
        rn = resource("adGroups", ids.take())
        group_rns.append((rn, group))
        group_status = group.get("status", "ENABLED")
        if group_status not in {"ENABLED", "PAUSED"}:
            raise ValueError("An ad group is ENABLED or PAUSED.")
        ops.append(
            {
                "adGroupOperation": {
                    "create": {
                        "resourceName": rn,
                        "name": group_name,
                        "campaign": campaign_rn,
                        "status": group_status,
                        "type": "SEARCH_STANDARD",
                    }
                }
            }
        )
    for rn, group in group_rns:
        keywords = _list(
            group.get("keywords"),
            label="Ad group keywords",
            minimum=BOUNDS["keywords_per_group"][0],
            maximum=BOUNDS["keywords_per_group"][1],
        )
        for item in keywords:
            ops.append(
                {
                    "adGroupCriterionOperation": {
                        "create": {"adGroup": rn, "status": "ENABLED", "keyword": _keyword(item)}
                    }
                }
            )
    for rn, group in group_rns:
        ops.append({"adGroupAdOperation": {"create": _rsa(rn, group, landing)}})
    asset_links: list[tuple[str, str]] = []
    for item in sitelinks:
        if not isinstance(item, dict):
            raise ValueError("Sitelinks are objects.")
        rn = resource("assets", ids.take())
        asset = {
            "resourceName": rn,
            "finalUrls": [_url(item.get("url"), label="Sitelink URL")],
            "sitelinkAsset": {
                "linkText": _text(
                    item.get("text"), limit=BOUNDS["sitelink_text"], label="Sitelink text"
                )
            },
        }
        d1, d2 = item.get("description1"), item.get("description2")
        if (d1 is None) != (d2 is None):
            raise ValueError("Sitelink descriptions come in pairs.")
        if d1 is not None:
            asset["sitelinkAsset"]["description1"] = _text(
                d1, limit=BOUNDS["sitelink_description"], label="Sitelink description"
            )
            asset["sitelinkAsset"]["description2"] = _text(
                d2, limit=BOUNDS["sitelink_description"], label="Sitelink description"
            )
        ops.append({"assetOperation": {"create": asset}})
        asset_links.append((rn, "SITELINK"))
    for item in callouts:
        rn = resource("assets", ids.take())
        ops.append(
            {
                "assetOperation": {
                    "create": {
                        "resourceName": rn,
                        "calloutAsset": {
                            "calloutText": _text(item, limit=BOUNDS["callout"], label="Callout")
                        },
                    }
                }
            }
        )
        asset_links.append((rn, "CALLOUT"))
    for rn, field_type in asset_links:
        ops.append(
            {
                "campaignAssetOperation": {
                    "create": {"campaign": campaign_rn, "asset": rn, "fieldType": field_type}
                }
            }
        )
    return ops


def _rsa(group_rn: str, group: dict, landing: str) -> dict:
    low, high, chars = BOUNDS["headlines"]
    headlines = [
        _text(h, limit=chars, label="Headline")
        for h in _list(group.get("headlines"), label="Headlines", minimum=low, maximum=high)
    ]
    low, high, chars = BOUNDS["descriptions"]
    descriptions = [
        _text(d, limit=chars, label="Description")
        for d in _list(group.get("descriptions"), label="Descriptions", minimum=low, maximum=high)
    ]
    if len({h.casefold() for h in headlines}) != len(headlines):
        raise ValueError("Headlines must be distinct.")
    if len({d.casefold() for d in descriptions}) != len(descriptions):
        raise ValueError("Descriptions must be distinct.")
    ad = {
        "finalUrls": [landing],
        "responsiveSearchAd": {
            "headlines": [{"text": h} for h in headlines],
            "descriptions": [{"text": d} for d in descriptions],
        },
    }
    path1, path2 = group.get("path1"), group.get("path2")
    if path2 and not path1:
        raise ValueError("A second display path needs a first one.")
    for key, value in (("path1", path1), ("path2", path2)):
        if value:
            path = _text(value, limit=BOUNDS["path"], label="Display path")
            if not re.fullmatch(r"[A-Za-z0-9-]+", path):
                raise ValueError("Display paths use letters, digits and hyphens only.")
            ad["responsiveSearchAd"][key] = path
    return {"adGroup": group_rn, "status": "ENABLED", "ad": ad}


def bulk_body(operations: list[dict], *, validate_only: bool) -> dict:
    if not isinstance(operations, list) or not operations or len(operations) > 10_000:
        raise ValueError("A bulk mutate carries between one and ten thousand operations.")
    return {
        "mutateOperations": operations,
        "partialFailure": False,
        "validateOnly": bool(validate_only),
        "responseContentType": "MUTABLE_RESOURCE",
    }


_RESULT_KEYS = {
    "campaignBudgetOperation": ("campaignBudgetResult", "campaignBudget"),
    "campaignOperation": ("campaignResult", "campaign"),
    "campaignCriterionOperation": ("campaignCriterionResult", "campaignCriterion"),
    "sharedSetOperation": ("sharedSetResult", "sharedSet"),
    "sharedCriterionOperation": ("sharedCriterionResult", "sharedCriterion"),
    "campaignSharedSetOperation": ("campaignSharedSetResult", "campaignSharedSet"),
    "adGroupOperation": ("adGroupResult", "adGroup"),
    "adGroupCriterionOperation": ("adGroupCriterionResult", "adGroupCriterion"),
    "adGroupAdOperation": ("adGroupAdResult", "adGroupAd"),
    "assetOperation": ("assetResult", "asset"),
    "campaignAssetOperation": ("campaignAssetResult", "campaignAsset"),
}


def _result_name(operation: dict, response: dict) -> str:
    (kind,) = operation
    result_key, resource_key = _RESULT_KEYS[kind]
    result = response.get(result_key)
    if not isinstance(result, dict):
        raise ValueError("A mutate result did not match its operation.")
    name = result.get("resourceName")
    if not isinstance(name, str) or not name:
        inner = result.get(resource_key)
        name = inner.get("resourceName") if isinstance(inner, dict) else None
    if not isinstance(name, str) or not RESOURCE.fullmatch(name):
        raise ValueError("A mutate result carried no resource name.")
    return name


def created_resources(operations: list[dict], responses: list[dict]) -> dict:
    """Map bulk responses back to the created resources by position."""
    if not isinstance(responses, list) or len(responses) != len(operations):
        raise ValueError("The mutate response did not cover every operation.")
    found: dict = {
        "budget": None,
        "campaign": None,
        "shared_set": None,
        "ad_groups": {},
        "ads": [],
        "keywords": [],
        "assets": [],
        "negatives": [],
        "locations": [],
    }
    for operation, response in zip(operations, responses, strict=True):
        if not isinstance(response, dict) or len(operation) != 1:
            raise ValueError("A mutate result did not match its operation.")
        (kind,) = operation
        name = _result_name(operation, response)
        if kind == "campaignBudgetOperation":
            found["budget"] = name
        elif kind == "campaignOperation":
            found["campaign"] = name
        elif kind == "sharedSetOperation":
            found["shared_set"] = name
        elif kind == "adGroupOperation":
            found["ad_groups"][operation[kind]["create"]["name"]] = name
        elif kind == "adGroupAdOperation":
            found["ads"].append(name)
        elif kind == "adGroupCriterionOperation":
            found["keywords"].append(name)
        elif kind == "assetOperation":
            found["assets"].append(name)
        elif kind == "sharedCriterionOperation":
            found["negatives"].append(name)
        elif kind == "campaignCriterionOperation":
            found["locations"].append(name)
    if not (found["budget"] and found["campaign"] and found["shared_set"]):
        raise ValueError("The mutate response is missing the campaign, budget or shared set.")
    return found


def _resource(value, *, kind: str) -> str:
    if not isinstance(value, str) or not RESOURCE.fullmatch(value) or f"/{kind}/" not in value:
        raise ValueError(f"Expected a {kind} resource name.")
    return value


def _body(operations: list[dict]) -> dict:
    return {"operations": operations, "partialFailure": False, "validateOnly": False}


def campaign_status_body(campaign_resource: str, status: str) -> tuple[str, dict]:
    if status not in {"ENABLED", "PAUSED"}:
        raise ValueError("Campaign status is ENABLED or PAUSED.")
    rn = _resource(campaign_resource, kind="campaigns")
    return "campaigns", _body(
        [{"update": {"resourceName": rn, "status": status}, "updateMask": "status"}]
    )


def budget_body(budget_resource: str, daily_usd) -> tuple[str, dict]:
    rn = _resource(budget_resource, kind="campaignBudgets")
    amount = _amount(daily_usd, minimum=BOUNDS["min_budget_usd"], label="Budget")
    return "campaignBudgets", _body(
        [{"update": {"resourceName": rn, "amountMicros": amount}, "updateMask": "amount_micros"}]
    )


def bidding_body(campaign_resource: str, strategy: str, target_cpa_usd=None) -> tuple[str, dict]:
    rn = _resource(campaign_resource, kind="campaigns")
    if strategy == "maximize_conversions":
        if target_cpa_usd is not None:
            raise ValueError("Maximise conversions takes no target.")
        # A bare "maximize_conversions" mask is refused (FIELD_HAS_SUBFIELDS); naming the
        # target subfield with an empty message switches the strategy without a target.
        update, mask = {"maximizeConversions": {}}, "maximize_conversions.target_cpa_micros"
    elif strategy == "target_cpa":
        amount = _amount(target_cpa_usd, minimum=BOUNDS["min_ceiling_usd"], label="Target CPA")
        update = {"maximizeConversions": {"targetCpaMicros": amount}}
        mask = "maximize_conversions.target_cpa_micros"
    else:
        raise ValueError("Unknown bidding strategy.")
    return "campaigns", _body([{"update": {"resourceName": rn, **update}, "updateMask": mask}])


def negatives_body(shared_set_resource: str, terms: list[dict]) -> tuple[str, dict]:
    rn = _resource(shared_set_resource, kind="sharedSets")
    items = _list(terms, label="Negatives", minimum=1, maximum=BOUNDS["negatives"])
    return "sharedCriteria", _body(
        [{"create": {"sharedSet": rn, "keyword": _keyword(item)}} for item in items]
    )


def pause_ads_body(ad_resources: list[str]) -> tuple[str, dict]:
    items = _list(ad_resources, label="Ads", minimum=1, maximum=200)
    return "adGroupAds", _body(
        [
            {
                "update": {"resourceName": _resource(rn, kind="adGroupAds"), "status": "PAUSED"},
                "updateMask": "status",
            }
            for rn in items
        ]
    )


def pause_keywords_body(criterion_resources: list[str]) -> tuple[str, dict]:
    items = _list(criterion_resources, label="Keywords", minimum=1, maximum=500)
    return "adGroupCriteria", _body(
        [
            {
                "update": {
                    "resourceName": _resource(rn, kind="adGroupCriteria"),
                    "status": "PAUSED",
                },
                "updateMask": "status",
            }
            for rn in items
        ]
    )


def subscriptions_body(types: list[str], status: str = "PAUSED") -> tuple[str, dict]:
    if status not in {"PAUSED", "ENABLED"}:
        raise ValueError("Subscription status is PAUSED or ENABLED.")
    items = _list(types, label="Recommendation types", minimum=1, maximum=len(SUBSCRIPTION_TYPES))
    if any(item not in SUBSCRIPTION_TYPES for item in items) or len(set(items)) != len(items):
        raise ValueError("Unknown or repeated recommendation type.")
    return "recommendationSubscriptions:mutateRecommendationSubscription", {
        "operations": [{"create": {"type": item, "status": status}} for item in items],
        "partialFailure": True,
    }


def conversion_action_body(
    name: str, category: str, counting_type: str, default_value, currency: str
) -> tuple[str, dict]:
    if category not in CONVERSION_CATEGORIES:
        raise ValueError("Unknown conversion category.")
    if counting_type not in {"ONE_PER_CLICK", "MANY_PER_CLICK"}:
        raise ValueError("Counting type is ONE_PER_CLICK or MANY_PER_CLICK.")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Currency is a three letter code.")
    value = Decimal(micros(default_value)) / _MICROS
    return "conversionActions", _body(
        [
            {
                "create": {
                    "name": _text(name, limit=200, label="Conversion action name"),
                    "type": "WEBPAGE",
                    "category": category,
                    "status": "ENABLED",
                    "countingType": counting_type,
                    "primaryForGoal": True,
                    "includeInConversionsMetric": True,
                    "valueSettings": {
                        "defaultValue": float(value),
                        "defaultCurrencyCode": currency,
                        "alwaysUseDefaultValue": True,
                    },
                    "attributionModelSettings": {
                        "attributionModel": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"
                    },
                    "clickThroughLookbackWindowDays": 30,
                }
            }
        ]
    )


def client_link_body(customer_id: str) -> dict:
    cid = globals()["customer_id"](customer_id)
    return {"operation": {"create": {"clientCustomer": f"customers/{cid}", "status": "PENDING"}}}


# ---------------------------------------------------------------- GAQL


def _literal(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError("GAQL literals are short text.")
    if any(ord(c) < 32 for c in value):
        raise ValueError("GAQL literals carry no control characters.")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _id(value) -> str:
    text = str(value)
    if not DIGITS.fullmatch(text):
        raise ValueError("Google Ads ids are digits.")
    return text


def _window(days: int) -> str:
    if days not in {7, 14, 30}:
        raise ValueError("Windows are 7, 14 or 30 days.")
    return f"LAST_{days}_DAYS"


_METRICS = (
    "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
    "metrics.all_conversions, metrics.ctr, metrics.average_cpc"
)


def _account() -> str:
    return (
        "SELECT customer.id, customer.descriptive_name, customer.currency_code, "
        "customer.time_zone, customer.manager, customer.test_account, "
        "customer.auto_tagging_enabled, customer.status, "
        "customer.conversion_tracking_setting.conversion_tracking_id, "
        "customer.conversion_tracking_setting.conversion_tracking_status, "
        "customer.conversion_tracking_setting.accepted_customer_data_terms, "
        "customer.conversion_tracking_setting.google_ads_conversion_customer "
        "FROM customer"
    )


def _billing() -> str:
    return (
        "SELECT billing_setup.id, billing_setup.status, billing_setup.start_date_time "
        "FROM billing_setup"
    )


def _conversion_actions(days: int) -> str:
    return (
        "SELECT conversion_action.id, conversion_action.name, conversion_action.category, "
        "conversion_action.status, conversion_action.primary_for_goal, conversion_action.type, "
        "metrics.all_conversions FROM conversion_action "
        f"WHERE segments.date DURING {_window(days)}"
    )


def _conversion_action_snippets(action_id) -> str:
    return (
        "SELECT conversion_action.id, conversion_action.name, conversion_action.status, "
        "conversion_action.tag_snippets FROM conversion_action "
        f"WHERE conversion_action.id = {_id(action_id)}"
    )


def _campaign_by_name(name: str) -> str:
    return (
        "SELECT campaign.id, campaign.resource_name, campaign.name, campaign.status, "
        "campaign.campaign_budget FROM campaign "
        f"WHERE campaign.name = {_literal(name)} AND campaign.status != 'REMOVED'"
    )


def _campaign_health(campaign_id, days: int) -> str:
    return (
        "SELECT campaign.id, campaign.name, campaign.status, campaign.primary_status, "
        "campaign.primary_status_reasons, campaign.bidding_strategy_type, "
        "campaign.target_spend.cpc_bid_ceiling_micros, "
        "campaign.maximize_conversions.target_cpa_micros, "
        "campaign.ai_max_setting.enable_ai_max, campaign.aca_migration_date_time, "
        "campaign.broad_match_migration_date_time, campaign_budget.resource_name, "
        "campaign_budget.amount_micros, campaign_budget.recommended_budget_amount_micros, "
        f"{_METRICS}, metrics.cost_per_conversion, metrics.search_impression_share, "
        "metrics.search_budget_lost_impression_share, "
        "metrics.search_rank_lost_impression_share FROM campaign "
        f"WHERE campaign.id = {_id(campaign_id)} AND segments.date DURING {_window(days)}"
    )


def _search_terms(campaign_id, days: int) -> str:
    return (
        "SELECT search_term_view.search_term, search_term_view.status, "
        "segments.keyword.info.text, segments.keyword.info.match_type, "
        "segments.search_term_match_source, ad_group.id, ad_group.name, "
        f"{_METRICS} FROM search_term_view "
        f"WHERE campaign.id = {_id(campaign_id)} AND segments.date DURING {_window(days)} "
        "ORDER BY metrics.cost_micros DESC LIMIT 500"
    )


def _keywords(campaign_id, days: int) -> str:
    return (
        "SELECT ad_group_criterion.resource_name, ad_group_criterion.criterion_id, "
        "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, "
        "ad_group_criterion.status, ad_group_criterion.approval_status, "
        "ad_group_criterion.system_serving_status, "
        "ad_group_criterion.quality_info.quality_score, ad_group.id, ad_group.name, "
        f"{_METRICS} FROM keyword_view "
        f"WHERE campaign.id = {_id(campaign_id)} AND segments.date DURING {_window(days)}"
    )


def _ad_policy(campaign_id) -> str:
    return (
        "SELECT ad_group_ad.resource_name, ad_group_ad.ad.id, ad_group_ad.status, "
        "ad_group_ad.ad_strength, ad_group_ad.policy_summary.approval_status, "
        "ad_group_ad.policy_summary.review_status, "
        "ad_group_ad.policy_summary.policy_topic_entries, ad_group_ad.primary_status, "
        "ad_group_ad.primary_status_reasons, ad_group.id, ad_group.name FROM ad_group_ad "
        f"WHERE campaign.id = {_id(campaign_id)} AND ad_group_ad.status != 'REMOVED'"
    )


def _asset_policy(campaign_id) -> str:
    return (
        "SELECT campaign_asset.resource_name, campaign_asset.asset, campaign_asset.field_type, "
        "campaign_asset.status, campaign_asset.primary_status, "
        "campaign_asset.primary_status_reasons, asset.policy_summary.approval_status "
        f"FROM campaign_asset WHERE campaign.id = {_id(campaign_id)}"
    )


def _subscriptions() -> str:
    return (
        "SELECT recommendation_subscription.type, recommendation_subscription.status "
        "FROM recommendation_subscription"
    )


def _client_link_status(customer_id: str) -> str:
    cid = globals()["customer_id"](customer_id)
    return (
        "SELECT customer_client_link.resource_name, customer_client_link.client_customer, "
        "customer_client_link.manager_link_id, customer_client_link.status, "
        "customer_client_link.hidden FROM customer_client_link "
        f"WHERE customer_client_link.client_customer = 'customers/{cid}'"
    )


def _shared_set_by_name(name: str) -> str:
    return (
        "SELECT shared_set.id, shared_set.resource_name, shared_set.name, shared_set.status "
        f"FROM shared_set WHERE shared_set.name = {_literal(name)} "
        "AND shared_set.status = 'ENABLED'"
    )


QUERIES = {
    "account": _account,
    "billing": _billing,
    "conversion_actions": _conversion_actions,
    "conversion_action_snippets": _conversion_action_snippets,
    "campaign_by_name": _campaign_by_name,
    "campaign_health": _campaign_health,
    "search_terms": _search_terms,
    "keywords": _keywords,
    "ad_policy": _ad_policy,
    "asset_policy": _asset_policy,
    "subscriptions": _subscriptions,
    "client_link_status": _client_link_status,
    "shared_set_by_name": _shared_set_by_name,
}
