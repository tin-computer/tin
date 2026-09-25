"""Google Ads launch activities against the in-memory receipt fake: every provider call is
receipted, nothing is written before approval, the bundle is validated then sent once, an
unconfirmed create is adopted rather than resent, the tracking and setup paths end where they
should, and a replay of every activity buys nothing. Postgres atomicity is proven elsewhere."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError
from test_organic_audit import MemoryDB
from test_paid_ads_launch import FakeModel as PureFakeModel
from test_procedure_publication import HistoryStorage, run_fixture

from tin_lite import paid_ads, paid_ads_launch
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.domain import EffectReceipt, RunStatus
from tin_lite.integrations import (
    GitHubPullRequestResult,
    GitHubRepositoryFile,
    GitHubRepositorySnapshot,
    GoogleAdsCallError,
    IntegrationDeliveryUnknownError,
    IntegrationUpstreamError,
)
from tin_lite.model_providers import ModelProviderError, ModelResult, ModelUsage, ProviderName
from tin_lite.organic_audit import canonical_json, digest
from tin_lite.paid_ads_launch_activities import PLACEHOLDER, PaidAdsLaunchActivities

FIXTURES = Path(__file__).parent / "fixtures" / "paid_ads"
CUSTOMER = "1234567890"
ASSESSMENT_SHA = "b" * 40
SITE = {
    "verdict": "ok",
    "pages": [
        {
            "url": "https://www.clawmessenger.com/",
            "kind": "home",
            "status": 200,
            "challenge": False,
            "chars": 900,
            "text": "The iMessage API for AI agents. Pricing from $5 a month.",
            "title": "Claw Messenger",
            "description": "iMessage API",
            "html": "<script src='https://www.googletagmanager.com/gtag/js?id=AW-17707549309'></script>",
        },
        {
            "url": "https://www.clawmessenger.com/pricing",
            "kind": "pricing",
            "status": 200,
            "challenge": False,
            "chars": 700,
            "text": "Base $5, Growth $15, Plus $25.",
            "title": "Pricing",
            "description": "",
            "html": "<script src='https://www.googletagmanager.com/gtag/js?id=AW-17707549309'></script>",
        },
    ],
    "readable": ["https://www.clawmessenger.com/", "https://www.clawmessenger.com/pricing"],
    "seconds": 0.2,
}
INDEX_HTML = "<html>\n<head>\n<title>Claw</title>\n</head>\n<body>hi</body>\n</html>\n"
GLOBAL_TAG = "<script async src='https://www.googletagmanager.com/gtag/js?id=AW-1'></script>"
EVENT_SNIPPET = "<script>gtag('event', 'conversion', {'send_to': 'AW-1/abc'});</script>"
RESULT_KEYS = {
    "campaignBudgetOperation": ("campaignBudgetResult", "campaignBudgets"),
    "campaignOperation": ("campaignResult", "campaigns"),
    "campaignCriterionOperation": ("campaignCriterionResult", "campaignCriteria"),
    "sharedSetOperation": ("sharedSetResult", "sharedSets"),
    "sharedCriterionOperation": ("sharedCriterionResult", "sharedCriteria"),
    "campaignSharedSetOperation": ("campaignSharedSetResult", "campaignSharedSets"),
    "adGroupOperation": ("adGroupResult", "adGroups"),
    "adGroupCriterionOperation": ("adGroupCriterionResult", "adGroupCriteria"),
    "adGroupAdOperation": ("adGroupAdResult", "adGroupAds"),
    "assetOperation": ("assetResult", "assets"),
    "campaignAssetOperation": ("campaignAssetResult", "campaignAssets"),
}


class LaunchDB(MemoryDB):
    """The receipt fake plus the launch's own rows: two runs, the campaign row, the review."""

    def __init__(self):
        super().__init__()
        self.assessment_run = replace(
            run_fixture(),
            project_id=self.project.id,
            executor=paid_ads.KEY,
            status=RunStatus.SUCCEEDED,
            canonical_commit_sha=ASSESSMENT_SHA,
            created_at=datetime.now(UTC),
        )
        self.run = replace(
            self.run,
            executor=paid_ads_launch.KEY,
            status=RunStatus.PENDING,
            created_at=datetime.now(UTC),
            review_decision=None,
            input={
                "project_id": str(self.project.id),
                "assessment_run_id": str(self.assessment_run.id),
                "max_cost_usd": 3,
            },
        )
        self.campaigns = {}
        self.reviews = []
        self.completions = []
        self.failures = []
        self.github_connection = None

    async def get_run(self, run_id, **kwargs):
        if run_id == self.assessment_run.id:
            return self.assessment_run
        assert run_id == self.run.id
        return self.run

    async def get_integration_connection(self, *, project_id, provider_key):
        if provider_key == "infra.github":
            return self.github_connection
        return None

    async def create_paid_ads_campaign(self, *, run_id, **fields):
        self.campaigns.setdefault(run_id, {"run_id": run_id, "status": "draft", **fields})
        return self.campaigns[run_id]

    async def get_paid_ads_campaign(self, run_id):
        return self.campaigns.get(run_id)

    async def approve_paid_ads_campaign(self, *, run_id):
        self.campaigns[run_id]["status"] = "approved"

    async def update_paid_ads_campaign(self, *, run_id, **fields):
        self.campaigns[run_id].update(fields)

    async def request_human_review(self, *, run_id, summary, **fields):
        assert run_id == self.run.id
        self.reviews.append({"summary": summary, **fields})
        self.run = replace(self.run, status=RunStatus.NEEDS_INPUT)
        return True

    async def record_human_review(self, *, run_id, decision, summary):
        assert run_id == self.run.id and decision == "approved"
        self.run = replace(self.run, status=RunStatus.RUNNING, review_decision="approved")

    async def complete_paid_ads_launch(self, conn, *, execution_key, run_id, **fields):
        assert self.run.review_decision == "approved"
        self.completions.append(fields)
        self.run = replace(self.run, status=RunStatus.SUCCEEDED)
        self.campaigns[run_id]["status"] = fields["campaign_status"]
        await self.complete_effect(conn, execution_key=execution_key, result={"projected": True})

    async def fail_paid_ads_launch(self, *, run_id, message, **fields):
        self.failures.append({"message": message, **fields})
        self.run = replace(self.run, status=RunStatus.FAILED)


