"""Project deletion: tombstone, fence, external cleanup, purge, replay and crash safety."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pierre_storage.errors import ApiError
from temporalio.service import RPCError, RPCStatusCode
from test_procedure_publication import HistoryStorage
from test_procedure_publication import publication_db as publication_db

from tin_lite import project_deletion
from tin_lite.code_storage import CodeStorage
from tin_lite.domain import SideEffectConflictError
from tin_lite.project_deletion import (
    ProjectDeletionPending,
    deletable_project,
    delete_project,
    receipt_key,
)
from tin_lite.projects import can_delete_project, personal_project_id

ACTOR = "user_projectowner"
OTHER = "user_projectmember"
STRANGER = "user_stranger"


def fake_temporal():
    return SimpleNamespace(
        get_workflow_handle=Mock(return_value=SimpleNamespace(terminate=AsyncMock())),
        get_schedule_handle=Mock(return_value=SimpleNamespace(delete=AsyncMock())),
    )


async def fixture(db, *, name="Acme", creator=ACTOR, project_id=None):
    storage = HistoryStorage()
    storage.delete_repo = AsyncMock(return_value=True)
    project = await db.create_project(name=name, state_repo_id=f"projects/{uuid4()}")
    if project_id is not None:
        await db.pool.execute("UPDATE projects SET id=$2 WHERE id=$1", project.id, project_id)
        project = await db.get_project(project_id)
    await db.pool.execute(
        "UPDATE projects SET created_by_clerk_user_id=$2 WHERE id=$1", project.id, creator
    )
    for user in (ACTOR, OTHER, STRANGER):
        await db.record_tin_user(user)
    for user in (ACTOR, OTHER):
        await db.grant_project_membership(project_id=project.id, clerk_user_id=user)
    await db.pool.execute(
        "INSERT INTO workspace_memberships(workspace_id, clerk_user_id) VALUES($1,$2)",
        project.workspace_id,
        ACTOR,
    )
    runtime = SimpleNamespace(
        database=db,
        storage=storage,
        temporal=fake_temporal(),
        sandboxes=SimpleNamespace(kill=AsyncMock()),
        integrations=SimpleNamespace(disconnect=AsyncMock(return_value=True)),
        settings=SimpleNamespace(task_queue="test"),
    )
    return SimpleNamespace(
        db=db, storage=storage, project=await db.get_project(project.id), runtime=runtime
    )


async def seed_work(f):
    """A running run with a sandbox, a scheduled workflow, a connection, chat and activity."""
    db, project = f.db, f.project
    workflow_id, run_id, configured_id = uuid4(), uuid4(), uuid4()
    await db.pool.execute(
        """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
                current_commit_sha, version_label, definition)
           VALUES ($1, 'research.deep_dive', 'Research', 'codex.procedure', 'registry/workflows',
                   'research.json', $2, '1', '{}')""",
        workflow_id,
        "d" * 40,
    )
    await db.pool.execute(
        """INSERT INTO project_workflows (id, project_id, workflow_id, definition_commit_sha, name,
                input_schema, schedule, status, temporal_schedule_id, request_id,
                created_by_clerk_user_id)
           VALUES ($1, $2, $3, $4, 'Weekly research', '{}', '{"cadence": "weekly"}', 'active',
                   $5, $6, $7)""",
        configured_id,
        project.id,
        workflow_id,
        "d" * 40,
        f"tin-lite-project-workflow:{configured_id}",
        uuid4(),
        ACTOR,
    )
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
                temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
                lease_owner, sandbox_id, expected_head_sha, ephemeral_branch, review_required,
                project_workflow_id)
           VALUES ($1,$2,$3,'codex.procedure',$4,$5,$6,1,1,'running',true,'owner','sbx-1',$7,$8,
                   false,$9)""",
        run_id,
        project.id,
        workflow_id,
        "d" * 40,
        f"codex.procedure:{run_id}",
        str(run_id),
        "a" * 40,
        f"procedures/{run_id}/1",
        configured_id,
    )
    await db.add_activity(run_id=run_id, event_type="run_started", audience="product")
    await db.pool.execute(
        """INSERT INTO integration_connections (id, project_id, provider_key,
                connected_by_clerk_user_id) VALUES ($1, $2, 'infra.github', $3)""",
        uuid4(),
        project.id,
        ACTOR,
    )
    await db.pool.execute(
        """INSERT INTO chat_messages (id, project_id, request_id, role, source, content,
                author_clerk_user_id) VALUES ($1, $2, $3, 'user', 'founder', 'hello', $4)""",
        uuid4(),
        project.id,
        uuid4(),
        ACTOR,
    )
    return SimpleNamespace(run_id=run_id, configured_id=configured_id)


