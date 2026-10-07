"""Studio voice purchases, recovery and hosted defaults against disposable Postgres."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from test_billing import ACTOR, finish, fund
from test_billing import billed as billed
from test_procedure_publication import publication_db as publication_db
from test_service_billing import admit
from test_studio import _studio_settings

from tin_lite.billing_contracts import receipt_charge
from tin_lite.codex_api import PROCEDURE_CONTRACT_V5, api_enabled
from tin_lite.codex_api_pricing import api_terms
from tin_lite.studio import StudioError, StudioService
from tin_lite.studio_billing import CARD, billing_event


async def studio_fixture(f, *, failure=None, late_cost=False):
    await fund(f)
    f.settings.codex_api_projects = {f.project.id}
    run = await admit(
        f,
        "creative.product_demo",
        {
            "slug": "billing-proof",
            "product_url": "https://example.com/",
        },
    )
    await f.db.pool.execute("UPDATE workflow_runs SET status='running' WHERE id=$1", run.id)
    state = {"calls": [], "late_cost": late_cost}

    def wire(request):
        state["calls"].append(request)
        if request.url.host == "api.fal.ai":
            if state["late_cost"]:
                return httpx.Response(200, json={"billing_events": [], "has_more": False})
            return httpx.Response(
                200,
                json={
                    "has_more": False,
                    "billing_events": [
                        {
                            "request_id": request.url.params["request_id"],
                            "endpoint_id": request.url.params["endpoint_id"],
                            "cost_total": 0.012,
                            "cost_estimate_nano_usd": 12_000_000,
                        }
                    ],
                },
            )
        if request.url.host == "audio.test":
            return httpx.Response(200, content=b"audio", headers={"content-type": "audio/mpeg"})
        if request.url.path.endswith("whisper"):
            if failure == "transcription":
                raise httpx.ReadTimeout("uncertain", request=request)
            return httpx.Response(200, json={"chunks": []}, headers={"x-fal-request-id": "stt"})
        return httpx.Response(
            200,
            json={"audio": {"url": "https://audio.test/a.mp3"}},
            headers={"x-fal-request-id": "tts"},
        )

    service = StudioService(
        settings=_studio_settings(),
        database=f.db,
        client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
    )
    arguments = dict(
        run_id=run.id,
        request_id="proof1",
        text="Meet Tin.",
        voice="Kore",
        style="",
        language_code="English (US)",
        transcription_language="en",
    )
    return run, service, arguments, state


def test_studio_terms_pin_both_kinds_without_repricing_codex():
    definition = {
        "executor": "codex.procedure",
        "procedure": {
            "sandbox": {"profile": "studio"},
            "output": {"validator": "demo-video.v1"},
        },
    }
    terms = api_terms(definition)
    assert terms["codex_contract"] == PROCEDURE_CONTRACT_V5
    assert terms["operations"] == ["codex_api", "tool"]
    assert terms["studio_pricing"] == CARD
    assert "service_pricing" not in terms
    assert receipt_charge(terms, "tool", {"provider": "fal", "usage": {}}) is None


@pytest.mark.parametrize("bad", [None, -1, True, "12000000", 1.5])
def test_fal_cost_needs_trusted_complete_exact_request(bad):
    event = {
        "request_id": "tts",
        "endpoint_id": CARD["endpoints"][0],
        "cost_total": 0.012,
        "cost_estimate_nano_usd": bad,
    }
    assert (
        billing_event({"billing_events": [event]}, request_id="tts", endpoint=event["endpoint_id"])
        is None
    )


async def test_two_voice_steps_charge_once_and_replay_without_provider_purchases(billed):
    f = billed
    run, service, arguments, state = await studio_fixture(f)
    try:
        one, two = await asyncio.gather(service.voice(**arguments), service.voice(**arguments))
        assert {one.replayed, two.replayed} == {True, False}
        assert len([c for c in state["calls"] if c.method == "POST"]) == 2
        assert (await f.db.studio_voice_usage(run.id)).lines == 1
        operations = await f.db.pool.fetch(
            "SELECT * FROM billing_operations WHERE run_id=$1", run.id
        )
        assert len(operations) == 2
        assert all(o["observed_nanos"] == 12_000_000 for o in operations)
        await finish(f, run)
        assert await f.billing.settle(run.id) == 20_000_000
        assert await f.billing.settle(run.id) == 20_000_000
        assert (
            await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 1
        )
        assert (await f.billing.overview(f.project.id, ACTOR))["reserved_usd"] == "0.00"
        with pytest.raises(StudioError, match="different voice"):
            await service.voice(**{**arguments, "text": "changed"})
    finally:
        await service.close()


async def test_uncertain_transcription_never_repurchases_either_step(billed):
    f = billed
    run, service, arguments, state = await studio_fixture(f, failure="transcription")
    try:
        with pytest.raises(StudioError):
            await service.voice(**arguments)
        with pytest.raises(StudioError, match="unresolved provider"):
            await service.voice(**arguments)
        assert len([c for c in state["calls"] if c.method == "POST"]) == 2
        assert (await f.db.studio_voice_usage(run.id)).lines == 1
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM billing_operations WHERE run_id=$1 AND status='pending'",
                run.id,
            )
            == 1
        )
    finally:
        await service.close()


async def test_voice_waits_for_inflight_cost_before_purchase(billed, monkeypatch):
    f = billed
    run, service, arguments, state = await studio_fixture(f)
    async with f.db.pool.acquire() as conn:
        await f.billing.begin_operation(
            conn,
            run_id=run.id,
            operation_id="inflight-model",
            kind="codex_api",
            maximum=4_900_000_000,
        )

    async def release_pending(_seconds):
        assert not state["calls"]  # no supplier purchase before funding is available
        async with f.db.pool.acquire() as conn:
            await f.billing.observe_operation(
                conn, operation_id="inflight-model", nanos=100_000_000, observation={}
            )

    monkeypatch.setattr("tin_lite.studio.asyncio.sleep", release_pending)
    try:
        await service.voice(**arguments)
        assert len([c for c in state["calls"] if c.method == "POST"]) == 2
    finally:
        await service.close()


@pytest.mark.parametrize("charge", [2_190_000_000, 3_000_000_000, 5_000_000_000])
async def test_studio_review_settles_large_nanodollar_values_once(billed, charge):
    f = billed
    run, service, _arguments, _state = await studio_fixture(f)
    try:
        async with f.db.pool.acquire() as conn:
            await f.billing.begin_operation(
                conn, run_id=run.id, operation_id="model", kind="codex_api", maximum=charge
            )
            await f.billing.observe_operation(
                conn, operation_id="model", nanos=charge, observation={}
            )
        await f.db.pool.execute("UPDATE workflow_runs SET status='needs_input' WHERE id=$1", run.id)
        assert await f.billing.settle(run.id) == charge
        assert await f.billing.settle(run.id) == charge
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM billing_ledger WHERE run_id=$1 AND kind='charge'", run.id
            )
            == 1
        )
    finally:
        await service.close()


async def test_late_fal_usage_is_reconciled_without_another_generation(billed):
    f = billed
    run, service, arguments, state = await studio_fixture(f, late_cost=True)
    try:
        await service.voice(**arguments)
        await finish(f, run)
        assert await f.billing.settle(run.id) is None
        state["late_cost"] = False
        await service.reconcile_usage()
        await service.reconcile_usage()
        assert await f.billing.settle(run.id) == 20_000_000
        assert len([c for c in state["calls"] if c.method == "POST"]) == 2
    finally:
        await service.close()


async def test_failed_voice_calls_consume_quota_and_stopped_run_cannot_buy(billed):
    f = billed
    run, service, arguments, state = await studio_fixture(f, failure="transcription")
    service._settings.studio_max_voice_lines_per_run = 1
    try:
        with pytest.raises(StudioError):
            await service.voice(**arguments)
        with pytest.raises(StudioError, match="voice lines"):
            await service.voice(**{**arguments, "request_id": "another"})
        assert len([c for c in state["calls"] if c.method == "POST"]) == 2
    finally:
        await service.close()


@pytest.mark.parametrize("legacy", [False, True])
async def test_hosted_welcome_enrolls_once_with_default_policy(billed, legacy):
    f = billed
    await f.db.pool.execute(
        "DELETE FROM billing_project_policies WHERE project_id=$1", f.project.id
    )
    await f.db.pool.execute(
        "DELETE FROM billing_accounts WHERE workspace_id=$1", f.project.workspace_id
    )
    if legacy:
        await f.db.pool.execute(
            "UPDATE workspaces SET created_by_clerk_user_id=NULL WHERE id=$1",
            f.project.workspace_id,
        )
    f.settings.billing_hosted_defaults_enabled = True
    f.settings.billing_welcome_credits_enabled = True
    await asyncio.gather(*(f.db.record_tin_user(ACTOR) for _ in range(4)))
    await f.db.list_projects_for_user(ACTOR)
    overview = await f.billing.overview(f.project.id, ACTOR)
    assert overview["available_usd"] == "10.00" and overview["run_billing_enabled"]
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_welcome_grants") == 1
    policy = await f.db.pool.fetchrow(
        "SELECT * FROM billing_project_policies WHERE project_id=$1", f.project.id
    )
    assert policy["per_run_nanos"] == 25_000_000_000
    assert policy["monthly_nanos"] == 100_000_000_000
    assert policy["schedule_max_nanos"] == 50_000_000_000
    assert api_enabled(f.settings, f.project.id)


async def test_hosted_defaults_preserve_existing_limits_and_no_extra_credit(billed):
    f = billed
    await fund(f)
    before = dict(
        await f.db.pool.fetchrow(
            "SELECT * FROM billing_project_policies WHERE project_id=$1", f.project.id
        )
    )
    f.settings.billing_hosted_defaults_enabled = True
    await f.billing.ensure_hosted_projects(ACTOR)
    after = dict(
        await f.db.pool.fetchrow(
            "SELECT * FROM billing_project_policies WHERE project_id=$1", f.project.id
        )
    )
    assert after == before
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_welcome_grants") == 0
    assert Decimal((await f.billing.overview(f.project.id, ACTOR))["available_usd"]) > 0
