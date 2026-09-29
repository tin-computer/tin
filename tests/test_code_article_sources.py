"""Reviewed article admission and replay through the ordinary code executor."""

import asyncio
import json
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from temporalio.testing import ActivityEnvironment
from test_content_delivery import approve, publish_draft
from test_content_draft import fixture as draft_fixture
from test_content_draft import start as start_draft
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db
from test_workflow_code import CheckpointStorage, SyntheticCompute, definition

from tin_lite import code_article_sources
from tin_lite.activities import TinActivities
from tin_lite.code_activities import CodeActivities
from tin_lite.domain import RunStatus
from tin_lite.private_workflows import validate_private_definition
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_code import validate_code_definition
from tin_lite.workflow_inputs import WorkflowInputError
from tin_lite.writing_style import STYLE_PATH


def consumer():
    value = definition()
    value["code"]["approved_article"] = {"input": "article_run"}
    value["input_schema"]["properties"]["article_run"] = {"type": "string", "format": "uuid"}
    value["input_schema"]["required"].append("article_run")
    value["human_review"] = {"eligible": True, "summary": "Review the social drafts"}
    return value


def test_private_code_can_declare_one_approved_article():
    value = consumer()
    validate_private_definition(value)
    assert validate_code_definition(value).approved_article_input == "article_run"
    assert validate_code_definition(definition()).approved_article_input is None


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["code"].update(approved_article={"input": "project_id"}),
        lambda d: d["code"].update(approved_article={"input": "missing"}),
        lambda d: d["code"].update(approved_article={"input": []}),
        lambda d: d["code"].update(approved_article={"input": "article_run", "path": "BRAND.md"}),
        lambda d: d["input_schema"]["required"].remove("article_run"),
        lambda d: d["input_schema"]["properties"]["article_run"].update(
            format="uri", maxLength=500
        ),
        lambda d: d.update(schedule_modes=["on_demand", "weekly"]),
    ],
)
def test_article_contract_rejects_ambiguous_sources_or_schedules(change):
    value = consumer()
    change(value)
    with pytest.raises(ValueError):
        validate_code_definition(value)


async def fixture(db, monkeypatch, *, approved=True):
    f = await draft_fixture(db, monkeypatch)
    draft = await start_draft(f)
    draft, f.article_context, f.raw = await publish_draft(f, draft)
    f.source = await approve(f, draft) if approved else draft
    f.definition = consumer()
    f.definition["key"] = "social.article_fixture"
    f.definition["code"]["files"] = ["main.py"]
    workflow_id = uuid4()
    f.path = "workflow_packages/social.article_fixture/workflow.json"
    revision = f.storage.repo.edit(
        {
            f.path: json.dumps(
                {"package_format": "tin-workflow-package-v1", "definition": f.definition}
            ).encode(),
            "workflow_packages/social.article_fixture/main.py": b"def run(ctx, inputs): pass\n",
        }
    )
    await db.upsert_registry_workflow(
        workflow_id=workflow_id,
        key=f.definition["key"],
        title="Article fixture",
        description="Synthetic article consumer",
        executor="workflow.code",
        definition_repo_id=f.project.state_repo_id,
        definition_path=f.path,
        current_commit_sha=revision,
        version_label="1.0.0",
        definition=f.definition,
    )
    f.consumer = await db.get_workflow(workflow_id)
    f.consumer_inputs = {"article_run": str(f.source.id), "minimum_cents": 1000}
    return f


async def start(f, *, key=None, inputs=None):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.consumer,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        start_idempotency_key=key,
        input_payload=inputs or f.consumer_inputs,
    )


