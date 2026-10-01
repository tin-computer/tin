"""Real disposable Postgres and Stripe wire fixtures; never live payments."""

import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_private_workflows import ACTOR, activate, app, fixture, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite.billing import BillingService
from tin_lite.billing_contracts import (
    NANOS_PER_CENT,
    NANOS_PER_DOLLAR,
    BillingError,
    ProjectSpendingPolicy,
    final_charge,
    token_charge,
)
from tin_lite.billing_payments import PRODUCT_ID, StripePayments
from tin_lite.run_service import start_workflow_run


@pytest.fixture
async def billed(publication_db):
    f = await fixture(publication_db)
    # Legacy ledger fixtures retain their pre-API quote and settlement contracts.
    f.settings.codex_api_projects = set()
    await activate(f)
    f.workflow = next(
        w
        for w in await f.db.list_workflows(project_id=f.project.id)
        if w.key == "custom.research_digest"
    )
    f.settings.billing_test_enabled = True
    f.settings.billing_enabled = True
    f.settings.stripe_secret_key = SecretStr("sk_test_fixture")
    f.settings.stripe_webhook_secret = SecretStr("whsec_fixture")
    f.billing = BillingService(database=f.db, settings=f.settings)
    f.db.billing = f.billing
    await f.db.pool.execute(
        "UPDATE workspaces SET created_by_clerk_user_id=$2 WHERE id=$1",
        f.project.workspace_id,
        ACTOR,
    )
    await f.db.pool.execute(
        "INSERT INTO workspace_memberships(workspace_id,clerk_user_id) VALUES($1,$2)",
        f.project.workspace_id,
        ACTOR,
    )
    await f.billing.enroll_test(f.project.workspace_id, ACTOR)
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=50 * NANOS_PER_DOLLAR,
            monthly_nanos=500 * NANOS_PER_DOLLAR,
            concurrency=5,
            expected_revision=0,
        ),
    )
    f.calls = []

    def stripe_wire(request):
        f.calls.append(request)
        if "/products" in request.url.path:
            return httpx.Response(
                200,
                json={"id": PRODUCT_ID, "livemode": False, "metadata": {"tin_product": "tin-lite"}},
            )
        if request.url.path.endswith("/checkout/sessions"):
            data = parse_qs(request.content.decode())
            pid = data["metadata[tin_payment_id]"][0]
            return httpx.Response(
                200,
                json={
                    "id": f"cs_test_{pid}",
                    "livemode": False,
                    "url": f"https://checkout.stripe.com/test/{pid}",
                },
            )
        if request.url.path.endswith("/refunds"):
            data = parse_qs(request.content.decode())
            rid = data["metadata[tin_refund_id]"][0]
            return httpx.Response(
                200,
                json={
                    "id": f"re_{rid}",
                    "status": "pending",
                    "amount": int(data["amount"][0]),
                    "currency": "usd",
                    "payment_intent": data["payment_intent"][0],
                    "metadata": {"tin_product": "tin-lite", "tin_refund_id": rid},
                },
            )
        raise AssertionError(request.url.path)

    f.payments = StripePayments(
        billing=f.billing, settings=f.settings, transport=httpx.MockTransport(stripe_wire)
    )
    return f


async def fund(f, cents=10000):
    checkout = await f.payments.checkout(
        workspace_id=f.project.workspace_id, actor=ACTOR, amount_cents=cents, request_id=uuid4()
    )
    event = {
        "id": f"evt_{checkout['id']}",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": f"cs_test_{checkout['id']}",
                "mode": "payment",
                "livemode": False,
                "currency": "usd",
                "amount_total": cents,
                "payment_status": "paid",
                "payment_intent": f"pi_{checkout['id']}",
                "metadata": {"tin_product": "tin-lite", "tin_payment_id": checkout["id"]},
            }
        },
    }
    await f.payments.handle_event(event)
    return checkout, event


async def quote(f):
    return await f.billing.quote(
        runtime=f.runtime,
        project_id=f.project.id,
        actor=ACTOR,
        workflow_id=f.workflow.id,
        inputs={"brief": "Explain the public docs"},
    )


