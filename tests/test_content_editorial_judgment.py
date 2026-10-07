import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from test_clean_content_draft import ARTICLE, notes
from test_content_delivery import context as sample_context
from test_content_draft import fixture, start
from test_private_workflows import app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_draft
from tin_lite.activities import TinActivities
from tin_lite.content_delivery import ContentDelivery, article_body
from tin_lite.content_draft_progress import check_existing, next_item
from tin_lite.content_editorial_judgment import (
    NO_DRAFT,
    assessment_document,
    validate_pair,
)
from tin_lite.content_plan import plan_path
from tin_lite.organic_audit import canonical_json, digest


def judgment(context, outcome="already_covered"):
    return {
        "outcome": outcome,
        "rationale": "The current docs and guide already explain the complete messaging setup.",
        "reader_gain": "A missing recovery sequence helps readers resume interrupted work."
        if outcome == "draft"
        else "",
        "change_scope": "Add the missing recovery steps; preserve the API reference and navigation."
        if outcome == "draft"
        else "",
        "compared_pages": [
            {
                "url": context["item"].get("destination") or "https://example.com/docs",
                "status": "inspected",
                "coverage": "Existing setup covers registration, readiness, sending and replies.",
            }
        ],
    }


def judgment_notes(context, value):
    return notes(context).replace(
        b"# Generation notes\n",
        (
            "# Generation notes\n\n## Editorial judgment\n```json\n" + json.dumps(value) + "\n```\n"
        ).encode(),
    )


@pytest.mark.parametrize("outcome", ["draft", *sorted(NO_DRAFT)])
def test_pair_contract_and_article_only_delivery(outcome):
    context = {**sample_context(), "output_validator": content_draft.EDITORIAL_VALIDATOR}
    value = judgment(context, outcome)
    primary = ARTICLE if outcome == "draft" else assessment_document(value)
    proof = validate_pair(primary, judgment_notes(context, value), context)
    assert proof["outcome"] == outcome
    if outcome == "draft":
        assert article_body(primary, context)[0].encode() == ARTICLE
        with pytest.raises(ValueError, match="requires article"):
            validate_pair(assessment_document(value), judgment_notes(context, value), context)
    else:
        with pytest.raises(ValueError, match="not an article"):
            article_body(primary, context)
        with pytest.raises(ValueError, match="only its assessment"):
            validate_pair(ARTICLE, judgment_notes(context, value), context)


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "unavailable",
        "no_gain",
        "wrong_target",
        "duplicate",
        "credentials",
        "unsupported",
        "provenance",
    ],
)
def test_invalid_judgments_cannot_publish(bad):
    context = {**sample_context(), "output_validator": content_draft.EDITORIAL_VALIDATOR}
    context["item"] = {
        **context["item"],
        "action": "update_page",
        "destination": "https://example.com/docs",
    }
    value = judgment(context, "draft" if bad in {"no_gain", "wrong_target"} else "already_covered")
    if bad == "unavailable":
        value["compared_pages"][0]["status"] = "unavailable"
    if bad == "no_gain":
        value["reader_gain"] = "Better."
    if bad == "wrong_target":
        value["compared_pages"][0]["url"] = "https://example.com/other"
    if bad == "duplicate":
        value["compared_pages"].append(deepcopy(value["compared_pages"][0]))
        value["compared_pages"][1]["url"] += "#quickstart"
    if bad == "credentials":
        value["compared_pages"][0]["url"] = "https://name:secret@example.com/docs"
    if bad == "unsupported":
        value["outcome"] = "publish_now"
    raw = judgment_notes(context, value)
    if bad == "missing":
        raw = notes(context)
    if bad == "provenance":
        raw = raw.replace(context["program_id"].encode(), b"other")
    with pytest.raises(ValueError):
        validate_pair(ARTICLE, raw, context)


def update_context():
    context = {**sample_context(), "output_validator": content_draft.EDITORIAL_VALIDATOR}
    context["item"] = {
        **context["item"],
        "action": "update_page",
        "destination": "https://example.com/docs",
    }
    return context


