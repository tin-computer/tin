from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from organic_site_stub import minimal_site
from test_organic_audit import page_fixture, panel_fixture, response
from test_procedure_publication import HistoryStorage
from test_procedure_publication import publication_db as publication_db

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.dataforseo import DataForSEO
from tin_lite.domain import RunStatus, SideEffectConflictError
from tin_lite.mcp_server import create_mcp_app
from tin_lite.organic_audit import audit_paths, canonical_json
from tin_lite.organic_audit_activities import OrganicAuditActivities
from tin_lite.run_service import WorkflowExecutorUnavailableError, start_workflow_run


async def fixture(db):
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == "organic.audit")
    project = await db.create_project(
        name="Audit contract", state_repo_id="projects/audit-contract"
    )
    await db.record_tin_user("user_auditor")
    await db.pool.execute(
        "INSERT INTO project_memberships (project_id,clerk_user_id) VALUES ($1,$2)",
        project.id,
        "user_auditor",
    )
    await db.pool.execute(
        """INSERT INTO workflows (id,key,title,description,executor,definition_repo_id,
            definition_path,current_commit_sha,version_label,definition)
           VALUES ($1,$2,$3,$4,$2,'registry/workflows',$5,$6,$7,$8::jsonb)""",
        spec.id,
        spec.key,
        spec.title,
        spec.description,
        spec.definition_path,
        "d" * 40,
        spec.version_label,
        json.dumps(spec.definition),
    )
    storage = HistoryStorage()

    async def read(*, repo_id, commit_sha, path):
        if repo_id == "registry/workflows":
            assert commit_sha == "d" * 40 and path == spec.definition_path
            return canonical_json(spec.definition)
        assert repo_id == project.state_repo_id
        return storage.repo.trees[commit_sha][path][1]

    storage.read_canonical_artifact = read

    async def list_files(*, repo_id, revision):
        assert repo_id == project.state_repo_id
        return list(storage.repo.trees[revision])

    storage.list_canonical_files_at = list_files
    settings = SimpleNamespace(
        dataforseo_login="configured",
        dataforseo_password="configured",  # noqa: S106
        organic_audit_max_cost_usd=8,
        task_queue="local-audit-test",
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.tin.test",
    )
    temporal = SimpleNamespace(
        start_workflow=AsyncMock(),
        get_workflow_handle=lambda _: SimpleNamespace(signal=AsyncMock()),
    )
    runtime = SimpleNamespace(database=db, storage=storage, temporal=temporal)
    provider = SimpleNamespace(
        validate_target=AsyncMock(return_value=("https://example.com/", "example.com")),
        crawl_request=DataForSEO.crawl_request,
        submit=AsyncMock(return_value={"task_id": str(uuid4()), "reported_cost_usd": "0.015"}),
        recover=AsyncMock(),
        summary=AsyncMock(return_value={"crawl_progress": "finished", "domain": "example.com"}),
        pages=AsyncMock(return_value=[page_fixture()]),
        stop=AsyncMock(),
    )
    # Provider injection ensures the test does not need or read actual secrets.
    settings.dataforseo_login = settings.dataforseo_password = None
    activities = OrganicAuditActivities(
        database=db,
        storage=storage,
        settings=settings,
        provider=provider,
        site_resolver=AsyncMock(return_value={"status": "observed", "redirects": []}),
        site_reader=minimal_site().reader,
    )
    settings.dataforseo_login = settings.dataforseo_password = "configured"  # noqa: S105
    workflow = await db.get_workflow(spec.id)
    return project, workflow, runtime, settings, activities, provider


async def start(runtime, settings, workflow, project):
    return await start_workflow_run(
        runtime=runtime,
        settings=settings,
        workflow=workflow,
        project_id=project.id,
        started_by_clerk_user_id="user_auditor",
        input_payload={"site_url": "https://example.com/", "market": "US"},
        start_idempotency_key="audit-contract",
    )


async def finish(activities, run):
    run_id = str(run.id)
    await activities.organic_prepare(run_id)
    await activities.organic_start_crawl(run_id)
    assert await activities.organic_poll_crawl(run_id)
    count = await activities.organic_prepare_panel(run_id)
    for index in range(count):
        await activities.organic_observe({"run_id": run_id, "index": index})
    await activities.organic_brand_checks(run_id)
    await activities.organic_publish(run_id)


