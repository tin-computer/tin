"""Emre, 10/1: Decisions keeps Approve and Discard; the founder's coding agent revises a waiting
brand or style proposal, Tin checks it with the capture's own validators, and approval applies
exactly the version the founder read. Runs pinned before 1.2.0 keep their old rules."""

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_brand_capture import DESIGN, TOKENS, brand_doc
from test_brand_capture import setup as brand_setup
from test_brand_capture import start as brand_start
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db
from test_style_capture import accept, capture_fixture
from test_style_capture import start as style_start

from tin_lite import brand_contract as brand
from tin_lite.activities import TinActivities
from tin_lite.capture_revisions import (
    REVISION_PROJECT_LOCK,
    CaptureRevisions,
    RevisionInvalid,
    RevisionRefused,
    StyleProposalReview,
    binds_style_approval,
    contract,
    pending_owner,
)
from tin_lite.document_handoff import document_handoff
from tin_lite.domain import RunStatus
from tin_lite.project_files import ProjectFileService
from tin_lite.proposal_decline import decline_proposal
from tin_lite.publication import OutputCheckpoint
from tin_lite.reviewed_documents import ReviewedDocuments
from tin_lite.workflow_review_store import ReviewConflict
from tin_lite.writing_style import STYLE_PATH

EDIT = "Use plain words and short sentences; the founder asked for this."
STALE = "changed after you opened it"


def head(f, path):
    tree = f.storage.repo.trees[f.storage.repo.head]
    return tree[path][1] if path in tree else None


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


async def rows(f):
    return await f.db.pool.fetch(
        "SELECT * FROM capture_proposal_revisions WHERE run_id=$1 ORDER BY number", f.run.id
    )


# The writing guide: one proposal file, approved through its review token from 1.2.0.


async def waiting_style(db, *, existing=None, **kwargs):
    f = await capture_fixture(db, **kwargs)
    if existing is not None:
        f.storage.repo.edit({STYLE_PATH: existing})
    run = await style_start(f)
    await f.activities.prepare(str(run.id))
    await f.activities.extract(str(run.id))
    assert await f.activities.propose(str(run.id))
    f.run = await f.db.get_run(run.id)
    f.revisions = CaptureRevisions(database=f.db, storage=f.storage)
    f.reviews = StyleProposalReview(database=f.db, storage=f.storage)
    return f


def edited_guide(f, line=EDIT):
    text = head(f, f.run.artifact_path).decode()
    return text.replace("## Avoid\n\n", f"## Avoid\n\n- {line}\n", 1)


async def revise(f, content, *, token=None, request_id=None, path=None, client="claude_code"):
    if token is None:
        token = (await f.reviews.view(f.run.id, ACTOR))["review_token"]
    return await f.revisions.revise(
        run_id=f.run.id,
        actor=ACTOR,
        review_token=token,
        request_id=request_id or uuid4(),
        files=[{"path": path or f.run.artifact_path, "content": content}],
        source="mcp",
        client=client,
        oauth_client_id="client_test",
    )


