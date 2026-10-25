"""Project source files stay whole; source and serialized-context bounds still apply."""

import hashlib
from copy import deepcopy
from uuid import uuid4

import pytest
from test_content_draft import fixture as draft_fixture
from test_content_draft import start
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_draft
from tin_lite.content_plan import plan_path
from tin_lite.organic_audit import canonical_json


async def with_sources(db, monkeypatch, files, *, missing=()):
    f = await draft_fixture(db, monkeypatch)
    plan = deepcopy(
        (await f.service.programs.read(project_id=f.project.id, program_id=f.configured.id))["plan"]
    )
    plan["batches"][0]["items"][0]["source_ids"] += [f"file:{path}" for path in [*files, *missing]]
    revision = f.storage.repo.edit({plan_path(f.configured.id): canonical_json(plan), **files})
    f.inputs["plan_revision"] = revision
    return f


@pytest.mark.parametrize("size", [20_000, 20_001, 35_418])
async def test_grown_source_kept_whole_at_prepared_revision(publication_db, monkeypatch, size):
    path = "notes/product.md"
    f = await with_sources(publication_db, monkeypatch, {path: b"Original product facts"})
    run = await start(f)
    # The approved brief is unchanged; an ordinary source grows after planning/admission.
    content = b"x" * (size - 2) + "ı".encode()
    revision = f.storage.repo.edit(
        {path: content, "wiki/INDEX.md": b"Unrelated wiki remains intact\n"}
    )
    tree = deepcopy(f.storage.repo.trees[revision])
    context = await f.service.prepare(run)
    row = next(row for row in context["evidence"] if row["source_id"] == f"file:{path}")
    assert row["content"].encode() == content
    assert row["sha256"] == hashlib.sha256(content).hexdigest()
    assert row["revision"] == revision
    assert f.storage.repo.head == revision
    assert f.storage.repo.trees[revision] == tree
    # Preparation does not start another model call or write to any project file.
    assert f.model.calls == 1
    f.runtime.temporal.start_workflow.assert_awaited_once()


@pytest.mark.parametrize("sizes", [(30_000, 30_000), (30_000, 30_001), (60_001,)])
async def test_combined_source_budget_still_uses_utf8_bytes(publication_db, monkeypatch, sizes):
    files = {
        f"notes/source-{index}.md": b"a" * (size - 2) + "ı".encode()
        for index, size in enumerate(sizes)
    }
    f = await with_sources(publication_db, monkeypatch, files)
    run = await start(f)
    # Isolate the raw source-byte bound from the independent serialized JSON bound.
    monkeypatch.setattr(content_draft, "bounded", lambda context: context)
    if sum(sizes) > 60_000:
        with pytest.raises(ValueError, match="60 KB"):
            await f.service.prepare(run)
        assert await f.service.saved(run.id) is None
    else:
        context = await f.service.prepare(run)
        rows = [row for row in context["evidence"] if row["source_id"].startswith("file:")]
        assert sum(len(row["content"].encode()) for row in rows) == 60_000
        assert {row["path"] for row in rows} == files.keys()


async def test_serialized_context_bound_still_refuses_escaped_text(publication_db, monkeypatch):
    # 24 KB of UTF-8 expands beyond the context bound when serialized as escaped JSON.
    f = await with_sources(publication_db, monkeypatch, {"notes/product.md": "ı".encode() * 12_000})
    run = await start(f)
    with pytest.raises(ValueError, match="too large"):
        await f.service.prepare(run)
    assert await f.service.saved(run.id) is None


async def test_missing_source_stays_unresolved_and_prepared_retry_stays_pinned(
    publication_db, monkeypatch
):
    path = "notes/product.md"
    content = b"Known product fact\n" * 1_500
    f = await with_sources(
        publication_db, monkeypatch, {path: content}, missing=("notes/missing.md",)
    )
    key = str(uuid4())
    run = await start(f, key=key)
    context = await f.service.prepare(run)
    assert context["unresolved_source_ids"] == ["file:notes/missing.md"]
    f.storage.repo.edit({path: b"Later unrelated facts", "notes/missing.md": b"New file"})
    assert (await start(f, key=key)).id == run.id
    assert await f.service.prepare(run) == context
    row = next(row for row in context["evidence"] if row["source_id"] == f"file:{path}")
    assert row["content"].encode() == content


@pytest.mark.parametrize("path", ["../private.md", ".env", "notes/./product.md"])
async def test_source_budget_change_does_not_allow_unsafe_paths(publication_db, monkeypatch, path):
    f = await with_sources(publication_db, monkeypatch, {}, missing=(path,))
    run = await start(f)
    with pytest.raises(ValueError, match="unsafe context file path"):
        await f.service.prepare(run)
    assert await f.service.saved(run.id) is None
