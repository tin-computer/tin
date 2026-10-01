import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from test_gak import settings_values
from test_procedure_publication import HistoryStorage
from test_procedure_publication import publication_db as publication_db

from tin_lite.activities import TinActivities
from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.mcp_server import _mcp_workflow, create_mcp_app
from tin_lite.private_workflows import (
    PackageActivation,
    PrivateWorkflowArchive,
    PrivateWorkflowError,
    PrivateWorkflows,
    authoring_guide,
    private_execution_blocker,
    private_execution_ready,
    validate_private_definition,
)
from tin_lite.procedures import load_pinned_codex_procedure
from tin_lite.run_service import start_workflow_run
from tin_lite.settings import Settings
from tin_lite.workflow_definitions import ensure_schedule_allowed, resolve_execution_contract
from tin_lite.workflow_packages import decode_workflow_source

ACTOR = "user_privateauthor"
KEY = "custom.research_digest"
PATH = f"workflow_packages/{KEY}/workflow.json"


async def fixture(db):
    storage = HistoryStorage()
    project = await db.create_project(name="Private pilot", state_repo_id=storage.repo.id)
    await db.record_tin_user(ACTOR)
    await db.grant_project_membership(project_id=project.id, clerk_user_id=ACTOR)
    settings = SimpleNamespace(
        private_workflow_projects={project.id},
        codex_api_projects={project.id},
        luna_api_key="synthetic",
        e2b_isolated_template="isolated-test",
        task_queue="private-test",
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.test",
    )
    files = {
        path: content.encode()
        for path, content in authoring_guide(settings=settings, project_id=project.id)[
            "example_files"
        ].items()
    }
    revision = storage.repo.edit(files)
    service = PrivateWorkflows(database=db, storage=storage, settings=settings)
    runtime = SimpleNamespace(
        database=db,
        storage=storage,
        temporal=SimpleNamespace(start_workflow=AsyncMock()),
        integrations=SimpleNamespace(ensure_requirements=AsyncMock()),
    )
    return SimpleNamespace(
        db=db,
        storage=storage,
        project=project,
        settings=settings,
        service=service,
        runtime=runtime,
        files=files,
        revision=revision,
    )


def selection(f, **kwargs):
    return PackageActivation(
        path=PATH, revision=f.revision, request_id=uuid4(), expected_revision=None, **kwargs
    )


async def activate(f, selected=None, **kwargs):
    return await f.service.activate(
        project_id=f.project.id,
        actor=ACTOR,
        client_id=None,
        selection=selected or selection(f),
        **kwargs,
    )


async def private_rows(f):
    return [w for w in await f.db.list_workflows(project_id=f.project.id) if w.project_id]


def app(f, actor=ACTOR):
    result = FastAPI()
    result.include_router(router)
    if getattr(f.settings, "billing_enabled", False):
        from tin_lite.billing_api import router as billing_router

        result.include_router(billing_router)
    result.state.runtime, result.state.settings = f.runtime, f.settings
    result.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=actor,
        token_type="session_token",  # noqa: S106 — credential category, not a secret
    )
    return result


def mcp(f, monkeypatch, actor=ACTOR):
    token = SimpleNamespace(subject=actor, scopes=["openid"], client_id="client_test")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    return create_mcp_app(settings=f.settings, auth=SimpleNamespace(), runtime=lambda: f.runtime)[0]


def structured(result):
    if isinstance(result, tuple):
        return result[1]
    return result.structured_content


async def test_validate_is_read_only_and_activation_replay_is_atomic(publication_db):
    f = await fixture(publication_db)
    chosen = selection(f)
    valid = await f.service.validate(project_id=f.project.id, actor=ACTOR, selection=chosen)
    assert valid["valid"] and valid["runtime_available"] and len(valid["files"]) == 3
    assert await private_rows(f) == []
    first, second = await asyncio.gather(activate(f, chosen), activate(f, chosen))
    assert first["workflow_id"] == second["workflow_id"]
    assert sorted([first["replayed"], second["replayed"]]) == [False, True]
    assert first["package_digest"] == valid["package_digest"]
    assert (
        await f.db.pool.fetchval("SELECT count(*) FROM workflows WHERE project_id=$1", f.project.id)
        == 1
    )
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE project_id=$1", f.project.id
        )
        == 0
    )
    events = await f.db.list_product_activity(project_id=f.project.id)
    assert len(events) == 1 and events[0].event_type == "private_workflow_activated"
    assert f.storage.repo.writes == 0  # activation never makes a second filesystem copy
    workflow = await f.db.get_workflow(UUID(first["workflow_id"]))
    assert (
        workflow.project_id == f.project.id
        and workflow.definition_repo_id == f.project.state_repo_id
    )
    with pytest.raises(PrivateWorkflowError, match="different operation"):
        await activate(f, chosen.model_copy(update={"expected_revision": f.revision}))