class Script:
    """What the Google Ads account answers; flags bend one call at a time."""

    def __init__(self):
        self.conversions = True
        self.billing = True
        self.subscriptions_enabled = True
        self.validate_error = None
        self.fail_create_once = False
        self.lookup_found = False
        self.enable_lost = False
        self.status_after_enable = None
        self.calls = []

    def rows(self, query):
        if "FROM billing_setup" in query:
            return [{"billingSetup": {"status": "APPROVED" if self.billing else "PENDING"}}]
        if "conversion_action.tag_snippets" in query:
            return [
                {
                    "conversionAction": {
                        "id": "55",
                        "tagSnippets": [
                            {
                                "type": "WEBPAGE",
                                "pageFormat": "AMP",
                                "globalSiteTag": "amp",
                                "eventSnippet": "amp",
                            },
                            {
                                "type": "WEBPAGE",
                                "pageFormat": "HTML",
                                "globalSiteTag": GLOBAL_TAG,
                                "eventSnippet": EVENT_SNIPPET,
                            },
                        ],
                    }
                }
            ]
        if "FROM conversion_action" in query:
            if not self.conversions:
                return []
            return [
                {
                    "conversionAction": {
                        "resourceName": f"customers/{CUSTOMER}/conversionActions/1",
                        "id": "1",
                        "name": "Trial started",
                        "category": "SIGNUP",
                        "status": "ENABLED",
                        "type": "WEBPAGE",
                        "primaryForGoal": True,
                    },
                    "metrics": {"allConversions": 12.0},
                }
            ]
        if "FROM recommendation_subscription" in query:
            status = "ENABLED" if self.subscriptions_enabled else "PAUSED"
            return [{"recommendationSubscription": {"type": "KEYWORD", "status": status}}]
        if "FROM campaign WHERE campaign.name" in query:
            if self.status_after_enable:
                return [
                    {
                        "campaign": {
                            "resourceName": f"customers/{CUSTOMER}/campaigns/2",
                            "campaignBudget": f"customers/{CUSTOMER}/campaignBudgets/1",
                            "status": self.status_after_enable,
                        }
                    }
                ]
            if not self.lookup_found:
                return []
            return [
                {
                    "campaign": {
                        "resourceName": f"customers/{CUSTOMER}/campaigns/900",
                        "campaignBudget": f"customers/{CUSTOMER}/campaignBudgets/901",
                        "name": "adopted",
                        "status": "PAUSED",
                    }
                }
            ]
        if "FROM shared_set" in query:
            return [{"sharedSet": {"resourceName": f"customers/{CUSTOMER}/sharedSets/902"}}]
        if "FROM customer" in query:
            return [
                {
                    "customer": {
                        "id": CUSTOMER,
                        "descriptiveName": "Claw Messenger",
                        "currencyCode": "USD",
                        "timeZone": "America/New_York",
                        "status": "ENABLED",
                        "autoTaggingEnabled": True,
                        "conversionTrackingSetting": {
                            "conversionTrackingId": "17707549309",
                            "conversionTrackingStatus": "CONVERSION_TRACKING_MANAGED_BY_SELF",
                            "acceptedCustomerDataTerms": True,
                        },
                    }
                }
            ]
        raise AssertionError(f"unexpected query {query}")

    async def google_ads_call(
        self, *, project_id, kind, request, execution_key, run_id, expected_customer_id
    ):
        assert expected_customer_id == CUSTOMER and execution_key.startswith("paid_ads_launch:")
        self.calls.append((kind, request, execution_key))
        if kind == "search":
            return {"rows": self.rows(request["query"]), "provider_request_id": "req"}
        if kind == "mutate":
            if request["validate_only"]:
                if self.validate_error:
                    raise GoogleAdsCallError(self.validate_error)
                return {"results": [], "provider_request_id": "req"}
            if self.fail_create_once:
                self.fail_create_once = False
                raise TimeoutError("response lost")
            results = []
            for index, operation in enumerate(request["operations"]):
                (op_kind,) = operation
                result_key, plural = RESULT_KEYS[op_kind]
                results.append(
                    {result_key: {"resourceName": f"customers/{CUSTOMER}/{plural}/{index + 1}"}}
                )
            return {"results": results, "provider_request_id": "req"}
        assert kind == "mutate_resource"
        segment = request["segment"].split(":")[0]
        if segment == "campaigns" and self.enable_lost:
            raise IntegrationDeliveryUnknownError("Google Ads did not confirm the change")
        return {
            "results": [{"resourceName": f"customers/{CUSTOMER}/{segment}/55"}],
            "provider_request_id": "req",
        }


