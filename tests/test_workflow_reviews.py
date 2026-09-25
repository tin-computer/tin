import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from test_content_draft import fixture as planned_fixture
from test_content_draft import start
from test_private_workflows import ACTOR, app, mcp, structured
from test_private_workflows import fixture as private_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_draft
from tin_lite.activities import TinActivities
from tin_lite.article_review import validate_article, validate_changes
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.content_draft_sources import ContentDraftSources
from tin_lite.organic_audit import canonical_json
from tin_lite.workflow_review_dispatch import dispatch_reviews
from tin_lite.workflow_review_store import ReviewConflict
from tin_lite.workflow_reviews import WorkflowReviews, saved_revision_context


async def setup(db, monkeypatch, *, planned=False):
    if planned:
        f = await planned_fixture(db, monkeypatch, clean=True)
    else:
        f = await private_fixture(db)
        f.settings.codex_api_projects = {f.project.id}
        f.settings.luna_api_key = "synthetic"
        spec = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.public_article")
        definition, resources = spec.definition_and_resource_files()
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

        async def resource(**kw):
            if kw["repo_id"] == "registry/workflows":
                return (
                    canonical_json(definition)
                    if kw["path"] == spec.definition_path
                    else resources[kw["path"]]
                )
            return await read(**kw)

        f.storage.read_canonical_artifact = resource
        f.workflow = await db.get_workflow(spec.id)
        f.inputs = {"brief": "Explain a practical buyer problem using our evidence."}
        f.storage.repo.edit(
            {".agents/skills/writing-style/SKILL.md": b"# Style\nDirect and specific.\n"}
        )
    f.reviews = WorkflowReviews(runtime=f.runtime, settings=f.settings)
    return f


async def save(f, run, *, assessment=False):
    if f.workflow.key == content_draft.KEY:
        await ContentDraftSources(database=f.db, storage=f.storage).prepare(
            run, output_validator=content_draft.CLEAN_VALIDATOR
        )
    path = f"content/drafts/{run.id}.md"
    raw = b"# A useful article\n\n" + b"Keep this useful explanation and source. " * 10
    notes = (
        b"# Generation notes\n\n## What changed\nClearer explanation.\n"
        b"\n## Feedback not followed\nNone.\n"
    )
    revision = f.storage.repo.edit({path: raw, content_draft.notes_path(path): notes})
    result = {"artifact_path": path, "canonical_commit_sha": revision}
    if assessment:
        result["content_editorial"] = {
            "schema": "content-editorial-check.v1",
            "outcome": "already_covered",
        }
    key = f"{run.id}:procedure_canonical_commit"
    async with f.db.effect_lock(key, "procedure_canonical_commit") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="procedure_canonical_commit")
        await f.db.complete_effect(conn, execution_key=key, result=result)
    await f.db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=revision,
        artifact_path=path,
        artifact_ref=f"code.storage://{f.project.state_repo_id}@{revision}/{path}",
    )
    if assessment:
        await f.db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", run.id)
    return await f.db.get_run(run.id)


async def revise(f, run, *, request_id=None, **kwargs):
    view = await f.reviews.view(run.id, ACTOR)
    return await f.reviews.request_changes(
        run_id=run.id,
        actor=ACTOR,
        feedback="Keep the opening; explain the missing mechanism.",
        request_id=request_id or uuid4(),
        token=view["review_token"],
        **kwargs,
    )


@pytest.mark.parametrize("planned", [False, True])
async def test_two_revisions_one_decision_and_only_latest_approval(
    publication_db, monkeypatch, planned
):
    f = await setup(publication_db, monkeypatch, planned=planned)
    first = await save(f, await start(f))
    original = await f.reviews.view(first.id, ACTOR)
    # Older waiting procedures keep their publication lease until finalization.
    await f.db.pool.execute("UPDATE workflow_runs SET lease_active=true WHERE id=$1", first.id)
    second = await revise(f, first)
    assert not (await f.db.get_run(first.id)).lease_active
    assert second.review_source_run_id == first.id and second.review_version == 2
    assert not await f.db.list_pending_decisions(project_id=f.project.id)
    with pytest.raises(ReviewConflict):
        await f.reviews.approve(run_id=first.id, actor=ACTOR, token=original["review_token"])
    await save(f, second)
    third = await revise(f, second)
    third = await save(f, third)
    decisions = await f.db.list_pending_decisions(project_id=f.project.id)
    assert len(decisions) == 1 and decisions[0]["id"] == first.id
    assert decisions[0]["run_id"] == third.id
    assert third.input == first.input
    view = await f.reviews.view(third.id, ACTOR)
    approved = await f.reviews.approve(run_id=third.id, actor=ACTOR, token=view["review_token"])
    assert approved.review_decision == "approved"
    assert not await f.db.list_pending_decisions(project_id=f.project.id)
    assert (await f.db.get_run(first.id)).status.value == "superseded"
    assert (await f.db.get_run(second.id)).status.value == "superseded"
    comparison = await f.reviews.compare(third.id, ACTOR)
    assert comparison["previous"]["run_id"] == str(second.id)
    assert comparison["revised"]["run_id"] == str(third.id)
    if planned:
        sources = ContentDraftSources(database=f.db, storage=f.storage)
        assert (await sources.saved(first.id))["item"] == (await sources.saved(third.id))["item"]