async def test_agent_revises_a_waiting_guide_and_approval_saves_the_revision(publication_db):
    f = await waiting_style(publication_db)
    first = f.run.canonical_commit_sha
    original = head(f, f.run.artifact_path)
    writes = f.storage.repo.writes
    result = await revise(f, edited_guide(f))
    assert result["revision_number"] == 1 and not result["replayed"]
    assert result["proposal"] == "writing style guide"

    run = await f.db.get_run(f.run.id)
    assert run.status == RunStatus.NEEDS_INPUT and run.review_decision is None
    assert run.canonical_commit_sha == result["project_revision"] == f.storage.repo.head
    assert (
        EDIT.encode() in head(f, run.artifact_path)
        and STYLE_PATH not in (f.storage.repo.trees[f.storage.repo.head])
    )
    assert f.storage.repo.writes == writes + 1
    # The earlier version stays readable at its own project revision.
    assert f.storage.repo.trees[first][run.artifact_path][1] == original
    [row] = await rows(f)
    assert (row["actor_clerk_user_id"], row["client"], row["source"]) == (
        ACTOR,
        "claude_code",
        "mcp",
    )
    assert row["oauth_client_id"] == "client_test" and row["base_revision"] == first
    assert json.loads(row["files"])[0]["sha256"] == sha(head(f, run.artifact_path))
    [decision] = await f.db.list_pending_decisions(project_id=f.project.id)
    assert decision["items"][0]["revision"] == run.canonical_commit_sha
    assert (
        await f.db.pool.fetchval(
            "SELECT summary FROM activity_events "
            "WHERE run_id=$1 AND event_type='capture_proposal_revised'",
            run.id,
        )
    ).startswith("Claude Code revised the proposed writing style guide (revision 1).")

    view = await f.reviews.view(run.id, ACTOR)
    assert view["can_approve"] and not view["can_request_changes"]
    assert view["artifact"]["proposal_revision"] == 1
    assert view["artifact"]["sha256"] == sha(head(f, run.artifact_path))
    revised = view["proposal_revisions"]
    assert (revised["count"], revised["latest_by"], revised["latest_by_you"]) == (
        1,
        "Claude Code",
        True,
    )
    assert view["documents"] == [
        {
            "path": run.artifact_path,
            "sha256": view["artifact"]["sha256"],
            "destination": STYLE_PATH,
            "change": "new",
        }
    ]
    with pytest.raises(LookupError):
        await f.reviews.view(run.id, "user_outsider")
    assert await binds_style_approval(f.db, f.storage, run)

    await f.reviews.approve(run_id=run.id, actor=ACTOR, token=view["review_token"])
    command = await f.db.pool.fetchrow(
        "SELECT artifact FROM workflow_review_commands WHERE source_run_id=$1 AND action='approve'",
        run.id,
    )
    assert json.loads(command["artifact"])["sha256"] == view["artifact"]["sha256"]
    await f.activities.record_approval(str(run.id))
    await f.activities.publish(str(run.id))
    assert head(f, STYLE_PATH) == head(f, run.artifact_path)
    assert EDIT.encode() in head(f, STYLE_PATH)
    assert (await f.db.get_run(run.id)).status == RunStatus.SUCCEEDED


async def test_a_revision_after_the_founder_opened_the_card_asks_again(publication_db):
    f = await waiting_style(publication_db)
    opened = await f.reviews.view(f.run.id, ACTOR)
    await revise(f, edited_guide(f))
    with pytest.raises(ReviewConflict, match=STALE):
        await f.reviews.approve(run_id=f.run.id, actor=ACTOR, token=opened["review_token"])
    with pytest.raises(ReviewConflict, match="Read the proposed guide"):
        await f.reviews.approve(run_id=f.run.id, actor=ACTOR, token=None)
    assert (await f.db.get_run(f.run.id)).review_decision is None
    again = await f.reviews.view(f.run.id, ACTOR)
    assert again["review_token"] != opened["review_token"]
    await f.reviews.approve(run_id=f.run.id, actor=ACTOR, token=again["review_token"])
    assert (await f.db.get_run(f.run.id)).review_decision == "approved"


@pytest.mark.parametrize("decision", ["approve", "discard"])
async def test_revising_a_decided_proposal_is_refused(publication_db, decision):
    f = await waiting_style(publication_db, existing=b"My current guide\n")
    content = edited_guide(f)
    if decision == "approve":
        await accept(f, f.run)
    else:
        await decline_proposal(database=f.db, run_id=f.run.id, actor=ACTOR)
    writes = f.storage.repo.writes
    with pytest.raises(RevisionRefused, match="no longer waiting in Decisions"):
        await revise(f, content, token="0" * 64)
    assert f.storage.repo.writes == writes and await rows(f) == []
    assert head(f, STYLE_PATH) == b"My current guide\n"