def test_an_unreadable_extra_page_does_not_sink_an_already_covered_judgment():
    # Sheepdogs: the refresh's own page was read in full; the canvas homepage could not be.
    context = update_context()
    value = judgment(context)
    value["compared_pages"].append(
        {
            "url": "https://example.com/",
            "status": "unavailable",
            "coverage": "The homepage returned no readable text to compare.",
        }
    )
    proof = validate_pair(assessment_document(value), judgment_notes(context, value), context)
    assert proof["outcome"] == "already_covered"


def test_covered_without_reading_the_destination_finishes_as_insufficient_evidence():
    # Claiming coverage from a page that couldn't be read is not evidence; the paid run
    # still finishes, with the honest outcome instead of a crash.
    context = update_context()
    value = judgment(context)
    value["compared_pages"][0]["status"] = "unavailable"
    proof = validate_pair(assessment_document(value), judgment_notes(context, value), context)
    assert proof["outcome"] == "insufficient_evidence"


async def prepared(f, outcome, *, publish=True):
    run = await start(f, key=str(uuid4()))
    activities = TinActivities(
        database=f.db,
        storage=f.storage,
        settings=f.settings,
        sandboxes=SimpleNamespace(create=AsyncMock(return_value="sandbox-test"), kill=AsyncMock()),
    )
    await activities.prepare_codex_procedure(str(run.id))
    ctx = await f.service.saved(run.id)
    assert ctx["output_validator"] == content_draft.EDITORIAL_VALIDATOR
    await activities.create_codex_procedure_sandbox(str(run.id))
    run = await f.db.get_run(run.id)
    value = judgment(ctx, outcome)
    primary = ARTICLE if outcome == "draft" else assessment_document(value)
    path = content_draft.PATH_TEMPLATE.format(run_id=run.id)
    base = f.storage.repo.head
    revision = f.storage.repo.edit(
        {path: primary, content_draft.notes_path(path): judgment_notes(ctx, value)},
        parent=run.expected_head_sha,
    )
    f.storage.branch_revision, f.storage.repo.head = revision, base
    await activities.persist_codex_procedure_artifact(str(run.id))
    if publish:
        await activities.commit_codex_procedure_artifact(str(run.id))
    return activities, run, ctx, primary


@pytest.mark.parametrize("outcome", sorted(NO_DRAFT))
async def test_no_draft_completes_once_without_review_or_delivery_and_is_readable(
    publication_db, monkeypatch, outcome
):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *args: None)
    f = await fixture(publication_db, monkeypatch, judgment=True)
    original_plan = f.storage.repo.trees[f.storage.repo.head][plan_path(f.configured.id)]
    activities, run, context, primary = await prepared(f, outcome)
    writes = f.storage.repo.writes
    for _ in range(2):
        assert not await activities.request_codex_procedure_review(str(run.id))
        await activities.project_codex_procedure_result(str(run.id))
        await activities.commit_codex_procedure_artifact(str(run.id))
    current = await f.db.get_run(run.id)
    assert current.status.value == "succeeded"
    # Nothing was drafted, so the run closes without a review instead of looking like a draft
    # that waits for one (run 1e474e10 kept review_required with no decision).
    assert not current.review_required and current.review_decision is None
    assert current.review_requested_at is None
    assert not current.lease_active
    assert f.storage.repo.writes == writes
    assert f.storage.repo.trees[f.storage.repo.head][plan_path(f.configured.id)] == original_plan
    assert (
        await f.db.pool.fetchval("SELECT count(*) FROM run_decisions WHERE run_id=$1", run.id) == 0
    )
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='codex_procedure_ready'",
            run.id,
        )
        == 1
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get(f"/api/workflows/runs/{run.id}/artifact/document")
        assert response.status_code == 200
        assert response.json()["markdown"].encode() == primary
        assert len(response.json()["related_documents"]) == 1
    result = structured(await mcp(f, monkeypatch).call_tool("get_run", {"run_id": str(run.id)}))
    assert result["status"] == "succeeded" and result["review_decision"] is None
    assert result["review_required"] is False
    sources = await f.service.discover(project_id=f.project.id, program_id=f.configured.id)
    item = next(i for i in sources["items"] if i["id"] == context["item"]["id"])
    assert not item["available"] and item["can_rewrite"]
    assert item["draft"]["stage"] == outcome and not item["draft"]["has_output"]
    assert sources["progress"]["drafted"] == sources["progress"]["awaiting_review"] == 0
    if outcome == "already_covered":
        assert sources["next"]["item_id"] != item["id"]
        assert sources["progress"]["already_covered"] == 1
        # The run records the page that covers the brief, and the plan item says it is covered.
        assert current.progress_summary.startswith("Already covered by https://example.com/docs:")
        published = await f.db.get_effect(f"{run.id}:procedure_canonical_commit")
        assert published.result["covered_by"] == "https://example.com/docs"
        assert item["draft"]["covered_by"] == "https://example.com/docs"
        assert item["covered"] == {
            "page": "https://example.com/docs",
            "reason": judgment(context)["rationale"],
            "run_id": str(run.id),
        }
    else:
        assert sources["next"]["item_id"] == item["id"]
        assert not sources["next"]["available"]
        assert item["covered"] is None and item["draft"]["covered_by"] is None
    # A changed brief is eligible again. The editable plan never stores execution state.
    read = await f.service.programs.read(project_id=f.project.id, program_id=f.configured.id)
    amended = read["plan"]
    selected = next(i for b in amended["batches"] for i in b["items"] if i["id"] == item["id"])
    selected["brief"] += " Explain recovery from an interrupted connection."
    f.storage.repo.edit({plan_path(f.configured.id): canonical_json(amended)})
    refreshed = await f.service.discover(project_id=f.project.id, program_id=f.configured.id)
    assert refreshed["next"]["item_id"] == item["id"] and refreshed["next"]["available"]


