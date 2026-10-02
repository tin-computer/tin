"""Who can read and change billing: deleted projects, run existence and workspace ledgers."""

from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from test_billing import ACTOR, fund
from test_billing import billed as billed
from test_incremental_billing import direct
from test_private_workflows import app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite.billing_contracts import ProjectSpendingPolicy

STRANGER = "user_billingstranger"


async def refusal(server, tool, arguments):
    with pytest.raises(ToolError) as error:
        await server.call_tool(tool, arguments)
    assert not isinstance(error.value, UnexpectedToolError), repr(error.value.__cause__)
    return str(error.value)


async def test_another_projects_run_reads_like_a_missing_run(billed, monkeypatch):
    f = billed
    await fund(f)
    run = await direct(f)
    server = mcp(f, monkeypatch, actor=STRANGER)
    for tool in (
        "get_run",
        "get_run_usage",
        "get_run_charge",
        "read_run_output",
        "stop_procedure",
        "stop_organic_audit",
        "get_workflow_review",
        "approve_workflow_run",
    ):
        foreign = await refusal(server, tool, {"run_id": str(run.id)})
        missing = await refusal(server, tool, {"run_id": str(uuid4())})
        assert foreign == missing, tool
        assert "project not found" not in foreign, tool
    assert (await refusal(server, "get_run", {"run_id": str(run.id)})).endswith(
        ": not_found: run not found"
    )
    for proposal_tool in ("approve_paid_ads_proposal", "discard_paid_ads_proposal"):
        assert (await refusal(server, proposal_tool, {"proposal_id": str(uuid4())})).endswith(
            ": not_found: proposal not found"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, actor=STRANGER)), base_url="https://tin.test"
    ) as client:
        for path in ("/api/workflows/runs/{}", "/api/workflows/runs/{}/charge"):
            foreign = await client.get(path.format(run.id))
            missing = await client.get(path.format(uuid4()))
            assert foreign.status_code == missing.status_code == 404
            assert foreign.json() == missing.json()
            assert "project" not in foreign.json()["detail"]
    # The member still reads it.
    assert structured(await mcp(f, monkeypatch).call_tool("get_run", {"run_id": str(run.id)}))


async def test_a_deleted_project_has_no_billing_surface(billed):
    f = billed
    await fund(f)
    run = await direct(f)
    revision = await f.db.pool.fetchval(
        "SELECT revision FROM billing_project_policies WHERE project_id=$1", f.project.id
    )
    # Deletion removes memberships in the same transaction; a membership an operator grants
    # afterwards must still not reopen the tombstoned project's billing.
    await f.db.pool.execute(
        "UPDATE projects SET deleted_at=now(), deleted_by_clerk_user_id=$2 WHERE id=$1",
        f.project.id,
        ACTOR,
    )
    with pytest.raises(LookupError, match="project not found"):
        await f.billing.overview(f.project.id, ACTOR)
    with pytest.raises(LookupError, match="run not found"):
        await f.billing.run_charge(run.id, ACTOR)
    with pytest.raises(LookupError, match="project not found"):
        await f.billing.quote(
            runtime=f.runtime, project_id=f.project.id, actor=ACTOR, workflow_id=f.workflow.id
        )
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_quotes") == 0
    with pytest.raises(LookupError, match="project not found"):
        await f.billing.update_policy(
            f.project.id,
            ACTOR,
            ProjectSpendingPolicy(
                per_run_nanos=1_000_000_000_000,
                monthly_nanos=1_000_000_000_000,
                expected_revision=revision,
            ),
        )
    assert (
        await f.db.pool.fetchval(
            "SELECT revision FROM billing_project_policies WHERE project_id=$1", f.project.id
        )
        == revision
    )


async def test_workspace_transactions_say_which_project_they_belong_to(billed):
    f = billed
    await fund(f)
    sibling = await f.db.create_workspace_project(
        workspace_id=f.project.workspace_id,
        project_id=uuid4(),
        name="Sibling",
        state_repo_id="projects/sibling-ledger",
        clerk_user_id=ACTOR,
        request_id=uuid4(),
    )
    async with f.db.pool.acquire() as conn, conn.transaction():
        account = await conn.fetchrow(
            "SELECT * FROM billing_accounts WHERE workspace_id=$1 FOR UPDATE",
            f.project.workspace_id,
        )
        for project_id in (f.project.id, sibling.id):
            await f.billing.post_ledger(
                conn,
                account=account,
                event_key=f"test:{project_id}",
                kind="adjustment",
                amount=-10_000_000,
                project_id=project_id,
            )

    async def scopes():
        view = await f.billing.overview(f.project.id, ACTOR)
        assert view["is_admin"]
        return sorted((t["scope"], t["project_id"]) for t in view["transactions"])

    assert await scopes() == sorted(
        [
            ("workspace", None),
            ("project", str(f.project.id)),
            ("other_project", str(sibling.id)),
        ]
    )
    # The billing admin sees a sibling project's spending, but not which project it is
    # unless they are its member, as with its run IDs.
    await f.db.pool.execute(
        "DELETE FROM project_memberships WHERE project_id=$1 AND clerk_user_id=$2",
        sibling.id,
        ACTOR,
    )
    assert await scopes() == sorted(
        [("workspace", None), ("project", str(f.project.id)), ("other_project", None)]
    )