async def test_paths_outside_the_proposal_and_direct_edits_are_refused(publication_db):
    f = await waiting_style(publication_db)
    writes = f.storage.repo.writes
    content = edited_guide(f)
    for path in (
        STYLE_PATH,
        "brand/BRAND.md",
        f.run.artifact_path + ".bak",
        "style/proposals/someone-else.md",
    ):
        with pytest.raises(RevisionRefused, match="not one of this run's proposal files"):
            await revise(f, content, path=path)
    # The generic file route cannot change a waiting proposal behind Decisions' back.
    files = ProjectFileService(database=f.db, storage=f.storage)
    with pytest.raises(ValueError, match="waiting in Decisions"):
        await files.commit(
            project=f.project,
            actor_clerk_user_id=ACTOR,
            client_id="client_test",
            request_id=uuid4(),
            expected_revision=f.storage.repo.head,
            message="Edit the proposal",
            changes=[{"operation": "upsert", "path": f.run.artifact_path, "content": content}],
        )
    assert await pending_owner(f.db, f.storage, f.project.id, {f.run.artifact_path}) == f.run.id
    assert await pending_owner(f.db, f.storage, f.project.id, {"notes/a.md"}) is None
    assert f.storage.repo.writes == writes and await rows(f) == []


async def test_a_revision_must_pass_the_captures_guide_contract(publication_db):
    f = await waiting_style(publication_db)
    content = edited_guide(f)
    writes = f.storage.repo.writes
    for invalid, reason in (
        (content.replace("## Boundaries", "## Limits"), "Boundaries"),
        (content.replace("name: writing-style", "name: voice"), "front matter"),
        ("x" * 25_000, "24,000"),
        (content + "\napi_key = " + "a" * 24 + "\n", "secret assignment"),
    ):
        with pytest.raises(RevisionInvalid, match=reason):
            await revise(f, invalid)
    with pytest.raises(RevisionRefused, match="nothing changed"):
        await revise(f, head(f, f.run.artifact_path).decode())
    assert f.storage.repo.writes == writes and await rows(f) == []


async def test_revisions_replay_by_request_and_need_the_current_token(publication_db):
    f = await waiting_style(publication_db)
    token = (await f.reviews.view(f.run.id, ACTOR))["review_token"]
    request, content = uuid4(), edited_guide(f)
    first = await revise(f, content, token=token, request_id=request)
    writes = f.storage.repo.writes
    again = await revise(f, content, token=token, request_id=request)
    assert again["replayed"] and again["revision_number"] == first["revision_number"] == 1
    assert f.storage.repo.writes == writes and len(await rows(f)) == 1
    with pytest.raises(RevisionRefused, match="already used"):
        await revise(f, edited_guide(f, "Another line."), token=token, request_id=request)
    with pytest.raises(ReviewConflict, match="changed since you read it"):
        await revise(f, edited_guide(f, "Another line."), token=token)
    second = await revise(f, edited_guide(f, "Another line."), client=None)
    assert second["revision_number"] == 2
    view = await f.reviews.view(f.run.id, ACTOR)
    assert view["proposal_revisions"]["count"] == 2
    assert view["proposal_revisions"]["latest_by"] == "your coding agent"
    assert [item["number"] for item in view["proposal_revisions"]["history"]] == [1, 2]


async def test_a_lost_revision_response_is_recorded_without_a_second_write(publication_db):
    f = await waiting_style(publication_db)
    token = (await f.reviews.view(f.run.id, ACTOR))["review_token"]
    content = edited_guide(f)
    original = f.db.add_activity

    async def fail(**kwargs):
        raise RuntimeError("Database unavailable after the commit")

    f.db.add_activity = fail
    request = uuid4()
    with pytest.raises(RuntimeError, match="Database unavailable"):
        await revise(f, content, token=token, request_id=request)
    assert await rows(f) == [] and f.storage.repo.writes == 2  # the proposal and the revision
    f.db.add_activity = original
    result = await revise(f, content, token=token, request_id=request)
    assert result["revision_number"] == 1 and f.storage.repo.writes == 2
    assert (await f.db.get_run(f.run.id)).canonical_commit_sha == f.storage.repo.head


