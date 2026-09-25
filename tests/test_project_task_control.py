"""HTTP and MCP answer, direct, and approve one project task through the same boundary."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.db import Database, apply_migrations
from tin_lite.domain import RunStatus, WorkflowRun
from tin_lite.mcp_server import _run_allowed_actions, create_mcp_app
from tin_lite.project_task_control import (
    approve_project_task,
    project_task_allowed_actions,
    project_task_view,
)

MEMBER = "user_member"
OUTSIDER = "user_outsider"
QUESTION = "Which audience should the charter target: founders or operators?"
ANSWER = "Founders."


def task_run(**overrides) -> WorkflowRun:
    run_id = overrides.pop("id", uuid4())
    values = dict(
        id=run_id,
        project_id=uuid4(),
        workflow_id=uuid4(),
        executor="project.task",
        definition_commit_sha="d" * 40,
        temporal_workflow_id=f"project.task:{run_id}",
        thread_id="thread",
        generation=1,
        fencing_token=1,
        status=RunStatus.NEEDS_INPUT,
        task_title="Project charter",
        task_phase="needs_input",
        task_question=QUESTION,
    )
    values.update(overrides)
    return WorkflowRun(**values)


def test_task_actions_follow_the_task_phase() -> None:
    assert _run_allowed_actions(task_run()) == ["answer"]
    assert _run_allowed_actions(task_run(task_phase="review")) == ["approve", "direct"]
    paused = task_run(status=RunStatus.PAUSED, task_phase="paused", task_question=None)
    assert _run_allowed_actions(paused) == ["direct"]
    working = task_run(status=RunStatus.RUNNING, task_phase="working", task_question=None)
    assert _run_allowed_actions(working) == ["direct"]
    assert _run_allowed_actions(task_run(status=RunStatus.SUCCEEDED, task_phase="completed")) == []
    assert project_task_allowed_actions(task_run(status=RunStatus.STOPPED)) == []


def test_task_view_exposes_the_question_and_changed_paths_without_patches() -> None:
    reviewing = task_run(
        task_phase="review",
        task_question=None,
        task_diff={
            "files": [{"path": "docs/charter.md", "state": "added", "patch": "+secret"}],
            "sha256": "0" * 64,
        },
    )
    view = project_task_view(reviewing)
    assert view["phase"] == "review"
    assert view["changed_files"] == [{"path": "docs/charter.md", "state": "added"}]
    assert "secret" not in json.dumps(view)
    assert "entries" not in view
    assert project_task_view(task_run())["question"] == QUESTION


@pytest.fixture
async def task_db():
    """Real migrations/SQL in a disposable schema, never the configured product DB."""
    dsn = os.environ.get("TIN_LITE_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("set TIN_LITE_TEST_DATABASE_DSN for isolated Postgres contract tests")
    schema = "task_control_test_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    database = Database(dsn)
    try:
        database._pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=10, server_settings={"search_path": schema}
        )
        scoped_dsn = dsn + ("&" if "?" in dsn else "?") + f"search_path={schema}"
        await apply_migrations(scoped_dsn, Path(__file__).parents[1] / "migrations")
        yield database
    finally:
        await database.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def seed_task(
    db,
    *,
    status: str = "needs_input",
    phase: str = "needs_input",
    question: str | None = QUESTION,
    control: str | None = None,
    task_diff: dict | None = None,
) -> WorkflowRun:
    project = await db.create_project(name="Charter", state_repo_id=f"projects/{uuid4().hex}")
    run_id = uuid4()
    workflow_id = await db.pool.fetchval(
        """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
                current_commit_sha, version_label, definition)
           VALUES ($1, 'project.task', 'Project task', 'project.task', 'registry/workflows',
                   'project_task.json', $2, '1', '{}')
           ON CONFLICT (key) WHERE project_id IS NULL DO UPDATE SET title = EXCLUDED.title
           RETURNING id""",
        uuid4(),
        "d" * 40,
    )
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
                temporal_workflow_id, thread_id, generation, fencing_token, status, task_title,
                task_phase, task_question, task_question_requested_at, task_control, task_diff,
                task_has_changes)
           VALUES ($1, $2, $3, 'project.task', $4, $5, 'thread', 1, 1, $6, 'Project charter',
                   $7, $8, CASE WHEN $8::text IS NULL THEN NULL ELSE now() END, $9, $10::jsonb,
                   $11)""",
        run_id,
        project.id,
        workflow_id,
        "d" * 40,
        f"project.task:{run_id}",
        status,
        phase,
        question,
        control,
        json.dumps(task_diff) if task_diff is not None else None,
        task_diff is not None,
    )
    await db.append_task_entry(
        run_id=run_id,
        kind="instruction",
        source="founder",
        content="Write the project charter.",
        author_clerk_user_id=MEMBER,
    )
    if question is not None:
        await db.append_task_entry(run_id=run_id, kind="question", source="codex", content=question)
    run = await db.get_run(run_id)
    assert run is not None
    return run