async def count(db, table, project_id, column="project_id"):
    return await db.pool.fetchval(
        f'SELECT count(*) FROM "{table}" WHERE "{column}" = $1',  # noqa: S608 — test table names
        project_id,
    )


async def test_creator_deletes_and_the_project_disappears_behind_the_tombstone(
    publication_db, monkeypatch
):
    f = await fixture(publication_db)
    work = await seed_work(f)
    events = []
    monkeypatch.setattr(
        project_deletion.analytics, "capture", lambda event, **kw: events.append((event, kw))
    )
    assert can_delete_project(f.project, ACTOR) is True
    assert can_delete_project(f.project, OTHER) is False
    assert (await deletable_project(f.runtime, project_id=f.project.id, actor=ACTOR)).id == (
        f.project.id
    )

    request_id = uuid4()
    result = await delete_project(
        f.runtime, project_id=f.project.id, actor=ACTOR, request_id=request_id
    )

    assert result["name"] == "Acme"
    assert result["stopped_runs"] == 1
    assert result["removed_schedules"] == 1
    assert result["disconnected"] == 1
    assert result["repo_deleted"] is True
    assert result["deleted_at"]
    temporal = f.runtime.temporal
    terminated = {call.args[0] for call in temporal.get_workflow_handle.call_args_list}
    assert terminated == {
        f"codex.procedure:{work.run_id}",
        f"tin-scheduled-dispatch:{work.configured_id}",
    }
    temporal.get_schedule_handle.assert_called_once_with(
        f"tin-lite-project-workflow:{work.configured_id}"
    )
    f.runtime.sandboxes.kill.assert_awaited_once_with("sbx-1")
    f.runtime.integrations.disconnect.assert_awaited_once_with(
        project_id=f.project.id, provider_key="infra.github"
    )
    f.storage.delete_repo.assert_awaited_once_with(f.project.state_repo_id)

    db = f.db
    row = await db.pool.fetchrow("SELECT * FROM projects WHERE id=$1", f.project.id)
    assert row["deleted_at"] is not None
    assert row["deleted_by_clerk_user_id"] == ACTOR
    run = await db.pool.fetchrow("SELECT * FROM workflow_runs WHERE id=$1", work.run_id)
    assert run["status"] == "stopped" and run["lease_active"] is False
    configured = await db.pool.fetchrow(
        "SELECT status, next_run_at FROM project_workflows WHERE id=$1", work.configured_id
    )
    assert configured["status"] == "archived" and configured["next_run_at"] is None
    for table in (
        "project_memberships",
        "integration_connections",
        "chat_messages",
        "activity_events",
    ):
        assert await count(db, table, f.project.id) == 0, table
    assert await count(db, "broker_grants", work.run_id, "run_id") == 0

    assert await db.has_project_access(project_id=f.project.id, clerk_user_id=ACTOR) is False
    assert [p.id for p in await db.list_projects_for_user(ACTOR)] == []
    assert [p.id for p in await db.list_projects_for_user(OTHER)] == []
    recreated = await db.create_workspace_project(
        workspace_id=f.project.workspace_id,
        project_id=uuid4(),
        name="Acme",
        state_repo_id="projects/acme-2",
        clerk_user_id=ACTOR,
        request_id=uuid4(),
    )
    assert recreated.name == "Acme" and recreated.id != f.project.id
    assert events == [
        (
            "project_deleted",
            {
                "distinct_id": f.project.id,
                "project_id": f.project.id,
                "properties": {"clerk_user_id": ACTOR, "stopped_runs": 1, "removed_schedules": 1},
            },
        )
    ]

    # Replay: the same request_id returns the saved result without repeating cleanup.
    again = await delete_project(
        f.runtime, project_id=f.project.id, actor=ACTOR, request_id=request_id
    )
    assert again == result
    assert temporal.get_workflow_handle.call_count == 2
    f.storage.delete_repo.assert_awaited_once()
    # A fresh request on the deleted project succeeds with nothing left to do.
    repeat = await delete_project(
        f.runtime, project_id=f.project.id, actor=ACTOR, request_id=uuid4()
    )
    assert repeat["deleted_at"] == result["deleted_at"]
    assert (repeat["stopped_runs"], repeat["removed_schedules"], repeat["disconnected"]) == (
        0,
        0,
        0,
    )


