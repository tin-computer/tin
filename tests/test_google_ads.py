"""Google Ads adapter and builders: exact request shapes, bounded bulk mutates, opaque failures,
credential scrubbing, transient-only retries and zero-cost usage receipts."""

from __future__ import annotations

import asyncio
import gzip
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from test_procedure_publication import run_fixture

from tin_lite import google_ads_requests as requests
from tin_lite.domain import EffectReceipt
from tin_lite.google_ads import GoogleAdsApi, GoogleAdsError, ManagerTokenSource, api_from_settings
from tin_lite.usage_capture import external_usage_scope

RUN_ID = "00000000-0000-4000-8000-0000000000bb"
CID = "7235744335"
MCC = "1002174488"
ACCESS = "ya29.synthetic-access-token"  # noqa: S105
REFRESH = "1//synthetic-refresh-token"  # noqa: S105


def plan(**overrides):
    value = {
        "marker": requests.marker(RUN_ID),
        "campaign_name": "Tin | Search | Core",
        "daily_budget_usd": 20,
        "cpc_ceiling_usd": 2.5,
        "markets": ["US", "GB"],
        "landing_page": "https://example.com/pricing",
        "start_date": "2026-09-23",
        "tracking_template": "{lpurl}?utm_source=google&utm_medium=cpc",
        "ad_groups": [
            {
                "name": "Core - exact",
                "keywords": [
                    {"text": "imessage api", "match_type": "exact"},
                    {"text": "imessage api for agents", "match_type": "phrase"},
                ],
                "headlines": ["iMessage API", "Send iMessage from code", "Start free"],
                "descriptions": ["Ship iMessage to your agent today.", "No card needed."],
                "path1": "api",
                "path2": "imessage",
            }
        ],
        "negatives": [{"text": "free", "match_type": "phrase"}],
        "sitelinks": [
            {
                "text": "Pricing",
                "description1": "Plans from $5",
                "description2": "No card needed",
                "url": "https://example.com/pricing",
            }
        ],
        "callouts": ["Free trial"],
    }
    value.update(overrides)
    return value


# ---------------------------------------------------------------- builders


def test_customer_id_normalises_dashes_and_refuses_other_shapes():
    assert requests.customer_id("723-574-4335") == CID
    assert requests.customer_id(" 7235744335 ") == CID
    for bad in ("12345", "abcdefghij", 7235744335, "72357443350"):
        with pytest.raises(ValueError):
            requests.customer_id(bad)


def test_micros_round_trip_and_marker_is_stable():
    assert requests.micros(2.5) == "2500000"
    assert requests.micros("0.05") == "50000"
    assert requests.usd("2500000") == 2.5
    assert requests.marker(RUN_ID) == requests.marker(RUN_ID)
    assert requests.marker(RUN_ID).startswith("[tin:") and len(requests.marker(RUN_ID)) == 18
    with pytest.raises(ValueError):
        requests.micros(True)
    with pytest.raises(ValueError):
        requests.micros("nan")