async def harness(db, monkeypatch):
    monkeypatch.setattr(
        db,
        "has_project_access",
        AsyncMock(side_effect=lambda **kw: kw["clerk_user_id"] == MEMBER),
    )
    monkeypatch.setattr(db, "record_mcp_usage", AsyncMock())
    monkeypatch.setattr(db, "record_tin_user", AsyncMock())
    handle = SimpleNamespace(signal=AsyncMock())
    runtime = SimpleNamespace(
        database=db,
        sandboxes=SimpleNamespace(control_task=AsyncMock(return_value=True)),
        temporal=SimpleNamespace(get_workflow_handle=Mock(return_value=handle)),
    )
    token = SimpleNamespace(subject=OUTSIDER, scopes=["openid"], client_id="test-client")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    server, _ = create_mcp_app(
        settings=SimpleNamespace(
            switchboard_public_url="https://tin.test",
            clerk_frontend_api_url="https://clerk.test",
        ),
        auth=SimpleNamespace(),
        runtime=lambda: runtime,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = runtime
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=token.subject,
        token_type="session_token",  # noqa: S106
    )
    return SimpleNamespace(
        db=db, runtime=runtime, handle=handle, token=token, server=server, app=app
    )


def web(h):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="https://tin.test")


async def call(h, tool: str, **arguments):
    return (await h.server.call_tool(tool, arguments)).structured_content


async def test_mcp_reads_the_waiting_question_and_answers_it_like_the_web(task_db, monkeypatch):
    h = await harness(task_db, monkeypatch)
    run = await seed_task(task_db)
    message = {"run_id": str(run.id), "message": ANSWER}

    with pytest.raises(ToolError, match="project not found"):
        await call(h, "get_run", run_id=str(run.id))
    with pytest.raises(ToolError, match="project not found"):
        await call(h, "send_project_task_message", **message)
    async with web(h) as client:
        assert (await client.post(f"/api/tasks/{run.id}/messages", json=message)).status_code == 404
    h.handle.signal.assert_not_awaited()
    assert [entry.kind for entry in await task_db.list_task_entries(run_id=run.id)] == [
        "instruction",
        "question",
    ]

    h.token.subject = MEMBER
    shown = await call(h, "get_run", run_id=str(run.id))
    assert shown["status"] == "needs_input"
    assert shown["allowed_actions"] == ["answer"]
    assert shown["task"]["phase"] == "needs_input"
    assert shown["task"]["question"] == QUESTION
    assert shown["task"]["title"] == "Project charter"
    assert [entry["kind"] for entry in shown["task"]["entries"]] == ["instruction", "question"]
    assert shown["task"]["entries"][-1]["content"] == QUESTION
    listed = await call(h, "list_project_runs", project_id=str(run.project_id))
    assert listed["result"][0]["task"] == {
        "title": "Project charter",
        "phase": "needs_input",
        "question": QUESTION,
    }

    request_id = str(uuid4())
    answered = await call(h, "send_project_task_message", request_id=request_id, **message)
    assert answered["message"]["kind"] == "answer"
    assert answered["message"]["delivery"] == "resumed"
    assert [entry["kind"] for entry in answered["task"]["entries"]] == [
        "instruction",
        "question",
        "answer",
    ]
    h.handle.signal.assert_awaited_once_with("resume")
    h.runtime.temporal.get_workflow_handle.assert_called_with(run.temporal_workflow_id)
    h.runtime.sandboxes.control_task.assert_not_awaited()

    async with web(h) as client:
        page = await client.get(f"/api/tasks/{run.id}")
        assert page.status_code == 200
        latest = page.json()["entries"][-1]
        assert latest["id"] == answered["message"]["entry_id"]
        assert latest["kind"] == "answer"
        assert latest["content"] == ANSWER
        assert latest["source"] == "founder"
        retried = await client.post(
            f"/api/tasks/{run.id}/messages", json={"request_id": request_id, **message}
        )
        reused = await client.post(
            f"/api/tasks/{run.id}/messages",
            json={"request_id": request_id, "message": "Operators."},
        )
    # The answer already resumed the task, so the replay keeps the saved entry and its kind
    # instead of failing because a fresh message would now count as direction.
    assert retried.status_code == 200
    assert [entry["kind"] for entry in retried.json()["entries"]] == [
        "instruction",
        "question",
        "answer",
    ]
    assert retried.json()["entries"][-1]["id"] == answered["message"]["entry_id"]
    assert h.handle.signal.await_count == 1
    # The resumed turn already has the answer; the replay must not steer it in again.
    h.runtime.sandboxes.control_task.assert_not_awaited()
    assert reused.status_code == 409
    assert reused.json()["detail"] == "task message request ID belongs to different content"