async def test_discard_after_a_revision_leaves_the_current_guide(publication_db):
    f = await waiting_style(publication_db, existing=b"My current guide\n")
    await revise(f, edited_guide(f))
    await decline_proposal(database=f.db, run_id=f.run.id, actor=ACTOR)
    run = await f.db.get_run(f.run.id)
    assert run.review_decision == "declined" and run.status == RunStatus.STOPPED
    assert head(f, STYLE_PATH) == b"My current guide\n"
    assert EDIT.encode() in head(f, run.artifact_path)  # The proposal stays readable in Files.


async def test_a_revision_in_progress_never_deadlocks_with_discard(publication_db):
    # Discard locks the run, then records its decision, whose reference to the project needs
    # FOR KEY SHARE on the project row. A revision holding that row FOR UPDATE, then waiting
    # for the run, deadlocked with it; the revision's lock must let the reference through.
    f = await waiting_style(publication_db, existing=b"My current guide\n")
    async with f.db.pool.acquire() as revision, revision.transaction():
        await revision.execute(REVISION_PROJECT_LOCK, f.run.project_id)
        run = await asyncio.wait_for(
            decline_proposal(database=f.db, run_id=f.run.id, actor=ACTOR), timeout=10
        )
        assert run.review_decision == "declined"
        # Approval still waits for the revision, so neither lands between the other's reads.
        async with f.db.pool.acquire() as approval, approval.transaction():
            await approval.execute("SET LOCAL lock_timeout = '1s'")
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await approval.execute(
                    "SELECT id FROM projects WHERE id=$1 FOR UPDATE", f.run.project_id
                )
    assert head(f, STYLE_PATH) == b"My current guide\n"


async def test_guides_pinned_to_1_1_keep_their_rules(publication_db):
    f = await waiting_style(publication_db, revisable=False)
    assert await contract(f.db, f.storage, f.run) is None
    assert not await binds_style_approval(f.db, f.storage, f.run)
    view = await f.reviews.view(f.run.id, ACTOR)
    assert "proposal_revisions" not in view and view["can_approve"]
    with pytest.raises(RevisionRefused, match="1.2.0"):
        await revise(f, edited_guide(f), token=view["review_token"])
    with pytest.raises(ReviewConflict, match="does not use a review token"):
        await f.reviews.approve(run_id=f.run.id, actor=ACTOR, token=view["review_token"])
    # Edits in Files still reach the guide on approval, as they did.
    assert await pending_owner(f.db, f.storage, f.project.id, {f.run.artifact_path}) is None
    f.storage.repo.edit({f.run.artifact_path: b"# Writing style\n\nMy corrected guide.\n"})
    await f.activities.record_approval(str(f.run.id))
    await f.activities.publish(str(f.run.id))
    assert head(f, STYLE_PATH) == b"# Writing style\n\nMy corrected guide.\n"
    assert await rows(f) == []


async def test_an_unbound_approval_of_a_1_2_guide_fails_without_saving(publication_db):
    f = await waiting_style(publication_db, existing=b"My current guide\n")
    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError, match="not bound to a reviewed version"):
        await f.activities.record_approval(str(f.run.id))
    assert head(f, STYLE_PATH) == b"My current guide\n"


