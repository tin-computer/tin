"""Real admission/projection tests, with synthetic storage and no provider calls."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from test_private_workflows import ACTOR, app, mcp, structured
from test_private_workflows import fixture as project_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import x_draft, x_style
from tin_lite.catalog import BUILTIN_WORKFLOWS, WORKFLOW_SYSTEMS
from tin_lite.onboarding_experience import result_links
from tin_lite.public_workflows import load_public_workflows
from tin_lite.run_service import start_workflow_run
from tin_lite.x_draft_activities import XDraftActivities

GUIDE = b"# X writing style\n\nX account ID: 12345\n\nUse precise technical details.\n"


async def fixture(db, *, connected=False, guide=False):
    f = await project_fixture(db)
    f.revision = "d" * 40
    for system in WORKFLOW_SYSTEMS:
        await db.upsert_workflow_system(
            system_id=system.id, name=system.name, display_order=system.display_order
        )
    native = [w for w in BUILTIN_WORKFLOWS if w.key in {x_draft.KEY, x_style.KEY}]
    package = next(w for w in await load_public_workflows() if w.key == "social.x_compose")
    files = {w.definition_path: json.dumps(w.definition).encode() for w in native}
    files.update(package.files)
    for w in (*native, package):
        await db.upsert_registry_workflow(
            workflow_id=w.id,
            key=w.key,
            title=w.title,
            description=w.description,
            executor=w.executor,
            definition_repo_id="registry/workflows",
            definition_path=w.definition_path,
            current_commit_sha=f.revision,
            version_label=w.version_label,
            definition=w.definition,
        )
    original = f.storage.read_canonical_artifact

    async def read(**kwargs):
        if kwargs["repo_id"] == "registry/workflows":
            assert kwargs["commit_sha"] == f.revision
            return files[kwargs["path"]]
        return await original(**kwargs)

    f.storage.read_canonical_artifact = read
    f.connection = (
        SimpleNamespace(
            id=uuid4(),
            status="connected",
            external_account_id="12345",
            configuration={"protected": False, "granted_capabilities": ["x.posts.read"]},
        )
        if connected
        else None
    )
    db.get_integration_connection = AsyncMock(side_effect=lambda **kw: f.connection)
    f.runtime.integrations.x = SimpleNamespace(
        connection=AsyncMock(side_effect=lambda *a, **kw: f.connection)
    )
    if guide:
        f.storage.repo.edit({x_style.GUIDE_PATH: GUIDE})
    f.activities = XDraftActivities(
        database=db, storage=f.storage, integrations=f.runtime.integrations, settings=f.settings
    )
    f.workflow = await db.get_registry_workflow(x_draft.KEY)
    return f


async def start(f, **inputs):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        start_idempotency_key=str(uuid4()),
        input_payload={"direction": "Draft a tweet about our new workflow inputs.", **inputs},
    )


async def step(f, run, name):
    return await f.activities.step({"run_id": str(run.id), "step": name})


@pytest.mark.parametrize(
    ("connected", "guide", "samples", "voice", "capture"),
    [
        (False, False, "", "project_context", False),
        (True, True, "", "existing_guide", False),
        (False, True, "", "existing_guide", False),
        (True, False, "", "connected_account", True),
        (False, False, "A precise example of a product update.", "supplied_samples", True),
    ],
)
async def test_source_selection_and_retry_do_not_duplicate_children(
    publication_db, connected, guide, samples, voice, capture
):
    f = await fixture(publication_db, connected=connected, guide=guide)
    run = await start(f, supplied_samples=samples)
    await f.activities.prepare(str(run.id))
    await f.activities.prepare(str(run.id))
    prepared = await f.activities.saved(run.id, "prepare")
    assert prepared["voice"] == voice
    first = await step(f, run, "style")
    assert ("run_id" in first) == capture
    assert await step(f, run, "style") == first
    if capture:
        child_id = UUID(first["run_id"])
        await f.db.pool.execute(
            "UPDATE workflow_runs SET status='needs_input' WHERE id=$1", child_id
        )
        with pytest.raises(ValueError, match="finish review"):
            await step(f, run, "compose")
        assert await f.activities.saved(run.id, "compose") is None
        owner = "12345" if connected else "unbound"
        f.storage.repo.edit({x_style.GUIDE_PATH: GUIDE.replace(b"12345", owner.encode())})
        await f.db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", child_id)
    composed = await step(f, run, "compose")
    assert await step(f, run, "compose") == composed
    child = await f.db.get_run(UUID(composed["run_id"]))
    assert child.definition_commit_sha == run.definition_commit_sha
    assert child.input["direction"] == run.input["direction"]
    assert child.input["post_count"] == 1
    assert child.input["account_id"] == ("12345" if connected or guide else "")
    assert f.runtime.temporal.start_workflow.await_count == 1
    assert await f.db.pool.fetchval(
        "SELECT count(*) FROM workflow_runs WHERE project_id=$1", f.project.id
    ) == (3 if capture else 2)


async def test_changed_account_or_guide_stops_composition(publication_db):
    f = await fixture(publication_db, connected=True, guide=True)
    run = await start(f)
    await f.activities.prepare(str(run.id))
    await step(f, run, "style")
    f.connection.external_account_id = "99999"
    with pytest.raises(Exception, match="account changed"):
        await step(f, run, "compose")
    f.connection.external_account_id = "12345"
    f.storage.repo.edit({x_style.GUIDE_PATH: GUIDE.replace(b"12345", b"99999")})
    with pytest.raises(ValueError, match="guide changed"):
        await step(f, run, "compose")
    assert await f.activities.saved(run.id, "compose") is None


async def test_parent_projects_existing_draft_and_http_mcp_agree(publication_db, monkeypatch):
    f = await fixture(publication_db)
    server = mcp(f, monkeypatch)
    response = structured(
        await server.call_tool(
            "start_workflow",
            {
                "workflow_id": str(f.workflow.id),
                "project_id": str(f.project.id),
                "request_id": str(uuid4()),
                "inputs": {"direction": "Draft a tweet about the new product update."},
            },
        )
    )
    run = await f.db.get_run(UUID(response["id"]))
    await f.activities.prepare(str(run.id))
    await step(f, run, "style")
    compose = await step(f, run, "compose")
    path = "social/x-drafts/2026-09-30-product-update.json"
    sha = f.storage.repo.edit({path: b'{"posts":[]}'})
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, "
        "artifact_path=$3, artifact_ref=$4 WHERE id=$1",
        UUID(compose["run_id"]),
        sha,
        path,
        "code.storage:test",
    )
    await f.activities.finish(str(run.id))
    await f.activities.finish(str(run.id))
    done = await f.db.get_run(run.id)
    assert done.status.value == "succeeded"
    assert done.canonical_commit_sha == sha and done.artifact_path == path
    assert f.storage.repo.head == sha
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events WHERE run_id=$1 AND event_type='x_draft_ready'",
            run.id,
        )
        == 1
    )
    facts = await x_draft.facts(f.db, done)
    assert facts["steps"][0]["status"] == "skipped"
    assert facts["steps"][1]["status"] == "succeeded"
    mcp_view = structured(await server.call_tool("get_run", {"run_id": str(run.id)}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        http = await client.get(f"/api/workflows/runs/{run.id}")
    assert http.status_code == 200, http.text
    assert http.json()["x_draft"] == mcp_view["x_draft"] == facts
    assert "/files?" in result_links(f.settings, done)[0]["url"]
    assert "x_draft=social%2Fx-drafts" in mcp_view["result_links"][0]["url"]


async def test_child_budget_admission_requires_pinned_recipe(publication_db):
    from tin_lite.billing import BillingService

    f = await fixture(publication_db)
    run = await start(f)
    await f.activities.prepare(str(run.id))
    prepared = await f.activities.saved(run.id, "prepare")
    service = object.__new__(BillingService)
    service.db = f.db
    parent = {"executor": x_draft.KEY, "run_id": run.id}
    child = {
        "start_idempotency_key": f"x-draft:{run.id}:compose",
        "definition_commit_sha": f.revision,
    }
    async with f.db.pool.acquire() as conn:
        assert await service.valid_child(conn, parent, child, prepared["definitions"]["compose"])
        changed = deepcopy(prepared["definitions"]["compose"])
        changed["version"] = "99.0.0"
        assert not await service.valid_child(conn, parent, child, changed)
        child["definition_commit_sha"] = "f" * 40
        assert not await service.valid_child(
            conn, parent, child, prepared["definitions"]["compose"]
        )