async def test_justified_article_still_requires_real_approval(publication_db, monkeypatch):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *args: None)
    f = await fixture(publication_db, monkeypatch, judgment=True)
    activities, run, _, _ = await prepared(f, "draft")
    with pytest.raises(RuntimeError, match="before review"):
        await activities.project_codex_procedure_result(str(run.id))
    assert await activities.request_codex_procedure_review(str(run.id))
    assert (await f.db.get_run(run.id)).status.value == "needs_input"


@pytest.mark.parametrize("resolution", ["applied", "kept"])
async def test_retained_assessment_is_never_counted_as_an_article(
    publication_db, monkeypatch, resolution
):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *args: None)
    f = await fixture(publication_db, monkeypatch, judgment=True)
    _, run, ctx, _ = await prepared(f, "already_covered", publish=False)
    discovery = await f.service.discover(project_id=f.project.id, program_id=f.configured.id)
    assert not discovery["next"]["available"]
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='failed', lease_active=false, output_resolution=$2 "
        "WHERE id=$1",
        run.id,
        json.dumps({"state": resolution}),
    )
    discovery = await f.service.discover(project_id=f.project.id, program_id=f.configured.id)
    item = next(i for i in discovery["items"] if i["id"] == ctx["item"]["id"])
    assert item["draft"]["stage"] == "assessment_saved"
    assert not item["draft"]["has_output"]
    assert item["can_rewrite"] and not item["available"]
    assert discovery["progress"]["drafted"] == discovery["progress"]["already_covered"] == 0


def test_covered_skips_only_unchanged_brief_and_never_other_assessments():
    original = {"id": "one", "brief": "Original"}
    progress = {
        "run_id": str(uuid4()),
        "stage": "already_covered",
        "assessment": {"outcome": "already_covered"},
        "brief_sha256": digest(original),
        "has_output": False,
    }
    items = [
        {"readiness": "needs_verification", "draft": progress},
        {"readiness": "needs_verification", "id": "two"},
    ]
    assert next_item(items)["id"] == "two"
    items[0]["brief_changed"] = True
    assert next_item(items) == items[0]
    with pytest.raises(ValueError, match="assessment"):
        check_existing(progress, item=original)
    check_existing(progress, item={**original, "brief": "Changed"})
    check_existing(progress, rewrite=True)