async def test_contribution_readme_copy_validates_and_activates_through_mcp(
    publication_db, monkeypatch
):
    from test_community_packages import readme_example_files

    f = await fixture(publication_db)
    files = readme_example_files(private=True)
    revision = f.storage.repo.edit(files)
    server = mcp(f, monkeypatch)
    chosen = {
        "project_id": str(f.project.id),
        "path": "workflow_packages/custom.example_play/workflow.json",
        "revision": revision,
    }
    valid = structured(await server.call_tool("validate_workflow_package", chosen))
    assert valid["valid"] and valid["runtime_available"]
    assert sorted(valid["files"]) == sorted(files)
    # Agents often select the package directory rather than its manifest.
    for directory in (
        "workflow_packages/custom.example_play",
        "workflow_packages/custom.example_play/",
    ):
        by_directory = {**chosen, "path": directory}
        assert (
            structured(await server.call_tool("validate_workflow_package", by_directory)) == valid
        )
    with pytest.raises(
        ToolError, match=r"invalid: path 'reports/x\.md' must be workflow_packages/custom\.<key>/"
    ):
        await server.call_tool("validate_workflow_package", {**chosen, "path": "reports/x.md"})
    with pytest.raises(ToolError, match="invalid: revision must be a lowercase 40-character"):
        await server.call_tool("validate_workflow_package", {**chosen, "revision": "main"})
    assert await private_rows(f) == []
    activation = {**chosen, "request_id": str(uuid4()), "expected_revision": None}
    first = structured(await server.call_tool("activate_workflow_package", activation))
    replay = structured(await server.call_tool("activate_workflow_package", activation))
    assert first["workflow_id"] == replay["workflow_id"]
    assert first["package_digest"] == valid["package_digest"]
    assert replay["replayed"]
    assert len(await private_rows(f)) == 1
    workflow = await f.db.get_workflow(UUID(first["workflow_id"]))
    assert workflow.key == "custom.example_play"
    assert workflow.current_commit_sha == revision
    assert await f.db.list_runs(project_id=f.project.id) == []
    f.runtime.temporal.start_workflow.assert_not_called()