async def test_direction_resumes_a_paused_task_and_clears_its_control(task_db, monkeypatch):
    h = await harness(task_db, monkeypatch)
    h.token.subject = MEMBER
    run = await seed_task(task_db, status="paused", phase="paused", question=None, control="pause")
    assert (await call(h, "get_run", run_id=str(run.id)))["allowed_actions"] == ["direct"]

    directed = await call(
        h, "send_project_task_message", run_id=str(run.id), message="Keep it to one page."
    )
    assert directed["message"] == {
        "entry_id": directed["message"]["entry_id"],
        "kind": "direction",
        "delivery": "resumed",
    }
    assert directed["task"]["control"] is None
    h.handle.signal.assert_awaited_once_with("resume")
    refreshed = await task_db.get_run(run.id)
    assert refreshed is not None and refreshed.task_control is None


async def test_direction_steers_a_running_task_or_queues_when_no_sandbox_handle(
    task_db, monkeypatch
):
    h = await harness(task_db, monkeypatch)
    h.token.subject = MEMBER
    run = await seed_task(task_db, status="running", phase="working", question=None)

    steered = await call(
        h, "send_project_task_message", run_id=str(run.id), message="Add a timeline."
    )
    assert steered["message"]["kind"] == "direction"
    assert steered["message"]["delivery"] == "steered"
    control = h.runtime.sandboxes.control_task.await_args.kwargs["control"]
    assert control["type"] == "steer"
    assert control["content"] == "Add a timeline."
    assert control["entry_id"] == steered["message"]["entry_id"]
    h.handle.signal.assert_not_awaited()

    h.runtime.sandboxes.control_task.return_value = False
    queued = await call(h, "send_project_task_message", run_id=str(run.id), message="And a budget.")
    assert queued["message"]["delivery"] == "queued"
    assert [entry.kind for entry in await task_db.list_task_entries(run_id=run.id)] == [
        "instruction",
        "direction",
        "direction",
    ]


async def test_finished_tasks_and_blank_messages_are_refused_on_both_surfaces(task_db, monkeypatch):
    h = await harness(task_db, monkeypatch)
    h.token.subject = MEMBER
    run = await seed_task(task_db, status="succeeded", phase="completed", question=None)

    with pytest.raises(ToolError, match="task has finished"):
        await call(h, "send_project_task_message", run_id=str(run.id), message="More?")
    with pytest.raises(ToolError, match="enter a message"):
        await call(h, "send_project_task_message", run_id=str(run.id), message="   ")
    async with web(h) as client:
        finished = await client.post(f"/api/tasks/{run.id}/messages", json={"message": "More?"})
    assert finished.status_code == 409
    assert finished.json()["detail"] == "task has finished"
    assert [entry.kind for entry in await task_db.list_task_entries(run_id=run.id)] == [
        "instruction"
    ]


async def test_a_saved_answer_survives_a_failed_resume_signal(task_db, monkeypatch):
    h = await harness(task_db, monkeypatch)
    h.token.subject = MEMBER
    h.handle.signal.side_effect = RuntimeError("temporal unreachable")
    run = await seed_task(task_db)

    with pytest.raises(ToolError, match="delivery_failed: task direction was saved"):
        await call(h, "send_project_task_message", run_id=str(run.id), message=ANSWER)
    entries = await task_db.list_task_entries(run_id=run.id)
    assert [entry.kind for entry in entries] == ["instruction", "question", "answer"]
    assert entries[-1].content == ANSWER