class FakeModel(PureFakeModel):
    def _tag_install(self, user, schema):
        path = schema["properties"]["path"]["enum"][0]
        original = user.split(f"=== {path} ===\n", 1)[1].split("\n\n=== ", 1)[0]
        content = original.replace("<head>\n", f"<head>\n{PLACEHOLDER}\n", 1)
        return {"path": path, "content": content, "reason": "head of the served page"}


def step_of(schema_name: str) -> str:
    name = schema_name.removeprefix("paid_ads_launch_")
    retry = name.endswith("_retry")
    name = name.removesuffix("_retry")
    if name.startswith("repair_"):
        name = f"repair:{name[len('repair_') :]}"
    return name + (":retry" if retry else "")


def router_for(model):
    async def generate(route, request):
        step = step_of(request.output_schema_name)
        assert route == paid_ads_launch.route_for(step).key
        try:
            parsed = await model(
                step, request.system, request.messages[0].content, request.output_schema, 0, ""
            )
        except paid_ads.UnusableModelResult as exc:
            raise ModelProviderError(str(exc)) from None
        return ModelResult(
            provider=ProviderName.OPENAI,
            model="synthetic",
            text="",
            parsed=parsed,
            request_id=f"request-{step}",
            usage=ModelUsage(10, 5),
        )

    return SimpleNamespace(generate=AsyncMock(side_effect=generate))