def test_campaign_bundle_has_exact_shapes_in_order():
    ops = requests.campaign_bundle(plan(), customer_id=CID)
    kinds = [next(iter(op)) for op in ops]
    assert kinds == [
        "campaignBudgetOperation",
        "campaignOperation",
        "campaignCriterionOperation",
        "campaignCriterionOperation",
        "sharedSetOperation",
        "sharedCriterionOperation",
        "campaignSharedSetOperation",
        "adGroupOperation",
        "adGroupCriterionOperation",
        "adGroupCriterionOperation",
        "adGroupAdOperation",
        "assetOperation",
        "assetOperation",
        "campaignAssetOperation",
        "campaignAssetOperation",
    ]
    budget = ops[0]["campaignBudgetOperation"]["create"]
    assert budget == {
        "resourceName": f"customers/{CID}/campaignBudgets/-1",
        "name": f"Tin | Search | Core budget {requests.marker(RUN_ID)}",
        "amountMicros": "20000000",
        "deliveryMethod": "STANDARD",
        "explicitlyShared": False,
    }
    campaign = ops[1]["campaignOperation"]["create"]
    assert campaign["resourceName"] == f"customers/{CID}/campaigns/-2"
    assert campaign["status"] == "PAUSED"
    assert campaign["advertisingChannelType"] == "SEARCH"
    assert campaign["campaignBudget"] == budget["resourceName"]
    assert campaign["targetSpend"] == {"cpcBidCeilingMicros": "2500000"}
    assert campaign["networkSettings"] == {
        "targetGoogleSearch": True,
        "targetSearchNetwork": False,
        "targetContentNetwork": False,
        "targetPartnerSearchNetwork": False,
    }
    assert campaign["geoTargetTypeSetting"] == {
        "positiveGeoTargetType": "PRESENCE",
        "negativeGeoTargetType": "PRESENCE",
    }
    assert campaign["aiMaxSetting"] == {"enableAiMax": False}
    assert [a["assetAutomationStatus"] for a in campaign["assetAutomationSettings"]] == [
        "OPTED_OUT",
        "OPTED_OUT",
    ]
    assert campaign["containsEuPoliticalAdvertising"] == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
    assert "startDate" not in campaign  # v25 has no such field; a campaign starts when enabled
    assert campaign["trackingUrlTemplate"].startswith("{lpurl}")
    assert "keywordMatchType" not in campaign
    assert "maximizeClicks" not in campaign
    assert ops[2]["campaignCriterionOperation"]["create"] == {
        "campaign": campaign["resourceName"],
        "location": {"geoTargetConstant": "geoTargetConstants/2840"},
    }
    assert ops[3]["campaignCriterionOperation"]["create"]["location"] == {
        "geoTargetConstant": "geoTargetConstants/2826"
    }
    assert not any(
        "language" in op.get("campaignCriterionOperation", {}).get("create", {}) for op in ops
    )
    shared = ops[4]["sharedSetOperation"]["create"]
    assert shared["type"] == "NEGATIVE_KEYWORDS" and shared["resourceName"].endswith("/-3")
    assert ops[5]["sharedCriterionOperation"]["create"] == {
        "sharedSet": shared["resourceName"],
        "keyword": {"text": "free", "matchType": "PHRASE"},
    }
    assert ops[6]["campaignSharedSetOperation"]["create"] == {
        "campaign": campaign["resourceName"],
        "sharedSet": shared["resourceName"],
    }
    group = ops[7]["adGroupOperation"]["create"]
    assert group == {
        "resourceName": f"customers/{CID}/adGroups/-4",
        "name": "Core - exact",
        "campaign": campaign["resourceName"],
        "status": "ENABLED",
        "type": "SEARCH_STANDARD",
    }
    assert ops[8]["adGroupCriterionOperation"]["create"] == {
        "adGroup": group["resourceName"],
        "status": "ENABLED",
        "keyword": {"text": "imessage api", "matchType": "EXACT"},
    }
    ad = ops[10]["adGroupAdOperation"]["create"]
    assert ad["adGroup"] == group["resourceName"] and ad["status"] == "ENABLED"
    assert ad["ad"]["finalUrls"] == ["https://example.com/pricing"]
    rsa = ad["ad"]["responsiveSearchAd"]
    assert [h["text"] for h in rsa["headlines"]] == [
        "iMessage API",
        "Send iMessage from code",
        "Start free",
    ]
    assert len(rsa["descriptions"]) == 2 and rsa["path1"] == "api" and rsa["path2"] == "imessage"
    assert all("pinnedField" not in h for h in rsa["headlines"])
    sitelink = ops[11]["assetOperation"]["create"]
    assert sitelink["finalUrls"] == ["https://example.com/pricing"]
    assert sitelink["sitelinkAsset"] == {
        "linkText": "Pricing",
        "description1": "Plans from $5",
        "description2": "No card needed",
    }
    assert ops[12]["assetOperation"]["create"]["calloutAsset"] == {"calloutText": "Free trial"}
    assert ops[13]["campaignAssetOperation"]["create"] == {
        "campaign": campaign["resourceName"],
        "asset": sitelink["resourceName"],
        "fieldType": "SITELINK",
    }
    assert ops[14]["campaignAssetOperation"]["create"]["fieldType"] == "CALLOUT"


def test_campaign_bundle_marks_an_already_marked_name_once():
    mark = requests.marker(RUN_ID)
    ops = requests.campaign_bundle(
        plan(campaign_name=f"Tin | Search | Core {mark}"), customer_id=CID
    )
    names = [next(iter(op.values()))["create"].get("name") for op in ops[:5]]
    assert names == [
        f"Tin | Search | Core budget {mark}",
        f"Tin | Search | Core {mark}",
        None,
        None,
        f"Tin | Search | Core negatives {mark}",
    ]
    assert requests.bundle_names(plan(campaign_name=f"Tin | Search | Core {mark}")) == (
        requests.bundle_names(plan())
    )