async def test_mcp_and_http_revise_the_waiting_guide(publication_db, monkeypatch):
    f = await waiting_style(publication_db)
    server = mcp(f, monkeypatch)
    read = structured(await server.call_tool("read_run_output", {"run_id": str(f.run.id)}))
    assert read["revise"]["tool"] == "revise_capture_proposal" and read["revise"]["keeps_review"]
    assert read["revise"]["arguments"]["files"][0]["path"] == f.run.artifact_path
    view = structured(await server.call_tool("get_workflow_review", {"run_id": str(f.run.id)}))
    assert view["proposal_revisions"] == {"count": 0}
    assert view["documents"][0]["path"] == f.run.artifact_path
    args = {
        "run_id": str(f.run.id),
        "review_token": view["review_token"],
        "request_id": str(uuid4()),
        "files": [{"path": f.run.artifact_path, "content": edited_guide(f)}],
        "client": "codex",
    }
    result = structured(await server.call_tool("revise_capture_proposal", args))
    assert result["revision_number"] == 1 and result["proposal_revisions"]["latest_by"] == "Codex"
    assert "still waits in Decisions" in result["relay"][0]
    current = structured(await server.call_tool("get_workflow_review", {"run_id": str(f.run.id)}))
    assert result["review_token"] == current["review_token"] != view["review_token"]
    with pytest.raises(ToolError, match="not one of this run's proposal files"):
        await server.call_tool(
            "revise_capture_proposal",
            {**args, "request_id": str(uuid4()), "files": [{"path": STYLE_PATH, "content": "x"}]},
        )
    with pytest.raises(ToolError, match=STALE):
        await server.call_tool(
            "approve_workflow_run",
            {"run_id": str(f.run.id), "review_token": view["review_token"]},
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        path = f"/api/workflows/runs/{f.run.id}/proposal-revisions"
        body = {
            "review_token": current["review_token"],
            "request_id": str(uuid4()),
            "files": [{"path": f.run.artifact_path, "content": edited_guide(f, "Second.")}],
        }
        invalid = {**body, "files": [{"path": f.run.artifact_path, "content": "# Nope\n"}]}
        assert (await client.post(path, json=invalid)).status_code == 422
        response = await client.post(path, json=body)
        assert response.status_code == 201, response.text
        assert response.json()["proposal_revisions"]["latest_by"] == "you, through the Tin API"
        assert (
            await client.post(path, json={**body, "request_id": str(uuid4())})
        ).status_code == 409
        review = (await client.get(f"/api/workflows/runs/{f.run.id}/review")).json()
        decision = (await f.db.list_pending_decisions(project_id=f.project.id))[0]["id"]
        apply = f"/api/decisions/{decision}/apply"
        stale = await client.post(
            apply, json={"action": "approve", "review_token": current["review_token"]}
        )
        assert stale.status_code == 409 and STALE in stale.json()["detail"]
        approved = await client.post(
            apply, json={"action": "approve", "review_token": review["review_token"]}
        )
        assert approved.status_code == 202, approved.text
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, "user_outsider")), base_url="https://tin.test"
    ) as client:
        outsider = await client.post(path, json={**body, "request_id": str(uuid4())})
        assert outsider.status_code == 404
    assert len(await rows(f)) == 2


def test_handoff_sends_the_agent_to_the_revision_tool_only_for_a_revisable_proposal():
    run = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        workflow_id=uuid4(),
        project_workflow_id=None,
        executor="style.capture",
        status=RunStatus.NEEDS_INPUT,
        review_required=True,
        review_decision=None,
        artifact_path="style/proposals/2026-10-01-writing-style-1a2b3c4d.md",
        canonical_commit_sha="a" * 40,
    )
    settings = SimpleNamespace(switchboard_public_url="https://tin.test")
    route = document_handoff(settings, run, SimpleNamespace(paths=(run.artifact_path,)))["revise"]
    assert route["tool"] == "revise_capture_proposal" and not route["metered"]
    assert route["arguments"]["files"] == [
        {"path": run.artifact_path, "content": "<the complete revised text>"}
    ]
    assert document_handoff(settings, run)["revise"]["tool"] is None  # 1.1.0: no route.


# The brand pair: two proposal files, applied together on approval.


