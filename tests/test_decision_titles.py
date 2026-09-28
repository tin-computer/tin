"""Decisions name what they ask about."""

from __future__ import annotations

import pytest
from test_procedure_publication import PATH, activity_fixture
from test_procedure_publication import publication_db as publication_db

SHA = "b" * 40


async def review_run(db, *, artifact_title=None):
    _, _, run, _ = await activity_fixture(db, review=True)
    await db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=SHA,
        artifact_ref=f"code.storage://repo@{SHA}/{PATH}",
        artifact_path=PATH,
        summary="Research is ready for your review.",
        artifact_title=artifact_title,
    )
    return run


async def titles(db, project_id):
    return [item["title"] for item in await db.list_pending_decisions(project_id=project_id)]


@pytest.mark.asyncio
async def test_review_title_names_the_output(publication_db):
    run = await review_run(publication_db, artifact_title="Which tools work with agents?")
    assert await titles(publication_db, run.project_id) == ["Review: Which tools work with agents?"]


@pytest.mark.asyncio
async def test_review_title_names_the_workflow_when_the_output_has_no_title(publication_db):
    run = await review_run(publication_db)
    assert await titles(publication_db, run.project_id) == ["Review: Research"]


@pytest.mark.asyncio
async def test_generic_titles_saved_earlier_are_named_when_listed(publication_db):
    run = await review_run(publication_db)
    await publication_db.pool.execute(
        "UPDATE run_decisions SET title='Review workflow output' WHERE run_id=$1", run.id
    )
    assert await titles(publication_db, run.project_id) == ["Review: Research"]
    await publication_db.pool.execute(
        "UPDATE workflow_runs SET artifact_title='A readable heading' WHERE id=$1", run.id
    )
    assert await titles(publication_db, run.project_id) == ["Review: A readable heading"]
    await publication_db.pool.execute(
        "UPDATE run_decisions SET title='Choose a hero' WHERE run_id=$1", run.id
    )
    assert await titles(publication_db, run.project_id) == ["Choose a hero"]