def test_temporary_ids_are_negative_unique_and_defined_before_use():
    ops = requests.campaign_bundle(plan(), customer_id=CID)
    defined: set[str] = set()
    for op in ops:
        create = next(iter(op.values()))["create"]
        for key, value in create.items():
            if key == "resourceName" or not isinstance(value, str):
                continue
            if value.startswith(f"customers/{CID}/") and value.rsplit("/", 1)[-1].startswith("-"):
                assert value in defined, value
        name = create.get("resourceName")
        if name:
            assert int(name.rsplit("/", 1)[-1]) < 0
            assert name not in defined
            defined.add(name)


@pytest.mark.parametrize(
    "override",
    [
        {
            "ad_groups": [
                {**plan()["ad_groups"][0], "keywords": [{"text": "x", "match_type": "BROAD"}]}
            ]
        },
        {"ad_groups": [{**plan()["ad_groups"][0], "headlines": ["a", "b"]}]},
        {"ad_groups": [{**plan()["ad_groups"][0], "headlines": ["x" * 31, "b", "c"]}]},
        {"ad_groups": [{**plan()["ad_groups"][0], "descriptions": ["only one"]}]},
        {"ad_groups": [{**plan()["ad_groups"][0], "path1": "has space"}]},
        {"ad_groups": [{**plan()["ad_groups"][0], "path2": "orphan", "path1": ""}]},
        {"ad_groups": []},
        {"markets": []},
        {"markets": ["XX"]},
        {"markets": ["US", "US"]},
        {"landing_page": "http://example.com"},
        {"daily_budget_usd": 0.5},
        {"cpc_ceiling_usd": 0},
        {"start_date": "23/09/2026"},
        {"tracking_template": "https://x?{lpurl}"},
        {"sitelinks": [{"text": "x" * 26, "url": "https://e.com"}]},
        {"sitelinks": [{"text": "A", "description1": "only one", "url": "https://e.com"}]},
        {"callouts": ["x" * 26]},
        {"negatives": [{"text": " ".join(["w"] * 11), "match_type": "phrase"}]},
        {"marker": ""},
    ],
)
def test_campaign_bundle_refuses_out_of_bound_plans(override):
    with pytest.raises(ValueError):
        requests.campaign_bundle(plan(**override), customer_id=CID)


def test_bulk_body_and_created_resources_round_trip():
    ops = requests.campaign_bundle(plan(), customer_id=CID)
    body = requests.bulk_body(ops, validate_only=True)
    assert body == {
        "mutateOperations": ops,
        "partialFailure": False,
        "validateOnly": True,
        "responseContentType": "MUTABLE_RESOURCE",
    }
    keys = {
        "campaignBudgetOperation": "campaignBudgetResult",
        "campaignOperation": "campaignResult",
        "campaignCriterionOperation": "campaignCriterionResult",
        "sharedSetOperation": "sharedSetResult",
        "sharedCriterionOperation": "sharedCriterionResult",
        "campaignSharedSetOperation": "campaignSharedSetResult",
        "adGroupOperation": "adGroupResult",
        "adGroupCriterionOperation": "adGroupCriterionResult",
        "adGroupAdOperation": "adGroupAdResult",
        "assetOperation": "assetResult",
        "campaignAssetOperation": "campaignAssetResult",
    }
    kinds = {
        "campaignBudgetOperation": "campaignBudgets/11",
        "campaignOperation": "campaigns/22",
        "campaignCriterionOperation": "campaignCriteria/22~2840",
        "sharedSetOperation": "sharedSets/33",
        "sharedCriterionOperation": "sharedCriteria/33~1",
        "campaignSharedSetOperation": "campaignSharedSets/22~33",
        "adGroupOperation": "adGroups/44",
        "adGroupCriterionOperation": "adGroupCriteria/44~5",
        "adGroupAdOperation": "adGroupAds/44~6",
        "assetOperation": "assets/77",
        "campaignAssetOperation": "campaignAssets/22~77~SITELINK",
    }
    responses = []
    for index, op in enumerate(ops):
        (kind,) = op
        name = f"customers/{CID}/{kinds[kind]}"
        # Alternate between the bare result and the MUTABLE_RESOURCE form.
        inner = (
            {"resourceName": name}
            if index % 2
            else {kind.removesuffix("Operation"): {"resourceName": name}}
        )
        responses.append({keys[kind]: inner})
    found = requests.created_resources(ops, responses)
    assert found["budget"] == f"customers/{CID}/campaignBudgets/11"
    assert found["campaign"] == f"customers/{CID}/campaigns/22"
    assert found["shared_set"] == f"customers/{CID}/sharedSets/33"
    assert found["ad_groups"] == {"Core - exact": f"customers/{CID}/adGroups/44"}
    assert len(found["keywords"]) == 2 and len(found["ads"]) == 1 and len(found["assets"]) == 2
    with pytest.raises(ValueError):
        requests.created_resources(ops, responses[:-1])
    with pytest.raises(ValueError):
        requests.created_resources(ops, [{"campaignResult": {}}] * len(ops))