async def test_duplicate_and_approval_race_is_one_transaction(publication_db, monkeypatch):
    f = await setup(publication_db, monkeypatch)
    source = await save(f, await start(f))
    request_id = uuid4()
    view = await f.reviews.view(source.id, ACTOR)

    async def submit():
        return await f.reviews.request_changes(
            run_id=source.id,
            actor=ACTOR,
            feedback="Keep the opening.",
            request_id=request_id,
            token=view["review_token"],
        )

    a, b = await asyncio.gather(submit(), submit())
    assert a.id == b.id
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_review_commands") == 1
    await save(f, a)
    latest = await f.reviews.view(a.id, ACTOR)
    outcomes = await asyncio.gather(
        revise(f, a),
        f.reviews.approve(run_id=a.id, actor=ACTOR, token=latest["review_token"]),
        return_exceptions=True,
    )
    assert sum(isinstance(o, ReviewConflict) for o in outcomes) == 1
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_review_commands WHERE source_run_id=$1", a.id
        )
        == 1
    )


async def test_admission_failure_keeps_review_and_reference_pin(publication_db, monkeypatch):
    f = await setup(publication_db, monkeypatch)
    source = await save(f, await start(f))
    f.db.billing = SimpleNamespace(admit=AsyncMock(side_effect=ValueError("Not enough credits")))
    with pytest.raises(ValueError, match="credits"):
        await revise(f, source, billing_quote_id=uuid4())
    assert (await f.db.get_run(source.id)).status.value == "needs_input"
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_review_commands") == 0
    f.db.billing = None
    f.storage.repo.edit({"notes/source.md": b"Explicit reference evidence."})
    next_run = await revise(f, source, reference_files=["notes/source.md"])
    f.storage.repo.edit({"notes/source.md": b"A later, different file."})
    context = await saved_revision_context(f.db, f.storage, next_run)
    assert context["reference_files"][0]["content"] == "Explicit reference evidence."
    assert context["previous_copy"].startswith("# A useful article")


@pytest.mark.parametrize("planned", [False, True])
async def test_text_feedback_can_read_new_project_files_without_selecting_them(
    publication_db, monkeypatch, planned
):
    from tin_lite.writing_style import STYLE_PATH

    f = await setup(publication_db, monkeypatch, planned=planned)
    source = await save(f, await start(f))
    sources = ContentDraftSources(database=f.db, storage=f.storage)
    original = await sources.saved(source.id) if planned else None
    reference_sha = f.storage.repo.edit(
        {"notes/product.md": b"New evidence after drafting.", STYLE_PATH: b"A newer guide."}
    )
    feedback = "Keep the opening; use notes/product.md to correct the explanation."
    view = await f.reviews.view(source.id, ACTOR)
    # The dashboard sends only text, not a file selection.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.post(
            f"/api/workflows/runs/{source.id}/request-changes",
            json={
                "feedback": feedback,
                "request_id": str(uuid4()),
                "review_token": view["review_token"],
            },
        )
    assert response.status_code == 202, response.text
    run = await f.db.get_run(UUID(response.json()["id"]))
    if planned:
        prepared = await sources.prepare(run, output_validator=content_draft.CLEAN_VALIDATOR)
        assert prepared["item"] == original["item"]
        assert prepared["project_revision"] == original["project_revision"]
        assert prepared["evidence"] == original["evidence"]
    context = await saved_revision_context(f.db, f.storage, run)
    assert context["feedback"] == feedback
    assert context["reference_files"] == []
    assert context["project_revision"] == reference_sha
    assert context["writing_guide"] != "A newer guide."
    assert context["previous_copy"].startswith("# A useful article")
    # Subsequent edits cannot silently change the reference snapshot on retry.
    f.storage.repo.edit({"notes/product.md": b"Later changes must not replace pinned evidence."})
    assert await saved_revision_context(f.db, f.storage, run) == context
    activities = TinActivities(
        database=f.db,
        storage=f.storage,
        settings=f.settings,
        sandboxes=SimpleNamespace(create=AsyncMock(return_value="sandbox-test")),
    )
    _, spec = await activities._pinned_codex_procedure(run.id)
    prompt = spec.sandbox_context(inputs=run.input)["prompt"]
    assert feedback in prompt
    assert "an explicit reference_files selection is not required" in prompt
    assert "missing or ambiguous" in prompt
    await activities.create_codex_procedure_sandbox(str(run.id))
    await activities.create_codex_procedure_sandbox(str(run.id))
    attached = await f.db.get_run(run.id)
    assert attached.expected_head_sha == reference_sha
    assert (
        f.storage.repo.trees[attached.expected_head_sha]["notes/product.md"][1]
        == b"New evidence after drafting."
    )
    activities._sandboxes.create.assert_awaited_once()