async def waiting_brand(db, *, revisable=True):
    f = await brand_setup(db)
    if not revisable:  # A run pinned to 1.1.0, before agent revisions.
        await db.pool.execute(
            "UPDATE workflows SET definition=definition #- '{procedure,output,agent_revision}' "
            "WHERE id=$1",
            f.workflow.id,
        )
    run = await brand_start(f, product_url="https://example.com/")
    prepared = await f.sources.prepare(run, f.procedure)
    base = prepared["project_revision"]
    await db.pool.execute(
        "UPDATE workflow_runs SET expected_head_sha=$2, status='running' WHERE id=$1", run.id, base
    )
    run = replace(run, expected_head_sha=base)
    procedure = f.procedure.resolve_inputs(run.input, run_id=run.id, started_at=run.created_at)
    revision = f.storage.repo.edit(
        {procedure.output_path: brand_doc(), procedure.companion_path: DESIGN}
    )
    f.storage.repo.head = base
    f.activities = TinActivities(
        database=db,
        storage=f.storage,
        settings=f.settings,
        sandboxes=SimpleNamespace(),
        integrations=f.runtime.integrations,
    )
    companions = await f.activities._procedure_companions(run, f.project, procedure, revision)
    checkpoint = OutputCheckpoint.create(
        run=run,
        revision=revision,
        path=procedure.output_path,
        media_type="text/markdown",
        content=brand_doc(),
        companions=companions,
    )
    key = f"{run.id}:procedure_canonical_commit"
    saved, _ = await f.storage.publish_procedure_output(
        repo_id=f.project.state_repo_id,
        branch="main",
        checkpoint=checkpoint,
        content=brand_doc(),
        execution_key=key,
        workflow_key=brand.KEY,
        intent=None,
        legacy_attempt=False,
        save_intent=AsyncMock(),
        validate_lease=AsyncMock(),
    )
    async with db.effect_lock(key, "procedure_canonical_commit") as (conn, _):
        await db.start_effect(conn, execution_key=key, operation="procedure_canonical_commit")
        await db.complete_effect(
            conn,
            execution_key=key,
            result={
                "checkpoint": checkpoint.to_dict(),
                "artifact_path": procedure.output_path,
                "canonical_commit_sha": saved,
            },
        )
    await db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=saved,
        artifact_path=procedure.output_path,
        artifact_ref=f"code.storage://{f.project.state_repo_id}@{saved}/{procedure.output_path}",
    )
    f.run = await db.get_run(run.id)
    f.paths = (procedure.output_path, procedure.companion_path)
    f.reviews = ReviewedDocuments(database=db, storage=f.storage)
    f.revisions = CaptureRevisions(database=db, storage=f.storage)
    return f


async def revise_brand(f, files, *, token=None):
    if token is None:
        token = (await f.reviews.view(f.run.id, ACTOR))["review_token"]
    return await f.revisions.revise(
        run_id=f.run.id,
        actor=ACTOR,
        review_token=token,
        request_id=uuid4(),
        files=[{"path": path, "content": raw.decode()} for path, raw in files.items()],
        source="mcp",
        client="claude_code",
    )


REVISED_DESIGN = DESIGN.replace(
    b"authenticated product states unavailable.",
    b"authenticated states unavailable; the founder confirmed the pricing table.",
    1,
)


