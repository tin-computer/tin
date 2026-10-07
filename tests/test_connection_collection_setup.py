"""Exercise normal saved-workflow admission, not preseeded collection run inputs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from test_connection_collection import ACTOR, FRIEND, TOKEN, USER, batch
from test_connection_collection import collection_db as collection_db

from tin_lite.api import _workflow_view, router
from tin_lite.auth import AuthContext, require_user
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.connection_collection import KEY, POLICY
from tin_lite.connection_collection_store import CollectionStore, token_hash
from tin_lite.mcp_server import create_mcp_app
from tin_lite.private_workflows import workflow_source_view


async def harness(db, monkeypatch):
    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == KEY)
    project = await db.create_project(name="Collection fixture", state_repo_id="projects/fixture")
    await db.grant_project_membership(project_id=project.id, clerk_user_id=USER)
    await db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=KEY,
        executor=KEY,
        title=builtin.title,
        description=builtin.description,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha="a" * 40,
        version_label=builtin.version_label,
        definition=builtin.definition,
    )
    settings = SimpleNamespace(
        connection_collection_projects=[str(project.id)],
        task_queue="test-collection",
        billing_enabled=False,
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.test",
    )
    store = CollectionStore(db, settings)
    grant = await store.grant(project.id, USER)
    await store.pair(grant["grant"], token_hash(TOKEN), ACTOR)
    runtime = SimpleNamespace(
        database=db,
        storage=None,
        temporal=SimpleNamespace(start_workflow=AsyncMock()),
        integrations=SimpleNamespace(ensure_requirements=AsyncMock()),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.runtime, app.state.settings = runtime, settings
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=USER,
        token_type="session_token",  # noqa: S106
    )
    monkeypatch.setattr(
        "tin_lite.mcp_server.get_access_token",
        lambda: SimpleNamespace(subject=USER, scopes=["openid"], client_id="test-client"),
    )
    server, _ = create_mcp_app(settings=settings, auth=SimpleNamespace(), runtime=lambda: runtime)
    return SimpleNamespace(
        db=db,
        project=project,
        builtin=builtin,
        runtime=runtime,
        settings=settings,
        store=store,
        app=app,
        server=server,
    )


async def save(f, surface, client, inputs):
    payload = dict(
        workflow_id=str(f.builtin.id), name="Collection", inputs=inputs, request_id=str(uuid4())
    )
    if surface == "http":
        response = await client.post(f"/api/projects/{f.project.id}/workflows", json=payload)
        assert response.status_code == 201, response.text
        return response.json()
    return (
        await f.server.call_tool(
            "create_project_workflow", {"project_id": str(f.project.id), **payload}
        )
    ).structured_content


@pytest.mark.parametrize("surface", ["http", "mcp"])
@pytest.mark.parametrize("mode", ["local_only", "cloud_preferred"])
async def test_saved_local_run_reaches_extension_with_server_bound_project(
    collection_db, monkeypatch, surface, mode
):
    f = await harness(collection_db, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=f.app), base_url="https://tin.test"
    ) as client:
        saved = await save(
            f, surface, client, {"friends": [FRIEND], "keywords": " founder ", "execution": mode}
        )
        assert "project_id" not in saved["inputs"]
        assert saved["inputs"]["keywords"] == "founder"
        path = f"/api/projects/{f.project.id}/workflows/{saved['id']}/runs"
        request_id = str(uuid4())
        for _ in range(2):
            if surface == "http":
                response = await client.post(path, headers={"Idempotency-Key": request_id})
                assert response.status_code == 202, response.text
                started = response.json()
            else:
                started = (
                    await f.server.call_tool(
                        "start_project_workflow",
                        {
                            "project_id": str(f.project.id),
                            "project_workflow_id": saved["id"],
                            "request_id": request_id,
                        },
                    )
                ).structured_content
        run = await f.db.get_run(UUID(started["id"]))
        assert run.input == saved["inputs"]
        assert len(await f.db.list_runs(project_id=f.project.id)) == 1
        assert f.runtime.temporal.start_workflow.await_count > 0
        assert all(
            call.kwargs["id"] == run.temporal_workflow_id
            for call in f.runtime.temporal.start_workflow.await_args_list
        )
        job = await f.store.prepare(run, POLICY)
        assert job["inputs"]["project_id"] == str(f.project.id)
        assert job["inputs"]["execution"] == mode
        assert job["cloud_template"] is None
        assert job["cloud_sandbox_id"] is None
        if mode == "cloud_preferred":
            assert job["cloud_transport"] == "local_backup"
            assert job["reason"] == "cloud_unavailable"
            from tin_lite.connection_collection_activities import CollectionActivities

            progress = AsyncMock()
            monkeypatch.setattr(f.db, "project_run_progress", progress)
            activity = CollectionActivities(database=f.db, storage=None, settings=f.settings)
            assert not await activity.poll(str(run.id))
            assert "collect in Chrome" in progress.call_args.kwargs["summary"]
            # Retry never upgrades this run into a session-transfer job.
            f.settings.linkedin_cloud_enabled = True
            f.settings.e2b_api_key = "existing-key"
            f.settings.integration_credential_key = "fixture-cipher"
            f.settings.linkedin_cloud_template = "fixture-template"
            repeated = await f.store.prepare(run, POLICY)
            assert repeated["cloud_transport"] == "local_backup"
            assert repeated["deadline"] == job["deadline"]
        claim = await f.store.claim(run.id, f.project.id, TOKEN, ACTOR["key"])
        page = batch(claim, more=False)
        page["source"]["collection_url"] += "&keywords=founder"
        await f.store.page(run.id, f.project.id, TOKEN, page)
        stored = await f.db.pool.fetchrow(
            "SELECT * FROM connection_collection_jobs WHERE run_id=$1", run.id
        )
        assert stored["state"] == "completed"


@pytest.mark.parametrize("surface", ["http", "mcp"])
async def test_names_rejected_before_saving_without_echoing_inputs(
    collection_db, monkeypatch, surface
):
    f = await harness(collection_db, monkeypatch)
    payload = dict(
        workflow_id=str(f.builtin.id),
        name="Collection",
        inputs={"friends": ["Private Person"]},
        request_id=str(uuid4()),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=f.app), base_url="https://tin.test"
    ) as client:
        if surface == "http":
            response = await client.post(f"/api/projects/{f.project.id}/workflows", json=payload)
            assert response.status_code == 422
            message = response.json()["detail"]
        else:
            with pytest.raises(ToolError) as error:
                await f.server.call_tool(
                    "create_project_workflow", {"project_id": str(f.project.id), **payload}
                )
            message = str(error.value)
        assert "LinkedIn profile URLs" in message
        assert "Private Person" not in message and "pydantic" not in message
        assert not await f.db.list_project_workflows(project_id=f.project.id)
        f.runtime.temporal.start_workflow.assert_not_awaited()
        saved = await save(f, surface, client, {"friends": [FRIEND]})
        update = {
            "name": saved["name"],
            "inputs": {"friends": ["Private Person"]},
            "expected_settings_revision": saved["settings_revision"],
        }
        if surface == "http":
            response = await client.put(
                f"/api/projects/{f.project.id}/workflows/{saved['id']}", json=update
            )
            assert response.status_code == 422
            assert "LinkedIn profile URLs" in response.json()["detail"]
        else:
            with pytest.raises(ToolError, match="LinkedIn profile URLs"):
                await f.server.call_tool(
                    "update_project_workflow",
                    {
                        "project_id": str(f.project.id),
                        "project_workflow_id": saved["id"],
                        **update,
                    },
                )
        assert (await f.db.get_project_workflow(UUID(saved["id"]))).inputs == saved["inputs"]


async def test_cloud_only_remains_saveable_but_reports_unavailable_before_run(
    collection_db, monkeypatch
):
    f = await harness(collection_db, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=f.app), base_url="https://tin.test"
    ) as client:
        saved = await save(f, "http", client, {"friends": [FRIEND], "execution": "cloud_only"})
        response = await client.post(f"/api/projects/{f.project.id}/workflows/{saved['id']}/runs")
        assert response.status_code == 409
        assert "Cloud collection is not available" in response.json()["detail"]
        assert not await f.db.list_runs(project_id=f.project.id)
        f.runtime.temporal.start_workflow.assert_not_awaited()
        workflow = await f.db.get_workflow(f.builtin.id)
        for view in (
            _workflow_view(workflow, f.settings).model_dump(),
            workflow_source_view(workflow, f.settings),
        ):
            assert view["collection_availability"] == {"cloud_ready": False}
        assert (
            "cloud_preferred"
            in workflow.definition["input_schema"]["properties"]["execution"]["enum"]
        )


async def test_caller_cannot_bind_a_different_project(collection_db, monkeypatch):
    f = await harness(collection_db, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=f.app), base_url="https://tin.test"
    ) as client:
        response = await client.post(
            f"/api/workflows/{f.builtin.id}/runs",
            json={
                "project_id": str(f.project.id),
                "inputs": {"project_id": str(uuid4()), "friends": [FRIEND]},
            },
        )
        assert response.status_code in {409, 422}
        assert "bound by Tin" in response.json()["detail"]
        f.runtime.temporal.start_workflow.assert_not_awaited()


@pytest.mark.parametrize("mode", ["cloud_only", "cloud_preferred"])
async def test_configured_cloud_admission_keeps_selected_policy(collection_db, monkeypatch, mode):
    f = await harness(collection_db, monkeypatch)
    f.settings.e2b_api_key = "fixture-general-key"
    f.settings.integration_credential_key = "fixture-credential-key"
    f.settings.linkedin_cloud_template = "fixture-collection-template"
    f.settings.linkedin_cloud_enabled = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=f.app), base_url="https://tin.test"
    ) as client:
        saved = await save(f, "http", client, {"friends": [FRIEND], "execution": mode})
        response = await client.post(f"/api/projects/{f.project.id}/workflows/{saved['id']}/runs")
        assert response.status_code == 202, response.text
        run = await f.db.get_run(UUID(response.json()["id"]))
        assert run.input["execution"] == mode
        await f.store.prepare(run, POLICY)
        template = await f.db.pool.fetchval(
            "SELECT cloud_template FROM connection_collection_jobs WHERE run_id=$1", run.id
        )
        assert template == "fixture-collection-template"
        workflow = await f.db.get_workflow(f.builtin.id)
        assert _workflow_view(workflow, f.settings).collection_availability == {"cloud_ready": True}


async def test_older_saved_inputs_keep_their_pin_while_job_normalizes_them(
    collection_db, monkeypatch
):
    f = await harness(collection_db, monkeypatch)
    configured = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=f.builtin.id,
        definition_commit_sha="a" * 40,
        name="Older collection",
        inputs={"friends": [FRIEND + "/"], "keywords": " founder ", "execution": "local_only"},
        input_schema=f.builtin.input_schema,
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=USER,
        pinned_definition=f.builtin.definition,
    )
    configured = await f.db.project_workflow_synced(
        project_workflow_id=configured.id, temporal_schedule_id=None, next_run_at=None
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=f.app), base_url="https://tin.test"
    ) as client:
        response = await client.post(f"/api/projects/{f.project.id}/workflows/{configured.id}/runs")
        assert response.status_code == 202, response.text
        run = await f.db.get_run(UUID(response.json()["id"]))
        assert run.input == configured.inputs
        job = await f.store.prepare(run, POLICY)
        assert job["inputs"]["friends"] == [FRIEND]
        assert job["inputs"]["keywords"] == "founder"
