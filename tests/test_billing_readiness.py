"""Legacy and new hosted accounts share one billing authority; no paid calls."""

import pytest
from test_billing import ACTOR
from test_billing import billed as billed
from test_incremental_billing import direct
from test_procedure_publication import publication_db as publication_db


async def test_existing_funded_legacy_wallet_can_start_style_capture(billed):
    f = billed
    await f.db.pool.execute(
        "UPDATE workspaces SET created_by_clerk_user_id=NULL WHERE id=$1",
        f.project.workspace_id,
    )
    await f.db.pool.execute(
        "DELETE FROM billing_project_policies WHERE project_id=$1", f.project.id
    )
    await f.db.pool.execute("UPDATE billing_accounts SET run_billing_enabled=false")
    f.settings.billing_welcome_credits_enabled = True
    await f.db.record_tin_user(ACTOR)
    assert not (await f.billing.overview(f.project.id, ACTOR))["run_billing_enabled"]
    # This is the incident's exact starting state: credits + admin, but no creator or limits.
    f.settings.billing_hosted_defaults_enabled = True
    run = await direct(f, "style.capture", {"source_path": "style/samples.md"})
    assert run.executor == "style.capture"
    overview = await f.billing.overview(f.project.id, ACTOR)
    assert overview["enabled"] and overview["run_billing_enabled"] and overview["is_admin"]
    # The $10 welcome credit; the running capture's $2 estimate is set aside from it.
    assert (overview["set_aside_usd"], overview["available_usd"]) == ("2.00", "8.00")
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 1
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_project_policies") == 1
    await f.billing.enroll_test(f.project.workspace_id, ACTOR)
    await f.billing.ensure_hosted_projects(ACTOR)
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_welcome_grants") == 1
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger") == 1
    assert (
        await f.db.pool.fetchval(
            "SELECT created_by_clerk_user_id FROM workspaces WHERE id=$1", f.project.workspace_id
        )
        is None
    )
    assert f.calls == []


@pytest.mark.parametrize(
    "case",
    ["assigned_admin", "revoked_admin", "ambiguous_legacy", "revoked_creator", "project_only"],
)
async def test_admin_resolution_does_not_guess_or_transfer_authority(billed, case):
    f = billed
    workspace = f.project.workspace_id
    other = "user_AnotherAdmin"
    await f.db.pool.execute(
        "INSERT INTO workspace_memberships(workspace_id,clerk_user_id) VALUES($1,$2)",
        workspace,
        other,
    )
    if case == "assigned_admin":
        await f.db.pool.execute("UPDATE billing_accounts SET admin_clerk_user_id=$1", other)
    elif case == "revoked_admin":
        await f.db.pool.execute("DELETE FROM workspace_memberships WHERE clerk_user_id=$1", ACTOR)
    else:
        await f.db.pool.execute("DELETE FROM billing_project_policies")
        await f.db.pool.execute("DELETE FROM billing_accounts")
        if case != "revoked_creator":
            await f.db.pool.execute(
                "UPDATE workspaces SET created_by_clerk_user_id=NULL WHERE id=$1", workspace
            )
        if case in {"revoked_creator", "project_only"}:
            await f.db.pool.execute(
                "DELETE FROM workspace_memberships WHERE clerk_user_id=$1", ACTOR
            )
        if case == "project_only":
            await f.db.pool.execute("DELETE FROM workspace_memberships")
    async with f.db.pool.acquire() as conn:
        assert await f.billing.workspace_billing_admin(conn, workspace) == (
            other if case == "assigned_admin" else None
        )
    with pytest.raises(LookupError, match="billing administrator"):
        await f.billing.enroll_test(workspace, ACTOR)
    if case == "assigned_admin":
        await f.billing.enroll_test(workspace, other)
        assert await f.db.pool.fetchval("SELECT admin_clerk_user_id FROM billing_accounts") == other