async def test_invalid_draft_and_activation_races_leave_one_active_revision(publication_db):
    f = await fixture(publication_db)
    first = await activate(f)
    bad = f.storage.repo.edit({f"workflow_packages/{KEY}/PROMPT.md": b"\xff"})
    invalid = selection(f).model_copy(update={"revision": bad, "expected_revision": f.revision})
    result = await f.service.validate(project_id=f.project.id, actor=ACTOR, selection=invalid)
    assert not result["valid"] and result["diagnostics"][0]["code"] == "invalid_package"
    with pytest.raises(PrivateWorkflowError):
        await activate(f, invalid)
    assert (await f.db.get_workflow(UUID(first["workflow_id"]))).current_commit_sha == f.revision
    b = f.storage.repo.edit({f"workflow_packages/{KEY}/PROMPT.md": b"Write version B."})
    c = f.storage.repo.edit({f"workflow_packages/{KEY}/PROMPT.md": b"Write version C."})
    results = await asyncio.gather(
        *[
            activate(
                f,
                selection(f).model_copy(
                    update={"revision": revision, "expected_revision": f.revision}
                ),
            )
            for revision in (b, c)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(r, PrivateWorkflowError) for r in results) == 1
    winner = next(r for r in results if isinstance(r, dict))
    assert (await f.db.get_workflow(UUID(first["workflow_id"]))).current_commit_sha == winner[
        "revision"
    ]
    assert len(await f.db.list_product_activity(project_id=f.project.id)) == 2


@pytest.mark.parametrize("surface", ["http", "mcp"])
async def test_saved_a_after_b_and_latest_new_save(publication_db, monkeypatch, surface):
    f = await fixture(publication_db)
    first = await activate(f)
    workflow_id = UUID(first["workflow_id"])
    workflow = await f.db.get_workflow(workflow_id)
    configured = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=workflow_id,
        definition_commit_sha=f.revision,
        name="Saved A",
        inputs={"brief": "Summarize project evidence"},
        input_schema=workflow.definition["input_schema"],
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
    )
    configured = await f.db.project_workflow_synced(
        project_workflow_id=configured.id, temporal_schedule_id=None, next_run_at=None
    )
    manifest = json.loads(f.files[PATH])
    manifest["definition"]["procedure"]["output"]["path"] = "reports/custom/VERSION_B.md"
    manifest["definition"]["human_review"] = {"eligible": True}
    b = f.storage.repo.edit({PATH: json.dumps(manifest).encode()})
    await activate(
        f, selection(f).model_copy(update={"revision": b, "expected_revision": f.revision})
    )
    if surface == "http":
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app(f)), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/projects/{f.project.id}/workflows/{configured.id}/runs"
            )
            assert response.status_code == 202, response.text
            new = await client.post(
                f"/api/projects/{f.project.id}/workflows",
                json={
                    "workflow_id": str(workflow_id),
                    "name": "Saved B",
                    "inputs": {"brief": "New brief"},
                    "request_id": str(uuid4()),
                },
            )
            assert new.status_code == 201, new.text
    else:
        server = mcp(f, monkeypatch)
        await server.call_tool(
            "start_project_workflow",
            {
                "project_id": str(f.project.id),
                "project_workflow_id": str(configured.id),
                "request_id": str(uuid4()),
            },
        )
        await server.call_tool(
            "create_project_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": str(workflow_id),
                "name": "Saved B",
                "inputs": {"brief": "New brief"},
                "request_id": str(uuid4()),
            },
        )
    runs = await f.db.list_runs(project_id=f.project.id)
    assert (
        len(runs) == 1
        and runs[0].definition_commit_sha == f.revision
        and not runs[0].review_required
    )
    saved = await f.db.list_project_workflows(project_id=f.project.id)
    assert {item.definition_commit_sha for item in saved} == {f.revision, b}
    pinned = await load_pinned_codex_procedure(
        storage=f.storage,
        repo_id=f.project.state_repo_id,
        commit_sha=runs[0].definition_commit_sha,
        definition_path=PATH,
    )
    assert pinned.output_path == "reports/custom/RESEARCH_DIGEST.md"


async def test_cross_project_and_revoked_member_never_read_sources(publication_db, monkeypatch):
    f = await fixture(publication_db)
    first = await activate(f)
    other = await f.db.create_project(name="Other", state_repo_id="projects/other")
    actor = "user_other"
    await f.db.record_tin_user(actor)
    await f.db.grant_project_membership(project_id=other.id, clerk_user_id=actor)
    f.storage.reads.clear()
    with pytest.raises(LookupError):
        await f.service.validate(project_id=f.project.id, actor=actor, selection=selection(f))
    server = mcp(f, monkeypatch, actor)
    with pytest.raises(Exception, match="workflow not found"):
        await server.call_tool(
            "get_workflow", {"project_id": str(other.id), "workflow_key": first["workflow_id"]}
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, actor)), base_url="http://test"
    ) as client:
        assert (await client.get(f"/api/workflows/{first['workflow_id']}")).status_code == 404
        assert all(
            w["scope"] == "builtin"
            for w in (await client.get(f"/api/workflows?project_id={other.id}")).json()
        )
        response = await client.post(
            f"/api/workflows/{first['workflow_id']}/runs",
            json={"project_id": str(other.id), "inputs": {"brief": "secret"}},
        )
        assert response.status_code == 404
        response = await client.put(
            f"/api/projects/{other.id}/workflow-templates/{first['workflow_id']}/saved"
        )
        assert response.status_code == 404
    await f.db.pool.execute(
        "DELETE FROM project_memberships WHERE project_id=$1 AND clerk_user_id=$2",
        f.project.id,
        ACTOR,
    )
    with pytest.raises(LookupError):
        await activate(f)
    assert f.storage.reads == []
    f.runtime.temporal.start_workflow.assert_not_awaited()


async def test_membership_is_rechecked_after_immutable_source_read(publication_db):
    f = await fixture(publication_db)
    original = f.service.load

    async def revoked(*args):
        value = await original(*args)
        await f.db.pool.execute(
            "DELETE FROM project_memberships WHERE project_id=$1 AND clerk_user_id=$2",
            f.project.id,
            ACTOR,
        )
        return value

    f.service.load = revoked
    with pytest.raises(LookupError):
        await activate(f)
    assert await private_rows(f) == []