async def test_assessment_never_advertises_or_attempts_automatic_pr_delivery():
    run = SimpleNamespace(
        id=uuid4(),
        workflow_id=UUID("00000000-0000-4000-8000-000000000031"),
        executor="codex.procedure",
        canonical_commit_sha="a" * 40,
        artifact_path="assessment.md",
        review_decision=None,
    )
    publication = {
        "canonical_commit_sha": run.canonical_commit_sha,
        "artifact_path": run.artifact_path,
        "content_editorial": {"schema": "content-editorial-check.v1", "outcome": "already_covered"},
    }

    async def receipt(key):
        return SimpleNamespace(
            status="completed",
            result=publication
            if key.endswith(":procedure_canonical_commit")
            else {"delivery": {"settings": {}}},
        )

    db = SimpleNamespace(get_run=AsyncMock(return_value=run), get_effect=receipt)
    service = ContentDelivery(database=db, storage=None, integrations=SimpleNamespace())
    assert await service.status(run) is None
    assert await service.deliver(run.id) is None


async def test_the_next_weekly_draft_moves_past_an_already_covered_item(
    publication_db, monkeypatch
):
    # Run 1e474e10: content.generate found its item already covered. The item is marked
    # covered with the page that covers it, and the next weekly occurrence drafts the item
    # after it instead of waiting on a review that nothing needs.
    from test_scheduled_planned_drafts import occurrence, weekly

    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *args: None)
    f = await fixture(publication_db, monkeypatch, judgment=True)
    activities, run, context, _ = await prepared(f, "already_covered")
    assert not await activities.request_codex_procedure_review(str(run.id))
    await activities.project_codex_procedure_result(str(run.id))
    covered = context["item"]["id"]
    # A second planned article, after the covered one.
    read = await f.service.programs.read(project_id=f.project.id, program_id=f.configured.id)
    plan = read["plan"]
    first = next(i for b in plan["batches"] for i in b["items"] if i["id"] == covered)
    plan["batches"][-1]["items"].append(
        {
            **first,
            "id": "topic_after",
            "title": "Recover an interrupted messaging setup",
            "intent": "Developer resuming an interrupted setup",
            "action": "new_page",
            "destination": "",
        }
    )
    f.storage.repo.edit({plan_path(f.configured.id): canonical_json(plan)})
    configured, common, pause = await weekly(f)
    dispatched = await common.dispatch_scheduled_workflow(occurrence(configured))
    selected = (await f.db.get_effect(content_draft.selection_key(dispatched["run_id"]))).result
    assert selected["mode"] == "next" and selected["item"]["id"] == "topic_after"
    pause.assert_not_awaited()


async def test_a_covered_run_saved_before_covered_by_still_names_its_page(
    publication_db, monkeypatch
):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *args: None)
    f = await fixture(publication_db, monkeypatch, judgment=True)
    activities, run, context, _ = await prepared(f, "already_covered")
    await activities.project_codex_procedure_result(str(run.id))
    # Runs published before this change have no covered_by; the judgment still names the page.
    await f.db.pool.execute(
        "UPDATE effect_receipts SET result = result - 'covered_by' WHERE execution_key=$1",
        f"{run.id}:procedure_canonical_commit",
    )
    sources = await f.service.discover(project_id=f.project.id, program_id=f.configured.id)
    item = next(i for i in sources["items"] if i["id"] == context["item"]["id"])
    assert item["covered"]["page"] == "https://example.com/docs"


def test_an_update_its_destination_already_satisfies_says_no_change_is_needed():
    from tin_lite.content_editorial_judgment import no_draft_summary

    page = "https://example.com/how-to-edit-text-in-image-with-same-font"
    update = {"title": "Same-font guide", "action": "update_page", "destination": page + "/"}
    covered = {"outcome": "already_covered"}
    # ImageTextEdit, 10-07: "Already covered by <the page this refresh was for>" read as if
    # the page collided with itself.
    assert no_draft_summary(covered, update, page) == (
        f"No change needed: {page} already does what this update asks: Same-font guide."
    )
    other = "https://example.com/edit-text-in-image"
    assert no_draft_summary(covered, update, other) == (
        f"Already covered by {other}: Same-font guide. No article drafted."
    )
    new_page = {"title": "Best tools", "action": "new_page"}
    assert no_draft_summary(covered, new_page, page).startswith(f"Already covered by {page}:")
    assert no_draft_summary({"outcome": "insufficient_evidence"}, update, None) == (
        "Coverage could not be established: Same-font guide. No article drafted."
    )