def test_update_and_create_bodies():
    campaign = f"customers/{CID}/campaigns/22"
    assert requests.campaign_status_body(campaign, "ENABLED") == (
        "campaigns",
        {
            "operations": [
                {"update": {"resourceName": campaign, "status": "ENABLED"}, "updateMask": "status"}
            ],
            "partialFailure": False,
            "validateOnly": False,
        },
    )
    segment, body = requests.budget_body(f"customers/{CID}/campaignBudgets/11", 24)
    assert segment == "campaignBudgets"
    assert body["operations"][0] == {
        "update": {
            "resourceName": f"customers/{CID}/campaignBudgets/11",
            "amountMicros": "24000000",
        },
        "updateMask": "amount_micros",
    }
    _, body = requests.bidding_body(campaign, "maximize_conversions")
    assert body["operations"][0] == {
        "update": {"resourceName": campaign, "maximizeConversions": {}},
        "updateMask": "maximize_conversions.target_cpa_micros",
    }
    _, body = requests.bidding_body(campaign, "target_cpa", 40)
    assert body["operations"][0] == {
        "update": {
            "resourceName": campaign,
            "maximizeConversions": {"targetCpaMicros": "40000000"},
        },
        "updateMask": "maximize_conversions.target_cpa_micros",
    }
    with pytest.raises(ValueError):
        requests.bidding_body(campaign, "target_cpa")
    with pytest.raises(ValueError):
        requests.bidding_body(campaign, "manual_cpc")
    with pytest.raises(ValueError):
        requests.campaign_status_body(f"customers/{CID}/adGroups/1", "ENABLED")
    segment, body = requests.negatives_body(
        f"customers/{CID}/sharedSets/33", [{"text": "jobs", "match_type": "exact"}]
    )
    assert segment == "sharedCriteria"
    assert body["operations"] == [
        {
            "create": {
                "sharedSet": f"customers/{CID}/sharedSets/33",
                "keyword": {"text": "jobs", "matchType": "EXACT"},
            }
        }
    ]
    segment, body = requests.pause_ads_body([f"customers/{CID}/adGroupAds/44~6"])
    assert segment == "adGroupAds" and body["operations"][0]["update"]["status"] == "PAUSED"
    segment, body = requests.pause_keywords_body([f"customers/{CID}/adGroupCriteria/44~5"])
    assert segment == "adGroupCriteria" and body["operations"][0]["updateMask"] == "status"
    segment, body = requests.subscriptions_body(["KEYWORD", "USE_BROAD_MATCH_KEYWORD"])
    assert segment == "recommendationSubscriptions:mutateRecommendationSubscription"
    assert body == {
        "operations": [
            {"create": {"type": "KEYWORD", "status": "PAUSED"}},
            {"create": {"type": "USE_BROAD_MATCH_KEYWORD", "status": "PAUSED"}},
        ],
        "partialFailure": True,
    }
    with pytest.raises(ValueError):
        requests.subscriptions_body(["NOT_A_TYPE"])
    segment, body = requests.conversion_action_body(
        "Tin signup [tin:abc]", "SIGNUP", "ONE_PER_CLICK", 50, "USD"
    )
    assert segment == "conversionActions"
    create = body["operations"][0]["create"]
    assert create["type"] == "WEBPAGE" and create["category"] == "SIGNUP"
    assert create["status"] == "ENABLED" and create["countingType"] == "ONE_PER_CLICK"
    assert create["primaryForGoal"] is True and create["includeInConversionsMetric"] is True
    assert create["valueSettings"] == {
        "defaultValue": 50.0,
        "defaultCurrencyCode": "USD",
        "alwaysUseDefaultValue": True,
    }
    assert create["attributionModelSettings"] == {
        "attributionModel": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"
    }
    assert create["clickThroughLookbackWindowDays"] == 30
    with pytest.raises(ValueError):
        requests.conversion_action_body("x", "MYSTERY", "ONE_PER_CLICK", 1, "USD")
    assert requests.client_link_body("723-574-4335") == {
        "operation": {"create": {"clientCustomer": f"customers/{CID}", "status": "PENDING"}}
    }