async def legacy_quote(f):
    """Model a quote issued before incremental funding; its binding stays valid."""
    q = await quote(f)
    terms = {k: v for k, v in q["terms"].items() if k not in {"funding", "estimate"}}
    await f.db.pool.execute(
        "UPDATE billing_quotes SET terms=$2::jsonb WHERE id=$1", UUID(q["id"]), json.dumps(terms)
    )
    return q


async def start(f, q, request_id=None):
    if q["terms"]["kind"] == "isolated_codex":
        # Seed a historical admission directly: new product starts cannot select OAuth.
        # This exercises the original reservation/idempotency/settlement SQL without compute.
        from tin_lite.workflow_inputs import normalize_workflow_inputs

        run, _ = await f.db.create_run(
            project_id=f.project.id,
            workflow_id=f.workflow.id,
            started_by_clerk_user_id=ACTOR,
            input_payload=normalize_workflow_inputs(
                schema=f.workflow.definition["input_schema"],
                project_id=f.project.id,
                inputs={"brief": "Explain the public docs"},
            ),
            start_idempotency_key=request_id or str(uuid4()),
            definition_commit_sha=f.workflow.current_commit_sha,
            pinned_definition=f.workflow.definition,
            billing_quote_id=UUID(q["id"]),
        )
        return run
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={"brief": "Explain the public docs"},
        start_idempotency_key=request_id or str(uuid4()),
        billing_quote_id=UUID(q["id"]),
    )


async def finish(f, run):
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', lease_active=false WHERE id=$1", run.id
    )


async def test_checkout_and_webhook_are_separate_idempotent_steps(billed):
    f = billed
    f.settings.app_url = "https://app.tin.test"
    request = uuid4()
    first = await f.payments.checkout(
        workspace_id=f.project.workspace_id, actor=ACTOR, amount_cents=2500, request_id=request
    )
    second = await f.payments.checkout(
        workspace_id=f.project.workspace_id, actor=ACTOR, amount_cents=2500, request_id=request
    )
    assert first == second
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "0.00"
    _, event = await fund(f, 2500)
    await asyncio.gather(*(f.payments.handle_event(event) for _ in range(4)))
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "25.00"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger") == 1
    data = parse_qs(
        next(
            call.content.decode()
            for call in f.calls
            if call.url.path.endswith("/checkout/sessions")
        )
    )
    assert data["invoice_creation[enabled]"] == ["true"]
    assert data["success_url"] == [f"https://app.tin.test/billing?billing_payment={first['id']}"]
    assert data["cancel_url"] == data["success_url"]
    assert data["line_items[0][price_data][product]"] == [PRODUCT_ID]
    assert all(call.headers["Authorization"] == "Bearer sk_test_fixture" for call in f.calls)


async def test_checkout_return_resolves_workspace_without_payment_effects(billed):
    f = billed
    payment = await f.payments.checkout(
        workspace_id=f.project.workspace_id,
        actor=ACTOR,
        amount_cents=1000,
        request_id=uuid4(),
    )
    calls = len(f.calls)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        path = f"/api/billing/payments/{payment['id']}/return"
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json() == {"workspace_id": str(f.project.workspace_id)}
        assert response.headers["Cache-Control"] == "no-store"
        assert (await client.get(f"/api/billing/payments/{uuid4()}/return")).status_code == 404
        assert (await client.get("/api/billing/payments/invalid/return")).status_code == 422
    assert len(f.calls) == calls, "return lookup must not call Stripe"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger") == 0
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "0.00"

    actor = "user_projectmember"
    await f.db.record_tin_user(actor)
    await f.db.grant_project_membership(project_id=f.project.id, clerk_user_id=actor)
    for reader in (actor, "user_stranger"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app(f, reader)), base_url="https://tin.test"
        ) as client:
            assert (await client.get(path)).status_code == 404
    await f.db.pool.execute(
        "DELETE FROM workspace_memberships WHERE workspace_id=$1 AND clerk_user_id=$2",
        f.project.workspace_id,
        ACTOR,
    )
    with pytest.raises(LookupError):
        await f.payments.checkout_return(UUID(payment["id"]), ACTOR)