async def test_standalone_revisions_keep_original_style_snapshot(publication_db, monkeypatch):
    from tin_lite.writing_style import STYLE_PATH

    f = await setup(publication_db, monkeypatch)
    first = await start(f)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET expected_head_sha=$2 WHERE id=$1", first.id, f.storage.repo.head
    )
    f.storage.repo.edit({STYLE_PATH: b"A later, different writing guide."})
    first = await save(f, first)
    second = await revise(f, first)
    second = await save(f, second)
    third = await revise(f, second)
    for run in (second, third):
        context = await saved_revision_context(f.db, f.storage, run)
        assert context["writing_guide"] == "# Style\nDirect and specific.\n"


async def test_assessment_recheck_and_http_mcp_share_contract(publication_db, monkeypatch):
    f = await setup(publication_db, monkeypatch, planned=True)
    source = await save(f, await start(f), assessment=True)
    server = mcp(f, monkeypatch)
    review = structured(await server.call_tool("get_workflow_review", {"run_id": str(source.id)}))
    assert review["can_request_changes"] and not review["can_approve"]
    result = structured(
        await server.call_tool(
            "request_workflow_changes",
            {
                "run_id": str(source.id),
                "feedback": "You missed this specific buyer gap.",
                "review_token": review["review_token"],
                "request_id": str(uuid4()),
            },
        )
    )
    revised = await f.db.get_run(UUID(result["run_id"]))
    row = await f.db.pool.fetchrow(
        "SELECT * FROM workflow_review_commands WHERE successor_run_id=$1", revised.id
    )
    assert row["coordinator_run_id"] is None
    await save(f, revised)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        old = (await client.get(f"/api/workflows/runs/{source.id}/review")).json()
        assert not old["is_current"] and not old["can_approve"]
        assert (await client.post(f"/api/workflows/runs/{source.id}/approve")).status_code == 409
        assert (
            await client.post(f"/api/decisions/{source.id}/apply", json={"action": "approve"})
        ).status_code == 409
    with pytest.raises(LookupError):
        await f.reviews.view(revised.id, "other_user")


def test_copy_and_notes_are_different_contracts():
    article = b"# Clear title\n\n" + b"A useful explanation. " * 20
    validate_article(article)
    with pytest.raises(ValueError, match="process notes"):
        validate_article(article + b"\n## What changed\nWe revised this.\n")
    validate_changes(
        b"# Generation notes\n\n## What changed\nShorter opening.\n"
        b"\n## Feedback not followed\nNone.\n"
    )
    with pytest.raises(ValueError, match="Feedback not followed"):
        validate_changes(b"# Generation notes\n\n## What changed\nShorter opening.\n")