def test_queries_escape_literals_and_check_ids():
    q = requests.QUERIES
    assert q["campaign_by_name"]("O'Reilly \\ Co") == (
        "SELECT campaign.id, campaign.resource_name, campaign.name, campaign.status, "
        "campaign.campaign_budget FROM campaign WHERE campaign.name = 'O\\'Reilly \\\\ Co' "
        "AND campaign.status != 'REMOVED'"
    )
    assert "LAST_7_DAYS" in q["conversion_actions"](7)
    assert "LAST_30_DAYS" in q["conversion_actions"](30)
    assert "WHERE campaign.id = 123 AND segments.date DURING LAST_7_DAYS" in q["campaign_health"](
        123, 7
    )
    assert q["search_terms"]("55", 7).endswith("ORDER BY metrics.cost_micros DESC LIMIT 500")
    assert "quality_info.quality_score" in q["keywords"](55, 30)
    assert "policy_topic_entries" in q["ad_policy"](55)
    assert "asset.policy_summary.approval_status" in q["asset_policy"](55)
    assert q["subscriptions"]().startswith("SELECT recommendation_subscription.type")
    assert q["client_link_status"]("723-574-4335").endswith(f"= 'customers/{CID}'")
    assert "billing_setup.status" in q["billing"]()
    assert "conversion_tracking_status" in q["account"]()
    assert "tag_snippets" in q["conversion_action_snippets"](9)
    assert "shared_set.name = 'x'" in q["shared_set_by_name"]("x")
    for bad in ("1 OR 1", "", "abc"):
        with pytest.raises(ValueError):
            q["campaign_health"](bad, 7)
    with pytest.raises(ValueError):
        q["conversion_actions"](3)
    with pytest.raises(ValueError):
        q["campaign_by_name"]("bad\nname")


# ---------------------------------------------------------------- adapter


async def token_source():
    return ACCESS


def api(handler, *, sleep=None, developer_token=None, tokens=token_source):
    slept = []

    async def record_sleep(seconds):
        slept.append(seconds)

    client = GoogleAdsApi(
        manager_customer_id="100-217-4488",
        token_source=tokens,
        developer_token=developer_token,
        transport=httpx.MockTransport(handler),
        sleep=sleep or record_sleep,
    )
    client.slept = slept
    return client


def error_body(category, value, message="secret detail"):
    return {
        "error": {
            "code": 400,
            "message": message,
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                    "errors": [{"errorCode": {category: value}, "message": message}],
                    "requestId": "req-1",
                }
            ],
        }
    }


async def test_search_paginates_and_sends_manager_headers():
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        if body.get("pageToken") == "p2":
            return httpx.Response(
                200, json={"results": [{"campaign": {"id": "2"}}]}, headers={"request-id": "r2"}
            )
        return httpx.Response(
            200,
            json={"results": [{"campaign": {"id": "1"}}], "nextPageToken": "p2"},
            headers={"request-id": "r1"},
        )

    client = api(handler, developer_token="dev-token")  # noqa: S106
    result = await client.search(CID, requests.QUERIES["subscriptions"]())
    assert result == {
        "rows": [{"campaign": {"id": "1"}}, {"campaign": {"id": "2"}}],
        "provider_request_id": "r2",
    }
    assert len(seen) == 2
    first = seen[0]
    assert (
        str(first.url) == f"https://googleads.googleapis.com/v25/customers/{CID}/googleAds:search"
    )
    assert first.headers["authorization"] == f"Bearer {ACCESS}"
    assert first.headers["login-customer-id"] == MCC
    assert first.headers["developer-token"] == "dev-token"
    assert first.headers["accept-encoding"] == "identity"
    assert json.loads(first.content) == {"query": requests.QUERIES["subscriptions"]()}
    assert "pageToken" not in json.loads(first.content)
    assert json.loads(seen[1].content)["pageToken"] == "p2"
    with pytest.raises(ValueError):
        await client.search(CID, "DELETE everything")