async def test_live_or_other_product_webhooks_cannot_fund_account(billed):
    f = billed
    _, event = await fund(f)
    before = await f.db.pool.fetchval("SELECT balance_nanos FROM billing_accounts")
    for changes in ({"livemode": True}, {"metadata": {"tin_product": "strangeloop"}}):
        copied = json.loads(json.dumps(event))
        copied["id"] = str(uuid4())
        if "livemode" in changes:
            copied["livemode"] = True
        else:
            copied["data"]["object"].update(changes)
        await f.payments.handle_event(copied)
    assert await f.db.pool.fetchval("SELECT balance_nanos FROM billing_accounts") == before


async def test_quote_reserve_settle_and_duplicate_start(billed):
    f = billed
    await fund(f)
    q = await legacy_quote(f)
    request = str(uuid4())
    run = await start(f, q, request)
    same = await start(f, q, request)
    assert run.id == same.id
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "90.00"
    async with f.db.pool.acquire() as conn:
        await f.billing.begin_operation(
            conn,
            run_id=run.id,
            operation_id="usage-one",
            kind="isolated_codex",
            maximum=9 * NANOS_PER_DOLLAR,
        )
        await f.billing.observe_operation(
            conn, operation_id="usage-one", nanos=480_000_000, observation={"outcome": "observed"}
        )
    await finish(f, run)
    assert await f.billing.settle(run.id) == 500_000_000
    assert await f.billing.settle(run.id) == 500_000_000
    charge = await f.billing.run_charge(run.id, ACTOR)
    assert charge["charged_usd"] == "0.50" and charge["released_usd"] == "9.50"
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "99.50"


async def test_last_balance_concurrent_starts_cannot_overspend(billed):
    f = billed
    await fund(f, 1000)
    quotes = [await legacy_quote(f), await legacy_quote(f)]
    results = await asyncio.gather(*(start(f, q) for q in quotes), return_exceptions=True)
    assert sum(isinstance(value, BillingError) for value in results) == 1
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 1
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "0.00"


async def test_expired_quote_or_reused_quote_does_not_create_another_run(billed):
    f = billed
    await fund(f)
    q = await quote(f)
    await start(f, q)
    with pytest.raises(BillingError, match="already belongs"):
        await start(f, q)
    expired = await quote(f)
    await f.db.pool.execute(
        "UPDATE billing_quotes SET expires_at=now()-interval '1 second' WHERE id=$1",
        UUID(expired["id"]),
    )
    with pytest.raises(BillingError, match="expired"):
        await start(f, expired)
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 1


async def test_unknown_usage_pending_then_absorbed_not_billed(billed):
    f = billed
    await fund(f)
    run = await start(f, await quote(f))
    async with f.db.pool.acquire() as conn:
        await f.billing.begin_operation(
            conn,
            run_id=run.id,
            operation_id="unknown",
            kind="isolated_codex",
            maximum=NANOS_PER_DOLLAR,
        )
    await finish(f, run)
    assert await f.billing.settle(run.id) is None
    assert (await f.billing.run_charge(run.id, ACTOR))["status"] == "pending"
    await f.db.pool.execute(
        "UPDATE billing_run_budgets SET reconcile_by=now()-interval '1 second' WHERE run_id=$1",
        run.id,
    )
    assert await f.billing.settle(run.id) == 0
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "100.00"


async def test_zero_work_cancellation_and_overages(billed):
    f = billed
    await fund(f)
    run = await start(f, await quote(f))
    await finish(f, run)
    assert await f.billing.settle(run.id) == 0
    run = await start(f, await quote(f))
    async with f.db.pool.acquire() as conn:
        await f.billing.begin_operation(
            conn,
            run_id=run.id,
            operation_id="over",
            kind="isolated_codex",
            maximum=NANOS_PER_DOLLAR,
        )
        await f.billing.observe_operation(
            conn, operation_id="over", nanos=200 * NANOS_PER_DOLLAR, observation={}
        )
    await finish(f, run)
    assert await f.billing.settle(run.id) == 1_020_000_000


