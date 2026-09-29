"""Google Ads monitor activities against the in-memory receipt fake: every read is receipted,
negatives and disapproved ads are applied through bounded mutate bodies, one proposal is saved
for the founder, the report publishes once, and a rerun makes no new provider or model calls."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError
from test_organic_audit import MemoryDB
from test_procedure_publication import HistoryStorage, run_fixture

from tin_lite import paid_ads_launch, paid_ads_monitor
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.domain import EffectReceipt, RunStatus
from tin_lite.model_providers import ModelResult, ModelUsage, ProviderName
from tin_lite.organic_audit import canonical_json, digest
from tin_lite.paid_ads_monitor_activities import (
    MODEL_TIMEOUT_SECONDS,
    READS,
    PaidAdsMonitorActivities,
)

CUSTOMER = "1234567890"
CAMPAIGN_ID = "24143450789"
CAMPAIGN_RN = f"customers/{CUSTOMER}/campaigns/{CAMPAIGN_ID}"
BUDGET_RN = f"customers/{CUSTOMER}/campaignBudgets/555"
SHARED_SET_RN = f"customers/{CUSTOMER}/sharedSets/777"
DISAPPROVED_AD = f"customers/{CUSTOMER}/adGroupAds/1~11"
APPROVED_AD = f"customers/{CUSTOMER}/adGroupAds/1~12"
WASTE = "free imessage api tutorial"
GOOD = "imessage api"
RIVAL = "sendblue api"


def campaign_json(launch_id: str, enabled_at: str) -> dict:
    return {
        "schema_version": 1,
        "launch_run_id": launch_id,
        "assessment_run_id": str(uuid4()),
        "marker": "[tin:abcdef123456]",
        "customer_id": CUSTOMER,
        "campaign_name": "Tin Search · Claw Messenger [tin:abcdef123456]",
        "resources": {"campaign": CAMPAIGN_RN, "budget": BUDGET_RN, "shared_set": SHARED_SET_RN},
        "campaign_id": CAMPAIGN_ID,
        "budget_id": "555",
        "shared_set_id": "777",
        "daily_budget_usd": 20.0,
        "cpc_ceiling_usd": 2.0,
        "markets": ["US"],
        "landing_page": "https://www.clawmessenger.com/",
        "conversion_action": None,
        "allowable_cpa_usd": 30.0,
        "target_cpa_usd": 25.0,
        "enabled": True,
        "enabled_at": enabled_at,
        "created_at": enabled_at,
        "paused_subscriptions": ["KEYWORD"],
        "ad_groups": [{"name": "Core", "match_type": "exact", "keywords": [GOOD]}],
        "negatives": [{"text": "free", "match_type": "PHRASE"}],
        "review": {"summary": "pending"},
    }


def metrics(*, clicks, cost_usd, conversions, impressions=1000, lost=None):
    value = {
        "impressions": str(impressions),
        "clicks": str(clicks),
        "costMicros": str(int(cost_usd * 1_000_000)),
        "conversions": conversions,
    }
    if lost is not None:
        value["searchBudgetLostImpressionShare"] = lost
    return value


def rows_for(query: str, *, reasons) -> list[dict]:
    campaign = {
        "id": CAMPAIGN_ID,
        "name": "Tin Search · Claw Messenger [tin:abcdef123456]",
        "status": "ENABLED",
        "primaryStatus": "LIMITED" if reasons else "ELIGIBLE",
        "primaryStatusReasons": reasons,
        "biddingStrategyType": "TARGET_SPEND",
        "targetSpend": {"cpcBidCeilingMicros": "2000000"},
        "aiMaxSetting": {"enableAiMax": False},
    }
    budget = {"resourceName": BUDGET_RN, "amountMicros": "20000000"}
    if "FROM campaign WHERE" in query:
        if "LAST_7_DAYS" in query:
            window = metrics(clicks=40, cost_usd=60, conversions=2, lost=0.35)
        elif "LAST_14_DAYS" in query:
            window = metrics(clicks=80, cost_usd=100, conversions=5)
        else:
            window = metrics(clicks=160, cost_usd=200, conversions=8)
        return [{"campaign": campaign, "campaignBudget": budget, "metrics": window}]
    if "FROM search_term_view" in query:
        return [
            {
                "searchTermView": {"searchTerm": WASTE, "status": "NONE"},
                "segments": {"keyword": {"info": {"text": GOOD, "matchType": "PHRASE"}}},
                "adGroup": {"id": "1"},
                "metrics": metrics(clicks=3, cost_usd=12, conversions=0),
            },
            {
                "searchTermView": {"searchTerm": GOOD, "status": "ADDED"},
                "segments": {"keyword": {"info": {"text": GOOD, "matchType": "EXACT"}}},
                "adGroup": {"id": "1"},
                "metrics": metrics(clicks=25, cost_usd=40, conversions=2),
            },
            {
                "searchTermView": {"searchTerm": RIVAL, "status": "NONE"},
                "segments": {"keyword": {"info": {"text": GOOD, "matchType": "PHRASE"}}},
                "adGroup": {"id": "1"},
                "metrics": metrics(clicks=1, cost_usd=4, conversions=0),
            },
        ]
    if "FROM keyword_view" in query:
        return [
            {
                "adGroupCriterion": {
                    "resourceName": f"customers/{CUSTOMER}/adGroupCriteria/1~900",
                    "criterionId": "900",
                    "keyword": {"text": GOOD, "matchType": "EXACT"},
                    "status": "ENABLED",
                    "qualityInfo": {"qualityScore": 7},
                },
                "metrics": metrics(clicks=160, cost_usd=200, conversions=8),
            }
        ]
    if "FROM ad_group_ad" in query:
        return [
            {
                "adGroupAd": {
                    "resourceName": DISAPPROVED_AD,
                    "ad": {"id": "11"},
                    "status": "ENABLED",
                    "adStrength": "GOOD",
                    "policySummary": {
                        "approvalStatus": "DISAPPROVED",
                        "reviewStatus": "REVIEWED",
                        "policyTopicEntries": [{"topic": "TRADEMARKS"}],
                    },
                    "primaryStatusReasons": ["AD_GROUP_AD_DISAPPROVED"],
                }
            },
            {
                "adGroupAd": {
                    "resourceName": APPROVED_AD,
                    "ad": {"id": "12"},
                    "status": "ENABLED",
                    "adStrength": "EXCELLENT",
                    "policySummary": {"approvalStatus": "APPROVED", "reviewStatus": "REVIEWED"},
                }
            },
        ]
    if "FROM campaign_asset" in query:
        return []
    if "FROM conversion_action" in query:
        return [
            {
                "conversionAction": {"name": "Signup", "category": "SIGNUP"},
                "metrics": {"allConversions": 8},
            }
        ]
    if "FROM recommendation_subscription" in query:
        return [
            {"recommendationSubscription": {"type": "USE_BROAD_MATCH_KEYWORD", "status": "ENABLED"}}
        ]
    raise AssertionError(f"unexpected query {query[:80]}")


class FakeIntegrations:
    def __init__(self, *, reasons=("BUDGET_CONSTRAINED",), customer=CUSTOMER, fail_negatives=False):
        self.reasons = list(reasons)
        self.customer = customer
        self.fail_negatives = fail_negatives
        self.searches: list[str] = []
        self.mutations: list[tuple[str, dict]] = []
        self.google_ads_link_status = AsyncMock()

    def is_configured(self, provider_key):
        return provider_key == "ads.google"

    async def google_ads_account(self, *, project_id):
        return self.customer

    async def google_ads_call(
        self, *, project_id, kind, request, execution_key, run_id, expected_customer_id
    ):
        assert expected_customer_id == self.customer
        if kind == "search":
            self.searches.append(request["query"])
            return {
                "rows": rows_for(request["query"], reasons=self.reasons),
                "provider_request_id": "req",
            }
        assert kind == "mutate_resource"
        if self.fail_negatives and request["segment"] == "sharedCriteria":
            raise ConnectionError("lost")
        self.mutations.append((request["segment"], request["body"]))
        return {
            "results": [{"resourceName": "x"} for _ in request["body"]["operations"]],
            "provider_request_id": "req",
        }


class MonitorDB(MemoryDB):
    def __init__(self, *, enabled_days_ago=10, campaign_status="live", open_proposal=False):
        super().__init__()
        self.launch_id = uuid4()
        self.enabled_at = datetime.now(UTC) - timedelta(days=enabled_days_ago)
        self.run = replace(
            self.run,
            executor=paid_ads_monitor.KEY,
            created_at=datetime.now(UTC),
            input={
                "project_id": str(self.project.id),
                "launch_run_id": str(self.launch_id),
                "auto_negatives_per_run": 20,
                "notes": "",
                "max_cost_usd": 2,
            },
        )
        self.launch_run = replace(
            run_fixture(),
            id=self.launch_id,
            project_id=self.project.id,
            executor=paid_ads_launch.KEY,
            status=RunStatus.SUCCEEDED,
            canonical_commit_sha="c" * 40,
        )
        self.campaign_row = {
            "run_id": self.launch_id,
            "project_id": self.project.id,
            "customer_id": CUSTOMER,
            "status": campaign_status,
            "external_campaign_id": CAMPAIGN_ID,
            "external_budget_id": "555",
            "external_shared_set_id": "777",
            "enabled_at": self.enabled_at,
        }
        self.proposals: dict[UUID, dict] = {}
        self.finalized: list[dict] = []
        if open_proposal:
            self.proposals[uuid4()] = {
                "campaign_run_id": self.launch_id,
                "project_id": self.project.id,
                "request_id": uuid4(),
                "status": "pending",
                "proposal_number": 1,
                "kind": "bid_strategy_change",
            }
        names = paid_ads_launch.paths(str(self.launch_id), paid_ads_launch.RESULT_DOCS)
        self.documents = {
            names["RESULT.md"]: "# Your Google Ads campaign\n",
            names["campaign.json"]: json.dumps(
                campaign_json(str(self.launch_id), self.enabled_at.isoformat())
            ),
        }
        self.effects[f"{paid_ads_launch.PREFIX}:{self.launch_id}:publish"] = EffectReceipt(
            f"{paid_ads_launch.PREFIX}:{self.launch_id}:publish",
            paid_ads_launch.KEY,
            "completed",
            {"canonical_commit_sha": "c" * 40, "documents_sha256": digest(self.documents)},
        )

    async def get_run(self, run_id, **kwargs):
        if run_id == self.launch_id:
            return self.launch_run
        return await super().get_run(run_id, **kwargs)

    async def get_paid_ads_campaign(self, run_id):
        return dict(self.campaign_row) if run_id == self.launch_id else None

    async def begin_paid_ads_proposal(self, **values):
        for row in self.proposals.values():
            if row["request_id"] == values["request_id"]:
                return dict(row)
        if any(
            row["campaign_run_id"] == values["campaign_run_id"]
            and row["status"] in {"pending", "approved"}
            for row in self.proposals.values()
        ):
            raise RuntimeError("the campaign already has a proposal awaiting you")
        number = len(self.proposals) + 1
        row = {
            **values,
            "id": values["proposal_id"],
            "status": "pending",
            "proposal_number": number,
        }
        self.proposals[values["proposal_id"]] = row
        return dict(row)

    async def finalize_paid_ads_proposal(self, **values):
        self.finalized.append(values)
        return dict(self.proposals[values["proposal_id"]])


class MonitorModel:
    def __init__(self):
        self.calls = []

    async def generate(self, route, request, *, timeout_seconds=None):
        # The client waits as long as the step's budget, not its 90-second default.
        assert timeout_seconds == MODEL_TIMEOUT_SECONDS
        name = request.output_schema_name.removeprefix("paid_ads_monitor_")
        step = name.removesuffix("_retry")
        self.calls.append(name)
        if step == "classify":
            terms = request.output_schema["properties"]["labels"]["items"]["properties"]["term"][
                "enum"
            ]
            labels = {WASTE: "job_or_free", GOOD: "relevant", RIVAL: "competitor"}
            parsed = {"labels": [{"term": t, "label": labels.get(t, "unsure")} for t in terms]}
        else:
            parsed = {
                "summary": "Tin removed one wasted search and paused a disapproved ad today.",
                "changes_explained": ["The free-tutorial search brought no customers."],
                "watch_for": ["Ad review of the paused ad."],
            }
        assert route == paid_ads_monitor.route_for(step).key
        return ModelResult(
            provider=ProviderName.OPENAI,
            model="synthetic",
            text="",
            parsed=parsed,
            request_id=f"request-{name}",
            usage=ModelUsage(10, 5),
        )


def fixture(*, db=None, integrations=None):
    db = db or MonitorDB()
    storage = HistoryStorage()
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == paid_ads_monitor.KEY)
    definition = canonical_json(spec.definition)

    async def read(*, repo_id, commit_sha, path):
        if path.startswith("workflows/"):
            return definition
        return db.documents[path].encode()

    storage.read_canonical_artifact = AsyncMock(side_effect=read)
    storage.publish_state_documents = AsyncMock(return_value=("e" * 40, True))

    async def project(conn, *, execution_key, **_kwargs):
        await db.complete_effect(conn, execution_key=execution_key, result={"projected": True})

    db._complete_readonly_report_projection = AsyncMock(side_effect=project)
    model = MonitorModel()
    integrations = integrations or FakeIntegrations()
    activities = PaidAdsMonitorActivities(
        database=db,
        storage=storage,
        settings=SimpleNamespace(luna_api_key="fixture"),
        router=SimpleNamespace(generate=AsyncMock(side_effect=model.generate)),
        integrations=integrations,
    )
    return activities, db, storage, integrations, model


STEPS = ("prepare", "read", "decide", "apply", "propose", "publish")


async def finish(activities, run_id):
    for step in STEPS:
        await getattr(activities, step)(run_id)


@pytest.mark.asyncio
async def test_full_path_applies_bounded_changes_saves_one_proposal_and_publishes_once():
    activities, db, storage, integrations, model = fixture()
    run_id = str(db.run.id)
    await finish(activities, run_id)

    assert len(integrations.searches) == 9
    for name, _query, _days in READS:
        receipt = db.effects[activities.key(run_id, f"read:{name}")]
        assert receipt.status == "completed" and receipt.result["status"] == "completed"

    segments = [segment for segment, _ in integrations.mutations]
    assert segments == ["sharedCriteria", "adGroupAds"]
    negatives = integrations.mutations[0][1]["operations"]
    assert [op["create"]["keyword"]["text"] for op in negatives] == [WASTE]
    assert all(op["create"]["sharedSet"] == SHARED_SET_RN for op in negatives)
    paused = integrations.mutations[1][1]["operations"]
    assert [op["update"]["resourceName"] for op in paused] == [DISAPPROVED_AD]
    assert all(op["update"]["status"] == "PAUSED" for op in paused)

    decision = db.effects[activities.key(run_id, "decision")].result["decision"]
    assert not decision["quiet"] and decision["auto"]["pause_keywords"] == []
    assert [p["kind"] for p in decision["proposals"]] == ["budget_change"]
    assert decision["proposals"][0]["proposed"]["daily_budget_usd"] == 24.0
    assert [row["kind"] for row in db.proposals.values()] == ["budget_change"]
    storage.publish_state_documents.assert_awaited_once()
    published = storage.publish_state_documents.await_args.kwargs["documents"]
    assert list(published) == [paid_ads_monitor.proposal_path(str(db.launch_id), 1)]
    assert len(db.finalized) == 1 and db.finalized[0]["review_commit_sha"] == "e" * 40

    assert storage.repo.writes == 1
    names = paid_ads_monitor.paths(str(db.launch_id), run_id)
    tree = storage.repo.trees[storage.repo.head]
    assert set(names.values()) <= set(tree)
    report = tree[names["MONITOR.md"]][1].decode()
    assert WASTE in report and "disapproved" in report.lower()
    monitor = json.loads(tree[names["monitor.json"]][1])
    assert monitor
    db._complete_readonly_report_projection.assert_awaited_once()
    assert db._complete_readonly_report_projection.await_args.kwargs["workflow_key"] == (
        paid_ads_monitor.KEY
    )
    assert model.calls == ["classify", "brief"]

    counts = (len(integrations.searches), len(integrations.mutations), len(model.calls))
    await finish(activities, run_id)
    assert (len(integrations.searches), len(integrations.mutations), len(model.calls)) == counts
    assert storage.repo.writes == 1
    storage.publish_state_documents.assert_awaited_once()


@pytest.mark.asyncio
async def test_quiet_period_reads_only_and_says_so():
    activities, db, storage, integrations, model = fixture(db=MonitorDB(enabled_days_ago=1))
    run_id = str(db.run.id)
    await finish(activities, run_id)
    assert len(integrations.searches) == 9
    assert integrations.mutations == []
    assert db.proposals == {}
    storage.publish_state_documents.assert_not_awaited()
    decision = db.effects[activities.key(run_id, "decision")].result["decision"]
    assert decision["quiet"] and decision["proposals"] == []
    report = storage.repo.trees[storage.repo.head][
        paid_ads_monitor.paths(str(db.launch_id), run_id)["MONITOR.md"]
    ][1].decode()
    assert "first three" in report


@pytest.mark.asyncio
async def test_a_second_open_proposal_is_skipped_not_forced():
    activities, db, storage, _integrations, _model = fixture(db=MonitorDB(open_proposal=True))
    run_id = str(db.run.id)
    await finish(activities, run_id)
    proposed = db.effects[activities.key(run_id, "proposed")].result["proposals"]
    assert [p["status"] for p in proposed] == ["skipped"]
    assert "awaiting you" in proposed[0]["reason"]
    assert len(db.proposals) == 1  # only the pre-existing one
    storage.publish_state_documents.assert_not_awaited()
    assert storage.repo.writes == 1


@pytest.mark.asyncio
async def test_unconfirmed_change_stays_unknown_and_the_report_still_publishes():
    integrations = FakeIntegrations(fail_negatives=True)
    activities, db, storage, integrations, _model = fixture(integrations=integrations)
    run_id = str(db.run.id)
    await finish(activities, run_id)
    applied = db.effects[activities.key(run_id, "applied")].result
    assert [item["status"] for item in applied["negatives"]] == ["unknown"]
    assert [item["status"] for item in applied["pause_ads"]] == ["applied"]
    receipt = db.effects[activities.key(run_id, "apply:negatives")].result
    assert receipt["status"] == "unknown" and receipt["reason"] == "provider_result_unavailable"
    assert [segment for segment, _ in integrations.mutations] == ["adGroupAds"]
    assert storage.repo.writes == 1
    db._complete_readonly_report_projection.assert_awaited_once()
    # A rerun never resends the unconfirmed negatives.
    await activities.apply(run_id)
    assert [segment for segment, _ in integrations.mutations] == ["adGroupAds"]


@pytest.mark.asyncio
async def test_prepare_refuses_without_a_live_campaign_or_with_another_account():
    activities, db, _storage, _integrations, _model = fixture(db=MonitorDB(campaign_status="draft"))
    with pytest.raises(ApplicationError, match="live campaign"):
        await activities.prepare(str(db.run.id))
    activities, db, _storage, _integrations, _model = fixture(
        integrations=FakeIntegrations(customer="9876543210")
    )
    with pytest.raises(ApplicationError, match="not the one the campaign lives in"):
        await activities.prepare(str(db.run.id))