def assessment_documents(run_id):
    names = paid_ads.paths(run_id)
    return {
        names["ASSESSMENT.md"]: "# Assessment\n\nContinue.\n",
        names["assessment.json"]: (FIXTURES / "assessment_sample.json").read_text(),
        names["keywords.csv"]: (FIXTURES / "keywords_sample.csv").read_text(),
        names["evidence.json"]: "{}",
    }


async def fixture(*, script=None, model=None, github=None, site=None):
    db, storage = LaunchDB(), HistoryStorage()
    script = script or Script()
    documents = assessment_documents(str(db.assessment_run.id))
    db.effects[f"paid_ads:{db.assessment_run.id}:publish"] = EffectReceipt(
        f"paid_ads:{db.assessment_run.id}:publish",
        paid_ads.KEY,
        "completed",
        {"canonical_commit_sha": ASSESSMENT_SHA, "documents_sha256": digest(documents)},
    )
    definition = next(w for w in BUILTIN_WORKFLOWS if w.key == paid_ads_launch.KEY).definition

    async def read_canonical_artifact(*, repo_id, commit_sha, path):
        if repo_id == "registry/workflows":
            return canonical_json(definition)
        assert commit_sha == ASSESSMENT_SHA
        return documents[path].encode()

    storage.read_canonical_artifact = read_canonical_artifact
    model = model or FakeModel()
    router = router_for(model)

    async def project(conn, *, execution_key, **_kwargs):
        await db.complete_effect(conn, execution_key=execution_key, result={"projected": True})

    db._complete_readonly_report_projection = AsyncMock(side_effect=project)

    async def read_site(url, *, html_scan=None):
        value = json.loads(json.dumps(site or SITE))
        for page in value["pages"]:
            page["signals"] = html_scan(page.get("html", "")) if html_scan else None
        return value

    connection = SimpleNamespace(id=uuid4(), configuration={"link_status": "active"})
    integrations = SimpleNamespace(
        is_configured=lambda key: True,
        google_ads_link_status=AsyncMock(return_value=connection),
        google_ads_account=AsyncMock(return_value=CUSTOMER),
        google_ads_call=AsyncMock(side_effect=script.google_ads_call),
        github_repository_snapshot=AsyncMock(
            side_effect=IntegrationUpstreamError("GitHub is down") if github is None else None,
            return_value=github,
        ),
        github_create_pull_request=AsyncMock(
            return_value=GitHubPullRequestResult(
                repository="acme/site", branch="tin/abc", number=7, url="https://gh/pr/7"
            )
        ),
    )
    activities = PaidAdsLaunchActivities(
        database=db,
        storage=storage,
        settings=SimpleNamespace(luna_api_key="fixture"),
        router=router,
        integrations=integrations,
        site_reader=read_site,
    )
    return activities, db, storage, script, router, integrations, model


def launch_receipts(db):
    return {key for key in db.effects if key.startswith(f"paid_ads_launch:{db.run.id}:")}


def mutates(script, *, validate_only):
    return [c for c in script.calls if c[0] == "mutate" and c[1]["validate_only"] is validate_only]


async def run_through_approval(activities, db):
    run_id = str(db.run.id)
    await activities.prepare(run_id)
    await activities.gather(run_id)
    mode = await activities.draft(run_id)
    await activities.request_review(run_id)
    await activities.record_approval(run_id)
    return run_id, mode