async def test_unauthorized_users_cannot_read_billing_or_change_limits(billed):
    f = billed
    with pytest.raises(LookupError):
        await f.billing.overview(f.project.id, "user_stranger")
    with pytest.raises(LookupError):
        await f.payments.checkout(
            workspace_id=f.project.workspace_id,
            actor="user_stranger",
            amount_cents=1000,
            request_id=uuid4(),
        )
    assert not f.calls


async def test_member_sees_project_spending_not_workspace_balance(billed):
    f = billed
    actor = "user_member"
    await f.db.record_tin_user(actor)
    await f.db.grant_project_membership(project_id=f.project.id, clerk_user_id=actor)
    await fund(f)
    view = await f.billing.overview(f.project.id, actor)
    assert not view["is_admin"] and "available_usd" not in view and view["transactions"] == []
    assert len(view["policies"]) == 1


async def test_refund_hold_then_success_and_duplicate_event(billed):
    f = billed
    payment, _ = await fund(f, 2500)
    refund = await f.payments.request_refund(UUID(payment["id"]), ACTOR, uuid4(), 1000)
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "15.00"
    event = {
        "id": "evt_refund",
        "type": "refund.updated",
        "livemode": False,
        "data": {
            "object": {
                "id": f"re_{refund['id']}",
                "status": "succeeded",
                "amount": 1000,
                "currency": "usd",
                "payment_intent": f"pi_{payment['id']}",
                "metadata": {"tin_product": "tin-lite", "tin_refund_id": refund["id"]},
            }
        },
    }
    await f.payments.handle_event(event)
    await f.payments.handle_event(event)
    view = await f.billing.overview(f.project.id, ACTOR)
    assert view["available_usd"] == "15.00" and view["reserved_usd"] == "0.00"
    assert len(view["transactions"]) == 2


async def test_signature_and_http_mcp_read_parity(billed, monkeypatch):
    f = billed
    await fund(f)
    body = json.dumps(
        {
            "id": "evt_unknown",
            "object": "event",
            "type": "unknown",
            "livemode": False,
            "data": {"object": {}},
        }
    ).encode()
    stamp = int(time.time())
    signature = hmac.new(b"whsec_fixture", f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    assert await f.payments.webhook(body, f"t={stamp},v1={signature}") == {
        "received": True,
        "ignored": True,
    }
    with pytest.raises(BillingError, match="Invalid Stripe signature"):
        await f.payments.webhook(body, "bad")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get(f"/api/projects/{f.project.id}/billing")
    server = mcp(f, monkeypatch)
    remote = structured(
        await server.call_tool("get_project_spending", {"project_id": str(f.project.id)})
    )
    assert response.status_code == 200 and response.json() == remote


def test_round_only_once_and_unknown_is_not_free():
    assert final_charge(4_900_000, NANOS_PER_DOLLAR) == 0
    assert final_charge(5_000_000, NANOS_PER_DOLLAR) == NANOS_PER_CENT
    assert token_charge({}, "native_model", {}) is None


async def native_run(f):
    from tin_lite.catalog import BUILTIN_WORKFLOWS

    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == "creative.character")
    workflow = await f.db.upsert_registry_workflow(
        workflow_id=spec.id,
        key=spec.key,
        title=spec.title,
        description=spec.description,
        executor=spec.executor,
        definition_repo_id="registry/workflows",
        definition_path="creative.character.json",
        current_commit_sha="c" * 40,
        version_label=spec.version_label,
        definition=spec.definition,
    )
    inputs = {"slug": "tin-test", "brief": "A friendly robot"}
    q = await f.billing.quote(
        runtime=f.runtime,
        project_id=f.project.id,
        actor=ACTOR,
        workflow_id=workflow.id,
        inputs=inputs,
    )
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=inputs,
        billing_quote_id=UUID(q["id"]),
        start_idempotency_key=str(uuid4()),
    )