async def test_failed_revision_retry_retains_copy_and_can_stop_chain(publication_db, monkeypatch):
    from tin_lite.run_service import start_workflow_run

    f = await setup(publication_db, monkeypatch)
    source = await save(f, await start(f))
    failed = await revise(f, source)
    await f.db.pool.execute("UPDATE workflow_runs SET status='failed' WHERE id=$1", failed.id)
    view = await f.reviews.view(failed.id, ACTOR)
    assert view["can_request_changes"] and not view["can_approve"]
    assert view["artifact"]["run_id"] == str(source.id)
    with pytest.raises(RuntimeError, match="Retry revision"):
        await start_workflow_run(
            runtime=f.runtime,
            settings=f.settings,
            workflow=f.workflow,
            project_id=f.project.id,
            started_by_clerk_user_id=ACTOR,
            input_payload=source.input,
            retry_of_run_id=failed.id,
            _prepare_only=True,
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get(f"/api/workflows/runs/{failed.id}/review/document")
        assert response.status_code == 200
        assert response.headers["X-Tin-Review-Copy-Run"] == str(source.id)
    retry = await revise(f, failed)
    row = await f.db.pool.fetchrow(
        "SELECT * FROM workflow_review_commands WHERE successor_run_id=$1", retry.id
    )
    assert row["coordinator_run_id"] == source.id and row["artifact_run_id"] == source.id
    await f.db.pool.execute("UPDATE workflow_runs SET status='failed' WHERE id=$1", retry.id)
    view = await f.reviews.view(retry.id, ACTOR)
    await f.reviews.cancel_failed_revision(run_id=retry.id, actor=ACTOR, token=view["review_token"])
    await f.reviews.cancel_failed_revision(run_id=retry.id, actor=ACTOR, token=view["review_token"])
    assert (await f.db.get_run(retry.id)).status.value == "stopped"
    assert not (await f.reviews.view(retry.id, ACTOR))["can_request_changes"]
    handle = SimpleNamespace(signal=AsyncMock(), cancel=AsyncMock())
    f.runtime.temporal.get_workflow_handle = lambda _: handle
    await dispatch_reviews(f.runtime, f.settings)
    handle.cancel.assert_awaited_once()
    assert (await f.db.get_run(source.id)).status.value == "superseded"


@pytest.mark.parametrize("ended", ["failed", "stopped"])
async def test_failed_or_stopped_revision_keeps_reviewed_draft_in_program_progress(
    publication_db, monkeypatch, ended
):
    from tin_lite.workflow_inputs import WorkflowInputError

    f = await setup(publication_db, monkeypatch, planned=True)
    source = await save(f, await start(f))
    revision = await revise(f, source)
    await f.db.pool.execute("UPDATE workflow_runs SET status='failed' WHERE id=$1", revision.id)
    if ended == "stopped":
        view = await f.reviews.view(revision.id, ACTOR)
        await f.reviews.cancel_failed_revision(
            run_id=revision.id, actor=ACTOR, token=view["review_token"]
        )
    assert (await f.db.get_run(revision.id)).status.value == ended
    choices = await f.service.discover(project_id=f.project.id, program_id=f.configured.id)
    item = next(i for i in choices["items"] if i["id"] == f.inputs["item_id"])
    assert item["draft"]["run_id"] == str(source.id) and item["draft"]["has_output"]
    assert not item["available"] and choices["next"]["item_id"] != item["id"]
    # Drafting this item again still needs the explicit rewrite; no new article is bought.
    with pytest.raises(WorkflowInputError, match="already has a draft"):
        await start(f)


async def test_dispatch_lost_ack_retries_only_command(publication_db, monkeypatch):
    f = await setup(publication_db, monkeypatch)
    source = await save(f, await start(f))
    successor = await revise(f, source)
    handle = SimpleNamespace(signal=AsyncMock(side_effect=[OSError("Lost ack"), None, None]))
    f.runtime.temporal.get_workflow_handle = lambda _: handle
    for _ in range(3):
        await dispatch_reviews(f.runtime, f.settings)
    assert handle.signal.await_count == 3
    assert len({str(c) for c in handle.signal.await_args_list}) == 1
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_review_commands") == 1
    assert (await f.db.get_run(successor.id)).status.value == "pending"
    await f.db.pool.execute(
        "UPDATE workflow_review_commands SET dispatch_state='received' WHERE successor_run_id=$1",
        successor.id,
    )
    await dispatch_reviews(f.runtime, f.settings)
    assert handle.signal.await_count == 3


async def test_revision_keeps_saved_card_not_edited_future_inputs(publication_db, monkeypatch):
    from tin_lite.run_service import start_workflow_run
    from tin_lite.workflow_inputs import normalize_workflow_inputs

    f = await setup(publication_db, monkeypatch)
    f.inputs = normalize_workflow_inputs(
        schema=f.workflow.definition["input_schema"], project_id=f.project.id, inputs=f.inputs
    )
    configured = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=f.workflow.id,
        definition_commit_sha=f.workflow.current_commit_sha,
        name="My articles",
        inputs=f.inputs,
        input_schema=f.workflow.definition["input_schema"],
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
    )
    await f.db.pool.execute(
        "UPDATE project_workflows SET status='active' WHERE id=$1", configured.id
    )
    source = await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=f.inputs,
        project_workflow_id=configured.id,
        definition_commit_sha=configured.definition_commit_sha,
        _prepare_only=True,
    )
    await save(f, source)
    await f.db.pool.execute(
        "UPDATE project_workflows SET inputs=$2::jsonb WHERE id=$1",
        configured.id,
        '{"brief":"A different future article"}',
    )
    successor = await revise(f, source)
    assert successor.project_workflow_id == configured.id
    assert successor.input == source.input
    await save(f, successor)
    await f.db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", successor.id)
    card = await f.db.get_project_workflow(configured.id)
    assert card.last_run_id == successor.id
    assert card.inputs["brief"] == "A different future article"