@pytest.mark.asyncio
async def test_ready_path_validates_then_creates_once_and_a_replay_buys_nothing():
    activities, db, storage, script, router, integrations, model = await fixture()
    run_id, mode = await run_through_approval(activities, db)
    assert mode == "launch"
    names = paid_ads_launch.paths(run_id, paid_ads_launch.PLAN_DOCS)
    tree = storage.repo.trees[storage.repo.head]
    assert set(names.values()) <= set(tree) and storage.repo.writes == 1
    campaign = db.campaigns[db.run.id]
    assert campaign["mode"] == "launch" and campaign["status"] == "approved"
    assert campaign["plan_path"] == names["PLAN.md"]
    assert campaign["daily_budget_micros"] == 10_000_000
    assert "$10.00 a day" in db.reviews[0]["summary"]
    assert db.reviews[0]["artifact_path"] == names["PLAN.md"]
    # Nothing was written to Google Ads before approval: only reads so far.
    assert all(c[0] == "search" for c in script.calls)

    await activities.apply(run_id)
    validates, creates = mutates(script, validate_only=True), mutates(script, validate_only=False)
    assert len(validates) == 1 and len(creates) == 1
    assert validates[0][1]["operations"] == creates[0][1]["operations"]
    assert [c[2] for c in validates + creates] == [
        activities.key(run_id, "apply:validate:call"),
        activities.key(run_id, "apply:create:call"),
    ]
    resource_calls = [c for c in script.calls if c[0] == "mutate_resource"]
    assert [c[1]["segment"] for c in resource_calls] == [
        "recommendationSubscriptions:mutateRecommendationSubscription",
        "campaigns",
    ]
    assert resource_calls[0][1]["body"]["operations"] == [
        {"create": {"type": "KEYWORD", "status": "PAUSED"}}
    ]
    assert resource_calls[1][1]["body"]["operations"][0]["update"]["status"] == "ENABLED"
    applied = await activities._result(run_id, "applied")
    assert applied["enabled"] and applied["paused_subscriptions"] == ["KEYWORD"]
    assert applied["resources"]["campaign"] == f"customers/{CUSTOMER}/campaigns/2"
    assert applied["campaign_id"] == "2" and applied["budget_id"] == "1"
    assert campaign["external_campaign_id"] == "2" and campaign["enabled_at"]

    await activities.publish(run_id)
    results = paid_ads_launch.paths(run_id, paid_ads_launch.RESULT_DOCS)
    tree = storage.repo.trees[storage.repo.head]
    assert set(results.values()) <= set(tree) and storage.repo.writes == 2
    saved = json.loads(tree[results["campaign.json"]][1])
    assert saved["campaign_id"] == "2" and saved["launch_run_id"] == run_id
    assert saved["allowable_cpa_usd"] == 30.0 and saved["enabled"] is True
    assert b"The campaign is on" in tree[results["RESULT.md"]][1]
    assert db.completions[0]["campaign_status"] == "live"
    assert db.completions[0]["event_type"] == "paid_ads_launch_ready"
    assert campaign["status"] == "live" and db.run.status is RunStatus.SUCCEEDED

    # Every provider call left a receipt under the launch prefix.
    for _kind, _request, execution_key in script.calls:
        assert execution_key.removesuffix(":call") in launch_receipts(db)
    for step in model.calls:
        assert activities.key(run_id, f"model:{step}") in launch_receipts(db)

    counts = (integrations.google_ads_call.await_count, router.generate.await_count)
    for step in ("prepare", "gather", "draft", "request_review", "apply", "publish"):
        await getattr(activities, step)(run_id)
    assert (integrations.google_ads_call.await_count, router.generate.await_count) == counts
    assert storage.repo.writes == 2 and len(db.completions) == 1


@pytest.mark.asyncio
async def test_a_rejected_validation_creates_nothing():
    script = Script()
    script.validate_error = "CampaignError.DUPLICATE_CAMPAIGN_NAME"
    activities, db, _, script, _, _, _ = await fixture(script=script)
    run_id, _ = await run_through_approval(activities, db)
    with pytest.raises(ApplicationError) as failed:
        await activities.apply(run_id)
    assert failed.value.non_retryable and "DUPLICATE_CAMPAIGN_NAME" in str(failed.value)
    assert mutates(script, validate_only=False) == []
    assert not any(c[0] == "mutate_resource" for c in script.calls)
    failure = await activities._result(run_id, "failure")
    assert failure["code"] == "google_ads" and failure["stage"] == "validate"
    validate = db.effects[activities.key(run_id, "apply:validate")]
    assert validate.result["value"]["error"] == "CampaignError.DUPLICATE_CAMPAIGN_NAME"