async def test_archive_is_reversible_and_does_not_delete_saved_or_historical_state(publication_db):
    f = await fixture(publication_db)
    first = await activate(f)
    workflow_id = UUID(first["workflow_id"])
    workflow = await f.db.get_workflow(workflow_id)
    configured = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=workflow_id,
        definition_commit_sha=f.revision,
        name="Keep this configuration",
        inputs={"brief": "Summarize project evidence"},
        input_schema=workflow.definition["input_schema"],
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
    )
    run = await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={"brief": "Summarize project evidence"},
    )
    selected = PrivateWorkflowArchive(request_id=uuid4(), expected_revision=f.revision)
    options = dict(
        project_id=f.project.id,
        workflow_id=workflow_id,
        actor=ACTOR,
        client_id=None,
        selection=selected,
    )
    archived = await f.service.archive(**options)
    assert archived["status"] == "archived"
    assert (await f.service.archive(**options))["replayed"]
    assert await private_rows(f) == []
    workflow = await f.db.get_workflow(workflow_id)
    assert (await f.db.get_run(run.id)).status == run.status
    assert (await f.db.list_project_workflows(project_id=f.project.id))[0].id == configured.id
    worker = TinActivities(database=f.db, storage=f.storage, settings=f.settings, sandboxes=None)
    pinned_workflow, _ = await worker._pinned_codex_procedure(run.id)
    await worker._check_private_attempt(run, pinned_workflow)  # Archive is not cancellation.
    with pytest.raises(ValueError, match="not active"):
        await resolve_execution_contract(
            storage=f.storage, workflow=workflow, project_id=f.project.id
        )
    restored = await activate(f, selection(f).model_copy(update={"expected_revision": f.revision}))
    assert restored["workflow_id"] == first["workflow_id"]
    assert f.storage.repo.head == f.revision and f.storage.repo.writes == 0


async def test_runtime_gate_blocks_activation_and_starts_without_effects(publication_db):
    f = await fixture(publication_db)
    first = await activate(f)
    workflow = await f.db.get_workflow(UUID(first["workflow_id"]))
    f.settings.private_workflow_projects = set()
    with pytest.raises(PrivateWorkflowError, match="not enabled"):
        await activate(f)
    with pytest.raises(PrivateWorkflowError, match="not enabled"):
        await start_workflow_run(
            runtime=f.runtime,
            settings=f.settings,
            workflow=workflow,
            project_id=f.project.id,
            started_by_clerk_user_id=ACTOR,
            input_payload={"brief": "Run"},
        )
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE project_id=$1", f.project.id
        )
        == 0
    )
    f.runtime.temporal.start_workflow.assert_not_awaited()


async def test_open_gate_admits_unlisted_projects_but_keeps_the_isolated_template(
    publication_db,
):
    f = await fixture(publication_db)
    f.settings.private_workflow_projects = set()
    f.settings.private_workflows_open = True
    active = await activate(f)
    workflow = await f.db.get_workflow(UUID(active["workflow_id"]))
    worker = TinActivities(database=f.db, storage=f.storage, settings=f.settings, sandboxes=None)
    run = SimpleNamespace(project_id=f.project.id, started_by_clerk_user_id=ACTOR)
    await worker._check_private_attempt(run, workflow)
    f.settings.e2b_isolated_template = None
    with pytest.raises(PrivateWorkflowError, match="no isolated runtime configured"):
        await worker._check_private_attempt(run, workflow)
    f.settings.e2b_isolated_template = "isolated-test"
    f.settings.private_workflows_open = False
    with pytest.raises(
        PrivateWorkflowError, match="not enabled for this project: it is not admitted"
    ):
        await worker._check_private_attempt(run, workflow)


