"""A task waiting for a decision never blocks the project's next task."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import asyncpg
import pytest
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.project_task_control import (
    ProjectTaskConflictError,
    resume_project_task,
    send_project_task_message,
)


async def task_workflow(db):
    workflow_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
                current_commit_sha, version_label, definition)
           VALUES ($1, $3, 'One-off project task', 'project.task',
                   'registry/workflows', 'task.json', $2, '1', '{}')""",
        workflow_id,
        "d" * 40,
        f"project.task.{workflow_id.hex[:8]}",
    )
    return workflow_id


async def task(db, project_id, workflow_id, *, status, phase):
    run_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
            temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
            task_phase, task_has_changes, task_title, task_diff)
           VALUES ($1,$2,$3,'project.task',$4,$5,$6,1,1,$7,false,$8,true,'A task',$9::jsonb)""",
        run_id,
        project_id,
        workflow_id,
        "d" * 40,
        f"project.task:{run_id}",
        str(run_id),
        status,
        phase,
        json.dumps({"sha256": "0" * 64, "files": [{"path": "notes.md", "state": "modified"}]}),
    )
    return run_id


async def project_with_task_workflow(db):
    _, _, run, _ = await activity_fixture(db)
    return run.project_id, await task_workflow(db)


async def start_task(db, project_id, workflow_id):
    run, _created = await db.create_run(
        project_id=project_id,
        workflow_id=workflow_id,
        started_by_clerk_user_id="user_1",
        input_payload={"title": "Try the custom API", "instruction": "Read last week's traffic."},
    )
    return run


@pytest.mark.asyncio
async def test_a_task_in_review_does_not_block_a_new_task(publication_db):
    db = publication_db
    project_id, workflow_id = await project_with_task_workflow(db)
    await task(db, project_id, workflow_id, status="needs_input", phase="review")
    await task(db, project_id, workflow_id, status="running", phase="applying")

    started = await start_task(db, project_id, workflow_id)

    assert started.status.value == "pending"
    assert await db.other_working_task(project_id=project_id, run_id=started.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "phase"),
    [("running", "working"), ("needs_input", "question"), ("paused", "working"), ("pending", None)],
)
async def test_a_working_asking_or_paused_task_still_holds_the_project(
    publication_db, status, phase
):
    db = publication_db
    project_id, workflow_id = await project_with_task_workflow(db)
    holder = await task(db, project_id, workflow_id, status=status, phase=phase)

    with pytest.raises(RuntimeError, match=f"project already has active task {holder}"):
        await start_task(db, project_id, workflow_id)
    # The database enforces the same rule when two writers race past the check.
    with pytest.raises(asyncpg.UniqueViolationError):
        await task(db, project_id, workflow_id, status="running", phase="working")


@pytest.mark.asyncio
async def test_waking_a_reviewed_task_waits_for_the_working_one(publication_db):
    db = publication_db
    project_id, workflow_id = await project_with_task_workflow(db)
    reviewed = await task(db, project_id, workflow_id, status="needs_input", phase="review")
    working = await task(db, project_id, workflow_id, status="running", phase="working")
    assert await db.other_working_task(project_id=project_id, run_id=reviewed) == working

    handle = Mock(signal=AsyncMock())
    runtime = SimpleNamespace(
        database=db,
        temporal=Mock(get_workflow_handle=Mock(return_value=handle)),
        sandboxes=Mock(control_task=AsyncMock(return_value=False)),
    )
    db.has_project_access = AsyncMock(return_value=True)

    with pytest.raises(ProjectTaskConflictError, match="Another task is working"):
        await send_project_task_message(
            runtime=runtime, run_id=reviewed, clerk_user_id="user_1", message="Shorten it."
        )
    with pytest.raises(ProjectTaskConflictError, match="Another task is working"):
        await resume_project_task(runtime=runtime, run_id=reviewed, clerk_user_id="user_1")
    # Nothing was saved or signalled, so the reviewed task stays exactly where it was.
    handle.signal.assert_not_awaited()
    assert await db.list_task_entries(run_id=reviewed) == []
    assert (await db.get_run(reviewed)).task_phase == "review"