async def test_mutate_validate_only_and_real_results():
    ops = requests.campaign_bundle(plan(), customer_id=CID)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body["validateOnly"]:
            return httpx.Response(200, json={}, headers={"request-id": "v"})
        responses = [{"campaignResult": {"resourceName": f"customers/{CID}/campaigns/1"}}] * len(
            ops
        )
        return httpx.Response(
            200, json={"mutateOperationResponses": responses}, headers={"request-id": "m"}
        )

    client = api(handler)
    dry = await client.mutate(CID, ops, validate_only=True)
    assert dry == {"results": [], "provider_request_id": "v"}
    assert bodies[0]["validateOnly"] is True and bodies[0]["partialFailure"] is False
    assert bodies[0]["responseContentType"] == "MUTABLE_RESOURCE"
    real = await client.mutate(CID, ops)
    assert len(real["results"]) == len(ops) and real["provider_request_id"] == "m"

    def short(request):
        return httpx.Response(200, json={"mutateOperationResponses": [{}]})

    with pytest.raises(GoogleAdsError) as info:
        await api(short).mutate(CID, ops)
    assert info.value.code == "envelope"


async def test_mutate_resource_paths_and_allowlist():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200, json={"results": [{"resourceName": f"customers/{CID}/campaigns/1"}]}
        )

    client = api(handler)
    segment, body = requests.campaign_status_body(f"customers/{CID}/campaigns/1", "ENABLED")
    result = await client.mutate_resource(CID, segment, body)
    assert result["results"][0]["resourceName"].endswith("/campaigns/1")
    assert seen[-1].endswith(f"/customers/{CID}/campaigns:mutate")
    segment, body = requests.subscriptions_body(["KEYWORD"])
    await client.mutate_resource(CID, segment, body)
    assert seen[-1].endswith(
        f"/customers/{CID}/recommendationSubscriptions:mutateRecommendationSubscription"
    )
    with pytest.raises(ValueError):
        await client.mutate_resource(CID, "customers", {"operations": []})
    with pytest.raises(ValueError):
        await client.mutate_resource(CID, "campaigns", {"nope": 1})


async def test_client_link_runs_on_the_manager():
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(
            200,
            json={"result": {"resourceName": f"customers/{MCC}/customerClientLinks/{CID}~99"}},
            headers={"request-id": "link"},
        )

    client = api(handler)
    result = await client.client_link("723-574-4335")
    assert result == {
        "resource_name": f"customers/{MCC}/customerClientLinks/{CID}~99",
        "provider_request_id": "link",
    }
    url, body = seen[0]
    assert url.endswith(f"/customers/{MCC}/customerClientLinks:mutate")
    assert body == {
        "operation": {"create": {"clientCustomer": f"customers/{CID}", "status": "PENDING"}}
    }


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            lambda: httpx.Response(
                401, json=error_body("authenticationError", "OAUTH_TOKEN_INVALID")
            ),
            "AuthenticationError.OAUTH_TOKEN_INVALID",
        ),
        (
            lambda: httpx.Response(
                403, json=error_body("authorizationError", "USER_PERMISSION_DENIED")
            ),
            "AuthorizationError.USER_PERMISSION_DENIED",
        ),
        (
            lambda: httpx.Response(400, json=error_body("policyFindingError", "POLICY_FINDING")),
            "PolicyFindingError.POLICY_FINDING",
        ),
        (
            lambda: httpx.Response(
                400, json=error_body("managerLinkError", "ALREADY_MANAGED_BY_THIS_MANAGER")
            ),
            "ManagerLinkError.ALREADY_MANAGED_BY_THIS_MANAGER",
        ),
        (
            lambda: httpx.Response(429, json=error_body("quotaError", "RESOURCE_EXHAUSTED")),
            "QuotaError.RESOURCE_EXHAUSTED",
        ),
        (lambda: httpx.Response(401, text="not json"), "http_401"),
        (lambda: httpx.Response(302, headers={"location": "https://evil.example/"}), "transport"),
        (
            lambda: httpx.Response(
                200, content=gzip.compress(b"{}"), headers={"content-encoding": "gzip"}
            ),
            "envelope",
        ),
        (
            lambda: httpx.Response(200, content=b'{"results": [' + b'{"a":1},' * 700_000 + b"{}]}"),
            "envelope",
        ),
        (lambda: httpx.Response(200, json={"results": [{"echo": ACCESS}]}), "envelope"),
        (lambda: httpx.Response(200, json={"results": "nope"}), "envelope"),
        (lambda: httpx.Response(200, text="[]"), "envelope"),
    ],
)
async def test_failures_are_opaque_and_carry_only_a_code(response, code):
    def handler(request):
        return response()

    client = api(handler)
    with pytest.raises(GoogleAdsError) as info:
        await client.search(CID, requests.QUERIES["subscriptions"]())
    assert info.value.code == code
    assert "secret detail" not in str(info.value)
    assert ACCESS not in str(info.value)
    assert client.slept == []