async def test_source_pinned_atomically_and_same_start_reuses_snapshot(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    request_id = str(uuid4())
    first, repeated = await asyncio.gather(start(f, key=request_id), start(f, key=request_id))
    assert first.id == repeated.id
    spec = validate_code_definition(f.definition)
    source = await code_article_sources.saved_source(f.db, first, spec)
    assert source["source_revision"] == f.source.canonical_commit_sha
    assert "Verification notes" not in source["article"]
    assert "Concrete mechanisms" in source["style"]["content"]
    f.storage.repo.edit(
        {f.source.artifact_path: b"Unapproved replacement", STYLE_PATH: b"New voice"}
    )
    assert (await start(f, key=request_id)).id == first.id
    assert await code_article_sources.saved_source(f.db, first, spec) == source
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE workflow_id=$1", f.consumer.id
        )
        == 1
    )


async def test_unapproved_and_foreign_sources_fail_before_dispatch(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, approved=False)
    f.runtime.temporal.start_workflow.reset_mock()
    with pytest.raises(WorkflowInputError, match="approve"):
        await start(f)
    other = await f.db.create_project(name="Other project", state_repo_id="projects/other")
    await f.db.pool.execute(
        "UPDATE workflow_runs SET project_id=$2 WHERE id=$1", f.source.id, other.id
    )
    with pytest.raises(WorkflowInputError, match="from this project"):
        await start(f)
    f.runtime.temporal.start_workflow.assert_not_called()


async def test_admission_rechecks_selected_revision_and_requires_snapshot(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    source = await code_article_sources.select(
        database=f.db,
        storage=f.storage,
        project_id=f.project.id,
        definition=f.definition,
        inputs=f.consumer_inputs,
    )
    args = dict(
        project_id=f.project.id,
        workflow_id=f.consumer.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=f.consumer_inputs,
        pinned_definition=f.definition,
        definition_commit_sha=f.consumer.current_commit_sha,
    )
    with pytest.raises(ValueError, match="Select the approved article"):
        await f.db.create_run(**args)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET canonical_commit_sha=$2 WHERE id=$1", f.source.id, "f" * 40
    )
    with pytest.raises(ValueError, match="approved"):
        await f.db.create_run(**args, approved_article_source=source)
    assert not await f.db.pool.fetchval(
        "SELECT id FROM workflow_runs WHERE workflow_id=$1", f.consumer.id
    )


async def test_mcp_lists_approved_choices_and_http_uses_same_guard(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    server = mcp(f, monkeypatch)
    contract = structured(
        await server.call_tool(
            "get_workflow", {"project_id": str(f.project.id), "workflow_id": str(f.consumer.id)}
        )
    )
    assert contract["preparation"]["source_input"] == "article_run"
    assert contract["preparation"]["articles"][0]["run_id"] == str(f.source.id)
    assert contract["preparation"]["sources"][0]["candidates"][0]["read_url"].endswith(
        f"/document/{f.source.id}?project={f.project.id}"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        choices = await client.get(f"/api/projects/{f.project.id}/workflow-sources/{f.consumer.id}")
        assert choices.status_code == 200, choices.text
        assert choices.json()["slots"][0]["candidates"][0]["run_id"] == str(f.source.id)
        assert (
            choices.json()["slots"][0]["candidates"]
            == contract["preparation"]["sources"][0]["candidates"]
        )
        response = await client.post(
            f"/api/workflows/{f.consumer.id}/runs",
            json={"project_id": str(f.project.id), "inputs": f.consumer_inputs},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 202, response.text
        run = await client.get(f"/api/workflows/runs/{response.json()['id']}")
        source = run.json()["selected_sources"]["approved_article"]
        assert source["run_id"] == str(f.source.id)
        assert source["revision"] == f.source.canonical_commit_sha
        assert "article" not in source and "style" not in source
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, "outsider")), base_url="https://tin.test"
    ) as client:
        choices = await client.get(f"/api/projects/{f.project.id}/workflow-sources/{f.consumer.id}")
        assert choices.status_code == 404
        denied = await client.post(
            f"/api/workflows/{f.consumer.id}/runs",
            json={"project_id": str(f.project.id), "inputs": f.consumer_inputs},
        )
        assert denied.status_code == 404


