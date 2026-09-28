"""Shared approved-article source proof for delivery and draft-only workflows."""

import json
from copy import deepcopy

import pytest
from test_content_repository_delivery import prepared
from test_private_workflows import ACTOR
from test_procedure_publication import publication_db as publication_db

from tin_lite import approved_article
from tin_lite.content_delivery import ContentDelivery
from tin_lite.workflow_reviews import WorkflowReviews


async def selected(f, *, include_style=False):
    return await approved_article.select(
        database=f.db,
        storage=f.storage,
        project_id=f.project.id,
        source_run_id=f.source.id,
        include_style=include_style,
    )


async def test_approved_article_pins_reviewed_bytes_and_writing_style(publication_db, monkeypatch):
    f = await prepared(publication_db, monkeypatch)
    source = await selected(f, include_style=True)
    assert source["source_run_id"] == str(f.source.id)
    assert source["source_revision"] == f.source.canonical_commit_sha
    assert source["source_path"] == f.source.artifact_path
    assert source["article"].startswith("# Useful buyer task")
    assert "Verification notes" not in source["article"]
    assert source["style"]["content"].startswith("# Writing style")
    assert source["style"]["sha256"] == f.context["style"]["sha256"]
    async with f.db.pool.acquire() as conn:
        await approved_article.guard(conn, project_id=f.project.id, source=source)


async def test_approved_article_is_available_even_with_automatic_delivery(
    publication_db, monkeypatch
):
    f = await prepared(publication_db, monkeypatch, approved=False)
    await f.delivery.choose(run=f.source, mode="github_pr", actor=ACTOR)
    from test_content_delivery import approve

    f.source = await approve(f, f.source)
    assert await ContentDelivery(database=f.db).intent(f.source)
    source = await selected(f)
    assert source["source_run_id"] == str(f.source.id)
    assert "content" not in source["style"]
    assert (await approved_article.discover(f.db, f.project.id))[0]["run_id"] == str(f.source.id)


async def test_altered_review_artifact_or_version_fails_closed(publication_db, monkeypatch):
    f = await prepared(publication_db, monkeypatch, approved=False)
    reviews = WorkflowReviews(runtime=f.runtime, settings=f.settings)
    view = await reviews.view(f.source.id, ACTOR)
    await reviews.approve(run_id=f.source.id, actor=ACTOR, token=view["review_token"])
    await f.activities.project_codex_procedure_result(str(f.source.id))
    f.source = await f.db.get_run(f.source.id)
    source = await selected(f)
    assert source["source_run_id"] == str(f.source.id)
    async with f.db.pool.acquire() as conn:
        await approved_article.guard(conn, project_id=f.project.id, source=source)
    original = await f.db.pool.fetchval(
        "SELECT artifact FROM workflow_review_commands WHERE source_run_id=$1", f.source.id
    )
    await f.db.pool.execute(
        "UPDATE workflow_review_commands SET artifact=$2::jsonb WHERE source_run_id=$1",
        f.source.id,
        json.dumps({"revision": "0" * 40}),
    )
    with pytest.raises(ValueError, match="review proof"):
        await selected(f)
    await f.db.pool.execute(
        "UPDATE workflow_review_commands SET artifact=$2::jsonb WHERE source_run_id=$1",
        f.source.id,
        original,
    )
    await f.db.pool.execute(
        "UPDATE workflow_runs SET review_version=review_version+1 WHERE id=$1", f.source.id
    )
    with pytest.raises(ValueError, match="review proof"):
        await selected(f)


async def test_legacy_publication_without_digest_remains_usable(publication_db, monkeypatch):
    f = await prepared(publication_db, monkeypatch, approved=False)
    key = f"{f.source.id}:procedure_canonical_commit"
    receipt = await f.db.get_effect(key)
    legacy = {name: value for name, value in receipt.result.items() if name != "checkpoint"}
    await f.db.pool.execute(
        "UPDATE effect_receipts SET result=$2::jsonb WHERE execution_key=$1",
        key,
        json.dumps(legacy),
    )
    reviews = WorkflowReviews(runtime=f.runtime, settings=f.settings)
    view = await reviews.view(f.source.id, ACTOR)
    await reviews.approve(run_id=f.source.id, actor=ACTOR, token=view["review_token"])
    await f.activities.project_codex_procedure_result(str(f.source.id))
    f.source = await f.db.get_run(f.source.id)
    source = await selected(f)
    assert source["publication_sha256"] is None
    assert source["source_sha256"]
    async with f.db.pool.acquire() as conn:
        await approved_article.guard(conn, project_id=f.project.id, source=source)


async def test_source_bytes_and_guard_revision_are_checked(publication_db, monkeypatch):
    f = await prepared(publication_db, monkeypatch)
    source = await selected(f)
    altered = deepcopy(source)
    altered["source_revision"] = "0" * 40
    async with f.db.pool.acquire() as conn:
        with pytest.raises(ValueError, match="not approved"):
            await approved_article.guard(conn, project_id=f.project.id, source=altered)

    read = f.storage.read_canonical_artifact

    async def corrupt(**kwargs):
        raw = await read(**kwargs)
        if kwargs["path"] == f.source.artifact_path:
            return raw + b"\nUnreviewed sentence.\n"
        return raw

    f.storage.read_canonical_artifact = corrupt
    with pytest.raises(ValueError, match="publication digest"):
        await selected(f)


async def test_declared_style_digest_mismatch_fails_only_when_style_requested(
    publication_db, monkeypatch
):
    f = await prepared(publication_db, monkeypatch)
    source = await selected(f)
    assert source["style"]["sha256"]
    read = f.storage.read_canonical_artifact

    async def corrupt(**kwargs):
        raw = await read(**kwargs)
        if kwargs["path"] == source["style"]["path"]:
            return raw + b"unreviewed"
        return raw

    f.storage.read_canonical_artifact = corrupt
    assert (await selected(f))["article"] == source["article"]
    with pytest.raises(ValueError, match="writing style differs"):
        await selected(f, include_style=True)
