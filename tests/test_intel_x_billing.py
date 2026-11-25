"""A client's X List pages, billed to its intel.daily run through the real budget and ledger."""

from __future__ import annotations

from test_billing import ACTOR, finish, fund
from test_billing import billed as billed
from test_intel_x_list import LIST, FakeX, read
from test_procedure_publication import publication_db as publication_db
from test_service_billing import admit

from tin_lite.intel_fetch import x_list


def host_x(f, **options):
    """X on the host project (here the same fixture project, so call records have a row)."""
    x = FakeX(**options)
    x.connected.project_id, x.connected.id = f.project.id, None
    return x


async def test_x_pages_are_charged_to_the_client_run_and_settle(billed):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    await fund(f)
    run = await admit(f, "intel.daily", {})
    await f.db.pool.execute("UPDATE workflow_runs SET status='running' WHERE id=$1", run.id)
    status, kept = await read(f.db, host_x(f), run.id)
    assert status["status"] == "ok" and status["pages"] == 2 and kept
    operations = await f.db.pool.fetch(
        "SELECT id, kind, status, maximum_nanos, observed_nanos FROM billing_operations "
        "WHERE run_id=$1 ORDER BY id",
        run.id,
    )
    assert [(o["kind"], o["status"], o["maximum_nanos"]) for o in operations] == [
        ("tool", "observed", 500_000_000),
        ("tool", "observed", 500_000_000),
    ]
    # 4 posts then 2, at $0.005: $0.03, and the run's charge is that.
    assert sum(o["observed_nanos"] for o in operations) == 30_000_000
    await finish(f, run)
    assert await f.billing.settle(run.id) == 30_000_000


async def test_an_unconfirmed_page_keeps_the_charge_pending(billed):
    from tin_lite.integrations import IntegrationDeliveryUnknownError

    f = billed
    f.settings.codex_api_projects = {f.project.id}
    await fund(f)
    run = await admit(f, "intel.daily", {})
    await f.db.pool.execute("UPDATE workflow_runs SET status='running' WHERE id=$1", run.id)
    x = host_x(f, fail={"page2": IntegrationDeliveryUnknownError("X's answer was lost")})
    status, _ = await read(f.db, x, run.id)
    assert status["reason"] == "x_uncertain"
    second = x_list.page_key(run.id, LIST, 2)
    assert (
        await f.db.pool.fetchval("SELECT status FROM billing_operations WHERE id=$1", second)
        == "pending"
    )
    await finish(f, run)
    # Unknown, so not settled now; the platform's reconciliation deadline decides, never zero.
    assert await f.billing.settle(run.id) is None
    assert (await f.billing.run_charge(run.id, ACTOR))["status"] == "pending"