async def test_a_failed_resume_signal_leaves_the_task_waiting_for_a_retry(task_db, monkeypatch):
    h = await harness(task_db, monkeypatch)
    h.token.subject = MEMBER
    h.handle.signal.side_effect = [RuntimeError("temporal unreachable"), None]
    run = await seed_task(task_db)
    message = {"run_id": str(run.id), "message": ANSWER, "request_id": str(uuid4())}

    with pytest.raises(ToolError, match="delivery_failed: task direction was saved"):
        await call(h, "send_project_task_message", **message)
    waiting = await task_db.get_run(run.id)
    assert waiting is not None
    assert (waiting.status, waiting.task_phase) == (RunStatus.NEEDS_INPUT, "needs_input")
    assert waiting.task_question == QUESTION

    retried = await call(h, "send_project_task_message", **message)
    assert retried["message"]["kind"] == "answer"
    assert retried["message"]["delivery"] == "resumed"
    assert retried["status"] == "running"
    assert h.handle.signal.await_count == 2
    h.runtime.sandboxes.control_task.assert_not_awaited()


async def test_web_resume_is_retryable_after_a_failed_signal(task_db, monkeypatch):
    h = await harness(task_db, monkeypatch)
    h.token.subject = MEMBER
    h.handle.signal.side_effect = [RuntimeError("temporal unreachable"), None]
    run = await seed_task(task_db, status="paused", phase="paused", question=None)

    async with web(h) as client:
        failed = await client.post(f"/api/tasks/{run.id}/resume")
        paused = await task_db.get_run(run.id)
        resumed = await client.post(f"/api/tasks/{run.id}/resume")
        running = await client.post(f"/api/tasks/{run.id}/resume")
    assert failed.status_code == 502
    assert failed.json()["detail"] == "task resume was not accepted"
    assert paused is not None and paused.status is RunStatus.PAUSED
    assert resumed.status_code == 200
    assert (resumed.json()["status"], resumed.json()["task_phase"]) == ("running", "working")
    # A running workflow never receives a resume that would skip its next question.
    assert running.status_code == 409
    assert running.json()["detail"] == "task is not paused or waiting for an answer"
    assert h.handle.signal.await_count == 2


async def test_a_replayed_direction_reaches_the_running_turn_once(task_db, monkeypatch):
    h = await harness(task_db, monkeypatch)
    h.token.subject = MEMBER
    run = await seed_task(task_db, status="running", phase="working", question=None)
    message = {"run_id": str(run.id), "message": "Add a timeline.", "request_id": str(uuid4())}

    steered = await call(h, "send_project_task_message", **message)
    replayed = await call(h, "send_project_task_message", **message)
    assert steered["message"]["delivery"] == "steered"
    assert replayed["message"]["entry_id"] == steered["message"]["entry_id"]
    assert replayed["message"]["delivery"] == "queued"
    assert h.runtime.sandboxes.control_task.await_count == 1
    h.handle.signal.assert_not_awaited()


async def test_an_apply_that_fails_at_once_can_be_approved_again(task_db, monkeypatch):
    h = await harness(task_db, monkeypatch)
    h.token.subject = MEMBER
    diff = {"files": [{"path": "docs/charter.md", "state": "added"}], "sha256": "0" * 64}
    run = await seed_task(task_db, phase="review", question=None, task_diff=diff)

    async def apply_fails_at_once(name):
        assert name == "approve"
        await task_db.defer_task_approval(
            run_id=run.id, summary="Tin could not apply the reviewed changes."
        )

    h.handle.signal.side_effect = apply_fails_at_once
    await call(h, "approve_workflow_run", run_id=str(run.id))
    deferred = await task_db.get_run(run.id)
    assert deferred is not None
    assert (deferred.status, deferred.task_phase) == (RunStatus.NEEDS_INPUT, "review")

    h.handle.signal.side_effect = RuntimeError("temporal unreachable")
    with pytest.raises(ToolError, match="delivery_failed: task approval was not accepted"):
        await call(h, "approve_workflow_run", run_id=str(run.id))
    reopened = await task_db.get_run(run.id)
    assert reopened is not None
    assert (reopened.status, reopened.task_phase) == (RunStatus.NEEDS_INPUT, "review")

    h.handle.signal.side_effect = None
    approved = await call(h, "approve_workflow_run", run_id=str(run.id))
    assert (approved["status"], approved["task"]["phase"]) == ("running", "applying")
    assert h.handle.signal.await_count == 3