@pytest.mark.asyncio
async def test_an_unconfirmed_create_is_adopted_by_name_and_never_resent():
    script = Script()
    script.fail_create_once = True
    activities, db, _, script, _, _, _ = await fixture(script=script)
    run_id, _ = await run_through_approval(activities, db)
    # The earlier attempt reached Google; from now on the account knows the campaign by name.
    script.lookup_found = True
    await activities.apply(run_id)
    create = db.effects[activities.key(run_id, "apply:create")]
    assert create.status == "completed" and create.result["status"] == "unknown"
    assert len(mutates(script, validate_only=False)) == 1
    lookups = [c for c in script.calls if c[0] == "search" and "campaign.name" in c[1]["query"]]
    # One lookup in gather (no campaign yet), one in apply that found the earlier attempt.
    assert len(lookups) == 2
    applied = await activities._result(run_id, "applied")
    assert applied["adopted"] and applied["campaign_id"] == "900"
    assert applied["resources"]["shared_set"] == f"customers/{CUSTOMER}/sharedSets/902"
    await activities.apply(run_id)
    assert len(mutates(script, validate_only=False)) == 1


@pytest.mark.asyncio
async def test_an_unconfirmed_create_with_no_trace_refuses_and_a_retry_sends_nothing():
    script = Script()
    script.fail_create_once = True
    activities, db, _, script, _, integrations, _ = await fixture(script=script)
    run_id, _ = await run_through_approval(activities, db)
    with pytest.raises(ApplicationError) as failed:
        await activities.apply(run_id)
    assert "did not confirm" in str(failed.value)
    sent = len(mutates(script, validate_only=False))
    count = integrations.google_ads_call.await_count
    with pytest.raises(ApplicationError):
        await activities.apply(run_id)
    assert len(mutates(script, validate_only=False)) == sent == 1
    assert integrations.google_ads_call.await_count == count
    assert not any(c[0] == "mutate_resource" for c in script.calls)


def enables(script):
    return [c for c in script.calls if c[0] == "mutate_resource" and c[1]["segment"] == "campaigns"]


@pytest.mark.asyncio
async def test_a_lost_enable_answer_reads_the_campaign_back_and_goes_live_when_it_is_on():
    script = Script()
    script.enable_lost = True
    activities, db, _, script, _, integrations, _ = await fixture(script=script)
    run_id, _ = await run_through_approval(activities, db)
    # Google applied the switch; only its answer was lost.
    script.status_after_enable = "ENABLED"
    await activities.apply(run_id)
    enable = db.effects[activities.key(run_id, "apply:enable")]
    assert enable.result["status"] == "unknown"
    assert activities.key(run_id, "apply:enable_check") in launch_receipts(db)
    applied = await activities._result(run_id, "applied")
    assert applied["enabled"] and applied["campaign_id"] == "2"
    assert db.campaigns[db.run.id]["enabled_at"]
    count = integrations.google_ads_call.await_count
    await activities.apply(run_id)
    assert len(enables(script)) == 1 and integrations.google_ads_call.await_count == count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "says"),
    [
        (None, "may already be running"),
        ("PAUSED", "is paused in your account"),
    ],
)
async def test_a_lost_enable_answer_only_claims_paused_when_the_account_says_so(status, says):
    script = Script()
    script.enable_lost = True
    activities, db, _, script, _, integrations, _ = await fixture(script=script)
    run_id, _ = await run_through_approval(activities, db)
    script.status_after_enable = status
    with pytest.raises(ApplicationError) as failed:
        await activities.apply(run_id)
    assert says in str(failed.value)
    assert ("paused" in str(failed.value)) is (status == "PAUSED")
    failure = await activities._result(run_id, "failure")
    assert failure["stage"] == "enable" and failure["detail"] == str(failed.value)
    assert await activities._result(run_id, "applied") is None
    count = integrations.google_ads_call.await_count
    with pytest.raises(ApplicationError):
        await activities.apply(run_id)
    assert len(enables(script)) == 1 and integrations.google_ads_call.await_count == count