@pytest.mark.asyncio
async def test_atomic_projection_survives_retry_and_storage_outage(publication_db):
    project, workflow, runtime, settings, activities, _ = await fixture(publication_db)
    run = await start(runtime, settings, workflow, project)
    await finish(activities, run)
    runtime.storage.read_canonical_artifact = AsyncMock(side_effect=ConnectionError)
    await asyncio.gather(*(activities.organic_project(str(run.id)) for _ in range(3)))
    saved = await publication_db.get_run(run.id)
    assert (
        saved.status == RunStatus.SUCCEEDED
        and saved.artifact_path == audit_paths(str(run.id))["AUDIT.md"]
    )
    assert (
        await publication_db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='organic_audit_ready'",
            run.id,
        )
        == 1
    )
    assert runtime.storage.read_canonical_artifact.await_count == 0
    assert "partial evidence" in saved.result_summary
    assert saved.progress_summary == saved.result_summary
    assert "AI visibility was not measured" in saved.result_summary
    events = await publication_db.list_product_activity(project_id=project.id)
    ready = [event for event in events if event.event_type == "organic_audit_ready"]
    assert ready[0].summary == saved.result_summary


@pytest.mark.asyncio
async def test_projection_transaction_rolls_back_if_receipt_fails(publication_db, monkeypatch):
    project, workflow, runtime, settings, activities, _ = await fixture(publication_db)
    run = await start(runtime, settings, workflow, project)
    await finish(activities, run)
    original = publication_db.complete_effect

    async def fail(conn, **kwargs):
        if kwargs["execution_key"].endswith(":projection"):
            raise RuntimeError("Injected receipt failure")
        return await original(conn, **kwargs)

    monkeypatch.setattr(publication_db, "complete_effect", fail)
    with pytest.raises(RuntimeError, match="Injected"):
        await activities.organic_project(str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.RUNNING
    assert (
        await publication_db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='organic_audit_ready'",
            run.id,
        )
        == 0
    )
    monkeypatch.setattr(publication_db, "complete_effect", original)
    await activities.organic_project(str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_mcp_discovery_idempotent_start_status_files_and_http_parity(
    publication_db, monkeypatch
):
    project, workflow, runtime, settings, activities, _ = await fixture(publication_db)
    token = SimpleNamespace(subject="user_auditor", scopes=["openid"], client_id="test_mcp")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    server, _ = create_mcp_app(settings=settings, auth=SimpleNamespace(), runtime=lambda: runtime)
    assert "organic.audit" in str(
        await server.call_tool("list_workflows", {"project_id": str(project.id)})
    )
    args = {
        "project_id": str(project.id),
        "workflow_id": "organic.audit",
        "inputs": {"site_url": "https://example.com/", "market": "US"},
        "request_id": str(uuid4()),
    }
    await server.call_tool("start_workflow", args)
    await server.call_tool("start_workflow", args)
    runs = await publication_db.list_runs(project_id=project.id)
    assert len(runs) == 1
    assert runs[0].trigger_source == "mcp" and runs[0].started_by_oauth_client_id == "test_mcp"
    await finish(activities, runs[0])
    await activities.organic_project(str(runs[0].id))
    saved = await publication_db.get_run(runs[0].id)
    assert "succeeded" in str(await server.call_tool("get_run", {"run_id": str(saved.id)}))
    assert "Organic visibility audit" in str(
        await server.call_tool("read_run_output", {"run_id": str(saved.id)})
    )
    for name, path in audit_paths(str(saved.id)).items():
        result = await server.call_tool(
            "read_project_file",
            {"project_id": str(project.id), "path": path, "revision": saved.canonical_commit_sha},
        )
        assert name in str(result)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime, app.state.settings = runtime, settings
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=token.subject,
        token_type="session_token",  # noqa: S106
    )  # noqa: S106
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        output = await client.get(f"/api/workflows/runs/{saved.id}")
        assert (
            output.status_code == 200
            and output.json()["canonical_commit_sha"] == saved.canonical_commit_sha
        )
        token.subject = "user_outsider"
        assert (await client.get(f"/api/workflows/runs/{saved.id}")).status_code == 404
        with pytest.raises(ToolError, match="project not found"):
            await server.call_tool("get_run", {"run_id": str(saved.id)})
        with pytest.raises(ToolError, match="project not found"):
            await server.call_tool("start_workflow", args)
    assert len(await publication_db.list_runs(project_id=project.id)) == 1


