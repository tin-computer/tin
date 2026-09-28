"""Decisions name what they ask about, and list only what someone can decide."""

from __future__ import annotations

from uuid import uuid4

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


async def task_run(db, workflow_id, *, phase, has_changes, title):
    # A project holds one active task at a time, so each task gets its own project.
    project = await db.create_project(name=title, state_repo_id=f"projects/{uuid4()}")
    run_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
            temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
            task_phase, task_has_changes, task_title)
           VALUES ($1,$2,$3,'project.task',$4,$5,$6,1,1,'needs_input',false,$7,$8,$9)""",
        run_id,
        project.id,
        workflow_id,
        "d" * 40,
        f"project.task:{run_id}",
        str(run_id),
        phase,
        has_changes,
        title,
    )
    return project.id, run_id


@pytest.mark.asyncio
async def test_only_tasks_with_changes_to_review_are_decisions(publication_db):
    db = publication_db
    workflow_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
                current_commit_sha, version_label, definition)
           VALUES ($1, 'project.task', 'One-off project task', 'project.task',
                   'registry/workflows', 'task.json', $2, '1', '{}')""",
        workflow_id,
        "d" * 40,
    )
    project_id, reviewing = await task_run(
        db, workflow_id, phase="review", has_changes=True, title="Update the FAQ"
    )
    decisions = await db.list_pending_decisions(project_id=project_id)
    assert [(item["run_id"], item["title"]) for item in decisions] == [
        (reviewing, "Review: Update the FAQ")
    ]
    for phase, has_changes in [("needs_input", False), ("review", False)]:
        project_id, _ = await task_run(
            db, workflow_id, phase=phase, has_changes=has_changes, title="Pick a tone"
        )
        assert not await db.list_pending_decisions(project_id=project_id)