async def test_transient_errors_retry_with_backoff_and_daily_quota_does_not():
    calls = {"count": 0}

    def flaky(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, text="upstream")
        if calls["count"] == 2:
            return httpx.Response(500, json=error_body("internalError", "TRANSIENT_ERROR"))
        return httpx.Response(200, json={"results": []})

    client = api(flaky)
    assert await client.search(CID, requests.QUERIES["subscriptions"]()) == {
        "rows": [],
        "provider_request_id": None,
    }
    assert calls["count"] == 3 and client.slept == [5, 10]

    def exhausted(request):
        calls["count"] += 1
        return httpx.Response(429, json=error_body("quotaError", "RESOURCE_EXHAUSTED"))

    calls["count"] = 0
    client = api(exhausted)
    with pytest.raises(GoogleAdsError) as info:
        await client.search(CID, requests.QUERIES["subscriptions"]())
    assert info.value.code == "QuotaError.RESOURCE_EXHAUSTED" and calls["count"] == 1
    assert client.slept == []

    def always(request):
        calls["count"] += 1
        return httpx.Response(503, text="down")

    calls["count"] = 0
    client = api(always)
    with pytest.raises(GoogleAdsError) as info:
        await client.search(CID, requests.QUERIES["subscriptions"]())
    assert info.value.code == "http_503" and calls["count"] == 4 and client.slept == [5, 10, 20]


async def test_transport_and_timeout_failures():
    def connect(request):
        raise httpx.ConnectError("secret detail")

    with pytest.raises(GoogleAdsError) as info:
        await api(connect).search(CID, requests.QUERIES["subscriptions"]())
    assert info.value.code == "transport"

    def timeout(request):
        raise httpx.ReadTimeout("secret detail")

    with pytest.raises(GoogleAdsError) as info:
        await api(timeout).search(CID, requests.QUERIES["subscriptions"]())
    assert info.value.code == "timeout"