@pytest.mark.parametrize("lost_billing_write", [False, True])
async def test_legacy_native_usage_and_receipt_recovery_are_charged_once(
    billed, monkeypatch, lost_billing_write
):
    from test_model_service import REQUEST, ROUTE, provider_for, response_body

    from tin_lite.model_providers import ModelProviderError
    from tin_lite.model_usage import ModelUsageRecorder, model_usage_scope

    f = billed
    await fund(f)
    # A pre-migration quote pins the illustrative tariff, not today's supplier card.
    from tin_lite.billing_contracts import test_terms

    monkeypatch.setattr(
        f.billing, "terms", lambda definition, project_id, inputs=None: test_terms(definition)
    )
    run = await native_run(f)
    calls = []

    def wire(request):
        calls.append(request)
        return httpx.Response(200, json=response_body())

    provider = provider_for(wire)
    recorder = ModelUsageRecorder(f.db)
    original = f.billing.observe_operation

    async def interrupted(*args, **kwargs):
        raise RuntimeError("switchboard interrupted after durable usage")

    if lost_billing_write:
        monkeypatch.setattr(f.billing, "observe_operation", interrupted)
    try:
        async with f.db.pool.acquire() as conn:
            with model_usage_scope(run_id=run.id, step="test-native", conn=conn):
                if lost_billing_write:
                    with pytest.raises(RuntimeError, match="interrupted"):
                        await recorder.generate(
                            ROUTE,
                            REQUEST,
                            lambda: provider.generate(model=ROUTE.model, request=REQUEST),
                        )
                else:
                    await recorder.generate(
                        ROUTE,
                        REQUEST,
                        lambda: provider.generate(model=ROUTE.model, request=REQUEST),
                    )
                with pytest.raises(ModelProviderError, match="already attempted"):
                    await recorder.generate(
                        ROUTE,
                        REQUEST,
                        lambda: provider.generate(model=ROUTE.model, request=REQUEST),
                    )
        monkeypatch.setattr(f.billing, "observe_operation", original)
        await finish(f, run)
        await f.billing.reconcile()
        assert (await f.billing.run_charge(run.id, ACTOR))["charged_usd"] == "0.02"
        assert len(calls) == 1
        assert (
            await f.db.pool.fetchval(
                "SELECT status FROM billing_operations WHERE run_id=$1", run.id
            )
            == "observed"
        )
    finally:
        await provider.close()


@pytest.mark.parametrize("failed", [False, True])
async def test_codex_final_usage_bills_even_when_result_fails(billed, failed):
    from tin_lite.codex_usage import record_codex_attempt

    f = billed
    await fund(f)
    run = await start(f, await quote(f))

    async def attempt(observed):
        await observed(
            {
                "final": True,
                "model": "test-codex",
                "total": {
                    "inputTokens": 1000,
                    "cachedInputTokens": 0,
                    "cacheWriteInputTokens": 0,
                    "outputTokens": 1000,
                },
            }
        )
        if failed:
            raise ValueError("artifact did not pass validation")
        return "checkpoint"

    async with f.db.pool.acquire() as conn:
        args = dict(
            db=f.db,
            conn=conn,
            run=run,
            execution_key=f"{run.id}:procedure",
            sandbox_id="test-sandbox",
            timeout_seconds=60,
            call=attempt,
        )
        if failed:
            with pytest.raises(ValueError, match="validation"):
                await record_codex_attempt(**args)
        else:
            assert await record_codex_attempt(**args) == "checkpoint"
        with pytest.raises(RuntimeError, match="already exists"):
            await record_codex_attempt(**args)
    await finish(f, run)
    assert await f.billing.settle(run.id) == 7 * NANOS_PER_CENT


async def test_duplicate_observation_and_ledger_immutability(billed):
    import asyncpg

    f = billed
    await fund(f)
    run = await start(f, await quote(f))
    async with f.db.pool.acquire() as conn:
        await f.billing.begin_operation(
            conn,
            run_id=run.id,
            operation_id="raced-observation",
            kind="isolated_codex",
            maximum=NANOS_PER_DOLLAR,
        )

    async def observe():
        async with f.db.pool.acquire() as conn:
            await f.billing.observe_operation(
                conn, operation_id="raced-observation", nanos=480_000_000, observation={}
            )

    await asyncio.gather(*(observe() for _ in range(4)))
    assert (
        await f.db.pool.fetchval(
            "SELECT committed_nanos FROM billing_run_budgets WHERE run_id=$1", run.id
        )
        == 480_000_000
    )
    await finish(f, run)
    assert await f.billing.settle(run.id) == 500_000_000
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await f.db.pool.execute("UPDATE billing_ledger SET amount_nanos=0 WHERE kind='charge'")