async def test_source_discovery_filters_unapproved_article(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, approved=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        choices = await client.get(f"/api/projects/{f.project.id}/workflow-sources/{f.consumer.id}")
    assert choices.status_code == 200, choices.text
    slot = choices.json()["slots"][0]
    assert slot["candidates"] == []
    assert "Review" in slot["missing_reason"]
    contract = structured(
        await mcp(f, monkeypatch).call_tool(
            "get_workflow", {"project_id": str(f.project.id), "workflow_id": str(f.consumer.id)}
        )
    )
    assert contract["preparation"]["sources"][0]["candidates"] == []
    assert contract["readiness"]["state"] == "blocked"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        workflow = await client.get(
            f"/api/workflows/{f.consumer.id}", params={"project_id": str(f.project.id)}
        )
        listing = await client.get("/api/workflows", params={"project_id": str(f.project.id)})
    assert workflow.json()["readiness"]["state"] == "blocked"
    listed = next(item for item in listing.json() if item["id"] == str(f.consumer.id))
    assert listed["readiness"]["state"] == "blocked"


async def test_invalid_publication_proof_is_not_a_source(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET canonical_commit_sha=$2 WHERE id=$1", f.source.id, "f" * 40
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        choices = await client.get(f"/api/projects/{f.project.id}/workflow-sources/{f.consumer.id}")
        workflow = await client.get(
            f"/api/workflows/{f.consumer.id}", params={"project_id": str(f.project.id)}
        )
    assert choices.json()["slots"][0]["candidates"] == []
    assert workflow.json()["readiness"]["state"] == "blocked"


async def test_mcp_start_replay_reports_existing_run(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    server = mcp(f, monkeypatch)
    arguments = {
        "project_id": str(f.project.id),
        "workflow_id": str(f.consumer.id),
        "inputs": f.consumer_inputs,
        "request_id": str(uuid4()),
    }
    first = structured(await server.call_tool("start_workflow", arguments))
    replay = structured(await server.call_tool("start_workflow", arguments))
    assert first["id"] == replay["id"]
    assert first["already_started"] is False
    assert replay["already_started"] is True
    assert "Tin already started" in " ".join(replay["relay"])


async def test_execution_reuses_source_and_publishes_one_reviewable_artifact(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    run = await start(f)
    output = {"path": f.definition["code"]["output"]["path"], "content": "# Social drafts\n"}

    class Compute(SyntheticCompute):
        async def run_code_and_kill(self, *, packet, **kwargs):
            self.source = deepcopy(packet["context"]["approved_article"])
            return await super().run_code_and_kill(packet=packet, **kwargs)

    compute = Compute(output)
    f.storage.branch_revision = None
    monkeypatch.setattr(
        f.storage, "stage_native_output", CheckpointStorage.stage_native_output.__get__(f.storage)
    )
    common = TinActivities(database=f.db, storage=f.storage, settings=f.settings, sandboxes=compute)
    code = CodeActivities(common=common)
    f.storage.repo.edit(
        {f.source.artifact_path: b"Later unapproved copy", STYLE_PATH: b"Later style"}
    )
    env = ActivityEnvironment()
    await env.run(code.execute, str(run.id))
    await env.run(code.execute, str(run.id))
    await code.publish(str(run.id))
    await code.publish(str(run.id))
    assert compute.calls == 1
    assert "Later" not in compute.source["article"]
    assert "Concrete mechanisms" in compute.source["style"]["content"]
    assert await code.review(str(run.id))
    assert (await f.db.get_run(run.id)).status == RunStatus.NEEDS_INPUT
    await f.db.set_review_actor(run_id=run.id, clerk_user_id=ACTOR)
    await code.approve(str(run.id))
    await code.project(str(run.id))
    await code.project(str(run.id))
    assert (await f.db.get_run(run.id)).status == RunStatus.SUCCEEDED
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='code_workflow_ready'",
            run.id,
        )
        == 1
    )
