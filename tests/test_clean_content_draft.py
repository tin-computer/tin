from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from test_content_delivery import context as sample_context
from test_content_draft import article as old_article
from test_content_draft import fixture, start
from test_private_workflows import app, mcp, structured
from test_procedure_publication import HistoryStorage, run_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_draft, content_plan_editorial
from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.content_delivery import article_body
from tin_lite.publication import (
    OutputCheckpoint,
    PublicationPendingError,
    StaleOutputComparisonError,
    related_output_documents,
)

ARTICLE = (
    "# Documented messaging setup\n\n"
    + ("Follow the published API reference for the supported request fields. " * 6).rstrip()
    + "\n"
).encode()


def notes(context):
    header = "\n".join(
        f"{key}: {value}" for key, value in content_draft.provenance(context).items()
    )
    checks = "\n".join(
        f"### v{i} — follow-up\nOptional later QA: {requirement}. Not performed and not a "
        "prerequisite for this draft. The article describes documentation, "
        "not live-tested behavior.\n"
        for i, requirement in enumerate(context["item"]["verification"], 1)
    )
    return (
        f"---\n{header}\n---\n# Generation notes\n\n"
        f"Sources: pinned first-party reference.\n\n{checks}"
    ).encode()


def test_clean_draft_notes_and_legacy_delivery_are_distinct():
    old = sample_context()
    ctx = {**old, "output_validator": content_draft.CLEAN_VALIDATOR}
    content_draft.validate_artifact(ARTICLE, ctx)
    content_draft.validate_notes(notes(ctx), ctx)
    assert article_body(ARTICLE, ctx)[0].encode() == ARTICLE
    assert "Verification notes" not in article_body(old_article(old), old)[0]
    for bad in (notes(ctx), ARTICLE + b"\n## Verification notes\nInternal checks"):
        with pytest.raises(ValueError):
            content_draft.validate_artifact(bad, ctx)
    for bad in (
        notes(ctx).replace(b"follow-up", b"approved"),
        notes(ctx).replace(b"v1", b"v8"),
        notes(ctx).replace(ctx["program_id"].encode(), b"other"),
        b"x" * (content_draft.NOTES_MAX_BYTES + 1),
    ):
        with pytest.raises(ValueError):
            content_draft.validate_notes(bad, ctx)


def test_planner_keeps_historical_instructions_and_new_scope_is_explicit():
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.plan")
    definition, _ = spec.definition_and_resource_files()
    assert content_plan_editorial.contract(definition).POLICY["version"] == "content-editorial-v7"
    old = deepcopy(definition)
    old.update(
        content_policy=content_plan_editorial.V3_POLICY,
        content_instructions=content_plan_editorial.V3_INSTRUCTIONS,
        content_schema=content_plan_editorial.PORTFOLIO_SCHEMA,
    )
    assert (
        content_plan_editorial.contract(old).INSTRUCTIONS == content_plan_editorial.V3_INSTRUCTIONS
    )
    assert "live product QA" not in content_plan_editorial.V3_INSTRUCTIONS
    assert "optional later follow-up" in definition["content_instructions"]
    draft = next(w for w in BUILTIN_WORKFLOWS if w.key == content_draft.KEY)
    _, resources = draft.definition_and_resource_files()
    prompt = next(raw.decode() for path, raw in resources.items() if path.endswith("PROMPT.md"))
    assert "older brief" in prompt and "Missing access for live QA does not block" in prompt


async def prepared_pair(db, monkeypatch):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *args: None)
    f = await fixture(db, monkeypatch, clean=True)
    run = await start(f)
    f.activities = TinActivities(
        database=f.db,
        storage=f.storage,
        settings=f.settings,
        sandboxes=SimpleNamespace(create=AsyncMock(return_value="sandbox-test"), kill=AsyncMock()),
    )
    await f.activities.prepare_codex_procedure(str(run.id))
    ctx = await f.service.saved(run.id)
    assert ctx["output_validator"] == content_draft.CLEAN_VALIDATOR
    await f.activities.create_codex_procedure_sandbox(str(run.id))
    run = await f.db.get_run(run.id)
    path = content_draft.PATH_TEMPLATE.format(run_id=run.id)
    companion = content_draft.notes_path(path)
    base = f.storage.repo.head
    revision = f.storage.repo.edit(
        {path: ARTICLE, companion: notes(ctx)}, parent=run.expected_head_sha
    )
    f.storage.branch_revision, f.storage.repo.head = revision, base
    return f, run, ctx, path, companion, revision