async def test_revoked_membership_and_lowered_limits_stop_new_paid_units(billed):
    f = billed
    await fund(f)
    run = await start(f, await quote(f))
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=50 * NANOS_PER_DOLLAR,
            monthly_nanos=NANOS_PER_DOLLAR,
            concurrency=5,
            expected_revision=1,
        ),
    )
    async with f.db.pool.acquire() as conn:
        with pytest.raises(BillingError, match="Current project limits"):
            await f.billing.begin_operation(
                conn,
                run_id=run.id,
                operation_id="limited",
                kind="isolated_codex",
                maximum=NANOS_PER_DOLLAR,
            )
        await conn.execute("DELETE FROM project_memberships WHERE project_id=$1", f.project.id)
        with pytest.raises(LookupError):
            await f.billing.begin_operation(
                conn,
                run_id=run.id,
                operation_id="revoked",
                kind="isolated_codex",
                maximum=NANOS_PER_DOLLAR,
            )
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0


async def test_unknown_temporal_start_keeps_reservation_and_recovers_same_id(billed):
    from temporalio.common import WorkflowIDReusePolicy

    from tin_lite.billing_recovery import recover_dispatches
    from tin_lite.run_service import TemporalStartError

    f = billed
    f.settings.codex_api_projects = {f.project.id}
    await fund(f)
    q = await quote(f)
    f.runtime.temporal.start_workflow.side_effect = TimeoutError()
    with pytest.raises(TemporalStartError) as error:
        await start(f, q, "unknown-start")
    run = await f.db.get_run(error.value.run_id)
    assert run.status == "pending"
    assert (await f.billing.run_charge(run.id, ACTOR))["status"] == "in_progress"
    await f.db.pool.execute(
        "UPDATE workflow_runs SET created_at=now()-interval '1 minute' WHERE id=$1", run.id
    )
    f.runtime.temporal.start_workflow.side_effect = None
    await recover_dispatches(f.runtime, f.settings)
    kwargs = f.runtime.temporal.start_workflow.call_args.kwargs
    assert kwargs["id"] == run.temporal_workflow_id
    assert kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert (await start(f, q, "unknown-start")).id == run.id
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 1


async def test_refund_cannot_return_already_consumed_payment(billed):
    f = billed
    payment, _ = await fund(f, 1000)
    run = await start(f, await quote(f))
    async with f.db.pool.acquire() as conn:
        await f.billing.begin_operation(
            conn,
            run_id=run.id,
            operation_id="consume",
            kind="isolated_codex",
            maximum=9 * NANOS_PER_DOLLAR,
        )
        await f.billing.observe_operation(
            conn, operation_id="consume", nanos=480_000_000, observation={}
        )
    await finish(f, run)
    await f.billing.settle(run.id)
    await fund(f, 2500)  # New money cannot make the spent portion of an old top-up refundable.
    with pytest.raises(BillingError, match="not available"):
        await f.payments.request_refund(UUID(payment["id"]), ACTOR, uuid4(), 1000)
    assert (
        await f.db.pool.fetchval(
            "SELECT consumed_cents FROM billing_payments WHERE id=$1", UUID(payment["id"])
        )
        == 50
    )