@pytest.mark.parametrize("surface", ["http", "mcp"])
async def test_authoring_lifecycle_on_both_transports(publication_db, monkeypatch, surface):
    f = await fixture(publication_db)
    chosen = selection(f).model_dump(mode="json")
    selected = {k: chosen[k] for k in ("path", "revision")}
    if surface == "http":
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app(f)), base_url="http://test"
        ) as client:
            root = f"/api/projects/{f.project.id}/workflow-packages"
            guide = (await client.get(f"{root}/guide")).json()
            validation = (await client.post(f"{root}/validate", json=selected)).json()
            response = await client.post(f"{root}/activate", json=chosen)
            assert response.status_code == 200, response.text
            activated = response.json()
            assert (await client.post(f"{root}/activate", json=chosen)).json()["replayed"]
            detail = (await client.get(f"/api/workflows/{activated['workflow_id']}")).json()
            archived = (
                await client.post(
                    f"{root}/{activated['workflow_id']}/archive",
                    json={"request_id": str(uuid4()), "expected_revision": f.revision},
                )
            ).json()
    else:
        server = mcp(f, monkeypatch)

        async def call(name, args=None):
            return structured(
                await server.call_tool(name, {"project_id": str(f.project.id), **(args or {})})
            )

        guide = await call("get_workflow_authoring_guide")
        assert "commit_project_changes" in {t.name for t in await server.list_tools()}
        validation = await call("validate_workflow_package", selected)
        activated = await call("activate_workflow_package", chosen)
        assert (await call("activate_workflow_package", chosen))["replayed"]
        detail = await call("get_workflow", {"workflow_key": activated["workflow_id"]})
        archived = await call(
            "archive_private_workflow",
            {
                "workflow_id": activated["workflow_id"],
                "request_id": str(uuid4()),
                "expected_revision": f.revision,
            },
        )
    assert guide["runtime_available"] and validation["valid"]
    assert detail["scope"] == "project" and detail["definition_revision"] == f.revision
    assert detail["allowed_actions"] == ["start", "save", "activate", "archive"]
    assert "project_id" not in detail["input_schema"]["properties"]
    assert detail["input_schema"]["required"] == ["brief"]
    assert archived["status"] == "archived"
    assert len(await f.db.list_product_activity(project_id=f.project.id)) == 2
    assert f.storage.repo.writes == 0


async def test_activation_projection_receipt_and_event_rollback_together(
    publication_db, monkeypatch
):
    f = await fixture(publication_db)
    chosen = selection(f)
    complete = f.db.complete_effect
    monkeypatch.setattr(f.db, "complete_effect", AsyncMock(side_effect=RuntimeError("fault")))
    with pytest.raises(RuntimeError, match="fault"):
        await activate(f, chosen)
    assert await private_rows(f) == []
    assert await f.db.list_product_activity(project_id=f.project.id) == []
    assert await f.db.pool.fetchval("SELECT count(*) FROM effect_receipts") == 0
    monkeypatch.setattr(f.db, "complete_effect", complete)
    assert not (await activate(f, chosen))["replayed"]


async def test_worker_rechecks_runtime_and_initiating_membership(publication_db):
    f = await fixture(publication_db)
    active = await activate(f)
    workflow = await f.db.get_workflow(UUID(active["workflow_id"]))
    worker = TinActivities(database=f.db, storage=f.storage, settings=f.settings, sandboxes=None)
    run = SimpleNamespace(project_id=f.project.id, started_by_clerk_user_id=ACTOR)
    await worker._check_private_attempt(run, workflow)
    f.settings.private_workflow_projects = set()
    with pytest.raises(PrivateWorkflowError):
        await worker._check_private_attempt(run, workflow)
    f.settings.private_workflow_projects = {f.project.id}
    await f.db.pool.execute("DELETE FROM project_memberships WHERE project_id=$1", f.project.id)
    with pytest.raises(LookupError):
        await worker._check_private_attempt(run, workflow)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(executor="project.task"),
        lambda d: d.update(key="research.deep_dive"),
        lambda d: d.update(model_route="anything"),
        lambda d: d.update(project_id=str(uuid4())),
        lambda d: d.update(system_wiki={"repo_id": "wiki/system"}),
        lambda d: d.update(schedule_modes=["daily"]),
        lambda d: d["procedure"].update(identity={"create": True}),
        lambda d: d["procedure"]["sandbox"].update(profile="default"),
        lambda d: d["procedure"]["sandbox"].update(egress="open"),
        lambda d: d["procedure"]["output"].update(path=".codex/auth.json"),
        lambda d: d["procedure"]["output"].update(path="wiki/INDEX.md"),
        lambda d: d["procedure"]["output"].update(allow_no_change=True),
        lambda d: d["procedure"]["output"].update(repair_policy="html-metadata-v3"),
        lambda d: d.update(
            integration_requirements=[
                {
                    "provider_key": "workspace.google",
                    "required": True,
                    "capabilities": ["gmail.messages.send"],
                }
            ]
        ),
    ],
)
def test_private_policy_rejects_unsupported_authority(mutation):
    guide = authoring_guide(settings=SimpleNamespace(), project_id=uuid4())
    definition = decode_workflow_source(
        guide["example_files"][PATH].encode(), definition_path=PATH
    ).definition
    mutation(definition)
    with pytest.raises((ValueError, TypeError)):
        validate_private_definition(definition)