async def test_two_files_recover_publish_once_and_expose_notes_without_another_review(
    publication_db, monkeypatch
):
    f, run, ctx, path, companion, revision = await prepared_pair(publication_db, monkeypatch)
    # Recover the actual two-file sandbox checkpoint without another model purchase.
    await f.activities.persist_codex_procedure_artifact(str(run.id))
    receipt = await f.db.get_effect(f"{run.id}:procedure_artifact_persist")
    checkpoint = OutputCheckpoint.load(receipt.result["checkpoint"], run=run)
    assert checkpoint.companions[0].artifact_path == companion
    assert checkpoint.companions[0].ephemeral_commit_sha == revision
    assert checkpoint.sha256 != checkpoint.companions[0].sha256
    before = f.storage.repo.writes
    f.storage.repo.lose_response = True
    with pytest.raises(ApplicationError, match="PublicationPendingError"):
        await f.activities.commit_codex_procedure_artifact(str(run.id))
    await f.activities.commit_codex_procedure_artifact(str(run.id))
    await f.activities.commit_codex_procedure_artifact(str(run.id))
    assert f.storage.repo.writes == before + 1
    assert f.storage.repo.trees[f.storage.repo.head][path][1] == ARTICLE
    assert f.storage.repo.trees[f.storage.repo.head][companion][1] == notes(ctx)
    assert await f.activities.request_codex_procedure_review(str(run.id))
    run = await f.db.get_run(run.id)
    assert run.status.value == "needs_input" and run.review_decision is None
    assert (
        await f.db.pool.fetchval("SELECT count(*) FROM run_decisions WHERE run_id=$1", run.id) == 1
    )
    links = await related_output_documents(f.db, run)
    assert links[0]["path"] == companion and links[0]["label"] == "Generation notes"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get(f"/api/workflows/runs/{run.id}/artifact/document")
        assert response.status_code == 200
        doc = response.json()
        assert doc["markdown"].encode() == ARTICLE
        assert "follow-up" not in doc["html"] and "brief_sha256" not in doc["html"]
        assert doc["related_documents"] == links
        note = await client.get(
            f"/api/projects/{run.project_id}/files/document",
            params={
                "path": companion,
                "revision": run.canonical_commit_sha,
            },
        )
        assert note.status_code == 200 and "Optional later QA" in note.json()["markdown"]
    result = structured(await mcp(f, monkeypatch).call_tool("get_run", {"run_id": str(run.id)}))
    assert result["related_documents"] == links
    assert result["review_decision"] is None


@pytest.mark.parametrize("invalid", ["missing", "wrong_provenance"])
async def test_missing_or_unbound_notes_do_not_publish_partial_article(
    publication_db, monkeypatch, invalid
):
    f, run, ctx, path, companion, revision = await prepared_pair(publication_db, monkeypatch)
    if invalid == "missing":
        del f.storage.repo.trees[revision][companion]
    else:
        f.storage.repo.trees[revision][companion] = (
            "100644",
            notes(ctx).replace(b"schema:", b"wrong:"),
        )
    with pytest.raises((ValueError, RuntimeError)):
        await f.activities.persist_codex_procedure_artifact(str(run.id))
    assert path not in f.storage.repo.trees[f.storage.repo.head]
    assert (await f.db.get_run(run.id)).canonical_commit_sha is None


async def test_companion_edit_prevents_silent_overwrite_and_retains_both_files(
    publication_db, monkeypatch
):
    f, run, ctx, path, companion, revision = await prepared_pair(publication_db, monkeypatch)
    await f.activities.persist_codex_procedure_artifact(str(run.id))
    f.storage.repo.edit({companion: b"Founder notes"})
    before = f.storage.repo.head
    with pytest.raises(ApplicationError, match="OutputConflictError"):
        await f.activities.commit_codex_procedure_artifact(str(run.id))
    assert f.storage.repo.head == before and path not in f.storage.repo.trees[before]
    retained = (await f.db.get_run(run.id)).retained_output
    assert retained["reason"] == "output_conflict"
    assert retained["companions"][0]["artifact_path"] == companion
    checkpoint = OutputCheckpoint.load(retained, run=run)
    with pytest.raises(ValueError):
        OutputCheckpoint.load(replace(checkpoint, companions=(checkpoint,)).to_dict(), run=run)


@pytest.mark.parametrize("edit_notes", [False, True])
async def test_retained_pair_apply_is_atomic_and_cannot_overwrite_unreviewed_notes(edit_notes):
    storage = HistoryStorage()
    run = run_fixture()
    path, companion = "article.md", "article.generation.md"
    original = storage.repo.head
    revision = storage.repo.edit({path: ARTICLE, companion: b"Internal notes"})
    storage.repo.head = original
    checkpoint = OutputCheckpoint.create(
        run=run,
        revision=revision,
        path=path,
        media_type="text/markdown",
        content=ARTICLE,
        companions=(
            OutputCheckpoint.create(
                run=run,
                revision=revision,
                path=companion,
                media_type="text/markdown",
                content=b"Internal notes",
            ),
        ),
    )
    assert checkpoint.version == 2
    changes = {path: b"Member article"}
    if edit_notes:
        changes[companion] = b"Member notes"
    head = storage.repo.edit(changes)
    intent = {}

    async def save(value):
        intent["value"] = value

    async def apply():
        return await storage.apply_saved_output(
            repo_id=storage.repo.id,
            branch="main",
            checkpoint=checkpoint,
            content=ARTICLE,
            expected_revision=head,
            execution_key="test:apply",
            intent=intent.get("value"),
            save_intent=save,
        )

    if edit_notes:
        with pytest.raises(StaleOutputComparisonError):
            await apply()
        assert storage.repo.head == head and storage.repo.writes == 0
        return
    storage.repo.lose_response = True
    with pytest.raises(PublicationPendingError):
        await apply()
    committed = storage.repo.head
    assert await apply() == (committed, True)
    assert storage.repo.writes == 1
    assert storage.repo.trees[committed][path][1] == ARTICLE
    assert storage.repo.trees[committed][companion][1] == b"Internal notes"
