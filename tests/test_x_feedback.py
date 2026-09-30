"""X feedback uses real admission, files, review and HTTP/MCP boundaries; no provider calls."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db
from test_x_draft import fixture as draft_fixture
from test_x_posts import draft
from test_x_style_db import fixture as style_fixture
from test_x_style_db import start as start_style

from tin_lite import x_feedback, x_style
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.model_providers import ModelResult, ModelUsage, ProviderName
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_review_dispatch import dispatch_reviews
from tin_lite.workflow_review_store import ReviewConflict
from tin_lite.workflow_reviews import WorkflowReviews
from tin_lite.x_feedback_activities import XFeedbackActivities

PATH = "social/x/draft.json"
GUIDE = "# X writing style\n\nX account ID: 12345\n\n## Explicit preferences\n\nNo hashtags.\n"
FEEDBACK = (
    "  For product updates, lead with the demo.\nRemove the second sentence from this post.  "
)
RULE = "Lead product updates with the demo."
RESULT = {
    "text": "we shipped the demo.",
    "summary": "Removed the second sentence.",
    "preferences": [{"rule": RULE, "evidence": "For product updates, lead with the demo."}],
}


async def setup(db, *, guide=False):
    f = await (style_fixture(db) if guide else draft_fixture(db))
    if guide:
        source = await start_style(f)
        await f.activities.prepare(str(source.id))
        await f.activities.extract(str(source.id))
        await f.activities.propose(str(source.id))
        f.style_activities = f.activities
    else:
        workflow = await db.get_registry_workflow("social.x_compose")
        source, _ = await db.create_run(
            project_id=f.project.id,
            workflow_id=workflow.id,
            started_by_clerk_user_id=ACTOR,
            input_payload={"project_id": str(f.project.id)},
        )
        value = draft(
            text="we shipped the demo. it is amazing.",
            attachments=[{"type": "image", "path": "images/demo.png", "alt_text": "The demo"}],
        )
        other = deepcopy(value["posts"][0])
        other["id"] = "p2"
        other["text"] = "another post."
        value["posts"].append(other)
        sha = f.storage.repo.edit(
            {PATH: json.dumps(value).encode(), x_style.GUIDE_PATH: GUIDE.encode()}
        )
        await db.pool.execute(
            "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, "
            "artifact_path=$3 WHERE id=$1",
            source.id,
            sha,
            PATH,
        )
    f.source = await db.get_run(source.id)
    builtin = next(w for w in BUILTIN_WORKFLOWS if w.key == x_feedback.KEY)
    await db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=builtin.key,
        title=builtin.title,
        description=builtin.description,
        executor=builtin.executor,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha="d" * 40,
        version_label=builtin.version_label,
        definition=builtin.definition,
    )
    f.storage.repo.list_commits = AsyncMock(
        side_effect=lambda **kw: {"commits": [f.storage.repo.commits[f.storage.repo.head]]}
    )
    original = f.storage.read_canonical_artifact

    async def read(**kw):
        if kw["repo_id"] == "registry/workflows" and kw["path"] == builtin.definition_path:
            return json.dumps(builtin.definition).encode()
        return await original(**kw)

    f.storage.read_canonical_artifact = read
    result = deepcopy(RESULT)
    if guide:
        result["text"] = f.storage.repo.trees[f.storage.repo.head][f.source.artifact_path][
            1
        ].decode()
    f.router = SimpleNamespace(
        generate=AsyncMock(
            return_value=ModelResult(
                provider=ProviderName.OPENAI,
                model="gpt-6-sol",
                text="",
                parsed=result,
                request_id="synthetic-revision",
                usage=ModelUsage(input_tokens=100, output_tokens=50),
            )
        )
    )
    f.activities = XFeedbackActivities(database=db, storage=f.storage, router=f.router)
    f.reviews = WorkflowReviews(runtime=f.runtime, settings=f.settings)
    return f


async def request(f, **kwargs):
    post_id = "" if f.source.executor == x_style.KEY else "p1"
    view = await f.reviews.view(f.source.id, ACTOR, post_id)
    return await f.reviews.request_changes(
        run_id=f.source.id,
        actor=ACTOR,
        feedback=FEEDBACK,
        request_id=uuid4(),
        token=view["review_token"],
        post_id=post_id,
        **kwargs,
    )


def current(f, path):
    return f.storage.repo.trees[f.storage.repo.head][path][1]


async def test_post_and_preferences_commit_once_with_lost_response(publication_db):
    f = await setup(publication_db)
    before = json.loads(current(f, PATH))
    run = await request(f)
    await f.activities.generate(str(run.id))
    await f.activities.generate(str(run.id))
    assert f.router.generate.await_count == 1
    packet = json.loads(f.router.generate.await_args.args[1].messages[0].content)
    assert packet["feedback"] == FEEDBACK
    assert packet["post"] == before["posts"][0]
    f.storage.repo.edit({"unrelated.md": b"Keep this later edit"})
    f.storage.repo.lose_response = True
    writes = f.storage.repo.writes
    await f.activities.publish(str(run.id))
    await f.activities.publish(str(run.id))
    assert f.storage.repo.writes == writes + 1
    after = json.loads(current(f, PATH))
    assert after["posts"][1] == before["posts"][1]
    assert after["posts"][0] == {**before["posts"][0], "text": RESULT["text"]}
    assert RULE in current(f, x_style.GUIDE_PATH).decode()
    assert "No hashtags." in current(f, x_style.GUIDE_PATH).decode()
    assert "second sentence" not in current(f, x_style.GUIDE_PATH).decode()
    assert current(f, "unrelated.md") == b"Keep this later edit"
    done = await f.db.get_run(run.id)
    assert (
        done.status.value == "succeeded" and "Remembered for future X posts" in done.result_summary
    )
    assert (await f.db.get_run(f.source.id)).status.value == "succeeded"
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events WHERE run_id=$1 "
            "AND event_type='x_revision_ready'",
            run.id,
        )
        == 1
    )


async def test_http_and_mcp_relay_verbatim_and_deduplicate(publication_db, monkeypatch):
    f = await setup(publication_db)
    server = mcp(f, monkeypatch)
    view = structured(
        await server.call_tool("get_workflow_review", {"run_id": str(f.source.id), "post_id": "p1"})
    )
    payload = {
        "feedback": FEEDBACK,
        "post_id": "p1",
        "request_id": str(uuid4()),
        "review_token": view["review_token"],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        first = await client.post(
            f"/api/workflows/runs/{f.source.id}/request-changes", json=payload
        )
        assert first.status_code == 202, first.text
        same = structured(
            await server.call_tool(
                "request_workflow_changes", {"run_id": str(f.source.id), **payload}
            )
        )
        assert same["run_id"] == first.json()["id"]
        run = await f.db.get_run(uuid_from(first.json()["id"]))
        assert run.input["feedback"] == FEEDBACK
        blocked = await client.post(
            f"/api/workflows/runs/{f.source.id}/request-changes",
            json={**payload, "request_id": str(uuid4())},
        )
        assert blocked.status_code == 409
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE executor=$1", x_feedback.KEY
        )
        == 1
    )
    with pytest.raises(ReviewConflict):
        await f.reviews.request_changes(
            run_id=f.source.id,
            actor=ACTOR,
            feedback="Different",
            request_id=uuid_from(payload["request_id"]),
            token=payload["review_token"],
            post_id="p1",
        )
    with pytest.raises(LookupError):
        await f.reviews.view(f.source.id, "another-member", "p1")


def uuid_from(value):
    from uuid import UUID

    return UUID(value)


async def test_guide_revision_keeps_original_gate_and_approval_pins_revised_copy(publication_db):
    f = await setup(publication_db, guide=True)
    old_view = await f.reviews.view(f.source.id, ACTOR)
    run = await request(f)
    with pytest.raises(ReviewConflict, match="pending revision"):
        await f.reviews.approve(run_id=f.source.id, actor=ACTOR, token=old_view["review_token"])
    await f.activities.generate(str(run.id))
    await f.activities.publish(str(run.id))
    original = await f.db.get_run(f.source.id)
    assert original.status.value == "needs_input" and original.review_decision is None
    assert x_style.GUIDE_PATH not in f.storage.repo.trees[f.storage.repo.head]
    assert RULE in current(f, original.artifact_path).decode()
    # Retried original proposal must not reset the revision's projection.
    assert await f.style_activities.propose(str(original.id))
    assert (await f.db.get_run(original.id)).canonical_commit_sha == original.canonical_commit_sha
    with pytest.raises(ReviewConflict):
        await f.reviews.approve(run_id=original.id, actor=ACTOR, token=old_view["review_token"])
    view = await f.reviews.view(original.id, ACTOR)
    approved = await f.reviews.approve(run_id=original.id, actor=ACTOR, token=view["review_token"])
    assert approved.review_decision == "approved"
    handle = SimpleNamespace(signal=AsyncMock())
    f.runtime.temporal.get_workflow_handle = lambda _: handle
    await dispatch_reviews(f.runtime, f.settings)
    handle.signal.assert_awaited_once_with("approve")
    await f.style_activities.record_approval(str(original.id))
    replay = await f.reviews.approve(run_id=original.id, actor=ACTOR, token=view["review_token"])
    assert replay.review_decision == "approved" and replay.artifact_path is None
    await f.style_activities.publish(str(original.id))
    assert RULE in current(f, x_style.GUIDE_PATH).decode()
    assert (await f.db.get_run(original.id)).status.value == "succeeded"


@pytest.mark.parametrize("path", [PATH, x_style.GUIDE_PATH])
async def test_concurrent_edits_are_not_overwritten(publication_db, path):
    f = await setup(publication_db)
    run = await request(f)
    await f.activities.generate(str(run.id))
    f.storage.repo.edit({path: b"A newer manual edit"})
    with pytest.raises((ValueError, json.JSONDecodeError)):
        await f.activities.publish(str(run.id))
    assert current(f, path) == b"A newer manual edit"
    assert f.router.generate.await_count == 1


async def test_uncertain_model_call_never_repurchased(publication_db):
    f = await setup(publication_db)
    run = await request(f)
    f.router.generate.side_effect = TimeoutError()
    for _ in range(2):
        with pytest.raises(ApplicationError):
            await f.activities.generate(str(run.id))
    assert f.router.generate.await_count == 1
    assert RULE not in current(f, x_style.GUIDE_PATH).decode()


async def test_concurrent_requests_admit_one_and_direct_start_is_refused(publication_db):
    f = await setup(publication_db)
    outcomes = await asyncio.gather(request(f), request(f), return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in outcomes) == 1
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE executor=$1", x_feedback.KEY
        )
        == 1
    )
    with pytest.raises(ValueError, match="request_workflow_changes"):
        await start_workflow_run(
            runtime=f.runtime,
            settings=f.settings,
            workflow=await f.db.get_workflow(x_feedback.WORKFLOW_ID),
            project_id=f.project.id,
            started_by_clerk_user_id=ACTOR,
            input_payload={},
        )


def test_preferences_need_literal_feedback_evidence_and_matching_guide():
    bad = deepcopy(RESULT)
    bad["preferences"][0]["evidence"] = "Always add three hashtags"
    with pytest.raises(ValueError, match="supported"):
        x_feedback.validate_result(
            bad, feedback=FEEDBACK, guide=GUIDE, account="12345", kind="post"
        )
    one_off = {
        "text": "a shorter post.",
        "summary": "Removed the last sentence.",
        "preferences": [],
    }
    result = x_feedback.validate_result(
        one_off, feedback="Just remove this sentence.", guide=GUIDE, account="12345", kind="post"
    )
    assert x_feedback.remember(GUIDE, result["preferences"], account="12345") == GUIDE
    with pytest.raises(ValueError, match="another account"):
        x_feedback.remember(GUIDE, [RULE], account="67890")
    assert RULE in x_feedback.remember("", [RULE], account="unbound")
    already_revised = GUIDE + "\n- " + RULE + "\n"
    assert x_feedback.remember(already_revised, [RULE], account="12345") == already_revised
    assert (
        "'just this post'" in x_feedback.INSTRUCTIONS
        and "Do not learn facts" in x_feedback.INSTRUCTIONS
    )


async def test_invalid_post_cannot_save_preferences_alone(publication_db):
    f = await setup(publication_db)
    original = f.storage.repo.head
    f.router.generate.return_value.parsed["text"] = "Too long " * 100
    run = await request(f)
    with pytest.raises(ApplicationError, match="validated"):
        await f.activities.generate(str(run.id))
    assert f.storage.repo.head == original
    assert f.router.generate.await_count == 1


async def test_revoked_member_cannot_apply_generated_revision(publication_db):
    f = await setup(publication_db)
    run = await request(f)
    await f.activities.generate(str(run.id))
    await f.db.pool.execute("DELETE FROM project_memberships WHERE project_id=$1", f.project.id)
    with pytest.raises(LookupError):
        await f.activities.publish(str(run.id))
    assert current(f, x_style.GUIDE_PATH) == GUIDE.encode()


async def test_projection_retry_recovers_commit_without_new_model_or_write(
    publication_db, monkeypatch
):
    f = await setup(publication_db)
    run = await request(f)
    await f.activities.generate(str(run.id))
    complete = f.db._complete_readonly_report_projection

    async def fail_once(*args, **kwargs):
        raise RuntimeError("Synthetic database interruption after commit")

    monkeypatch.setattr(f.db, "_complete_readonly_report_projection", fail_once)
    with pytest.raises(RuntimeError, match="interruption"):
        await f.activities.publish(str(run.id))
    writes = f.storage.repo.writes
    monkeypatch.setattr(f.db, "_complete_readonly_report_projection", complete)
    await f.activities.publish(str(run.id))
    assert f.storage.repo.writes == writes
    assert (await f.db.get_run(run.id)).status.value == "succeeded"
    assert f.router.generate.await_count == 1


async def test_lost_dispatch_is_recovered_without_billing_loop(publication_db):
    from temporalio.common import WorkflowIDReusePolicy

    from tin_lite.run_service import TemporalStartError
    from tin_lite.workflows import XFeedbackWorkflow

    f = await setup(publication_db)
    f.runtime.temporal.start_workflow.side_effect = TimeoutError()
    with pytest.raises(TemporalStartError) as caught:
        await request(f)
    assert "unconfirmed" in str(caught.value)
    run = await f.db.get_run(caught.value.run_id)
    assert run.status.value == "pending"
    await f.db.pool.execute(
        "UPDATE workflow_runs SET created_at=now()-interval '31 seconds' WHERE id=$1", run.id
    )
    f.runtime.temporal.start_workflow.side_effect = None
    await dispatch_reviews(f.runtime, f.settings)
    call = f.runtime.temporal.start_workflow.await_args
    assert call.args == (XFeedbackWorkflow.run, str(run.id))
    assert call.kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE executor=$1", x_feedback.KEY
        )
        == 1
    )


async def test_project_purge_removes_revision_content_receipts(publication_db):
    f = await setup(publication_db)
    run = await request(f)
    await f.activities.generate(str(run.id))
    async with f.db.pool.acquire() as conn, conn.transaction():
        await f.db.purge_project_data(conn, project_id=f.project.id)
    assert await f.db.get_effect(f"{run.id}:x_feedback_model") is None
