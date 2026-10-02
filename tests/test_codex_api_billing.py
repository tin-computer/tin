"""Real Postgres ledger plus supplier-wire tests; no live payments."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from test_billing import ACTOR, finish, fund, quote, start
from test_billing import billed as billed
from test_codex_api import BODY, GRANT, post, result_event
from test_procedure_publication import publication_db as publication_db

from tin_lite.billing_contracts import BillingError, final_charge
from tin_lite.codex_api import (
    ATTEMPT,
    CONTRACT,
    PROCEDURE_CONTRACT,
    PROCEDURE_CONTRACT_V5,
    SESSION_CONTRACT,
    SESSION_CONTRACT_V4,
    USAGE,
    attempt_key,
    select_contract,
    token_hash,
)
from tin_lite.codex_api_pricing import (
    RATE_CARD,
    REQUEST_MAXIMUM,
    api_terms,
    isolated_v1_terms,
    price_response,
)
from tin_lite.codex_api_relay import CodexAPIRelay, router
from tin_lite.procedures import SandboxProfile
from tin_lite.run_usage import read_run_usage
from tin_lite.workflow_costs import configured_terms

GOLDEN = [(9250, 117, 0, 9247), (12456, 241, 9247, 3206), (12753, 72, 12453, 297)]


def facts(inputs=9250, outputs=117, cached=0, writes=9247, searches=0):
    return {
        "pricing": RATE_CARD,
        "provider": "openai",
        "model": CONTRACT["model"],
        "service_tier": "default",
        "outcome": "response_received",
        "endpoint": "responses",
        "usage": {
            "input_tokens": inputs,
            "output_tokens": outputs,
            "total_tokens": inputs + outputs,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": writes,
            "web_search_calls": searches,
        },
    }


def test_actual_pilot_usage_and_context_boundary():
    total = sum(price_response(RATE_CARD, facts(*usage))[0] for usage in GOLDEN)
    assert total == 40_533_000
    assert final_charge(total, 5_000_000_000) == 40_000_000
    for inputs, band, rate, output in (
        (272000, "standard", 2000, 10000),
        (272001, "long_context", 4000, 15000),
    ):
        amount, details = price_response(RATE_CARD, facts(inputs, 10, 0, 0, 2))
        assert details["context_band"] == band
        assert amount == inputs * rate + 10 * output + 20_000_000


@pytest.mark.parametrize(
    "change",
    [
        {"model": "another-model"},
        {"service_tier": "priority"},
        {"service_tier": None},
        {"pricing": None},
        {"endpoint": "responses/compact"},
        {"provider": "anthropic"},
        {"usage": {"input_tokens": 100}},
        {"usage": {**facts()["usage"], "cache_write_input_tokens": None}},
        {"usage": {**facts()["usage"], "total_tokens": 99}},
        {"usage": {**facts()["usage"], "cached_input_tokens": True}},
        {"usage": {**facts()["usage"], "cache_write_input_tokens": 10000}},
    ],
)
def test_unknown_or_inconsistent_usage_never_becomes_zero(change):
    assert price_response(RATE_CARD, {**facts(), **change}) is None


async def paid_relay(
    f, *, missing_usage=False, provider_usage=None, response_status="completed", contract=CONTRACT
):
    await fund(f)
    f.settings.codex_api_projects = {f.project.id}
    q = await quote(f)
    historical = (CONTRACT, PROCEDURE_CONTRACT, SESSION_CONTRACT_V4)
    if contract in historical and q["terms"].get("codex_contract") != contract:
        # These fixtures model already-issued v1/v3/v4 quotes. Keep their old funding,
        # $5 ceiling and execution pins rather than quietly upgrading the tests to v5.
        terms = api_terms(
            f.workflow.definition, session_budget=contract == SESSION_CONTRACT_V4, before_v5=True
        )
        q["terms"] = configured_terms(
            isolated_v1_terms(terms) if contract == CONTRACT else terms,
            f.workflow.definition,
            {"brief": "Explain the public docs"},
        )
        await f.db.pool.execute(
            "UPDATE billing_quotes SET terms=$2::jsonb, maximum_nanos=$3 WHERE id=$1",
            UUID(q["id"]),
            json.dumps(q["terms"]),
            q["terms"]["maximum_nanos"],
        )
    assert q["terms"].get("codex_contract", CONTRACT) == contract
    # v5 admissions carry the $10 default; quotes issued before it keep $5.
    assert q["terms"]["maximum_nanos"] == (
        10_000_000_000 if contract in (SESSION_CONTRACT, PROCEDURE_CONTRACT_V5) else 5_000_000_000
    )
    assert q["terms"]["execution_fee_nanos"] == 0
    run = await start(f, q)
    await f.db.pool.execute(
        """UPDATE workflow_runs SET status='running', lease_active=true,
           sandbox_id='sandbox-test', lease_owner='test' WHERE id=$1""",
        run.id,
    )
    run = await f.db.get_run(run.id)
    record = {
        "run_id": str(run.id),
        "outcome": "running",
        "contract": contract,
        "pricing": RATE_CARD,
        "grant_sha256": token_hash(GRANT),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        **{
            name: str(getattr(run, name))
            if name in {"project_id", "thread_id"}
            else getattr(run, name)
            for name in (
                "project_id",
                "sandbox_id",
                "generation",
                "fencing_token",
                "thread_id",
                "lease_owner",
            )
        },
    }
    async with f.db.pool.acquire() as conn:
        await f.db.start_effect(conn, execution_key=attempt_key(run.id), operation=ATTEMPT)
        await f.db.save_effect_progress(conn, execution_key=attempt_key(run.id), result=record)
    sent = []

    def wire(request):
        sent.append(request)
        i, o, c, w = GOLDEN[(len(sent) - 1) % len(GOLDEN)]
        if provider_usage is not None:
            i, o, c, w = provider_usage
        return httpx.Response(
            200,
            content=result_event(
                status=response_status,
                usage=None
                if missing_usage
                else {
                    "input_tokens": i,
                    "output_tokens": o,
                    "total_tokens": i + o,
                    "input_tokens_details": {"cached_tokens": c, "cache_write_tokens": w},
                },
            ),
        )

    relay = CodexAPIRelay(
        database=f.db,
        api_key="synthetic",
        client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
    )
    app = FastAPI()
    app.state.runtime = SimpleNamespace(codex_api=relay)
    app.include_router(router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://tin.test")
    return run, relay, client, sent


async def test_paid_responses_settle_once_with_cache_write_pricing(billed):
    f = billed
    run, relay, client, sent = await paid_relay(f)
    try:
        replies = await asyncio.gather(post(client, run), post(client, run))
        assert sorted(r.status_code for r in replies) == [200, 409]
        for i in range(2):
            assert (await post(client, run, {**BODY, "input": str(i)})).status_code == 200
        assert len(sent) == 3
        assert await f.billing.settle(run.id) is None  # Still executing.
        readout = await read_run_usage(database=f.db, run=run)
        prices = [x["api_list_price"]["total_nanos"] for x in readout["own"]["observations"]]
        assert sum(prices) == 40_533_000
        assert Decimal(readout["inclusive_totals"]["known_api_list_price_usd"]) == Decimal(
            "0.040533"
        )
        assert readout["inclusive_totals"]["unpriced_observations"] == 0
        assert readout["rate_cards"][-1] == RATE_CARD
        await finish(f, run)
        assert await f.billing.settle(run.id) == 40_000_000
        assert await f.billing.settle(run.id) == 40_000_000
        charge = await f.billing.run_charge(run.id, ACTOR)
        assert charge["charged_usd"] == "0.04" and charge["released_usd"] is None
        assert charge["rate_card"] == RATE_CARD["id"]
        assert (
            await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 1
        )
        assert await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == 0
        assert not await f.db.pool.fetchval(
            "SELECT true FROM effect_receipts WHERE operation='isolated_codex_attempt_v1'"
        )
    finally:
        await client.aclose()
        await relay.close()


async def test_unknown_response_blocks_new_purchase_and_is_absorbed_after_deadline(billed):
    f = billed
    run, relay, client, sent = await paid_relay(f, missing_usage=True)
    try:
        assert (await post(client, run)).status_code == 200
        assert (await post(client, run, {**BODY, "input": "next"})).status_code == 409
        assert len(sent) == 1
        await finish(f, run)
        assert await f.billing.settle(run.id) is None
        await f.db.pool.execute(
            "UPDATE billing_run_budgets SET reconcile_by=now()-interval '1 second' WHERE run_id=$1",
            run.id,
        )
        assert await f.billing.settle(run.id) == 0
        assert await f.db.pool.fetchval("SELECT status FROM billing_operations") == "absorbed"
    finally:
        await client.aclose()
        await relay.close()


async def test_reconciliation_repairs_projection_gap_without_repurchase(billed):
    f = billed
    run, relay, client, sent = await paid_relay(f)
    original = f.billing.observe_operation
    f.billing.observe_operation = AsyncMock(side_effect=RuntimeError("projection interrupted"))
    try:
        # Streaming closes on the interruption, but the supplier receipt was already durable.
        await post(client, run)
        f.billing.observe_operation = original
        assert len(sent) == 1
        assert await f.db.pool.fetchval("SELECT status FROM billing_operations") == "pending"
        await f.db.pool.execute("UPDATE workflow_runs SET status='needs_input' WHERE id=$1", run.id)
        expected = final_charge(price_response(RATE_CARD, facts())[0], 5_000_000_000)
        assert await f.billing.settle(run.id) == expected
        assert len(sent) == 1
    finally:
        f.billing.observe_operation = original
        await client.aclose()
        await relay.close()


async def test_budget_context_and_compaction_bounds_before_dispatch(billed, caplog):
    f = billed
    run, relay, client, sent = await paid_relay(f)
    try:
        # A pinned v1 isolated run keeps its 100,000-byte request envelope.
        with caplog.at_level("WARNING", logger="tin_lite.codex_api_relay"):
            assert (await post(client, run, {**BODY, "input": "x" * 100001})).status_code == 422
            assert (await post(client, run, operation="responses/compact")).status_code == 422
            terms = json.loads(
                await f.db.pool.fetchval(
                    "SELECT terms FROM billing_run_budgets WHERE run_id=$1", run.id
                )
            )
            await f.db.pool.execute(
                "UPDATE billing_run_budgets SET terms=$2::jsonb WHERE run_id=$1",
                run.id,
                json.dumps({**terms, "codex_contract": SESSION_CONTRACT}),
            )
            response = await post(client, run)
            assert response.status_code == 422
            assert response.json()["detail"] == (
                "This API request exceeds its reserved execution contract"
            )
            await f.db.pool.execute(
                "UPDATE billing_run_budgets SET terms=$2::jsonb WHERE run_id=$1",
                run.id,
                json.dumps(terms),
            )
        reasons = [
            r.getMessage().split("reason=")[1].split()[0]
            for r in caplog.records
            if r.name == "tin_lite.codex_api_relay"
        ]
        assert reasons == ["request_too_large", "operation_not_allowed", "contract_mismatch"]
        # The first contract rejection is the attempt's recorded cause, not only a log line.
        attempt = await f.db.get_effect(attempt_key(run.id))
        assert attempt.result["relay_rejection"]["status"] == 422
        assert attempt.result["relay_rejection"]["reason"] == "request_too_large"
        await f.db.pool.execute(
            "UPDATE billing_run_budgets SET committed_nanos=$2 WHERE run_id=$1",
            run.id,
            5_000_000_000 - REQUEST_MAXIMUM + 1,
        )
        assert (await post(client, run)).status_code == 402
        assert not sent
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM effect_receipts WHERE operation=$1", USAGE
            )
            == 0
        )
    finally:
        await client.aclose()
        await relay.close()


async def test_quote_and_admission_pin_auth_across_operator_changes(billed):
    f = billed
    await fund(f)
    oauth_quote = await quote(f)
    oauth_run = await start(f, oauth_quote)
    f.settings.codex_api_projects = {f.project.id}
    with pytest.raises(BillingError, match="quote changed"):
        await start(f, oauth_quote)
    api_run = await start(f, await quote(f))
    f.settings.codex_api_projects = set()
    f.settings.luna_api_key = "synthetic"
    procedure = SimpleNamespace(sandbox=SandboxProfile(profile="isolated"))
    async with f.db.pool.acquire() as conn:
        assert (
            await select_contract(
                db=f.db, conn=conn, run=api_run, procedure=procedure, settings=f.settings
            )
            == SESSION_CONTRACT
        )
        f.settings.codex_api_projects = {f.project.id}
        assert await select_contract(
            db=f.db, conn=conn, run=oauth_run, procedure=procedure, settings=f.settings
        ) == {"mode": "chatgpt_oauth"}


@pytest.mark.parametrize("status", ["failed", "incomplete"])
@pytest.mark.parametrize("contract", [CONTRACT, SESSION_CONTRACT])
async def test_verified_supplier_usage_is_charged_even_when_generation_fails(
    billed, status, contract
):
    f = billed
    run, relay, client, sent = await paid_relay(f, response_status=status, contract=contract)
    try:
        assert (await post(client, run)).status_code == 200
        await f.db.pool.execute(
            "UPDATE workflow_runs SET status='failed', lease_active=false WHERE id=$1", run.id
        )
        assert await f.billing.settle(run.id) == 20_000_000
        assert len(sent) == 1
    finally:
        await client.aclose()
        await relay.close()


async def test_supplier_overage_is_absorbed_and_stops_further_purchases(billed):
    f = billed
    run, relay, client, sent = await paid_relay(f, provider_usage=(300000, 100, 0, 0))
    try:
        assert (await post(client, run)).status_code == 200
        assert (await post(client, run, {**BODY, "input": "next"})).status_code == 429
        row = await f.db.pool.fetchrow("SELECT * FROM billing_operations")
        assert row["observed_nanos"] == REQUEST_MAXIMUM
        observation = json.loads(row["observation"])
        assert observation["overage_absorbed_nanos"] == 1_201_500_000 - REQUEST_MAXIMUM
        await finish(f, run)
        assert await f.billing.settle(run.id) == final_charge(REQUEST_MAXIMUM, 5_000_000_000)
        assert len(sent) == 1
    finally:
        await client.aclose()
        await relay.close()


async def test_insufficient_test_funds_rejects_api_run_before_dispatch(billed):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    q = await quote(f)
    before = await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs")
    with pytest.raises(BillingError, match="Add credits"):
        await start(f, q)
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == before
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 0