async def test_only_the_creator_may_delete(publication_db):
    f = await fixture(publication_db)
    for actor in (OTHER, STRANGER):
        with pytest.raises(LookupError):
            await deletable_project(f.runtime, project_id=f.project.id, actor=actor)
        with pytest.raises(LookupError):
            await delete_project(
                f.runtime, project_id=f.project.id, actor=actor, request_id=uuid4()
            )
    assert (await f.db.get_project(f.project.id)).deleted_at is None
    with pytest.raises(LookupError):
        await deletable_project(f.runtime, project_id=uuid4(), actor=ACTOR)

    # Older rows without a creator: any member may delete, a stranger may not.
    g = await fixture(publication_db, name="Legacy", creator=None)
    assert can_delete_project(g.project, OTHER) is True
    with pytest.raises(LookupError):
        await delete_project(g.runtime, project_id=g.project.id, actor=STRANGER, request_id=uuid4())
    result = await delete_project(
        g.runtime, project_id=g.project.id, actor=OTHER, request_id=uuid4()
    )
    assert result["name"] == "Legacy"
    # After the members are gone, the deleter can still replay.
    await delete_project(g.runtime, project_id=g.project.id, actor=OTHER, request_id=uuid4())
    with pytest.raises(LookupError):
        await delete_project(g.runtime, project_id=g.project.id, actor=ACTOR, request_id=uuid4())


async def test_the_personal_project_is_refused(publication_db):
    by_name = await fixture(publication_db, name="QA’s project")
    assert can_delete_project(by_name.project, ACTOR) is False
    with pytest.raises(ValueError, match="personal project"):
        await deletable_project(by_name.runtime, project_id=by_name.project.id, actor=ACTOR)
    with pytest.raises(ValueError, match="personal project"):
        await delete_project(
            by_name.runtime, project_id=by_name.project.id, actor=ACTOR, request_id=uuid4()
        )
    # A stranger learns nothing about it.
    with pytest.raises(LookupError):
        await deletable_project(by_name.runtime, project_id=by_name.project.id, actor=STRANGER)

    by_id = await fixture(publication_db, name="Acme", project_id=personal_project_id(ACTOR))
    assert can_delete_project(by_id.project, ACTOR) is False
    with pytest.raises(ValueError, match="personal project"):
        await delete_project(
            by_id.runtime, project_id=by_id.project.id, actor=ACTOR, request_id=uuid4()
        )
    assert (await by_id.db.get_project(by_id.project.id)).deleted_at is None


async def test_a_request_id_owned_by_another_operation_conflicts(publication_db):
    f = await fixture(publication_db)
    request_id = uuid4()
    key = receipt_key(f.project.id, request_id)
    async with f.db.effect_lock(key, "something_else") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="something_else")
    with pytest.raises(SideEffectConflictError):
        await delete_project(f.runtime, project_id=f.project.id, actor=ACTOR, request_id=request_id)
    assert (await f.db.get_project(f.project.id)).deleted_at is None


async def test_external_failure_leaves_a_hidden_project_and_a_retryable_receipt(publication_db):
    f = await fixture(publication_db)
    await seed_work(f)
    handle = f.runtime.temporal.get_workflow_handle.return_value
    handle.terminate.side_effect = RPCError("down", RPCStatusCode.UNAVAILABLE, b"")
    request_id = uuid4()

    with pytest.raises(ProjectDeletionPending):
        await delete_project(f.runtime, project_id=f.project.id, actor=ACTOR, request_id=request_id)

    db = f.db
    receipt = await db.get_effect(receipt_key(f.project.id, request_id))
    assert receipt.status == "failed"
    assert (await db.get_project(f.project.id)).deleted_at is not None
    assert await db.has_project_access(project_id=f.project.id, clerk_user_id=ACTOR) is False
    assert await count(db, "project_memberships", f.project.id) == 0
    assert await count(db, "chat_messages", f.project.id) == 1  # purge did not run
    f.storage.delete_repo.assert_not_awaited()

    handle.terminate.side_effect = None
    result = await delete_project(
        f.runtime, project_id=f.project.id, actor=ACTOR, request_id=request_id
    )
    assert result["stopped_runs"] == 0  # already stopped in the first attempt
    assert await count(db, "chat_messages", f.project.id) == 0
    assert (await db.get_effect(receipt_key(f.project.id, request_id))).status == "completed"