@pytest.mark.asyncio
async def test_nothing_is_written_before_approval():
    activities, db, _, script, _, _, _ = await fixture()
    run_id = str(db.run.id)
    await activities.prepare(run_id)
    await activities.gather(run_id)
    assert await activities.draft(run_id) == "launch"
    await activities.request_review(run_id)
    assert db.run.status is RunStatus.NEEDS_INPUT and db.run.review_decision is None
    with pytest.raises(ApplicationError) as failed:
        await activities.apply(run_id)
    assert "not approved" in str(failed.value)
    assert all(c[0] == "search" for c in script.calls)
    # The wrappers admit a run that is waiting for its review.
    run = await activities._active(run_id)
    assert run.status is RunStatus.NEEDS_INPUT


@pytest.mark.parametrize("with_github", [True, False])
@pytest.mark.asyncio
async def test_tracking_path_creates_one_conversion_action_and_offers_the_tag(with_github):
    script = Script()
    script.conversions = False
    snapshot = GitHubRepositorySnapshot(
        repository="acme/site",
        default_branch="main",
        head_sha="c" * 40,
        files=(
            GitHubRepositoryFile(path="index.html", content=INDEX_HTML),
            GitHubRepositoryFile(path="package.json", content="{}"),
        ),
    )
    activities, db, storage, script, _, integrations, model = await fixture(
        script=script, github=snapshot if with_github else None
    )
    if with_github:
        db.github_connection = SimpleNamespace(
            status="connected", configuration={"write_opted_in": True}
        )
    run_id, mode = await run_through_approval(activities, db)
    assert mode == "tracking"
    names = paid_ads_launch.paths(run_id, paid_ads_launch.TRACKING_DOCS)
    tree = storage.repo.trees[storage.repo.head]
    assert set(names.values()) <= set(tree)
    draft = await activities._result(run_id, "draft")
    assert draft["action"]["create"] and draft["action"]["category"] == "SIGNUP"
    assert ("tag_install" in model.calls) is with_github
    assert (draft["tag_change"] is not None) is with_github
    summary = db.reviews[0]["summary"]
    assert "conversion action" in summary and draft["action"]["name"] in summary
    assert ("pull request" in summary) is with_github
    assert db.campaigns[db.run.id]["mode"] == "tracking"

    await activities.apply(run_id)
    resource_calls = [c for c in script.calls if c[0] == "mutate_resource"]
    assert [c[1]["segment"] for c in resource_calls] == ["conversionActions"]
    body = resource_calls[0][1]["body"]["operations"][0]["create"]
    assert body["name"] == draft["action"]["name"] and body["category"] == "SIGNUP"
    assert mutates(script, validate_only=False) == []
    snippets = [c for c in script.calls if c[0] == "search" and "tag_snippets" in c[1]["query"]]
    assert len(snippets) == 1 and "conversion_action.id = 55" in snippets[0][1]["query"]
    applied = await activities._result(run_id, "applied")
    assert applied["action"]["id"] == "55"
    assert applied["snippets"] == {"global_site_tag": GLOBAL_TAG, "event_snippet": EVENT_SNIPPET}
    if with_github:
        assert integrations.github_create_pull_request.await_count == 1
        call = integrations.github_create_pull_request.await_args.kwargs
        (change,) = call["files"]
        assert change.path == "index.html" and GLOBAL_TAG in change.content
        assert PLACEHOLDER not in change.content
        assert change.content.replace(GLOBAL_TAG + "\n", "") == INDEX_HTML
        assert call["expected_base_sha"] == "c" * 40 and EVENT_SNIPPET in call["body"]
        assert applied["pull_request"]["url"] == "https://gh/pr/7"
    else:
        assert integrations.github_create_pull_request.await_count == 0
        assert applied["pull_request"] is None

    await activities.publish(run_id)
    results = paid_ads_launch.paths(run_id, paid_ads_launch.RESULT_DOCS)
    tree = storage.repo.trees[storage.repo.head]
    assert set(results.values()) <= set(tree)
    assert b"site tag" in tree[results["RESULT.md"]][1]
    assert GLOBAL_TAG.encode() in tree[results["campaign.json"]][1]
    assert EVENT_SNIPPET.encode() in tree[results["campaign.json"]][1]
    assert db.completions[0]["campaign_status"] == "tracking"
    assert db.completions[0]["event_type"] == "paid_ads_tracking_ready"
    assert db.run.status is RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_tracking_path_reuses_an_action_that_already_carries_the_name():
    script = Script()
    script.conversions = False
    activities, db, _, script, _, _, _ = await fixture(script=script)
    run_id = str(db.run.id)
    await activities.prepare(run_id)
    await activities.gather(run_id)
    assert await activities.draft(run_id) == "tracking"
    draft = await activities._result(run_id, "draft")
    name = draft["action"]["name"]
    original_rows = script.rows

    def rows(query):
        if "FROM conversion_action" in query and "tag_snippets" not in query:
            return [
                {
                    "conversionAction": {
                        "resourceName": f"customers/{CUSTOMER}/conversionActions/55",
                        "id": "55",
                        "name": name,
                        "category": "SIGNUP",
                        "status": "ENABLED",
                        "type": "WEBPAGE",
                    },
                    "metrics": {"allConversions": 0},
                }
            ]
        return original_rows(query)

    script.rows = rows
    await activities.request_review(run_id)
    await activities.record_approval(run_id)
    await activities.apply(run_id)
    assert not any(c[0] == "mutate_resource" for c in script.calls)
    applied = await activities._result(run_id, "applied")
    assert applied["action"]["id"] == "55" and applied["snippets"]["event_snippet"]