async def test_a_cancelled_approve_signal_returns_the_task_to_review() -> None:
    earlier = "The last approval could not reach the task."
    runs = {"current": task_run(task_phase="review", task_question=None, error_message=earlier)}

    class Database:
        async def get_run(self, run_id):
            return runs["current"]

        async def has_project_access(self, *, project_id, clerk_user_id):
            return clerk_user_id == MEMBER

        async def begin_task_approval(self, *, run_id, clerk_user_id):
            runs["current"] = replace(
                runs["current"],
                status=RunStatus.RUNNING,
                task_phase="applying",
                error_message=None,
            )
            return runs["current"]

        async def reopen_task_review(self, *, run_id, error_message=None):
            runs["current"] = replace(
                runs["current"],
                status=RunStatus.NEEDS_INPUT,
                task_phase="review",
                error_message=error_message,
            )

    handle = SimpleNamespace(signal=AsyncMock(side_effect=asyncio.CancelledError()))
    runtime = SimpleNamespace(
        database=Database(),
        temporal=SimpleNamespace(get_workflow_handle=Mock(return_value=handle)),
    )
    run_id = runs["current"].id

    with pytest.raises(asyncio.CancelledError):
        await approve_project_task(runtime=runtime, run_id=run_id, clerk_user_id=MEMBER)
    reopened = runs["current"]
    assert (reopened.status, reopened.task_phase) == (RunStatus.NEEDS_INPUT, "review")
    assert reopened.error_message == earlier

    handle.signal.side_effect = None
    approved = await approve_project_task(runtime=runtime, run_id=run_id, clerk_user_id=MEMBER)
    assert (approved.status, approved.task_phase) == (RunStatus.RUNNING, "applying")
    assert handle.signal.await_count == 2


async def test_a_cancelled_approval_can_be_approved_again(task_db, monkeypatch):
    h = await harness(task_db, monkeypatch)
    h.token.subject = MEMBER
    diff = {"files": [{"path": "docs/charter.md", "state": "added"}], "sha256": "0" * 64}
    run = await seed_task(task_db, phase="review", question=None, task_diff=diff)
    earlier = "The last approval could not reach the task."
    await task_db.pool.execute(
        "UPDATE workflow_runs SET error_message = $2 WHERE id = $1", run.id, earlier
    )

    # An MCP disconnect or shutdown cancels the approval while the signal is in flight.
    h.handle.signal.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await approve_project_task(runtime=h.runtime, run_id=run.id, clerk_user_id=MEMBER)
    reopened = await task_db.get_run(run.id)
    assert reopened is not None
    assert (reopened.status, reopened.task_phase) == (RunStatus.NEEDS_INPUT, "review")
    assert reopened.error_message == earlier

    h.handle.signal.side_effect = None
    approved = await call(h, "approve_workflow_run", run_id=str(run.id))
    assert (approved["status"], approved["task"]["phase"]) == ("running", "applying")
    assert h.handle.signal.await_count == 2


async def test_mcp_approves_reviewed_task_changes_like_the_web(task_db, monkeypatch):
    h = await harness(task_db, monkeypatch)
    h.token.subject = MEMBER
    asking = await seed_task(task_db)
    with pytest.raises(ToolError, match="task changes are not waiting for approval"):
        await call(h, "approve_workflow_run", run_id=str(asking.id))
    h.handle.signal.assert_not_awaited()

    diff = {"files": [{"path": "docs/charter.md", "state": "added"}], "sha256": "0" * 64}
    run = await seed_task(task_db, phase="review", question=None, task_diff=diff)

    shown = await call(h, "get_run", run_id=str(run.id))
    assert shown["allowed_actions"] == ["approve", "direct"]
    assert shown["task"]["phase"] == "review"
    assert shown["task"]["changed_files"] == [{"path": "docs/charter.md", "state": "added"}]

    approved = await call(h, "approve_workflow_run", run_id=str(run.id))
    assert approved["status"] == "running"
    assert approved["task"]["phase"] == "applying"
    assert approved["allowed_actions"] == ["direct"]
    h.handle.signal.assert_awaited_once_with("approve")

    async with web(h) as client:
        again = await client.post(f"/api/tasks/{run.id}/approve")
    assert again.status_code == 202
    assert again.json()["status"] == "running"
    assert again.json()["task_phase"] == "applying"
    assert h.handle.signal.await_count == 1