@pytest.mark.asyncio
async def test_stop_fences_new_spend_and_does_not_revive_on_failure(publication_db):
    project, workflow, runtime, settings, activities, provider = await fixture(publication_db)
    run = await start(runtime, settings, workflow, project)
    await activities.organic_prepare(str(run.id))
    await activities.organic_start_crawl(str(run.id))
    for _ in range(2):
        await publication_db.stop_organic_audit(
            run_id=run.id, project_id=project.id, actor="user_auditor"
        )
    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError):
        await activities._paid(str(run.id), "new-call", {}, "0.20", AsyncMock())
    await activities.organic_failure(str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.STOPPED
    assert provider.stop.await_count == 1
    assert (
        await publication_db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='organic_audit_stopped'",
            run.id,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_publication_in_flight_cannot_be_reinterpreted_as_stopped(publication_db):
    project, workflow, runtime, settings, activities, _ = await fixture(publication_db)
    run = await start(runtime, settings, workflow, project)
    await finish(activities, run)
    with pytest.raises(SideEffectConflictError, match="publishing"):
        await publication_db.stop_organic_audit(
            run_id=run.id, project_id=project.id, actor="user_auditor"
        )
    await activities.organic_project(str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_full_frozen_ai_panel_duplicate_execution_uses_saved_calls(publication_db):
    project, workflow, runtime, settings, activities, _ = await fixture(publication_db)
    calls = []

    async def model(request):
        calls.append(request)
        name = request.get("text", {}).get("format", {}).get("name")
        if name == "BuyerPanel":
            return response(json.dumps(panel_fixture()))
        if name == "PanelValidation":
            return response(
                json.dumps(
                    {"accepted": True, "explanation": "All questions are grounded and unbranded."}
                ),
                search=False,
            )
        if name == "AnswerJudgment":
            return response(
                json.dumps(
                    {
                        "mentioned": True,
                        "mention_quote": "Acme is not suitable for this buyer.",
                        "shortlisted": False,
                        "shortlist_quote": "",
                        "selected_first": False,
                        "first_choice_quote": "",
                    }
                ),
                search=False,
            )
        return response("Consider several planning tools. Acme is not suitable for this buyer.")

    activities.responses = SimpleNamespace(create=model)
    run = await start(runtime, settings, workflow, project)
    await finish(activities, run)
    first_calls = len(calls)
    await finish(activities, run)
    # Research, draft, four blind interpretations, review, 8 answer/judge pairs, 2 branded probes.
    assert len(calls) == first_calls == 25
    await activities.organic_project(str(run.id))
    saved = await publication_db.get_run(run.id)
    evidence = json.loads(
        await runtime.storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=saved.canonical_commit_sha,
            path=audit_paths(str(run.id))["evidence.json"],
        )
    )
    assert evidence["ai_visibility"]["metrics"] == {
        "mentioned": 8,
        "owned_domain_cited": 0,
        "shortlisted": 0,
        "selected_first": 0,
    }
    unbranded = [
        request
        for request in calls
        if "text" not in request and not request["input"].startswith("{")
    ][:8]
    assert len(unbranded) == 8
    assert all("example.com" not in request["input"] for request in unbranded)
    inventory = json.loads(
        await runtime.storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=saved.canonical_commit_sha,
            path=audit_paths(str(run.id))["findings.json"],
        )
    )
    assert any(
        item["next_action"] == "content_plan" and item["status"] == "review"
        for item in inventory["findings"]
    )


@pytest.mark.asyncio
async def test_zero_budget_fails_before_creating_run_or_provider_effect(publication_db):
    project, workflow, runtime, settings, _, provider = await fixture(publication_db)
    settings.organic_audit_max_cost_usd = 0
    with pytest.raises(WorkflowExecutorUnavailableError, match="spending limit"):
        await start(runtime, settings, workflow, project)
    assert await publication_db.list_runs(project_id=project.id) == []
    assert provider.submit.await_count == 0


@pytest.mark.asyncio
async def test_http_and_mcp_stop_share_membership_and_one_projection(publication_db, monkeypatch):
    project, workflow, runtime, settings, activities, _ = await fixture(publication_db)
    run = await start(runtime, settings, workflow, project)
    await activities.organic_prepare(str(run.id))
    token = SimpleNamespace(subject="user_outsider", scopes=["openid"], client_id="test_mcp")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    server, _ = create_mcp_app(settings=settings, auth=SimpleNamespace(), runtime=lambda: runtime)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = runtime
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=token.subject,
        token_type="session_token",  # noqa: S106
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        url = f"/api/workflows/runs/{run.id}/stop-organic-audit"
        assert (await client.post(url)).status_code == 404
        with pytest.raises(ToolError, match="project not found"):
            await server.call_tool("stop_organic_audit", {"run_id": str(run.id)})
        assert (await publication_db.get_run(run.id)).status != RunStatus.STOPPED
        token.subject = "user_auditor"
        assert (await client.post(url)).json()["status"] == "stopped"
        assert "stopped" in str(
            await server.call_tool("stop_organic_audit", {"run_id": str(run.id)})
        )
    assert (
        await publication_db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='organic_audit_stopped'",
            run.id,
        )
        == 1
    )