def test_github_pr_workflows_and_worker_verification_are_supported():
    builtin = next(w for w in BUILTIN_WORKFLOWS if w.key == "site.health_improve")
    definition, _ = builtin.definition_and_resource_files()
    definition = deepcopy(definition)
    old = definition["key"]
    definition["key"] = "custom.site_fix"
    definition.pop("presentation", None)
    definition.pop("public_discovery", None)  # Catalog visibility is not a private field.
    definition["schedule_modes"] = ["on_demand"]
    # The private authoring language deliberately excludes legacy regex inputs.
    definition["input_schema"] = json.loads(
        authoring_guide(settings=SimpleNamespace(), project_id=uuid4())["example_files"][PATH]
    )["definition"]["input_schema"]
    procedure = definition["procedure"]
    procedure.pop("identity")  # Disabled built-in metadata is not a private capability.
    for field in ("prompt_path", "skills_path"):
        procedure[field] = procedure[field].replace(old, definition["key"])
    procedure["skill_files"] = [p.replace(old, definition["key"]) for p in procedure["skill_files"]]
    procedure["sandbox"] = {"profile": "isolated", "egress": "fenced", "timeout_seconds": 900}
    spec = validate_private_definition(definition)
    assert spec.result_kind == "github.pull_request" and spec.verification_commands
    assert spec.allow_no_change is True
    assert spec.verification_commands == ("git diff --check",)
    # Snapshot bounds belong to the gateway; a package's former limits are ignored.
    assert "limits" not in definition["procedure"]["workspace"]
    older = deepcopy(definition)
    older["procedure"]["workspace"]["limits"] = {"max_files": 1000, "max_bytes": 100_000_000}
    assert validate_private_definition(older) == spec
    ensure_schedule_allowed(definition, None)
    with pytest.raises(ValueError, match="scheduling"):
        ensure_schedule_allowed(definition, SimpleNamespace(cadence="daily"))


def test_key_lookup_rejects_ambiguity():
    rows = [SimpleNamespace(id=uuid4(), key="same") for _ in range(2)]
    with pytest.raises(ToolError, match="ambiguous"):
        _mcp_workflow(rows, "same")
    assert _mcp_workflow(rows, str(rows[1].id)) == rows[1]


def test_open_private_workflows_require_billing(monkeypatch):
    with pytest.raises(ValueError, match="Open private workflows require billing"):
        Settings(
            _env_file=None, **settings_values(monkeypatch, TIN_LITE_PRIVATE_WORKFLOWS_OPEN="true")
        )
    settings = Settings(
        _env_file=None,
        **settings_values(
            monkeypatch, TIN_LITE_PRIVATE_WORKFLOWS_OPEN="true", TIN_LITE_BILLING_ENABLED="true"
        ),
    )
    assert settings.private_workflows_open is True
    assert Settings(_env_file=None, **settings_values(monkeypatch)).private_workflows_open is False


def test_execution_readiness_accepts_the_list_or_the_open_gate():
    project = uuid4()
    listed = SimpleNamespace(private_workflow_projects={project}, e2b_isolated_template="iso")
    unlisted = SimpleNamespace(private_workflow_projects=set(), e2b_isolated_template="iso")
    opened = SimpleNamespace(
        private_workflow_projects=set(), private_workflows_open=True, e2b_isolated_template="iso"
    )
    assert private_execution_ready(listed, project)
    assert not private_execution_ready(unlisted, project)
    assert private_execution_ready(opened, project)
    opened.e2b_isolated_template = None
    assert not private_execution_ready(opened, project)
    assert "not admitted to private workflow execution" in private_execution_blocker(
        unlisted, project
    )
    assert "no isolated runtime" in private_execution_blocker(opened, project, "activation")
    assert private_execution_blocker(listed, project) is None