async def test_final_review_does_not_hold_unused_money_or_change_approval(billed):
    f = billed
    await fund(f)
    run = await start(f, await quote(f))
    async with f.db.pool.acquire() as conn:
        await f.billing.begin_operation(
            conn,
            run_id=run.id,
            operation_id="before-review",
            kind="isolated_codex",
            maximum=NANOS_PER_DOLLAR,
        )
        await f.billing.observe_operation(
            conn, operation_id="before-review", nanos=480_000_000, observation={}
        )
        await conn.execute(
            "UPDATE workflow_runs SET status='needs_input', lease_active=true WHERE id=$1", run.id
        )
    await f.billing.reconcile()
    assert (await f.billing.run_charge(run.id, ACTOR))["charged_usd"] == "0.50"
    waiting = await f.db.get_run(run.id)
    assert waiting.status == "needs_input" and waiting.lease_active
    async with f.db.pool.acquire() as conn:
        with pytest.raises(BillingError, match="No additional paid work"):
            await f.billing.begin_operation(
                conn,
                run_id=run.id,
                operation_id="after-review",
                kind="isolated_codex",
                maximum=NANOS_PER_DOLLAR,
            )


async def test_ads_launch_budget_stays_open_while_awaiting_approval(billed):
    f = billed
    await fund(f)
    run = await start(f, await quote(f))
    async with f.db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE workflow_runs SET status='needs_input', executor='ads.launch' WHERE id=$1",
            run.id,
        )
    await f.billing.reconcile()
    assert (
        await f.db.pool.fetchval("SELECT status FROM billing_run_budgets WHERE run_id=$1", run.id)
        == "reserved"
    )
    async with f.db.pool.acquire() as conn:
        await conn.execute("UPDATE workflow_runs SET status='running' WHERE id=$1", run.id)
        await f.billing.begin_operation(
            conn,
            run_id=run.id,
            operation_id="after-approval",
            kind="isolated_codex",
            maximum=0,
        )


async def test_roots_waiting_on_unknown_usage_do_not_hold_up_settlement(billed, monkeypatch):
    f = billed
    await fund(f)
    waiting = await start(f, await quote(f))
    async with f.db.pool.acquire() as conn:
        await f.billing.begin_operation(
            conn,
            run_id=waiting.id,
            operation_id="unconfirmed",
            kind="isolated_codex",
            maximum=NANOS_PER_DOLLAR,
        )
    await finish(f, waiting)
    assert await f.billing.settle(waiting.id) is None  # unknown usage: waits for reconcile_by
    done = await start(f, await quote(f))
    async with f.db.pool.acquire() as conn:
        await f.billing.begin_operation(
            conn,
            run_id=done.id,
            operation_id="observed",
            kind="isolated_codex",
            maximum=9 * NANOS_PER_DOLLAR,
        )
        await f.billing.observe_operation(
            conn, operation_id="observed", nanos=480_000_000, observation={"outcome": "observed"}
        )
    await finish(f, done)
    # The waiting root has the earlier deadline, and one slot stands in for a full batch.
    await f.db.pool.execute(
        "UPDATE billing_run_budgets SET reconcile_by = now() + interval '1 hour' WHERE run_id=$1",
        waiting.id,
    )
    await f.db.pool.execute(
        "UPDATE billing_run_budgets SET reconcile_by = now() + interval '2 hours' WHERE run_id=$1",
        done.id,
    )
    monkeypatch.setattr(f.billing, "RECONCILE_BATCH", 1)
    await f.billing.reconcile()
    status = "SELECT status FROM billing_run_budgets WHERE run_id=$1"
    assert await f.db.pool.fetchval(status, done.id) == "settled"
    assert await f.db.pool.fetchval(status, waiting.id) == "pending"


async def test_run_admission_does_not_block_the_ledgers_reference_to_the_project(billed):
    from tin_lite.db import PROJECT_ADMISSION_LOCK

    f = billed
    async with f.db.pool.acquire() as admission, f.db.pool.acquire() as settlement:
        async with admission.transaction():
            # Admission holds the project row, as create_run does before locking the account.
            assert await admission.fetchval(PROJECT_ADMISSION_LOCK, f.project.id)
            async with settlement.transaction():
                await settlement.execute("SET LOCAL lock_timeout = '2s'")
                # Settlement, holding the account, inserts ledger rows that reference the
                # project; their foreign-key check takes this lock on the project row.
                assert await settlement.fetchval(
                    "SELECT true FROM projects WHERE id = $1 FOR KEY SHARE", f.project.id
                )
