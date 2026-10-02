"""Anything waiting in Decisions can be discarded instead of approved.

The run ends as declined, a current guide stays unchanged, the decision leaves Decisions and
every count, and the waiting durable run is ended through the review dispatcher.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from test_procedure_publication import publication_db as publication_db

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.workflow_review_dispatch import dispatch_reviews

MEMBER = "user_member"
SHA = "a" * 40
WORKFLOWS = {
    "style.capture": ("Capture writing style", "style.capture"),
    "brand.capture": ("Capture brand and design", "codex.procedure"),
    "research.deep_dive": ("Research", "codex.procedure"),
}


async def proposal(db, key="style.capture"):
    """A project whose member has one proposal from `key` waiting in Decisions."""
    project = await db.create_project(name="Example project", state_repo_id=f"projects/{uuid4()}")
    await db.pool.execute(
        "INSERT INTO project_memberships (project_id, clerk_user_id) VALUES ($1, $2)",
        project.id,
        MEMBER,
    )
    title, executor = WORKFLOWS[key]
    workflow_id = await db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", key)
    if workflow_id is None:
        workflow_id = uuid4()
        await db.pool.execute(
            """INSERT INTO workflows (id, key, title, executor, definition_repo_id,
                   definition_path, current_commit_sha, version_label, definition)
               VALUES ($1, $2, $3, $4, 'registry/workflows', 'workflow.json', $5, '1', '{}')""",
            workflow_id,
            key,
            title,
            executor,
            "d" * 40,
        )
    run_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
            temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
            review_required)
           VALUES ($1,$2,$3,$4,$5,$6,$7,1,1,'running',false,true)""",
        run_id,
        project.id,
        workflow_id,
        executor,
        "d" * 40,
        f"{key}:{run_id}",
        str(run_id),
    )
    path = f"style/proposals/2026-09-29-writing-style-{run_id}.md"
    await db.request_human_review(
        run_id=run_id,
        canonical_commit_sha=SHA,
        artifact_ref=f"code.storage://{project.state_repo_id}@{SHA}/{path}",
        artifact_path=path,
        artifact_title="Proposed writing style guide",
        summary="Your writing style guide is ready. Approve it to save it for future drafts.",
    )
    return project, run_id


def client_for(db, temporal=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=MEMBER,
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
    )
    app.state.runtime = SimpleNamespace(database=db, temporal=temporal)
    app.state.settings = SimpleNamespace()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_discard_ends_the_run_as_declined_and_leaves_decisions(publication_db):
    db = publication_db
    project, run_id = await proposal(db)
    [decision] = await db.list_pending_decisions(project_id=project.id)
    assert (await db.get_project_system_summary(project_id=project.id))["waiting_count"] == 1

    async with client_for(db) as client:
        response = await client.post(
            f"/api/decisions/{decision['id']}/apply", json={"action": "decline"}
        )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "stopped"
    assert response.json()["review_decision"] == "declined"

    run = await db.get_run(run_id)
    assert (run.status.value, run.review_decision) == ("stopped", "declined")
    assert run.reviewed_at is not None and run.finished_at is not None
    # The proposal stays readable in Files; nothing it proposed is used.
    assert run.artifact_path.startswith("style/proposals/")
    assert not await db.list_pending_decisions(project_id=project.id)
    assert (await db.get_project_system_summary(project_id=project.id))["waiting_count"] == 0
    row = await db.pool.fetchrow(
        "SELECT status, response FROM run_decisions WHERE run_id=$1", run_id
    )
    assert row["status"] == "dismissed" and '"declined"' in row["response"]
    event = await db.pool.fetchrow(
        "SELECT summary, audience FROM activity_events "
        "WHERE run_id=$1 AND event_type='human_review_declined'",
        run_id,
    )
    assert event["summary"] == (
        "You discarded the proposed writing style guide. The current guide is unchanged."
    )
    assert event["audience"] == "product"


@pytest.mark.asyncio
async def test_a_declined_proposal_is_decided_once(publication_db):
    db = publication_db
    project, run_id = await proposal(db)
    first = await db.decline_review(run_id=run_id, clerk_user_id=MEMBER, summary="Discarded.")
    again = await db.decline_review(run_id=run_id, clerk_user_id=MEMBER, summary="Discarded.")
    assert first.id == again.id and again.review_decision == "declined"
    assert (
        await db.pool.fetchval(
            "SELECT count(*) FROM workflow_review_commands WHERE source_run_id=$1", run_id
        )
        == 1
    )
    assert (
        await db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='human_review_declined'",
            run_id,
        )
        == 1
    )
    # A retried approval cannot reopen it.
    with pytest.raises(RuntimeError, match="different review decision"):
        await db.record_human_review(run_id=run_id, decision="approved")


