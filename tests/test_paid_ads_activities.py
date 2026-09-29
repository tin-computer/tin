"""Paid ads assessment activities against the in-memory receipt fake: every call is receipted,
a retry buys nothing, the gate spends nothing, unconfirmed steps stop, the ledger bounds spend
and four files publish once. Postgres atomicity is proven by the shared publication tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import ApplicationError
from test_organic_audit import MemoryDB
from test_paid_ads import PROJECT_ID, FakeModel, volume_items
from test_procedure_publication import HistoryStorage

from tin_lite import paid_ads
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.model_providers import ModelProviderError, ModelResult, ModelUsage, ProviderName
from tin_lite.organic_audit import canonical_json
from tin_lite.paid_ads_activities import MODEL_TIMEOUT_SECONDS, PaidAdsActivities

INPUTS = {
    "project_id": PROJECT_ID,
    "product_url": "https://www.clawmessenger.com",
    "market": "US",
    "budget": "under_500",
    "hard_nos": [],
    "price_point": 15,
    "billing": "monthly",
    "gross_margin": "software_high",
    "conversion_event": "trial_card_required",
    "ads_history": "running",
    "ads_platforms": ["google_search"],
    "ads_spend_usd": 51,
    "ads_clicks": 26,
    "ads_purchases": 5,
    "ads_soft_conversions": 0,
    "ads_notes": "exact match tests worked",
    "competitor_domains": ["sendblue.co"],
    "notes": "",
    "max_cost_usd": 6,
}
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
            "html": "",
        },
    ],
    "readable": ["https://www.clawmessenger.com/", "https://www.clawmessenger.com/pricing"],
    "seconds": 0.2,
}


class ActivityModel(FakeModel):
    def _profile(self, user, schema):
        return {
            "business_name": "Claw Messenger",
            "industry": "dev_tools",
            "buyer_type": "developer",
            "price_band": "low",
            "billing": "monthly",
            "motion": "self_serve",
            "conversion_event": "trial_card_required",
            "visual_fit": 1,
            "offer_clarity": 3,
            "pricing_shown": True,
            "mobile_ok": True,
            "free_step": False,
            "geography": "US",
            "stage": "early_revenue",
            "summary": "iMessage API for AI agents",
            "unknowns": [],
            "basis": [{"field": "price_band", "quote": "from $5", "source": "home"}],
        }

    def _seeds(self, user, schema):
        return {
            "seeds": [{"phrase": k, "why": "buyer"} for k in FakeModel.INTENT],
            "competitor_domains": ["bluebubbles.app"],
            "negative_themes": ["free", "jobs"],
        }


def step_of(schema_name: str) -> str:
    name = schema_name.removeprefix("paid_ads_")
    retry = name.endswith("_retry")
    name = name.removesuffix("_retry")
    for prefix in ("classify", "repair"):
        if name.startswith(prefix + "_"):
            name = f"{prefix}:{name[len(prefix) + 1 :]}"
    return name + (":retry" if retry else "")


def providers(model):
    async def dfs(kind, *, market, value, tag):
        if kind == "ad_traffic":
            items = [{"bid": value["bid"], "clicks": 30.1, "cost": 247.6, "average_cpc": 8.22}]
        elif kind == "serp":
            items = [
                {"type": "paid", "domain": "sendblue.co"},
                {"type": "organic", "domain": "x.example"},
            ]
        elif kind == "ads_search":
            items = [
                {
                    "format": "text",
                    "first_shown": "2026-04-14 01:41:32 +00:00",
                    "last_shown": "2026-05-19 12:20:39 +00:00",
                }
            ]
        elif kind == "ads_advertisers":
            items = [{"title": "EMOTION MACHINE, INC.", "verified": True, "approx_ads_count": 1}]
        elif kind == "ranked_paid":
            items = [{"keyword_data": {"keyword": "try sendblue", "keyword_info": {"cpc": 13.9}}}]
        else:
            items = [
                {
                    "keyword": v["keyword"],
                    "keyword_info": {
                        "cpc": 9.0,
                        "competition_level": "MEDIUM",
                        "search_volume": v["avg_monthly_searches"],
                    },
                    "search_intent_info": {"main_intent": "commercial"},
                }
                for v in volume_items()
                if v["keyword"] in value
            ]
        return {
            "items": items,
            "items_count": len(items),
            "total_count": len(items),
            "reported_cost_usd": "0.01",
            "provider_task_id": "fixture",
        }

    async def gak(kind, *, market, value, tag):
        rows = volume_items()
        if kind == "ideas" and "url" in value:
            rows = [{**rows[0], "keyword": "rcs message"}, rows[1]]
        elif kind == "volume":
            rows = [row for row in rows if row["keyword"] in value["keywords"]]
        return {
            "items": rows,
            "items_count": len(rows),
            "reported_cost_usd": "0",
            "provider_task_id": "tin-fixture",
            "cached": False,
        }

    async def generate(route, request, *, timeout_seconds=None):
        step = step_of(request.output_schema_name)
        assert route == paid_ads.route_for(step).key
        # The client waits as long as the step's budget, not its 90-second default.
        assert timeout_seconds == MODEL_TIMEOUT_SECONDS
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

    return (
        SimpleNamespace(query=AsyncMock(side_effect=dfs)),
        SimpleNamespace(query=AsyncMock(side_effect=gak)),
        SimpleNamespace(generate=AsyncMock(side_effect=generate)),
    )


async def fixture(*, inputs=None, budget=6, model=None, definition=None, site=None):
    db, storage = MemoryDB(), HistoryStorage()
    db.run = replace(
        db.run,
        executor=paid_ads.KEY,
        created_at=datetime.now(UTC),
        input={**INPUTS, **(inputs or {})},
    )
    db.get_integration_connection = AsyncMock(return_value=None)

    async def project(conn, *, execution_key, **_kwargs):
        # The real helper completes its own receipt inside the projection transaction.
        await db.complete_effect(conn, execution_key=execution_key, result={"projected": True})

    db._complete_readonly_report_projection = AsyncMock(side_effect=project)
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == paid_ads.KEY)
    storage.read_canonical_artifact = AsyncMock(
        return_value=canonical_json(definition or spec.definition)
    )
    model = model or ActivityModel()
    provider, gak, router = providers(model)

    async def read_site(url, *, html_scan=None):
        value = json.loads(json.dumps(site or SITE))
        for page in value["pages"]:
            page["signals"] = html_scan(page.get("html", "")) if html_scan else None
        return value

    activities = PaidAdsActivities(
        database=db,
        storage=storage,
        settings=SimpleNamespace(paid_ads_max_cost_usd=budget, luna_api_key="fixture"),
        router=router,
        integrations=SimpleNamespace(search_console_analytics=AsyncMock()),
        provider=provider,
        gak=gak,
        site_reader=read_site,
    )
    return activities, db, storage, provider, gak, router, model


async def finish(activities, run_id):
    for step in ("prepare", "gather", "research", "assess", "publish", "project"):
        await getattr(activities, step)(run_id)


@pytest.mark.asyncio
async def test_full_path_receipts_every_call_and_a_retry_buys_nothing():
    activities, db, storage, provider, gak, router, model = await fixture()
    run_id = str(db.run.id)
    await finish(activities, run_id)
    counts = (provider.query.await_count, gak.query.await_count, router.generate.await_count)
    assert gak.query.await_count == 3  # seed ideas, url ideas, volume
    assert storage.repo.writes == 1
    tree = storage.repo.trees[storage.repo.head]
    names = paid_ads.paths(run_id)
    assert set(names.values()) <= set(tree)
    report = tree[names["ASSESSMENT.md"]][1].decode()
    assert report.count("```") == 2 and "Keep the current ads running" in report
    assessment = json.loads(tree[names["assessment.json"]][1])
    assert assessment["decision"] == "continue" and assessment["campaign"]["ad_groups"]
    assert db._complete_readonly_report_projection.await_count == 1
    await finish(activities, run_id)
    assert (
        provider.query.await_count,
        gak.query.await_count,
        router.generate.await_count,
    ) == counts
    assert storage.repo.writes == 1 and db._complete_readonly_report_projection.await_count == 1
    ledger = db.effects[activities.key(run_id, "budget")].result
    assert sum(float(v) for v in ledger.values()) <= 6
    assert all(k.startswith(("model:", "research:", "gather:")) for k in ledger)
    assert "rcs message" not in report  # a URL idea that shares no seed word is filtered out


@pytest.mark.asyncio
async def test_the_gate_publishes_a_not_now_report_without_any_provider_call():
    activities, db, storage, provider, gak, router, _ = await fixture(
        inputs={"hard_nos": ["no_paid_ads"]}
    )
    run_id = str(db.run.id)
    await finish(activities, run_id)
    assert provider.query.await_count == gak.query.await_count == router.generate.await_count == 0
    assert storage.repo.writes == 1
    report = storage.repo.trees[storage.repo.head][paid_ads.paths(run_id)["ASSESSMENT.md"]][1]
    assert b"no paid ads" in report and b"Not now" in report and b"Words used" not in report
    assert activities.key(run_id, "budget") not in db.effects


@pytest.mark.asyncio
async def test_a_site_that_cannot_be_read_gates_after_the_fetch():
    activities, db, storage, provider, *_ = await fixture(
        site={"verdict": "unreachable", "pages": [], "readable": []}
    )
    run_id = str(db.run.id)
    await finish(activities, run_id)
    gathered = db.effects[activities.key(run_id, "gathered")].result
    assert gathered["gate"]["reason"] == "no_site"
    assert (
        provider.query.await_count == 1
    )  # the own-domain transparency read happened before the gate
    report = storage.repo.trees[storage.repo.head][paid_ads.paths(run_id)["ASSESSMENT.md"]][1]
    assert b"could not read the site" in report


@pytest.mark.asyncio
async def test_an_unconfirmed_model_step_stops_instead_of_buying_again():
    activities, db, *_rest, router, _ = await fixture()
    router.generate.side_effect = TimeoutError
    run_id = str(db.run.id)
    await activities.prepare(run_id)
    await activities.gather(run_id)
    with pytest.raises(ApplicationError):
        await activities.research(run_id)
    with pytest.raises(ApplicationError):
        await activities.research(run_id)
    assert router.generate.await_count == 1
    receipt = db.effects[activities.key(run_id, "model:profile")].result
    assert receipt["status"] == "unknown"
    assert db.effects[activities.key(run_id, "failure")].result["code"] == "model_unavailable"
    await activities.failure(run_id)
    assert "could not be confirmed" in db.project_failure.await_args.kwargs["error_message"]


@pytest.mark.asyncio
async def test_unusable_results_replay_and_one_replacement_is_bought():
    model = ActivityModel(unusable=["profile"])
    activities, db, *_rest, router, _ = await fixture(model=model)
    run_id = str(db.run.id)
    await activities.prepare(run_id)
    await activities.gather(run_id)
    await activities.research(run_id)
    assert model.calls[:2] == ["profile", "profile:retry"]
    assert db.effects[activities.key(run_id, "model:profile")].result["value"]["unusable"]
    calls = router.generate.await_count
    await activities.research(run_id)
    assert router.generate.await_count == calls


@pytest.mark.asyncio
async def test_the_ledger_refuses_reservations_above_the_ceiling():
    activities, db, *_ = await fixture(budget=3)
    run_id = str(db.run.id)
    await activities.prepare(run_id)
    assert db.effects[activities.key(run_id, "scope")].result["max_cost_usd"] == "3"
    assert await activities._reserve(run_id, "a", "2.90")
    assert not await activities._reserve(run_id, "b", "0.20")
    assert await activities._reserve(run_id, "a", "2.90")


@pytest.mark.asyncio
async def test_a_worker_serving_another_contract_refuses_the_run():
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == paid_ads.KEY)
    activities, db, *_ = await fixture(
        definition={**spec.definition, "paid_ads_contract_sha256": "0" * 64}
    )
    with pytest.raises(ApplicationError) as excinfo:
        await activities.prepare(str(db.run.id))
    assert excinfo.value.non_retryable and "contract" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_ceiling_below_three_dollars_or_a_missing_provider_refuses_admission():
    activities, db, *_ = await fixture(budget=2)
    with pytest.raises(ApplicationError):
        await activities.prepare(str(db.run.id))
    activities, db, *_ = await fixture()
    activities.gak = None
    with pytest.raises(ApplicationError):
        await activities.prepare(str(db.run.id))


@pytest.mark.asyncio
async def test_provider_price_above_the_reservation_stops_research():
    activities, db, _, provider, *_ = await fixture()
    run_id = str(db.run.id)
    original = provider.query.side_effect

    async def expensive(kind, **kwargs):
        value = await original(kind, **kwargs)
        return {**value, "reported_cost_usd": "5.00"}

    provider.query.side_effect = expensive
    await activities.prepare(run_id)
    with pytest.raises(ApplicationError):
        await activities.gather(run_id)
