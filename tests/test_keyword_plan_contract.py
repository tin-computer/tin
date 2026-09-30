"""Real isolated Postgres and public HTTP/MCP contracts, with fixture-only providers."""

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
from temporalio.exceptions import ApplicationError
from test_keyword_plan import INPUTS, SPEC, finish, providers
from test_organic_audit_contract import finish as finish_audit
from test_organic_audit_contract import fixture as audit_fixture
from test_organic_audit_contract import start as start_audit
from test_procedure_publication import publication_db as publication_db

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.domain import RunStatus, SideEffectConflictError
from tin_lite.keyword_plan import KEY, paths
from tin_lite.keyword_plan_activities import LOOKUP_CONCURRENCY, KeywordPlanActivities
from tin_lite.mcp_server import create_mcp_app
from tin_lite.organic_audit import canonical_json
from tin_lite.run_service import WorkflowExecutorUnavailableError, start_workflow_run


async def fixture(db):
    project, audit_workflow, runtime, settings, audit_activities, _ = await audit_fixture(db)
    runtime.audit_fixture = (audit_workflow, audit_activities)
    await db.pool.execute(
        """INSERT INTO workflows (id,key,title,description,executor,definition_repo_id,
            definition_path,current_commit_sha,version_label,definition)
           VALUES ($1,$2,$3,$4,$2,'registry/workflows',$5,$6,$7,$8::jsonb)""",
        SPEC.id,
        KEY,
        SPEC.title,
        SPEC.description,
        SPEC.definition_path,
        "d" * 40,
        SPEC.version_label,
        json.dumps(SPEC.definition),
    )
    original_read = runtime.storage.read_canonical_artifact

    async def read(**kwargs):
        if kwargs["repo_id"] == "registry/workflows" and kwargs["path"] == SPEC.definition_path:
            assert kwargs["commit_sha"] == "d" * 40
            return canonical_json(SPEC.definition)
        return await original_read(**kwargs)

    runtime.storage.read_canonical_artifact = read
    settings.keyword_plan_max_cost_usd = 10
    settings.luna_api_key = "fixture-configured"
    provider, model = providers()
    activities = KeywordPlanActivities(
        database=db, storage=runtime.storage, settings=settings, provider=provider, router=model
    )
    return project, await db.get_workflow(SPEC.id), runtime, settings, activities, provider


async def start(runtime, settings, workflow, project, **inputs):
    return await start_workflow_run(
        runtime=runtime,
        settings=settings,
        workflow=workflow,
        project_id=project.id,
        started_by_clerk_user_id="user_auditor",
        input_payload={**INPUTS, **inputs},
        start_idempotency_key="keyword-contract",
    )


def public_clients(runtime, settings, monkeypatch, *, subject="user_auditor"):
    token = SimpleNamespace(subject=subject, scopes=["openid"], client_id="test_mcp")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    server, _ = create_mcp_app(settings=settings, auth=SimpleNamespace(), runtime=lambda: runtime)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime, app.state.settings = runtime, settings
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=token.subject,
        token_type="session_token",  # noqa: S106
    )  # noqa: S106
    return token, server, app