@pytest.mark.asyncio
async def test_setup_path_publishes_the_note_and_settles_as_failed():
    script = Script()
    script.billing = False
    activities, db, storage, script, _, _, model = await fixture(script=script)
    run_id = str(db.run.id)
    await activities.prepare(run_id)
    await activities.gather(run_id)
    assert await activities.draft(run_id) == "setup"
    assert model.calls == [] and all(c[0] == "search" for c in script.calls)
    names = paid_ads_launch.paths(run_id, paid_ads_launch.SETUP_DOCS)
    tree = storage.repo.trees[storage.repo.head]
    assert names["SETUP.md"] in tree and b"payment" in tree[names["SETUP.md"]][1].lower()
    assert db.campaigns[db.run.id]["mode"] == "setup"
    await activities.settle_setup(run_id)
    (failure,) = db.failures
    gate = (await activities._result(run_id, "gathered"))["gate"]
    assert gate["outcome"] == "needs_billing" and failure["message"] == gate["text"]
    assert failure["artifact_path"] == names["SETUP.md"]
    assert failure["canonical_commit_sha"] == storage.repo.head
    assert failure["artifact_ref"].endswith(names["SETUP.md"])
    assert db.run.status is RunStatus.FAILED and db.reviews == []


@pytest.mark.asyncio
async def test_an_unusable_model_result_is_receipted_and_retried_once():
    activities, db, _, _, router, _, model = await fixture(model=FakeModel(unusable=["copy"]))
    # The $3 default ceiling holds one copy call, not two: 1.20 + 1.20 + 0.15 + 0.80 = 3.35.
    db.run = replace(db.run, input={**db.run.input, "max_cost_usd": 5})
    run_id = str(db.run.id)
    await activities.prepare(run_id)
    await activities.gather(run_id)
    assert await activities.draft(run_id) == "launch"
    assert model.calls[:2] == ["copy", "copy:retry"]
    receipts = launch_receipts(db)
    assert activities.key(run_id, "model:copy") in receipts
    assert activities.key(run_id, "model:copy:retry") in receipts
    assert db.effects[activities.key(run_id, "model:copy")].result["value"]["unusable"]
    count = router.generate.await_count
    assert await activities.draft(run_id) == "launch"
    assert router.generate.await_count == count


def test_failure_text_names_which_side_failed():
    from tin_lite.paid_ads_launch_activities import _failed

    gated = _failed(
        {"status": "unavailable", "reason": "spending_stopped"},
        "the plan check",
        "nothing was created",
    )
    assert "Tin could not authorize" in gated and "spending_stopped" in gated
    assert "Google Ads was not contacted" in gated
    refused = _failed({"status": "completed", "error": "POLICY"}, "the plan check", "x")
    assert refused.startswith("Google Ads refused the plan check (POLICY)")
    lost = _failed({"status": "unknown", "reason": "provider_result_unavailable"}, "a", "x")
    assert "did not receive Google Ads' answer" in lost
