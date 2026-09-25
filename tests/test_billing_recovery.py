"""Payment transport and HTTP/MCP parity; no real Stripe requests."""

from uuid import UUID, uuid4

import httpx
import pytest
from test_billing import billed as billed
from test_billing import fund
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite.billing_contracts import BillingError


async def test_saved_retry_recovers_after_last_run_is_already_pending(billed):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    await fund(f)
    configured = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=f.workflow.id,
        definition_commit_sha=f.workflow.current_commit_sha,
        name="Saved paid workflow",
        inputs={"brief": "Explain the public docs"},
        input_schema=f.workflow.definition["input_schema"],
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
        pinned_definition=f.workflow.definition,
    )
    await f.db.project_workflow_synced(
        project_workflow_id=configured.id,
        temporal_schedule_id=None,
        next_run_at=None,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        base = f"/api/projects/{f.project.id}/workflows/{configured.id}"
        response = await client.post(
            base + "/runs",
            headers={
                "Idempotency-Key": "initial",
            },
        )
        assert response.status_code == 202, response.json()
        first = UUID(response.json()["id"])
        await f.db.pool.execute(
            "UPDATE workflow_runs SET status='failed', lease_active=false WHERE id=$1", first
        )
        headers = {"Idempotency-Key": "retry"}
        retried = await client.post(base + "/retry", headers=headers)
        assert retried.status_code == 202, retried.json()
        again = await client.post(base + "/retry", headers=headers)
        assert again.status_code == 202 and again.json()["id"] == retried.json()["id"]
        assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 2
        assert await f.db.pool.fetchval("SELECT count(*) FROM billing_quotes") == 0


async def test_paid_http_and_mcp_starts_share_admission(billed, monkeypatch):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    await fund(f)
    server = mcp(f, monkeypatch)
    request_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        path = f"/api/workflows/{f.workflow.id}/runs"
        body = {"project_id": str(f.project.id), "inputs": {"brief": "Explain the public docs"}}
        response = await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": "http-start"},
        )
        assert response.status_code == 202
    arguments = {
        "project_id": str(f.project.id),
        "workflow_id": f.workflow.key,
        "inputs": body["inputs"],
        "request_id": str(request_id),
    }
    started = structured(await server.call_tool("start_workflow", arguments))
    replayed = structured(await server.call_tool("start_workflow", arguments))
    assert started["id"] == replayed["id"]
    assert started["id"] != response.json()["id"]
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 2
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_quotes") == 0


async def test_stripe_dashboard_refund_is_not_ignored(billed):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    payment, _ = await fund(f, 2500)
    event = {
        "id": "evt_dashboard_refund",
        "type": "refund.updated",
        "livemode": False,
        "data": {
            "object": {
                "id": "re_dashboard",
                "currency": "usd",
                "amount": 500,
                "payment_intent": f"pi_{payment['id']}",
                "status": "succeeded",
                "metadata": {},
            }
        },
    }
    await f.payments.handle_event(event)
    await f.payments.handle_event({**event, "id": "evt_duplicate_refund"})
    view = await f.billing.overview(f.project.id, ACTOR)
    assert view["available_usd"] == "20.00" and view["reserved_usd"] == "0.00"
    assert view["status"] == "suspended"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_refunds") == 1


async def test_dashboard_refund_failing_after_success_restores_credits(billed):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    payment, _ = await fund(f, 2500)
    obj = {
        "id": "re_async_failure",
        "currency": "usd",
        "amount": 500,
        "payment_intent": f"pi_{payment['id']}",
        "status": "succeeded",
        "metadata": {},
    }
    event = {
        "id": "evt_refund_succeeded",
        "type": "refund.updated",
        "livemode": False,
        "data": {"object": obj},
    }
    await f.payments.handle_event(event)
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "20.00"
    failed = {
        **event,
        "id": "evt_refund_failed",
        "type": "refund.failed",
        "data": {"object": {**obj, "status": "failed"}},
    }
    await f.payments.handle_event(failed)
    await f.payments.handle_event({**failed, "id": "evt_refund_failed_again"})
    view = await f.billing.overview(f.project.id, ACTOR)
    assert view["available_usd"] == "25.00" and view["reserved_usd"] == "0.00"
    assert view["status"] == "suspended"
    assert await f.db.pool.fetchval("SELECT status FROM billing_refunds") == "failed"
    assert (
        await f.db.pool.fetchval(
            "SELECT refunded_cents FROM billing_payments WHERE id=$1", UUID(payment["id"])
        )
        == 0
    )
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger") == 3