@pytest.mark.asyncio
async def test_the_dashboard_and_mcp_discard_through_one_service(publication_db):
    from tin_lite.proposal_decline import ONLY_REVIEWS, discard_review

    db = publication_db
    _, run_id = await proposal(db)
    runtime = SimpleNamespace(database=db)
    # Another project's run reads the same as a run that doesn't exist.
    for actor, target in (("user_stranger", run_id), (MEMBER, uuid4())):
        with pytest.raises(LookupError, match="run not found"):
            await discard_review(runtime=runtime, run_id=target, actor=actor)
    first = await discard_review(runtime=runtime, run_id=run_id, actor=MEMBER)
    assert (first.status.value, first.review_decision) == ("stopped", "declined")
    # Discarding again returns the run as it was left; one decision is recorded.
    again = await discard_review(runtime=runtime, run_id=run_id, actor=MEMBER)
    assert again.id == first.id and again.review_decision == "declined"
    assert (
        await db.pool.fetchval(
            "SELECT count(*) FROM workflow_review_commands WHERE source_run_id=$1", run_id
        )
        == 1
    )
    # What waits without something to approve is refused, as the dashboard refuses it.
    _, other = await proposal(db)
    await db.pool.execute("UPDATE run_decisions SET kind='select' WHERE run_id=$1", other)
    with pytest.raises(ValueError, match=ONLY_REVIEWS):
        await discard_review(runtime=runtime, run_id=other, actor=MEMBER)
    decision = await db.get_pending_decision_for_run(run_id=other)
    async with client_for(db) as client:
        response = await client.post(
            f"/api/decisions/{decision['id']}/apply", json={"action": "decline"}
        )
    assert response.status_code == 409 and response.json()["detail"] == ONLY_REVIEWS


@pytest.mark.asyncio
async def test_any_waiting_draft_can_be_discarded_once(publication_db):
    db = publication_db
    _, brand = await proposal(db, "brand.capture")
    project, research = await proposal(db, "research.deep_dive")
    async with client_for(db) as client:
        [decision] = await db.list_pending_decisions(project_id=project.id)
        accepted = await client.post(
            f"/api/decisions/{decision['id']}/apply", json={"action": "decline"}
        )
        assert accepted.status_code == 202, accepted.text
        run = await db.get_run(research)
        assert (run.status.value, run.review_decision) == ("stopped", "declined")
        event = await db.pool.fetchval(
            "SELECT summary FROM activity_events "
            "WHERE run_id=$1 AND event_type='human_review_declined'",
            research,
        )
        assert event == (
            "You discarded this. It stays readable in Files; nothing was published or applied."
        )

        brand_decision = await db.pool.fetchval(
            "SELECT id FROM run_decisions WHERE run_id=$1", brand
        )
        accepted = await client.post(
            f"/api/decisions/{brand_decision}/apply", json={"action": "decline"}
        )
        assert accepted.status_code == 202
        # Once decided, the decision is gone.
        gone = await client.post(
            f"/api/decisions/{brand_decision}/apply", json={"action": "decline"}
        )
        assert gone.status_code == 404
    _, running = await proposal(db, "research.deep_dive")
    with pytest.raises(RuntimeError, match="no longer waiting"):
        await db.pool.execute("UPDATE workflow_runs SET status='running' WHERE id=$1", running)
        await db.decline_review(run_id=running, clerk_user_id=MEMBER, summary="Discarded.")


@pytest.mark.asyncio
async def test_the_dispatcher_ends_the_waiting_run_until_temporal_accepts(publication_db):
    db = publication_db
    _, run_id = await proposal(db)
    await db.decline_review(run_id=run_id, clerk_user_id=MEMBER, summary="Discarded.")
    handle = SimpleNamespace(signal=AsyncMock(), cancel=AsyncMock(side_effect=[OSError, None]))
    runtime = SimpleNamespace(
        database=db, temporal=SimpleNamespace(get_workflow_handle=lambda _: handle)
    )
    await dispatch_reviews(runtime, SimpleNamespace())
    await dispatch_reviews(runtime, SimpleNamespace())
    await dispatch_reviews(runtime, SimpleNamespace())
    assert handle.cancel.await_count == 2
    handle.signal.assert_not_awaited()
    assert (
        await db.pool.fetchval(
            "SELECT dispatch_state FROM workflow_review_commands WHERE source_run_id=$1", run_id
        )
        == "received"
    )
    # Ending the durable run does not turn the declined review into a failure.
    await db.project_failure(run_id=run_id, error_message="cancelled")
    run = await db.get_run(run_id)
    assert (run.status.value, run.review_decision, run.error_message) == (
        "stopped",
        "declined",
        None,
    )


@pytest.mark.asyncio
async def test_discarding_a_task_stops_it_without_applying_its_changes(publication_db):
    from test_task_slots import task, task_workflow

    db = publication_db
    project, _ = await proposal(db)
    run_id = await task(
        db, project.id, await task_workflow(db), status="needs_input", phase="review"
    )
    decision = next(
        item
        for item in await db.list_pending_decisions(project_id=project.id)
        if item["run_id"] == run_id
    )
    handle = SimpleNamespace(signal=AsyncMock())
    temporal = SimpleNamespace(get_workflow_handle=lambda _id: handle)
    async with client_for(db, temporal=temporal) as client:
        client._transport.app.state.runtime.sandboxes = SimpleNamespace(
            control_task=AsyncMock(return_value=True), kill=AsyncMock()
        )
        response = await client.post(
            f"/api/decisions/{decision['id']}/apply", json={"action": "decline"}
        )
    assert response.status_code == 202, response.text
    handle.signal.assert_awaited_once_with("stop")
    run = await db.get_run(run_id)
    assert run.task_control == "stop" and run.review_decision is None
