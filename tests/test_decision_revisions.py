"""Approving a draft never uses a copy older than a task's revision of the same file."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from test_answer_page import activity_fixture as answer_page_fixture
from test_answer_page import authenticate
from test_procedure_publication import PATH, activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.api import router
from tin_lite.catalog import ANSWER_PAGE_WORKFLOW_ID
from tin_lite.domain import RunStatus
from tin_lite.mcp_server import create_mcp_app
from tin_lite.review_revisions import approval_conflict

SHA = "b" * 40


async def saved_draft(db):
    _, _, run, _ = await activity_fixture(db, review=True)
    await db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=SHA,
        artifact_ref=f"code.storage://repo@{SHA}/{PATH}",
        artifact_path=PATH,
        summary="Research is ready for your review.",
    )
    return await db.get_run(run.id)


async def draft_decision(db, draft):
    # The waiting task is a decision of its own; read the draft's.
    decisions = await db.list_pending_decisions(project_id=draft.project_id)
    return next(item for item in decisions if item["run_id"] == draft.id)


async def revising_task(db, project_id, *, path=PATH, applied=False, applied_before=False):
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
    run_id = uuid4()
    # Insert the final state directly: a project holds one active task at a time.
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
            temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
            task_phase, task_has_changes, task_title, task_diff, review_decision,
            canonical_commit_sha, finished_at, reviewed_at)
           VALUES ($1,$2,$3,'project.task',$4,$5,$6,1,1,$7,false,$8,true,
                   'Revise answer page draft',$9::jsonb,$10,$11,$12,$12)""",
        run_id,
        project_id,
        workflow_id,
        "d" * 40,
        f"project.task:{run_id}",
        str(run_id),
        "succeeded" if applied else "needs_input",
        "finished" if applied else "review",
        json.dumps({"sha256": "0" * 64, "files": [{"path": path, "state": "modified"}]}),
        "approved" if applied else None,
        "c" * 40 if applied else None,
        (datetime.now(UTC) - timedelta(days=1 if applied_before else 0)) if applied else None,
    )
    return run_id


def as_answer_page(run):
    # The shared fixture registers a research workflow; approval only publishes answer pages,
    # articles and planned drafts.
    return replace(run, workflow_id=ANSWER_PAGE_WORKFLOW_ID)


@pytest.mark.asyncio
async def test_a_waiting_revision_replaces_the_older_copy_in_decisions(publication_db):
    db = publication_db
    draft = await saved_draft(db)
    task_id = await revising_task(db, draft.project_id)
    # Only the newer version is listed and counted; the older copy waits out of sight.
    listed = await db.list_pending_decisions(project_id=draft.project_id)
    assert [item["run_id"] for item in listed] == [task_id]
    summary = await db.get_project_system_summary(project_id=draft.project_id)
    assert summary["waiting_count"] == 1
    assert (await db.output_revision(run_id=draft.id))["state"] == "waiting"
    for delivery in (None, "github_commit", "github_pr", "none"):
        conflict = await approval_conflict(db, as_answer_page(draft), delivery)
        assert conflict and "“Revise answer page draft”" in conflict
        assert "Review it first" in conflict


@pytest.mark.asyncio
async def test_an_applied_revision_keeps_the_older_copy_out_of_publication(publication_db):
    db = publication_db
    draft = await saved_draft(db)
    await revising_task(db, draft.project_id, applied=True)
    decision = await draft_decision(db, draft)
    assert decision["revision"]["state"] == "applied"
    assert decision["revision"]["revision"] == "c" * 40
    for delivery in (None, "github_commit", "github_pr"):
        conflict = await approval_conflict(db, as_answer_page(draft), delivery)
        assert conflict and "would send the older copy" in conflict
    # Keeping the older copy in Tin publishes nothing, and a report never publishes.
    assert await approval_conflict(db, as_answer_page(draft), "none") is None
    assert await approval_conflict(db, draft, None) is None


@pytest.mark.asyncio
async def test_unrelated_or_earlier_task_changes_do_not_block_approval(publication_db):
    db = publication_db
    draft = await saved_draft(db)
    await revising_task(db, draft.project_id, path="reports/OTHER.md")
    await revising_task(db, draft.project_id, applied=True, applied_before=True)
    decision = await draft_decision(db, draft)
    assert decision["revision"] is None
    assert await approval_conflict(db, as_answer_page(draft), "github_commit") is None


@pytest.mark.asyncio
async def test_approval_refuses_before_recording_a_delivery_or_signalling_the_run() -> None:
    project, run, _ = answer_page_fixture()
    run = replace(run, status=RunStatus.NEEDS_INPUT, artifact_path="reports/ANSWER_PAGE.md")

    class Database:
        async def get_run(self, run_id):
            return run if run_id == run.id else None

        async def get_project(self, project_id):
            return project if project_id == project.id else None

        async def has_project_access(self, *, project_id, clerk_user_id):
            return project_id == project.id and clerk_user_id == "user_test"

        async def output_revision(self, *, run_id):
            assert run_id == run.id
            return {"run_id": str(uuid4()), "title": "Revise answer page draft", "state": "waiting"}

        def __getattr__(self, name):
            raise AssertionError(f"a refused approval touched {name}")

    class TemporalMustNotBeSignaled:
        def __getattr__(self, name):
            raise AssertionError(f"a refused approval touched Temporal: {name}")

    app = FastAPI()
    app.include_router(router)
    authenticate(app)
    app.state.runtime = SimpleNamespace(database=Database(), temporal=TemporalMustNotBeSignaled())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/workflows/runs/{run.id}/approve", json={"delivery": "github_commit"}
        )
    assert response.status_code == 409
    assert "A revision of this draft is waiting" in response.json()["detail"]


@pytest.mark.asyncio
async def test_agents_cannot_approve_a_draft_while_its_revision_waits(publication_db, monkeypatch):
    db = publication_db
    draft = await saved_draft(db)
    await revising_task(db, draft.project_id)
    monkeypatch.setattr(db, "has_project_access", AsyncMock(return_value=True))
    monkeypatch.setattr(db, "record_mcp_usage", AsyncMock())
    monkeypatch.setattr(db, "record_tin_user", AsyncMock())
    handle = SimpleNamespace(signal=AsyncMock())
    runtime = SimpleNamespace(
        database=db, temporal=SimpleNamespace(get_workflow_handle=Mock(return_value=handle))
    )
    token = SimpleNamespace(subject="user_member", scopes=["openid"], client_id="client_test")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    server, _ = create_mcp_app(
        settings=SimpleNamespace(
            switchboard_public_url="https://tin.test", clerk_frontend_api_url="https://clerk.test"
        ),
        auth=SimpleNamespace(),
        runtime=lambda: runtime,
    )
    with pytest.raises(ToolError, match="conflict: A revision of this draft is waiting"):
        await server.call_tool("approve_workflow_run", {"run_id": str(draft.id)})
    handle.signal.assert_not_awaited()
    assert (await db.get_run(draft.id)).status == RunStatus.NEEDS_INPUT