async def test_closed_dispute_inquiry_restores_credits(billed):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    payment, _ = await fund(f, 2500)
    obj = {
        "id": "dp_inquiry",
        "currency": "usd",
        "amount": 2500,
        "payment_intent": f"pi_{payment['id']}",
        "status": "warning_needs_response",
    }
    created = {
        "id": "evt_inquiry_created",
        "type": "charge.dispute.created",
        "livemode": False,
        "data": {"object": obj},
    }
    await f.payments.handle_event(created)
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "0.00"
    closed = {
        **created,
        "id": "evt_inquiry_closed",
        "type": "charge.dispute.closed",
        "data": {"object": {**obj, "status": "warning_closed"}},
    }
    await f.payments.handle_event(closed)
    await f.payments.handle_event({**closed, "id": "evt_inquiry_closed_again"})
    view = await f.billing.overview(f.project.id, ACTOR)
    assert view["available_usd"] == "25.00" and view["status"] == "suspended"
    assert await f.db.pool.fetchval("SELECT status FROM billing_disputes") == "won"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger") == 3


async def test_dispute_closed_before_created_is_idempotent(billed):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    payment, _ = await fund(f, 2500)
    obj = {
        "id": "dp_test",
        "currency": "usd",
        "amount": 2500,
        "payment_intent": f"pi_{payment['id']}",
        "status": "won",
    }
    closed = {
        "id": "evt_dispute_closed",
        "type": "charge.dispute.closed",
        "livemode": False,
        "data": {"object": obj},
    }
    await f.payments.handle_event(closed)
    await f.payments.handle_event(
        {
            **closed,
            "id": "evt_dispute_created",
            "type": "charge.dispute.created",
            "data": {"object": {**obj, "status": "needs_response"}},
        }
    )
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "25.00"
    assert await f.db.pool.fetchval("SELECT status FROM billing_disputes") == "won"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger") == 3


async def test_lost_checkout_webhook_recovers_by_verified_provider_read(billed):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    payment = await f.payments.checkout(
        workspace_id=f.project.workspace_id, actor=ACTOR, amount_cents=2500, request_id=uuid4()
    )
    await f.db.pool.execute(
        "UPDATE billing_payments SET created_at=now()-interval '1 minute' WHERE id=$1",
        UUID(payment["id"]),
    )

    def wire(request):
        assert request.method == "GET"
        assert request.url.path == f"/v1/checkout/sessions/cs_test_{payment['id']}"
        return httpx.Response(
            200,
            json={
                "id": f"cs_test_{payment['id']}",
                "livemode": False,
                "currency": "usd",
                "amount_total": 2500,
                "mode": "payment",
                "status": "complete",
                "payment_status": "paid",
                "payment_intent": f"pi_{payment['id']}",
                "metadata": {"tin_product": "tin-lite", "tin_payment_id": payment["id"]},
            },
        )

    f.payments.transport = httpx.MockTransport(wire)
    await f.payments.reconcile()
    await f.payments.reconcile()
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "25.00"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger") == 1


async def test_parked_or_failing_checkouts_do_not_starve_lost_webhook_recovery(billed):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    # Session-less requests past Stripe's retry window await operator reconciliation.
    await f.db.pool.execute(
        """INSERT INTO billing_payments(id,workspace_id,actor_clerk_user_id,request_id,
           amount_cents,created_at)
           SELECT gen_random_uuid(),$1,$2,gen_random_uuid(),2500,now()-interval '25 hours'
           FROM generate_series(1,20)""",
        f.project.workspace_id,
        ACTOR,
    )
    failing = await f.db.pool.fetchval(
        """INSERT INTO billing_payments(id,workspace_id,actor_clerk_user_id,request_id,
           amount_cents,created_at)
           VALUES(gen_random_uuid(),$1,$2,gen_random_uuid(),2500,now()-interval '1 hour')
           RETURNING id""",
        f.project.workspace_id,
        ACTOR,
    )
    payment = await f.payments.checkout(
        workspace_id=f.project.workspace_id, actor=ACTOR, amount_cents=2500, request_id=uuid4()
    )
    await f.db.pool.execute(
        "UPDATE billing_payments SET created_at=now()-interval '1 minute' WHERE id=$1",
        UUID(payment["id"]),
    )

    def wire(request):
        if "/products" in request.url.path:
            return httpx.Response(500, json={})
        assert request.method == "GET"
        assert request.url.path == f"/v1/checkout/sessions/cs_test_{payment['id']}"
        return httpx.Response(
            200,
            json={
                "id": f"cs_test_{payment['id']}",
                "livemode": False,
                "currency": "usd",
                "amount_total": 2500,
                "mode": "payment",
                "status": "complete",
                "payment_status": "paid",
                "payment_intent": f"pi_{payment['id']}",
                "metadata": {"tin_product": "tin-lite", "tin_payment_id": payment["id"]},
            },
        )

    f.payments.transport = httpx.MockTransport(wire)
    # The failing request still surfaces, after every other row has been reconciled.
    with pytest.raises(BillingError, match="Stripe could not complete"):
        await f.payments.reconcile()
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "25.00"
    assert (
        await f.db.pool.fetchval("SELECT status FROM billing_payments WHERE id=$1", failing)
        == "pending"
    )
