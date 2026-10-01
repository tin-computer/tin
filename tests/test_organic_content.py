"""Fixed-parent content handoff with real Postgres, fixture models/storage/GitHub."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from test_content_delivery import configured
from test_private_workflows import ACTOR
from test_procedure_publication import publication_db as publication_db
from test_workflow_reviews import revise, save, setup

from tin_lite import content_draft, organic_system
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.content_repository_delivery import source_key
from tin_lite.organic_audit import canonical_json, digest
from tin_lite.organic_content import pin_destination
from tin_lite.organic_system_activities import OrganicSystemActivities
from tin_lite.organic_system_control import stop_system
from tin_lite.service_pricing import service_terms
from tin_lite.workflow_inputs import normalize_workflow_inputs


async def effect(db, key, value):
    async with db.effect_lock(key, organic_system.KEY) as (conn, _):
        await db.start_effect(conn, execution_key=key, operation=organic_system.KEY)
        await db.complete_effect(conn, execution_key=key, result=value)


async def system_fixture(
    db, monkeypatch, *, github=True, inputs=None, temporal=None, content=None, policy=None
):
    f = await configured(await setup(db, monkeypatch, planned=True))
    monkeypatch.setattr(
        db,
        "get_integration_connection",
        AsyncMock(
            return_value=(
                SimpleNamespace(
                    status="connected", configuration={"selected_repository": "owner/site"}
                )
                if github
                else None
            )
        ),
    )
    definitions = {w.key: w.definition for w in BUILTIN_WORKFLOWS}
    definitions["content.generate"] = f.workflow.definition
    resources = {}
    for key in (organic_system.KEY, "content.deliver", "website.change"):
        spec = next(w for w in BUILTIN_WORKFLOWS if w.key == key)
        definition, files = spec.definition_and_resource_files()
        definitions[key] = definition
        resources.update(files)
        await db.upsert_registry_workflow(
            workflow_id=spec.id,
            key=spec.key,
            title=spec.title,
            description=spec.description,
            executor=spec.executor,
            definition_repo_id="registry/workflows",
            definition_path=spec.definition_path,
            current_commit_sha="e" * 40,
            version_label=spec.version_label,
            definition=definition,
        )
    read = f.storage.read_canonical_artifact

    async def read_resource(**kw):
        if kw["repo_id"] == "registry/workflows":
            key = kw["path"].removeprefix("workflows/").removesuffix(".json")
            if key in definitions:
                return canonical_json(definitions[key])
            if kw["path"] in resources:
                return resources[kw["path"]]
        return await read(**kw)

    f.storage.read_canonical_artifact = read_resource
    template = await db.get_registry_workflow(organic_system.KEY)
    inputs = normalize_workflow_inputs(
        schema=template.definition["input_schema"],
        project_id=f.project.id,
        inputs={
            "site_url": "https://example.com/",
            "market": "US",
            "buyer_context": "Useful software for independent consultants.",
            "start_date": "2026-09-14",
            **(inputs or {}),
        },
    )
    f.parent, _ = await db.create_run(
        project_id=f.project.id,
        workflow_id=template.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=inputs,
        pinned_definition=template.definition,
        definition_commit_sha="e" * 40,
    )
    await db.mark_run_running(f.parent.id)
    f.parent = await db.get_run(f.parent.id)
    intent = await pin_destination(db, f.runtime.integrations, f.parent)
    await effect(
        db,
        f"traffic:{f.parent.id}:prepare",
        {
            "definition_revision": "e" * 40,
            # These cases pin v5, whose delivery step is content.deliver.
            "policy": policy or organic_system.REFRESH_POLICY,
            "definitions": {
                step: definitions[key]
                for step, key in organic_system.policy_steps(
                    policy or organic_system.REFRESH_POLICY
                ).items()
            },
            "input_sha256": digest(inputs),
            "content_delivery": intent,
        },
    )
    await effect(
        db, f"traffic:{f.parent.id}:step:content", content or {"run_id": str(f.initial.id)}
    )
    f.system = OrganicSystemActivities(
        database=db,
        storage=f.storage,
        settings=f.settings,
        integrations=f.runtime.integrations,
        temporal=temporal,
    )
    return f


async def draft(f):
    payload = {"run_id": str(f.parent.id), "step": "draft"}
    result = await f.system.organic_system_step(payload)
    assert await f.system.organic_system_step(payload) == result
    run = await f.db.get_run(UUID(result["run_id"]))
    selected = (await f.db.get_effect(content_draft.selection_key(run.id))).result
    assert selected["mode"] == "next" and selected["item"]["id"] == f.inputs["item_id"]
    assert not selected.get("delivery")  # Never also run the old Markdown publisher.
    return await save(f, run)


async def approve(f, run):
    view = await f.reviews.view(run.id, ACTOR)
    await f.reviews.approve(run_id=run.id, actor=ACTOR, token=view["review_token"])
    # Fixture the procedure's terminal projection, not a real model run.
    await f.db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", run.id)
    return await f.db.get_run(run.id)


async def test_parent_drafts_next_and_delivers_only_final_approved_revision(
    publication_db, monkeypatch
):
    f = await system_fixture(publication_db, monkeypatch)
    first = await draft(f)
    intent = (await f.db.get_effect(content_draft.selection_key(first.id))).result[
        "system_delivery"
    ]
    assert intent["binding"]["repository"] == "owner/site"
    status = await f.delivery.status(first)
    assert status["approval_label"] == "Approve & open PR"
    assert (await f.system._child_inputs(f.parent, "delivery"))[1] == "draft_unavailable"
    second = await save(f, await revise(f, first))
    assert (await f.db.get_effect(content_draft.selection_key(second.id))).result[
        "system_delivery"
    ] == intent
    second = await approve(f, second)
    facts = await organic_system.system_facts(
        database=f.db, project_id=f.project.id, run_id=f.parent.id
    )
    assert next(row for row in facts["steps"] if row["step"] == "draft")["run_id"] == str(second.id)
    payload = {"run_id": str(f.parent.id), "step": "delivery"}
    result = await f.system.organic_system_step(payload)
    assert await f.system.organic_system_step(payload) == result
    source = (await f.db.get_effect(source_key(UUID(result["run_id"])))).result
    assert source["source_run_id"] == str(second.id)
    assert source["source_revision"] == second.canonical_commit_sha
    f.runtime.integrations.github_create_pull_request.assert_not_awaited()
    assert (await f.delivery.status(second))["delivery_run_id"] == result["run_id"]


async def test_no_github_retains_reviewed_markdown_without_delivery(publication_db, monkeypatch):
    f = await system_fixture(publication_db, monkeypatch, github=False)
    run = await approve(f, await draft(f))
    assert await f.delivery.status(run) is None
    result = await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "delivery"})
    assert result == {"status": "skipped", "reason": "github_not_connected"}
    assert (await f.db.get_run(run.id)).artifact_path == f"content/drafts/{run.id}.md"
    assert await f.storage.read_canonical_artifact(
        repo_id=f.project.state_repo_id,
        commit_sha=run.canonical_commit_sha,
        path=run.artifact_path,
    )
    f.runtime.integrations.github_create_pull_request.assert_not_awaited()


async def test_system_approval_choice_cannot_trigger_generic_markdown_publisher(
    publication_db, monkeypatch
):
    f = await system_fixture(publication_db, monkeypatch)
    run = await draft(f)
    with pytest.raises(ValueError, match="reviewable PR"):
        await f.delivery.choose(run=run, mode="github_commit", actor=ACTOR)
    await f.delivery.choose(run=run, mode="github_pr", actor=ACTOR)
    assert await f.delivery.intent(run) is None
    assert (await f.delivery.status(run))["approval_label"] == "Approve & open PR"
    assert (await f.delivery.statuses([run]))[run.id]["system_run_id"] == str(f.parent.id)
    await f.delivery.choose(run=run, mode="none", actor=ACTOR)
    assert await f.delivery.status(run) is None
    run = await approve(f, run)
    result = await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "delivery"})
    assert result == {"status": "skipped", "reason": "draft_only_selected"}
    await f.delivery.deliver(run.id)
    f.runtime.integrations.github_create_pull_request.assert_not_awaited()


async def test_repository_switch_cannot_redirect_approved_system_article(
    publication_db, monkeypatch
):
    f = await system_fixture(publication_db, monkeypatch)
    run = await approve(f, await draft(f))
    f.runtime.integrations.github_repository_binding.return_value = replace(
        f.binding, repository_id=999
    )
    with pytest.raises(ValueError, match="repository changed"):
        await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "delivery"})
    assert (await f.db.get_run(run.id)).status.value == "succeeded"
    f.runtime.integrations.github_create_pull_request.assert_not_awaited()


async def test_no_copy_assessment_never_creates_delivery(publication_db, monkeypatch):
    f = await system_fixture(publication_db, monkeypatch)
    result = await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "draft"})
    run = await save(f, await f.db.get_run(UUID(result["run_id"])), assessment=True)
    assert await f.delivery.status(run) is None
    result = await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "delivery"})
    assert result == {"status": "skipped", "reason": "already_covered"}


async def test_stop_fences_delivery_and_preserves_waiting_draft(publication_db, monkeypatch):
    f = await system_fixture(publication_db, monkeypatch)
    first = await draft(f)
    f.runtime.temporal.get_workflow_handle = Mock(
        return_value=SimpleNamespace(cancel=AsyncMock(), signal=AsyncMock())
    )
    result = await stop_system(runtime=f.runtime, run_id=f.parent.id, actor=ACTOR)
    assert result["status"] == "stopped"
    assert (await f.db.get_run(first.id)).status.value == "needs_input"
    assert result["finishing_run_ids"] == []
    assert await f.delivery.status(first) is None
    with pytest.raises(Exception, match="no longer active"):
        await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "delivery"})
    f.runtime.integrations.github_create_pull_request.assert_not_awaited()


async def test_rechecking_completed_assessment_does_not_promise_finished_parent_delivery(
    publication_db, monkeypatch
):
    f = await system_fixture(publication_db, monkeypatch)
    result = await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "draft"})
    first = await save(f, await f.db.get_run(UUID(result["run_id"])), assessment=True)
    await f.db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", f.parent.id)
    second = await save(f, await revise(f, first))
    assert await f.delivery.status(second) is None


async def test_explicit_draft_only_does_not_touch_github():
    integrations = SimpleNamespace(github_repository_binding=AsyncMock())
    database = SimpleNamespace(get_integration_connection=AsyncMock())
    run = SimpleNamespace(input={"content_delivery": "draft_only"})
    assert (await pin_destination(database, integrations, run))["mode"] == "draft_only"
    database.get_integration_connection.assert_not_awaited()
    integrations.github_repository_binding.assert_not_awaited()


def test_new_parent_cost_bounds_leave_historical_definition_unchanged():
    definition = next(w.definition for w in BUILTIN_WORKFLOWS if w.key == organic_system.KEY)
    historical = {**definition, "organic_system_policy": organic_system.LEGACY_POLICY}
    # A saved $9 keyword limit, plus audit $2 and content plan $1; drafts add $5 each and the
    # first page refresh $2.50 ($24.50 of children); the pool caps the drafting recipe at the
    # keyword limit plus $10 plus the refresh's $2.50 ($21.50).
    assert service_terms(historical)["maximum_nanos"] == 12_000_000_000
    assert service_terms(definition)["maximum_nanos"] == 21_500_000_000
    assert (
        service_terms(definition, inputs={"content_delivery": "draft_only"})["maximum_nanos"]
        == 19_500_000_000
    )
