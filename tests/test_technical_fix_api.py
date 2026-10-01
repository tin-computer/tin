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


def arguments(f):
    return {
        key: str(value) if key in {"project_id", "audit_run_id"} else value
        for key, value in f.selection.items()
    }


RELAY = {"quote", "relay", "tell_the_founder"}


def without_relay(value):
    """MCP adds the founder-facing relay to the HTTP preview; the facts match exactly."""
    return {key: item for key, item in value.items() if key not in RELAY}


async def test_all_http_and_mcp_surfaces_deny_before_source_or_provider_reads(surface_fixture):
    f = surface_fixture
    args = arguments(f)
    body = {k: v for k, v in args.items() if k != "project_id"}
    for suffix in ("/sources", f"/sources/{f.run.id}"):
        assert (await f.client.get(f.root + suffix)).status_code == 404
    assert (await f.client.post(f.root + "/preflight", json=body)).status_code == 404
    for name, fields in (
        ("list_technical_fix_sources", ["project_id"]),
        ("get_technical_fix_source", ["project_id", "audit_run_id"]),
        ("preflight_technical_fix", args),
    ):
        with pytest.raises(ToolError, match="project not found"):
            await f.server.call_tool(name, {key: args[key] for key in fields})
    f.db.get_run.assert_not_awaited()
    f.db.pool.fetch.assert_not_awaited()
    f.storage.read_canonical_artifact.assert_not_awaited()
    f.integrations.github_repository_binding.assert_not_awaited()


async def test_members_read_the_same_verified_source_and_preview_over_http_and_mcp(surface_fixture):
    f = surface_fixture
    f.token.subject = "member"
    args = arguments(f)
    body = {k: v for k, v in args.items() if k != "project_id"}
    assert (await f.client.get(f.root + "/sources")).json()["sources"] == []
    await f.server.call_tool("list_technical_fix_sources", {"project_id": args["project_id"]})
    response = await f.client.get(f.root + f"/sources/{f.run.id}")
    assert response.status_code == 200
    assert response.json()["findings"][0]["source_eligible"] is True
    await f.server.call_tool(
        "get_technical_fix_source",
        {
            "project_id": args["project_id"],
            "audit_run_id": args["audit_run_id"],
        },
    )
    response = await f.client.post(f.root + "/preflight", json=body)
    assert response.status_code == 200
    assert response.json()["repository_mapping"] == "member_asserted_not_verified"
    assert response.json()["execution_available"] is True
    assert response.json()["summary"]["fixable"] >= 1
    result = await f.server.call_tool("preflight_technical_fix", args)
    assert without_relay(result.structured_content) == response.json()
    assert result.structured_content["relay"][0].startswith("Tin can fix")
    assert f.integrations.github_repository_binding.await_count == 2
    first, second = f.integrations.github_repository_binding.await_args_list
    assert first == second
    assert first.kwargs == {"project_id": f.project.id, "expected_repository": "owner/site"}


@pytest.mark.parametrize("surface_fixture", ["content"], indirect=True)
async def test_content_only_audit_is_visible_and_rejected_consistently(surface_fixture):
    f = surface_fixture
    f.token.subject = "member"
    response = await f.client.get(f.root + f"/sources/{f.run.id}")
    assert response.status_code == 200
    result = await f.server.call_tool(
        "get_technical_fix_source",
        {"project_id": str(f.project.id), "audit_run_id": str(f.run.id)},
    )
    assert result.structured_content == response.json()
    assert response.json()["repair_availability"]["reason"] == "no_technical_findings"
    # A content finding is copy: listed for the content workflows, never a repair.
    for row in response.json()["excluded_findings"]:
        f.selection["finding_id"] = row["finding"]["id"]
        args = arguments(f)
        preview = await f.client.post(
            f.root + "/preflight", json={k: v for k, v in args.items() if k != "project_id"}
        )
        assert preview.status_code == 200
        plan = preview.json()["plan"]
        assert plan["repairs"] == [] and preview.json()["execution_available"] is False
        assert [item["id"] for item in plan["left_out"]["copy"]] == [row["finding"]["id"]]
        result = await f.server.call_tool("preflight_technical_fix", args)
        assert without_relay(result.structured_content) == preview.json()
    f.integrations.github_repository_binding.assert_not_awaited()


@pytest.mark.parametrize(
    "change,code,status",
    [
        ("project", "source_not_found", 404),
        ("revision", "source_changed", 409),
        ("finding", "finding_not_found", 404),
        ("receipt", "invalid_source", 409),
        ("confirmation", "repository_confirmation_required", 409),
        ("storage", "source_unavailable", 503),
    ],
)
async def test_http_and_mcp_rejections_match_and_never_reach_github(
    surface_fixture, change, code, status
):
    f = surface_fixture
    f.token.subject = "member"
    if change == "project":
        f.run.project_id = uuid4()
    elif change == "revision":
        f.selection["audit_revision"] = "b" * 40
    elif change == "finding":
        f.selection["finding_id"] = "oa_" + "0" * 20
    elif change == "receipt":
        f.receipt.status = "started"
    elif change == "confirmation":
        f.selection["repository_serves_site"] = False
    else:
        f.storage.read_canonical_artifact.side_effect = RuntimeError("secret-provider-detail")
    args = arguments(f)
    response = await f.client.post(
        f.root + "/preflight",
        json={k: v for k, v in args.items() if k != "project_id"},
    )
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert "secret-provider-detail" not in response.text
    with pytest.raises(ToolError, match=code) as failure:
        await f.server.call_tool("preflight_technical_fix", args)
    assert "secret-provider-detail" not in str(failure.value)
    f.integrations.github_repository_binding.assert_not_awaited()


async def test_bad_selection_and_pagination_rejected_without_reads(surface_fixture):
    f = surface_fixture
    f.token.subject = "member"
    body = {k: v for k, v in arguments(f).items() if k != "project_id"}
    assert (
        await f.client.post(f.root + "/preflight", json={**body, "repository_serves_site": "true"})
    ).status_code == 422
    assert (
        await f.client.post(f.root + "/preflight", json={**body, "extra": "ignored?"})
    ).status_code == 422
    assert (await f.client.get(f.root + "/sources?offset=-1")).status_code == 422
    with pytest.raises(ToolError, match="invalid_offset"):
        await f.server.call_tool(
            "list_technical_fix_sources", {"project_id": str(f.project.id), "offset": -1}
        )
    with pytest.raises(ToolError, match="repository_serves_site"):
        await f.server.call_tool(
            "preflight_technical_fix", {**arguments(f), "repository_serves_site": "true"}
        )
    f.db.get_run.assert_not_awaited()
    f.db.pool.fetch.assert_not_awaited()
    f.storage.read_canonical_artifact.assert_not_awaited()


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