async def test_a_retry_closes_the_executions_and_sandboxes_a_failed_attempt_left_open(
    publication_db,
):
    f = await fixture(publication_db)
    work = await seed_work(f)
    earlier_id = uuid4()
    await f.db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
                temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
                sandbox_id, expected_head_sha, ephemeral_branch, review_required, finished_at)
           SELECT $1, project_id, workflow_id, executor, definition_commit_sha, $2, $3, 1, 1,
                  'stopped', false, 'sbx-earlier', expected_head_sha, $4, false,
                  now() - interval '1 day'
           FROM workflow_runs WHERE id = $5""",
        earlier_id,
        f"codex.procedure:{earlier_id}",
        str(earlier_id),
        f"procedures/{earlier_id}/1",
        work.run_id,
    )
    temporal = f.runtime.temporal
    handle = temporal.get_workflow_handle.return_value
    handle.terminate.side_effect = RPCError("down", RPCStatusCode.UNAVAILABLE, b"")
    f.runtime.sandboxes.kill.side_effect = TimeoutError()
    request_id = uuid4()

    with pytest.raises(ProjectDeletionPending, match="kill sandbox sbx-1"):
        await delete_project(f.runtime, project_id=f.project.id, actor=ACTOR, request_id=request_id)

    # The run is already stopped, but its execution and sandbox are still open.
    handle.terminate.side_effect = None
    f.runtime.sandboxes.kill.side_effect = None
    temporal.get_workflow_handle.reset_mock()
    f.runtime.sandboxes.kill.reset_mock()
    result = await delete_project(
        f.runtime, project_id=f.project.id, actor=ACTOR, request_id=request_id
    )

    assert result["stopped_runs"] == 0
    terminated = {call.args[0] for call in temporal.get_workflow_handle.call_args_list}
    assert terminated == {
        f"codex.procedure:{work.run_id}",
        f"tin-scheduled-dispatch:{work.configured_id}",
    }
    f.runtime.sandboxes.kill.assert_awaited_once_with("sbx-1")


async def test_nothing_new_is_admitted_into_a_deleted_project(publication_db):
    f = await fixture(publication_db)
    work = await seed_work(f)
    await delete_project(f.runtime, project_id=f.project.id, actor=ACTOR, request_id=uuid4())
    workflow_id = await f.db.pool.fetchval(
        "SELECT workflow_id FROM workflow_runs WHERE id=$1", work.run_id
    )

    with pytest.raises(LookupError, match="does not exist"):
        await f.db.create_run(
            project_id=f.project.id, workflow_id=workflow_id, started_by_clerk_user_id=ACTOR
        )
    with pytest.raises(LookupError, match="does not exist"):
        await f.db.create_project_workflow(
            project_id=f.project.id,
            workflow_id=workflow_id,
            definition_commit_sha="d" * 40,
            name="Late research",
            inputs={},
            input_schema={},
            schedule=None,
            request_id=uuid4(),
            created_by_clerk_user_id=ACTOR,
        )
    assert await count(f.db, "workflow_runs", f.project.id) == 1
    assert await count(f.db, "project_workflows", f.project.id) == 1


async def test_already_closed_temporal_and_storage_state_is_tolerated(publication_db):
    f = await fixture(publication_db)
    await seed_work(f)
    temporal = f.runtime.temporal
    temporal.get_workflow_handle.return_value.terminate.side_effect = RPCError(
        "gone", RPCStatusCode.NOT_FOUND, b""
    )
    temporal.get_schedule_handle.return_value.delete.side_effect = RPCError(
        "gone", RPCStatusCode.NOT_FOUND, b""
    )
    result = await delete_project(
        f.runtime, project_id=f.project.id, actor=ACTOR, request_id=uuid4()
    )
    assert result["stopped_runs"] == 1 and result["removed_schedules"] == 1


async def test_code_storage_delete_repo_tolerates_missing_repositories():
    storage = CodeStorage(organization="tin", private_key="key")
    storage._client = SimpleNamespace(delete_repo=AsyncMock())
    assert await storage.delete_repo("projects/x") is True
    storage._client.delete_repo.assert_awaited_once_with(id="projects/x", ttl=300)
    for code in (404, 409):
        storage._client.delete_repo.side_effect = ApiError("gone", status_code=code)
        assert await storage.delete_repo("projects/x") is True
    storage._client.delete_repo.side_effect = ApiError("down", status_code=500)
    with pytest.raises(ApiError):
        await storage.delete_repo("projects/x")