async def test_manager_token_source_caches_and_never_echoes():
    calls = []

    def handler(request):
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600})

    source = ManagerTokenSource(
        "client-id", "client-secret", REFRESH, transport=httpx.MockTransport(handler)
    )
    assert await source() == ACCESS
    assert await source() == ACCESS
    assert len(calls) == 1
    assert calls[0] == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": REFRESH,
        "grant_type": "refresh_token",
    }
    assert REFRESH in source.secrets and ACCESS in source.secrets

    def echo(request):
        return httpx.Response(200, json={"results": [{"note": REFRESH}]})

    client = api(echo, tokens=source)
    with pytest.raises(GoogleAdsError) as info:
        await client.search(CID, requests.QUERIES["subscriptions"]())
    assert info.value.code == "envelope"

    def refused(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    broken = ManagerTokenSource("a", "b", "c", transport=httpx.MockTransport(refused))
    with pytest.raises(GoogleAdsError) as info:
        await api(refused, tokens=broken).search(CID, requests.QUERIES["subscriptions"]())
    assert info.value.code == "token_refresh"


def test_api_from_settings_requires_the_manager_pair_and_oauth_client():
    base = {
        "google_ads_manager_customer_id": "1002174488",
        "google_ads_manager_refresh_token": SecretStr(REFRESH),
        "google_ads_developer_token": None,
        "google_ads_api_version": "v25",
        "google_oauth_client_id": "client-id",
        "google_oauth_client_secret": SecretStr("client-secret"),
    }
    client = api_from_settings(SimpleNamespace(**base))
    assert isinstance(client, GoogleAdsApi) and client.manager_customer_id == MCC
    for missing in (
        "google_ads_manager_customer_id",
        "google_ads_manager_refresh_token",
        "google_oauth_client_id",
    ):
        assert api_from_settings(SimpleNamespace(**{**base, missing: None})) is None
    with pytest.raises(ValueError):
        api_from_settings(SimpleNamespace(**{**base, "google_ads_api_version": "latest"}))
    with pytest.raises(ValueError):
        api_from_settings(SimpleNamespace(**{**base, "google_ads_manager_customer_id": "123"}))


# ---------------------------------------------------------------- receipts


class MemoryDB:
    def __init__(self):
        self.run = run_fixture()
        self.effects = {}
        self.locks = {}

    async def get_effect(self, key, **kwargs):
        return self.effects.get(key)

    async def fetchval(self, *args):
        return None

    @asynccontextmanager
    async def effect_lock(self, key, operation, **kwargs):
        async with self.locks.setdefault(key, asyncio.Lock()):
            yield self, self.effects.get(key)

    async def start_effect(self, conn, *, execution_key, operation):
        self.effects.setdefault(
            execution_key, EffectReceipt(execution_key, operation, "started", {})
        )

    async def save_effect_progress(self, conn, *, execution_key, result):
        self.effects[execution_key] = replace(self.effects[execution_key], result=result)

    async def complete_effect(self, conn, *, execution_key, result):
        self.effects[execution_key] = replace(
            self.effects[execution_key], status="completed", result=result
        )


async def test_calls_leave_zero_cost_receipts_inside_a_usage_scope():
    db = MemoryDB()

    def handler(request):
        return httpx.Response(200, json={"results": []}, headers={"request-id": "r"})

    client = api(handler)
    with external_usage_scope(db, None, db.run.id, "read:subscriptions"):
        await client.search(CID, requests.QUERIES["subscriptions"]())
    (receipt,) = db.effects.values()
    assert receipt.status == "completed" and receipt.operation == "external_usage_v1"
    assert receipt.result["provider"] == "google_ads" and receipt.result["category"] == "tool"
    assert receipt.result["endpoint"] == "googleAds:search"
    assert receipt.result["reported_cost_usd"] == "0" and receipt.result["usage"] == {"requests": 1}
    assert ACCESS not in json.dumps(receipt.result)
    with (
        pytest.raises(RuntimeError),
        external_usage_scope(db, None, db.run.id, "read:subscriptions"),
    ):
        await client.search(CID, requests.QUERIES["subscriptions"]())


async def test_failed_call_keeps_bounded_transport_facts():
    db = MemoryDB()

    def handler(request):
        return httpx.Response(403, json=error_body("authorizationError", "USER_PERMISSION_DENIED"))

    client = api(handler)
    with pytest.raises(GoogleAdsError), external_usage_scope(db, None, db.run.id, "read:account"):
        await client.search(CID, requests.QUERIES["account"]())
    (receipt,) = db.effects.values()
    assert receipt.status == "started" and receipt.result["outcome"] == "unconfirmed"
    assert receipt.result["failure"] == {
        **receipt.result["failure"],
        "kind": "http",
        "status_code": 403,
    }
    assert "secret detail" not in json.dumps(receipt.result)


async def test_no_usage_scope_means_no_receipt():
    def handler(request):
        return httpx.Response(200, json={"results": []})

    client = api(handler)
    assert await client.search(CID, requests.QUERIES["subscriptions"]()) == {
        "rows": [],
        "provider_request_id": None,
    }


def test_campaign_bundle_creates_a_held_group_paused():
    held = plan()
    held["ad_groups"][0]["status"] = "PAUSED"
    ops = requests.campaign_bundle(held, customer_id=CID)
    groups = [op["adGroupOperation"]["create"] for op in ops if "adGroupOperation" in op]
    assert groups[0]["status"] == "PAUSED" and all(g["status"] == "ENABLED" for g in groups[1:])
    held["ad_groups"][0]["status"] = "REMOVED"
    with pytest.raises(ValueError):
        requests.campaign_bundle(held, customer_id=CID)


def test_api_from_settings_prefers_the_dedicated_manager_oauth_client():
    from types import SimpleNamespace

    from pydantic import SecretStr

    from tin_lite import google_ads

    base = dict(
        google_ads_manager_customer_id="1002174488",
        google_ads_manager_refresh_token=SecretStr("refresh"),
        google_ads_developer_token=None,
        google_ads_api_version="v25",
        google_oauth_client_id="tin-client",
        google_oauth_client_secret=SecretStr("tin-secret"),
    )
    assert google_ads.manager_oauth_client(SimpleNamespace(**base))[0] == "tin-client"
    dedicated = SimpleNamespace(
        **base,
        google_ads_oauth_client_id="ads-client",
        google_ads_oauth_client_secret=SecretStr("ads-secret"),
    )
    assert google_ads.manager_oauth_client(dedicated)[0] == "ads-client"
    assert google_ads.api_from_settings(dedicated) is not None
    assert google_ads.api_from_settings(SimpleNamespace(google_oauth_client_id="x")) is None