async def test_agent_revises_the_brand_pair_and_approval_applies_that_version(publication_db):
    f = await waiting_brand(publication_db)
    opened = await f.reviews.view(f.run.id, ACTOR)
    assert opened["proposal_revisions"] == {"count": 0}
    assert "proposal_revision" not in opened["artifact"]
    assert [item["path"] for item in opened["documents"]] == list(f.paths)
    result = await revise_brand(f, {f.paths[1]: REVISED_DESIGN})
    assert result["proposal"] == "brand and design guide"
    assert [item["path"] for item in result["files"]] == list(f.paths)
    assert head(f, f.paths[1]) == REVISED_DESIGN and head(f, f.paths[0]) == brand_doc()
    assert head(f, brand.DESIGN_PATH) is None and head(f, brand.BRAND_PATH) is None

    view = await f.reviews.view(f.run.id, ACTOR)
    assert view["can_approve"] and view["artifact"]["proposal_revision"] == 1
    assert view["artifact"]["revision"] == result["project_revision"]
    assert view["documents"][1]["sha256"] == sha(REVISED_DESIGN)
    assert view["palette_preview"] == TOKENS["light"]
    assert view["proposal_revisions"]["count"] == 1
    with pytest.raises(ReviewConflict, match=STALE):
        await f.reviews.approve(run_id=f.run.id, actor=ACTOR, token=opened["review_token"])
    await f.reviews.approve(run_id=f.run.id, actor=ACTOR, token=view["review_token"])
    with pytest.raises(RevisionRefused, match="no longer waiting"):
        await revise_brand(f, {f.paths[1]: DESIGN}, token=view["review_token"])
    await f.activities.record_codex_procedure_approval(str(f.run.id))
    assert head(f, brand.DESIGN_PATH) == REVISED_DESIGN
    assert head(f, brand.BRAND_PATH) == brand_doc()
    await f.activities.project_codex_procedure_result(str(f.run.id))
    done = await f.db.get_run(f.run.id)
    assert done.status == RunStatus.SUCCEEDED
    assert done.canonical_commit_sha == result["project_revision"]


async def test_a_brand_revision_must_pass_the_capture_validators(publication_db):
    f = await waiting_brand(publication_db)
    writes = f.storage.repo.writes
    bad_tokens = {**TOKENS, "light": {**TOKENS["light"], "accent": "#fff"}}
    for files, reason in (
        ({f.paths[0]: brand_doc(tokens=bad_tokens)}, "invalid"),
        ({f.paths[1]: b"# Design\n\nNo sections.\n"}, "invalid"),
        ({f.paths[0]: b"x" * 48_001}, "limit"),
    ):
        with pytest.raises(RevisionInvalid, match=reason):
            await revise_brand(f, files)
    with pytest.raises(RevisionRefused, match="not one of this run's proposal files"):
        await revise_brand(f, {brand.BRAND_PATH: brand_doc()})
    assert f.storage.repo.writes == writes and await rows(f) == []


async def test_an_edit_outside_the_revision_route_blocks_approval_until_revised(publication_db):
    f = await waiting_brand(publication_db)
    f.storage.repo.edit({f.paths[1]: b"Edited behind Decisions\n"})
    view = await f.reviews.view(f.run.id, ACTOR)
    assert not view["can_approve"] and "revise_capture_proposal" in view["conflict"]
    with pytest.raises(ReviewConflict, match="revise_capture_proposal"):
        await f.reviews.approve(run_id=f.run.id, actor=ACTOR, token=view["review_token"])
    await revise_brand(f, {f.paths[1]: REVISED_DESIGN}, token=view["review_token"])
    assert (await f.reviews.view(f.run.id, ACTOR))["can_approve"]


async def test_discarding_a_revised_brand_pair_leaves_the_active_files(publication_db):
    f = await waiting_brand(publication_db)
    await revise_brand(f, {f.paths[1]: REVISED_DESIGN})
    await decline_proposal(database=f.db, run_id=f.run.id, actor=ACTOR)
    assert head(f, brand.DESIGN_PATH) is None and head(f, brand.BRAND_PATH) is None
    assert head(f, f.paths[1]) == REVISED_DESIGN


async def test_brand_pairs_pinned_to_1_1_keep_their_rules(publication_db):
    f = await waiting_brand(publication_db, revisable=False)
    assert await contract(f.db, f.storage, f.run) is None
    view = await f.reviews.view(f.run.id, ACTOR)
    assert "proposal_revisions" not in view and "proposal_revision" not in view["artifact"]
    with pytest.raises(RevisionRefused, match="1.2.0"):
        await revise_brand(f, {f.paths[1]: REVISED_DESIGN}, token=view["review_token"])
    f.storage.repo.edit({f.paths[1]: REVISED_DESIGN})
    with pytest.raises(ReviewConflict, match="Use Files or start a new capture"):
        await f.reviews.approve(run_id=f.run.id, actor=ACTOR, token=view["review_token"])