@pytest.mark.asyncio
async def test_mcp_discovery_start_progress_projection_files_and_http_parity(
    publication_db, monkeypatch
):
    project, _, runtime, settings, activities, provider = await fixture(publication_db)
    token, server, app = public_clients(runtime, settings, monkeypatch)
    assert KEY in str(await server.call_tool("list_workflows", {"project_id": str(project.id)}))
    args = {
        "project_id": str(project.id),
        "workflow_id": KEY,
        "inputs": INPUTS,
        "request_id": str(uuid4()),
    }
    for _ in range(2):
        await server.call_tool("start_workflow", args)
    runs = await publication_db.list_runs(project_id=project.id)
    assert len(runs) == 1 and runs[0].trigger_source == "mcp"
    run = runs[0]
    await finish(activities, str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.RUNNING
    assert "review" in str(await server.call_tool("get_run", {"run_id": str(run.id)}))
    await asyncio.gather(*(activities.keyword_project(str(run.id)) for _ in range(3)))
    await finish(activities, str(run.id))
    saved = await publication_db.get_run(run.id)
    assert saved.status == RunStatus.SUCCEEDED and saved.progress_percent == 100
    assert saved.artifact_path == paths(str(run.id))["PLAN.md"]
    assert runtime.storage.repo.writes == 1 and provider.query.await_count == 7
    events = await publication_db.list_product_activity(project_id=project.id)
    ready = [event for event in events if event.event_type == "keyword_plan_ready"]
    assert len(ready) == 1 and ready[0].summary == saved.result_summary
    assert "Keyword opportunity plan" in str(
        await server.call_tool("read_run_output", {"run_id": str(run.id)})
    )
    for name, path in paths(str(run.id)).items():
        assert name in str(
            await server.call_tool(
                "read_project_file",
                {
                    "project_id": str(project.id),
                    "path": path,
                    "revision": saved.canonical_commit_sha,
                },
            )
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        response = await client.get(f"/api/workflows/runs/{run.id}")
        assert response.json()["canonical_commit_sha"] == saved.canonical_commit_sha
        token.subject = "user_outsider"
        assert (await client.get(f"/api/workflows/runs/{run.id}")).status_code == 404
        with pytest.raises(ToolError, match="project not found"):
            await server.call_tool("start_workflow", args)


@pytest.mark.asyncio
async def test_projection_receipt_and_activity_are_one_transaction(publication_db, monkeypatch):
    project, workflow, runtime, settings, activities, _ = await fixture(publication_db)
    run = await start(runtime, settings, workflow, project)
    await finish(activities, str(run.id))
    original = publication_db.complete_effect

    async def fail(conn, **kwargs):
        if kwargs["execution_key"].endswith(":projection"):
            raise RuntimeError("Injected projection receipt failure")
        return await original(conn, **kwargs)

    monkeypatch.setattr(publication_db, "complete_effect", fail)
    with pytest.raises(RuntimeError, match="Injected"):
        await activities.keyword_project(str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.RUNNING
    assert not [
        event
        for event in await publication_db.list_product_activity(project_id=project.id)
        if event.event_type == "keyword_plan_ready"
    ]
    monkeypatch.setattr(publication_db, "complete_effect", original)
    runtime.storage.read_canonical_artifact = AsyncMock(side_effect=ConnectionError)
    await activities.keyword_project(str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.SUCCEEDED
    assert runtime.storage.read_canonical_artifact.await_count == 0


@pytest.mark.asyncio
async def test_http_mcp_stop_membership_fence_and_deduplication(publication_db, monkeypatch):
    project, workflow, runtime, settings, activities, provider = await fixture(publication_db)
    run = await start(runtime, settings, workflow, project)
    await activities.keyword_prepare(str(run.id))
    token, server, app = public_clients(runtime, settings, monkeypatch, subject="user_outsider")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        url = f"/api/workflows/runs/{run.id}/stop-keyword-plan"
        assert (await client.post(url)).status_code == 404
        with pytest.raises(ToolError, match="not_found: run not found"):
            await server.call_tool("stop_keyword_plan", {"run_id": str(run.id)})
        token.subject = "user_auditor"
        assert (await client.post(url)).json()["status"] == "stopped"
        assert "stopped" in str(
            await server.call_tool("stop_keyword_plan", {"run_id": str(run.id)})
        )
    with pytest.raises(ApplicationError):
        await activities.keyword_collect(str(run.id))
    await activities.keyword_failure(str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.STOPPED
    assert provider.query.await_count == 0
    events = await publication_db.list_product_activity(project_id=project.id)
    assert len([event for event in events if event.event_type == "keyword_plan_stopped"]) == 1


@pytest.mark.asyncio
async def test_cannot_stop_published_run_waiting_on_projection(publication_db):
    project, workflow, runtime, settings, activities, _ = await fixture(publication_db)
    run = await start(runtime, settings, workflow, project)
    await finish(activities, str(run.id))
    with pytest.raises(SideEffectConflictError, match="publishing"):
        await publication_db.stop_keyword_plan(
            run_id=run.id, project_id=project.id, actor="user_auditor"
        )
    await activities.keyword_project(str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_batched_samples_share_one_ledger_on_postgres(publication_db):
    from test_keyword_plan_concurrency import track

    project, workflow, runtime, settings, activities, provider = await fixture(publication_db)
    state = track(provider)
    run = await start(runtime, settings, workflow, project)
    run_id = str(run.id)
    await activities.keyword_prepare(run_id)
    await activities.keyword_collect(run_id)
    count = await activities.keyword_sample_count(run_id)
    # Concurrent samples each hold their receipt lock and reserve on that same connection.
    await asyncio.gather(*(activities.keyword_inspect_batch(run_id) for _ in range(2)))
    await activities.keyword_review(run_id)
    await activities.keyword_publish(run_id)
    await activities.keyword_project(run_id)
    assert (await publication_db.get_run(run.id)).status == RunStatus.SUCCEEDED
    serps = [call for call in provider.query.await_args_list if call.args[0] == "serp"]
    assert count > LOOKUP_CONCURRENCY and len(serps) == count
    assert state["serp_peak"] > 1
    ledger = (await publication_db.get_effect(activities.key(run_id, "budget"))).result
    assert {f"serp:{index}" for index in range(count)} <= set(ledger)


@pytest.mark.asyncio
async def test_disabled_preflight_creates_no_run_or_external_call(publication_db):
    project, workflow, runtime, settings, _, provider = await fixture(publication_db)
    settings.keyword_plan_max_cost_usd = 0
    with pytest.raises(WorkflowExecutorUnavailableError):
        await start(runtime, settings, workflow, project)
    assert await publication_db.list_runs(project_id=project.id) == []
    assert runtime.temporal.start_workflow.await_count == provider.query.await_count == 0


@pytest.mark.asyncio
async def test_wrong_project_audit_fails_before_paid_calls(publication_db):
    project, workflow, runtime, settings, activities, provider = await fixture(publication_db)
    other = await publication_db.create_project(name="Other", state_repo_id="projects/other")
    source = await start_workflow_run(
        runtime=runtime,
        settings=settings,
        workflow=workflow,
        project_id=other.id,
        started_by_clerk_user_id="user_auditor",
        input_payload=INPUTS,
        start_idempotency_key="other",
    )
    run = await start(runtime, settings, workflow, project, audit_run_id=str(source.id))
    with pytest.raises(ApplicationError, match="in this project"):
        await activities.keyword_prepare(str(run.id))
    assert provider.query.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("matching", [False, True])
async def test_exact_published_audit_is_optional_context_not_a_latest_file_lookup(
    publication_db, matching
):
    project, workflow, runtime, settings, activities, provider = await fixture(publication_db)
    audit_workflow, audit_activities = runtime.audit_fixture
    source = await start_audit(runtime, settings, audit_workflow, project)
    await finish_audit(audit_activities, source)
    await audit_activities.organic_project(str(source.id))
    source = await publication_db.get_run(source.id)
    site = "https://example.com/" if matching else "https://other.example/"
    provider.validate_target.return_value = (site, site.split("/")[2])
    run = await start(
        runtime, settings, workflow, project, audit_run_id=str(source.id), site_url=site
    )
    if matching:
        await activities.keyword_prepare(str(run.id))
        scope = await activities._result(str(run.id), "scope")
        assert scope["audit"]["revision"] == source.canonical_commit_sha
        assert "Organic visibility audit" in scope["audit"]["context_excerpt"]
    else:
        with pytest.raises(ApplicationError, match="different target"):
            await activities.keyword_prepare(str(run.id))
    assert provider.query.await_count == 0
