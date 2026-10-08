from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from test_technical_fix_sources import content_source_fixture, source_fixture

from tin_lite.auth import AuthContext, require_user
from tin_lite.mcp_server import create_mcp_app
from tin_lite.technical_fix_api import router, system_router


@pytest.fixture
async def surface_fixture(monkeypatch, request):
    f = (
        content_source_fixture()
        if getattr(request, "param", None) == "content"
        else source_fixture()
    )
    token = SimpleNamespace(subject="outsider", scopes=["openid"], client_id="test_client")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)

    async def access(*, project_id, clerk_user_id):
        return clerk_user_id == "member" and project_id == f.project.id

    f.db.has_project_access = access
    f.db.record_mcp_usage = AsyncMock()
    f.db.record_tin_user = AsyncMock()
    runtime = SimpleNamespace(database=f.db, storage=f.storage, integrations=f.integrations)
    settings = SimpleNamespace(
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.tin.test",
    )
    server, _ = create_mcp_app(settings=settings, auth=SimpleNamespace(), runtime=lambda: runtime)
    app = FastAPI()
    app.include_router(router)
    app.include_router(system_router)
    app.state.runtime = runtime
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=token.subject,
        token_type="session_token",  # noqa: S106
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://tin.test",
    ) as client:
        f.client, f.server, f.token, f.runtime = client, server, token, runtime
        f.root = f"/api/projects/{f.project.id}/technical-fixes"
        yield f


async def test_retired_preview_routes_and_tools_are_gone(surface_fixture):
    # organic.technical_fix is retired for new work; preflight_website_change previews repairs.
    f = surface_fixture
    f.token.subject = "member"
    assert (await f.client.get(f"{f.root}/sources")).status_code in {404, 405}
    assert (await f.client.post(f"{f.root}/preflight", json={})).status_code in {404, 405}
    names = {tool.name for tool in await f.server.list_tools()}
    assert not names & {
        "list_technical_fix_sources",
        "get_technical_fix_source",
        "preflight_technical_fix",
    }
    assert {"stop_technical_fix", "preflight_website_change"} <= names


def test_technical_fix_is_an_explicit_codex_catalog_template():
    from tin_lite.catalog import BUILTIN_WORKFLOWS

    template = next(row for row in BUILTIN_WORKFLOWS if row.key == "organic.technical_fix")
    assert template.executor == "codex.procedure"
    assert template.definition["procedure"]["output"]["repair_policy"] == "site-fix-v5"
    assert template.definition["procedure"]["output"]["max_files"] == 20
    assert template.definition["procedure"]["entry_skill"] == "audit-batch-repair"


async def test_new_controls_enforce_membership_before_any_stop(surface_fixture):
    f = surface_fixture
    f.db.stop_technical_fix = AsyncMock()
    f.runtime.temporal = SimpleNamespace(get_workflow_handle=AsyncMock())
    roots = [f.root, f"/api/projects/{f.project.id}/organic-system"]
    for root in roots:
        assert (await f.client.post(f"{root}/runs/{f.run.id}/stop")).status_code == 404
    for name in ("stop_organic_system", "stop_technical_fix"):
        with pytest.raises(ToolError, match="not_found: run not found"):
            await f.server.call_tool(name, {"run_id": str(f.run.id)})
    f.db.stop_technical_fix.assert_not_awaited()
    f.runtime.temporal.get_workflow_handle.assert_not_called()


async def test_parent_progress_http_and_existing_mcp_get_run_use_same_postgres_facts(
    surface_fixture,
):
    f = surface_fixture
    f.token.subject = "member"
    f.run.executor = f.run.workflow_name = "organic.traffic_system"
    f.run.workflow_id = uuid4()
    f.run.artifact_path = f.run.artifact_ref = f.run.retained_output = None
    f.run.output_resolution = f.run.review_decision = f.run.error_message = None
    f.run.review_required = False
    f.run.progress_mode = "indeterminate"
    f.run.progress_step = f.run.progress_summary = f.run.progress_updated_at = None
    f.run.progress_current = f.run.progress_total = f.run.progress_percent = None
    f.db.get_effect = AsyncMock(return_value=None)
    response = await f.client.get(f"/api/projects/{f.project.id}/organic-system/runs/{f.run.id}")
    result = await f.server.call_tool("get_run", {"run_id": str(f.run.id)})
    assert response.status_code == 200
    assert result.structured_content["system"] == response.json()
    assert result.structured_content["progress_mode"] == "indeterminate"
    assert len(response.json()["steps"]) == 4
    assert all(row["status"] == "not_started" for row in response.json()["steps"])
    f.storage.read_canonical_artifact.assert_not_awaited()
