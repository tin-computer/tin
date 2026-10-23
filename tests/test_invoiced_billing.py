"""Invoiced workspaces: managed clients billed outside Stripe, using disposable Postgres only."""

from uuid import uuid4

import asyncpg
import pytest
from test_billing import ACTOR, finish, fund
from test_billing import billed as billed
from test_incremental_billing import direct, observe, operation
from test_procedure_publication import publication_db as publication_db

from tin_lite.billing import run_room
from tin_lite.billing_contracts import (
    NANOS_PER_CENT,
    NANOS_PER_DOLLAR,
    BillingError,
    ProjectSpendingPolicy,
    usd,
)


async def invoiced(f):
    return await f.billing.set_funding(
        f.project.workspace_id, "invoiced", reason="Managed client", operator="ops"
    )


async def charge_usage(f):
    """Run one paid audit step on an empty invoiced wallet and settle it; return the charge."""
    run = await direct(f)
    await operation(f, run, "one", 500_000_000)
    await observe(f, "one", 125_000_000)
    await finish(f, run)
    charge = await f.billing.settle(run.id)
    assert charge > 0
    return charge


def test_invoiced_room_is_capped_by_limits_alone():
    credits = {"available": -5 * NANOS_PER_DOLLAR, "invoiced": True}
    assert run_room(None, {"exposure": 0}, credits) is None
    policy = {"per_run_nanos": 10, "monthly_nanos": 100}
    assert run_room(policy, {"exposure": 95}, credits) == 5
    assert run_room(policy, {"exposure": 0}, {**credits, "invoiced": False}) < 0


async def test_invoiced_wallet_runs_without_credits_and_records_its_usage(billed):
    f = billed
    with pytest.raises(BillingError) as error:
        await direct(f)
    assert error.value.code == "insufficient_funds"
    await invoiced(f)
    charge = await charge_usage(f)
    view = await f.billing.overview(f.project.id, ACTOR)
    assert view["funding"] == "invoiced"
    assert view["used_usd"] == usd(charge)
    assert view["credits_left_usd"] == "0.00"
    assert view["workspace_spent_this_month_usd"] == usd(charge)
    assert "topup_min_cents" not in view
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 1


async def test_invoiced_wallet_keeps_project_limits(billed):
    f = billed
    await invoiced(f)
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        # One cent: below any audit's estimate, so only the limit can refuse it.
        ProjectSpendingPolicy(
            per_run_nanos=NANOS_PER_CENT, monthly_nanos=NANOS_PER_CENT, expected_revision=1
        ),
    )
    with pytest.raises(BillingError) as error:
        await direct(f)
    assert error.value.code == "project_limit"


async def test_back_to_prepaid_stops_paid_work_until_the_balance_is_cleared(billed):
    f = billed
    await invoiced(f)
    await charge_usage(f)
    changed = await f.billing.set_funding(
        f.project.workspace_id, "prepaid", reason="Contract ended", operator="ops"
    )
    assert changed["changed"]
    with pytest.raises(BillingError) as error:
        await direct(f)
    assert error.value.code == "insufficient_funds"


async def test_invoiced_wallet_shows_credits_left_until_they_run_out(billed):
    f = billed
    await fund(f, 1000)
    await invoiced(f)
    view = await f.billing.overview(f.project.id, ACTOR)
    assert (view["credits_left_usd"], view["used_usd"]) == ("10.00", "0.00")


async def test_invoiced_wallet_refuses_checkout(billed):
    f = billed
    await fund(f, 1000)
    await invoiced(f)
    with pytest.raises(BillingError) as error:
        await f.payments.checkout(
            workspace_id=f.project.workspace_id, actor=ACTOR, amount_cents=1000, request_id=uuid4()
        )
    assert error.value.code == "invoiced_account"
    # Credits paid before the switch are still spent first.
    assert (await f.billing.overview(f.project.id, ACTOR))["available_usd"] == "10.00"


async def test_funding_changes_are_audited_once_and_append_only(billed):
    f = billed
    assert (await invoiced(f))["changed"]
    assert not (await invoiced(f))["changed"]
    rows = await f.db.pool.fetch("SELECT * FROM billing_funding_changes")
    assert [(r["funding"], r["reason"], r["operator"]) for r in rows] == [
        ("invoiced", "Managed client", "ops")
    ]
    with pytest.raises(asyncpg.RaiseError):
        await f.db.pool.execute("DELETE FROM billing_funding_changes")
    with pytest.raises(BillingError):
        await f.billing.set_funding(f.project.workspace_id, "free", reason="x", operator="ops")
    with pytest.raises(BillingError):
        await f.billing.set_funding(f.project.workspace_id, "prepaid", reason=" ", operator="ops")
    with pytest.raises(LookupError):
        await f.billing.set_funding(uuid4(), "invoiced", reason="x", operator="ops")


async def test_invoice_payment_settles_unpaid_usage_once(billed):
    f = billed
    workspace = f.project.workspace_id
    with pytest.raises(BillingError) as error:
        await f.billing.record_invoice_payment(workspace, 1, reference="INV-1")
    assert error.value.code == "not_invoiced"
    await invoiced(f)
    charge = await charge_usage(f)
    with pytest.raises(BillingError) as error:
        await f.billing.record_invoice_payment(workspace, charge + 1, reference="INV-1")
    assert error.value.code == "overpayment"
    paid = await f.billing.record_invoice_payment(workspace, charge, reference="INV-1")
    assert paid == {"workspace_id": str(workspace), "recorded": True, "used_usd": "0.00"}
    again = await f.billing.record_invoice_payment(workspace, charge, reference="INV-1")
    assert not again["recorded"]
    with pytest.raises(BillingError) as error:
        await f.billing.record_invoice_payment(workspace, charge - 1, reference="INV-1")
    assert error.value.code == "ledger_conflict"
    rows = await f.db.pool.fetch(
        "SELECT amount_nanos, reference FROM billing_ledger WHERE kind='adjustment'"
    )
    assert [(r["amount_nanos"], r["reference"]) for r in rows] == [(charge, "Invoice paid: INV-1")]
