from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from test_procedure_publication import CONTENT, PATH, activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.domain import RunStatus, SideEffectConflictError
from tin_lite.mcp_server import create_mcp_app
from tin_lite.output_resolution import (
    MAX_COMPARISON_BYTES,
    MAX_STARTING_BYTES,
    OutputResolutionError,
    OutputResolutionRequest,
    OutputResolutionService,
)
from tin_lite.publication import read_run_output


async def conflict_fixture(db, *, current=b"Founder version\n"):
    _, storage, run, checkpoint = await activity_fixture(db, review=True)
    storage.repo.edit({PATH: current})
    await db.pool.execute(
        """UPDATE workflow_runs SET status='failed', lease_active=false,
           retained_output=$2::jsonb, error_message='Output changed during generation'
           WHERE id=$1""",
        run.id,
        json.dumps({**checkpoint.to_dict(), "reason": "output_conflict"}),
    )
    service = OutputResolutionService(database=db, storage=storage)
    return service, storage, run, checkpoint


def request_for(storage, checkpoint, *, action="use_saved"):
    return OutputResolutionRequest(
        request_id=uuid4(),
        action=action,
        expected_revision=storage.repo.head,
        saved_revision=checkpoint.ephemeral_commit_sha,
    )


async def resolve(service, run, request, **kwargs):
    return await service.resolve(
        run_id=run.id,
        request=request,
        actor_clerk_user_id=kwargs.get("actor", "user_member"),
        client_id=kwargs.get("client", "test_client"),
    )


async def resolution_events(db, run):
    return await db.pool.fetch(
        "SELECT * FROM activity_events WHERE run_id=$1 AND event_type LIKE 'procedure_output_%'",
        run.id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [b"Founder version\n", b"", None])
async def test_comparison_pins_current_to_saved_not_original_base(publication_db, current):
    service, storage, run, checkpoint = await conflict_fixture(publication_db, current=current)
    result = await service.compare(run_id=run.id)
    assert result["complete"] and result["allowed_actions"] == ["keep_current", "use_saved"]
    assert result["current"]["revision"] == storage.repo.head != run.expected_head_sha
    assert result["current"]["content"] == (current.decode() if current is not None else None)
    assert result["current"]["presence"] == ("file" if current is not None else "missing")
    assert result["saved"]["revision"] == checkpoint.ephemeral_commit_sha
    assert result["saved"]["content"] == CONTENT.decode()
    assert storage.repo.writes == 0 and not await resolution_events(publication_db, run)


@pytest.mark.asyncio
async def test_starting_context_is_optional_bounded_and_does_not_change_eligibility(
    publication_db, monkeypatch
):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    base = checkpoint.source_base_sha
    storage.repo.trees[base][PATH] = ("100644", b"Original\n")
    result = await service.compare(run_id=run.id)
    assert result["starting"]["content"] == "Original\n"
    assert result["starting"]["revision"] == base
    for content in (b"x" * (MAX_STARTING_BYTES + 1), b"\xff"):
        storage.repo.trees[base][PATH] = ("100644", content)
        result = await service.compare(run_id=run.id)
        assert not result["starting"]["available"]
        assert result["starting"]["content"] is None
        assert result["complete"] and "use_saved" in result["allowed_actions"]
    del storage.repo.trees[base][PATH]
    assert (await service.compare(run_id=run.id))["starting"]["presence"] == "missing"
    original = storage.read_output_destination

    async def unavailable(**kwargs):
        if kwargs["revision"] == base:
            raise ConnectionError("private storage URL")
        return await original(**kwargs)

    monkeypatch.setattr(storage, "read_output_destination", unavailable)
    result = await service.compare(run_id=run.id)
    assert result["starting"]["blocked_reason"] == "starting_unavailable"
    assert "private" not in json.dumps(result)


@pytest.mark.asyncio
async def test_postgres_only_recovery_requires_original_actor_and_client(
    publication_db, monkeypatch
):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    request = request_for(storage, checkpoint)
    storage.repo.lose_response = True
    with pytest.raises(OutputResolutionError):
        await resolve(service, run, request)

    async def offline(*args, **kwargs):
        raise AssertionError("A status read must not read storage or apply anything")

    monkeypatch.setattr(storage, "get_repo", offline)
    result = await service.status(
        run_id=run.id, actor_clerk_user_id="user_member", client_id="test_client"
    )
    assert result["retry_request"] == request.model_dump(mode="json")
    for actor, client in [("someone_else", "test_client"), ("user_member", None)]:
        other = await service.status(run_id=run.id, actor_clerk_user_id=actor, client_id=client)
        assert other["resolution"]["state"] == "applying"
        assert other["retry_request"] is None
    assert storage.repo.writes == 1 and not await resolution_events(publication_db, run)


@pytest.mark.asyncio
async def test_conflict_decision_queue_count_and_lifecycle_are_postgres_only(publication_db):
    db = publication_db
    service, storage, run, checkpoint = await conflict_fixture(db)
    decisions = await db.list_pending_decisions(project_id=run.project_id)
    assert len(decisions) == 1 and decisions[0]["kind"] == "output_conflict"
    assert decisions[0]["items"][0]["revision"] == checkpoint.ephemeral_commit_sha
    assert (await db.get_project_system_summary(project_id=run.project_id))["waiting_count"] == 1
    assert not await db.list_pending_decisions(project_id=uuid4())
    assert await db.get_pending_decision(decision_id=run.id) is None  # Cannot approve a conflict.
    request = request_for(storage, checkpoint)
    storage.repo.lose_response = True
    with pytest.raises(OutputResolutionError):
        await resolve(service, run, request)
    assert (await db.list_pending_decisions(project_id=run.project_id))[0]["output_resolution"][
        "state"
    ] == "applying"
    await resolve(service, run, request)
    assert not await db.list_pending_decisions(project_id=run.project_id)
    assert (await db.get_project_system_summary(project_id=run.project_id))["waiting_count"] == 0
    assert (await db.get_run(run.id)).status == RunStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["publication_pending", "reconciliation_pending"])
async def test_unsettled_publication_never_enters_decisions(publication_db, reason):
    _, _, run, checkpoint = await conflict_fixture(publication_db)
    await publication_db.pool.execute(
        "UPDATE workflow_runs SET retained_output=$2::jsonb WHERE id=$1",
        run.id,
        json.dumps({**checkpoint.to_dict(), "reason": reason}),
    )
    assert not await publication_db.list_pending_decisions(project_id=run.project_id)


@pytest.mark.asyncio
async def test_apply_duplicates_once_preserve_run_and_replay_without_storage(
    publication_db, monkeypatch
):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    db = publication_db
    original = await db.get_run(run.id)
    storage.repo.edit({"unrelated.md": b"Keep this"})
    request = request_for(storage, checkpoint)
    # More contenders than the connection pool: a lock holder must not borrow a second connection.
    results = await asyncio.wait_for(
        asyncio.gather(*(resolve(service, run, request) for _ in range(15))),
        timeout=10,
    )
    assert storage.repo.writes == 1
    assert sum(not result["replayed"] for result in results) == 1
    sha = results[0]["revision"]
    assert all(result["revision"] == sha and result["state"] == "applied" for result in results)
    assert storage.repo.trees[sha][PATH][1] == CONTENT
    assert storage.repo.trees[sha]["unrelated.md"][1] == b"Keep this"
    assert storage.repo.commits[sha]["parent_shas"] == [request.expected_revision]
    projected = await db.get_run(run.id)
    assert replace(projected, output_resolution=None) == original
    assert projected.status == RunStatus.FAILED and projected.review_decision is None
    assert (
        projected.canonical_commit_sha is None and projected.output_resolution["state"] == "applied"
    )
    events = await resolution_events(db, run)
    assert len(events) == 1
    details = json.loads(events[0]["details"])
    assert details["actor_clerk_user_id"] == "user_member" and details["client_id"] == "test_client"
    assert details["kind"] == "your_edits" and details["basis"] == "member_request"
    assert (await service.compare(run_id=run.id))["allowed_actions"] == []

    async def offline(*args, **kwargs):
        raise ConnectionError("Storage is unavailable")

    monkeypatch.setattr(storage, "get_repo", offline)
    assert (await resolve(service, run, request))["replayed"]
    with pytest.raises(OutputResolutionError, match="already exists"):
        await resolve(service, run, request.model_copy(update={"request_id": uuid4()}))


@pytest.mark.asyncio
async def test_keep_changes_no_file_and_keeps_generated_result_readable(publication_db):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    request = request_for(storage, checkpoint, action="keep_current")
    result = await resolve(service, run, request)
    assert result["state"] == "kept" and not result["changed"]
    assert storage.repo.head == request.expected_revision and storage.repo.writes == 0
    assert (await resolve(service, run, request))["replayed"]
    projected = await publication_db.get_run(run.id)
    assert (
        await read_run_output(
            storage=storage,
            run=projected,
            repo_id=storage.repo.id,
            source="retained",
        )
    ).content == CONTENT
    assert len(await resolution_events(publication_db, run)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("action", "keep_current"), ("actor", "other"), ("client", "other")]
)
async def test_request_identity_includes_action_actor_and_client(publication_db, field, value):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    request = request_for(storage, checkpoint)
    await resolve(service, run, request)
    with pytest.raises(SideEffectConflictError):
        await resolve(
            service,
            run,
            request.model_copy(update={field: value}) if field == "action" else request,
            **({field: value} if field != "action" else {}),
        )
    assert storage.repo.writes == 1


@pytest.mark.asyncio
async def test_stale_comparison_is_final_for_request_but_allows_new_decision(publication_db):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    request = request_for(storage, checkpoint)
    storage.repo.edit({"unrelated.md": b"Even unrelated edits require a refreshed comparison"})
    for _ in range(2):
        with pytest.raises(OutputResolutionError) as failure:
            await resolve(service, run, request)
        assert failure.value.code == "stale_comparison"
    assert (await publication_db.get_run(run.id)).output_resolution is None
    with pytest.raises(SideEffectConflictError):
        await resolve(
            service, run, request.model_copy(update={"expected_revision": storage.repo.head})
        )
    assert not await resolution_events(publication_db, run) and storage.repo.writes == 0
    result = await resolve(service, run, request_for(storage, checkpoint))
    assert result["changed"] and storage.repo.writes == 1


@pytest.mark.asyncio
async def test_cas_race_reconciles_absence_before_allowing_new_request(publication_db):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    request = request_for(storage, checkpoint)
    storage.repo.before_send = lambda: storage.repo.edit({PATH: b"Latest founder edit"})
    with pytest.raises(OutputResolutionError) as failure:
        await resolve(service, run, request)
    assert failure.value.code == "resolution_pending" and storage.repo.writes == 0
    with pytest.raises(OutputResolutionError) as failure:
        await resolve(service, run, request)
    assert failure.value.code == "stale_comparison"
    assert (await publication_db.get_run(run.id)).output_resolution is None
    await resolve(service, run, request_for(storage, checkpoint, action="keep_current"))
    assert storage.repo.trees[storage.repo.head][PATH][1] == b"Latest founder edit"


@pytest.mark.asyncio
@pytest.mark.parametrize("later_path", [PATH, "later.md"])
async def test_lost_response_recovers_original_commit_without_overwriting_later_edits(
    publication_db, later_path
):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    request = request_for(storage, checkpoint)
    storage.repo.lose_response = True
    with pytest.raises(OutputResolutionError, match="unconfirmed"):
        await resolve(service, run, request)
    applied_sha = storage.repo.head
    storage.repo.edit({later_path: b"Later work must survive"})
    head = storage.repo.head
    with pytest.raises(OutputResolutionError, match="already exists"):
        await resolve(service, run, request_for(storage, checkpoint, action="keep_current"))
    result = await resolve(service, run, request)
    assert result["revision"] == applied_sha and storage.repo.head == head
    assert storage.repo.writes == 1
    assert storage.repo.trees[head][later_path][1] == b"Later work must survive"
    assert len(await resolution_events(publication_db, run)) == 1


@pytest.mark.asyncio
async def test_incomplete_reconciliation_never_repeats_a_write(publication_db):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    request = request_for(storage, checkpoint)
    storage.repo.lose_response = True
    with pytest.raises(OutputResolutionError):
        await resolve(service, run, request)
    applied_sha = storage.repo.head
    for _ in range(5):
        storage.repo.edit({"later.md": uuid4().hex.encode()})
    storage.incomplete_history = True
    with pytest.raises(OutputResolutionError, match="unconfirmed"):
        await resolve(service, run, request)
    assert storage.repo.writes == 1 and not await resolution_events(publication_db, run)
    storage.incomplete_history = False
    assert (await resolve(service, run, request))["revision"] == applied_sha


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_method", ["add_activity", "complete_effect", "set_output_resolution"]
)
async def test_projection_failure_rolls_back_then_recovers_without_new_commit(
    publication_db, monkeypatch, failure_method
):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    db = publication_db
    request = request_for(storage, checkpoint)
    original = getattr(db, failure_method)

    async def fail(*args, **kwargs):
        # Let the pending intent projection be stored; fail only after the remote write.
        if storage.repo.writes:
            raise ConnectionError("Projection interrupted")
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, failure_method, fail)
    with pytest.raises(OutputResolutionError, match="unconfirmed"):
        await resolve(service, run, request)
    assert not await resolution_events(db, run)
    assert (await db.get_run(run.id)).output_resolution["state"] == "applying"
    receipt = await db.get_effect(f"{run.id}:output_resolution:{request.request_id}")
    assert (
        receipt.status == "failed"
        and receipt.result["publication"]["checkpoint"] == checkpoint.to_dict()
    )
    monkeypatch.setattr(db, failure_method, original)
    await resolve(service, run, request)
    assert storage.repo.writes == 1 and len(await resolution_events(db, run)) == 1


@pytest.mark.asyncio
async def test_already_identical_records_no_change_not_a_pretend_commit(publication_db):
    service, storage, run, checkpoint = await conflict_fixture(publication_db, current=CONTENT)
    assert (await service.compare(run_id=run.id))["identical"]
    result = await resolve(service, run, request_for(storage, checkpoint))
    assert result["state"] == "applied" and not result["changed"] and storage.repo.writes == 0
    assert result["revision"] == storage.repo.head


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current,mode", [(("100755", b"Founder text"), "100755"), (None, "100644")]
)
async def test_apply_preserves_destination_mode_or_creates_regular_file(
    publication_db, current, mode
):
    service, storage, run, checkpoint = await conflict_fixture(publication_db, current=current)
    result = await resolve(service, run, request_for(storage, checkpoint))
    assert storage.repo.trees[result["revision"]][PATH] == (mode, CONTENT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current,reason",
    [
        (b"x" * MAX_COMPARISON_BYTES, "comparison_limit"),
        (b"\n" * 10_000, "comparison_limit"),
        (b"\xff", "non_text_destination"),
        (("120000", b"elsewhere"), "unsafe_destination"),
    ],
)
async def test_incomplete_comparison_hides_bodies_and_disallows_apply(
    publication_db, current, reason
):
    service, storage, run, checkpoint = await conflict_fixture(publication_db, current=current)
    comparison = await service.compare(run_id=run.id)
    assert not comparison["complete"] and comparison["blocked_reason"] == reason
    assert comparison["saved"]["content"] is None and comparison["current"]["content"] is None
    assert comparison["allowed_actions"] == ["keep_current"]
    with pytest.raises(OutputResolutionError) as failure:
        await resolve(service, run, request_for(storage, checkpoint))
    assert failure.value.code == "comparison_blocked"
    await resolve(service, run, request_for(storage, checkpoint, action="keep_current"))
    assert storage.repo.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"executor": "project.task"},
        {"status": "running"},
        {"lease_active": True},
        {"canonical_commit_sha": "c" * 40},
        {"reason": "reconciliation_pending"},
        {"reason": "publication_pending"},
        {"sha256": "0" * 64},
        {"artifact_path": ".env"},
    ],
)
async def test_only_settled_verified_procedure_conflicts_are_resolvable(publication_db, change):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    for field, value in change.items():
        if field in {"reason", "sha256", "artifact_path"}:
            await publication_db.pool.execute(
                "UPDATE workflow_runs SET retained_output=retained_output || $2::jsonb WHERE id=$1",
                run.id,
                json.dumps({field: value}),
            )
        else:
            # Fixed, test-owned column names only.
            await publication_db.pool.execute(
                f"UPDATE workflow_runs SET {field}=$2 WHERE id=$1",  # noqa: S608
                run.id,
                value,
            )
    with pytest.raises(OutputResolutionError):
        await resolve(service, run, request_for(storage, checkpoint))
    assert storage.repo.writes == 0 and not storage.reads


async def add_member(db, run):
    await db.record_tin_user("user_member")
    await db.pool.execute(
        "INSERT INTO project_memberships (project_id, clerk_user_id) VALUES ($1, 'user_member')",
        run.project_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["keep_current", "use_saved"])
async def test_http_contract_auth_staleness_and_no_implicit_approval(publication_db, action):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    await add_member(publication_db, run)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(
        database=publication_db, storage=storage, output_resolution=service
    )
    user = "user_member"
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=user,
        token_type="session_token",  # noqa: S106
    )
    root = f"/api/workflows/runs/{run.id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        comparison = await client.get(root + "/output-comparison")
        assert comparison.status_code == 200 and comparison.headers["Cache-Control"] == "no-store"
        recovery = await client.get(root + "/output-resolution")
        assert recovery.status_code == 200
        assert recovery.headers["X-Tin-Read-Source"] == "postgres"
        assert recovery.json()["retry_request"] is None
        request = request_for(storage, checkpoint, action=action).model_dump(mode="json")
        assert (
            await client.post(
                root + "/output-resolution", json={**request, "actor_clerk_user_id": "spoof"}
            )
        ).status_code == 422
        user = "user_outsider"
        storage.reads.clear()
        assert (await client.get(root + "/output-comparison")).status_code == 404
        assert (await client.get(root + "/output-resolution")).status_code == 404
        assert (await client.post(root + "/output-resolution", json=request)).status_code == 404
        assert not storage.reads and storage.repo.writes == 0
        user = "user_member"
        result = await client.post(root + "/output-resolution", json=request)
        assert result.status_code == 200 and result.json()["action"] == action
        assert (await client.post(root + "/output-resolution", json=request)).json()["replayed"]
        projected = (await client.get(root)).json()
        assert projected["status"] == "failed" and projected["review_decision"] is None
        assert projected["output_resolution"]["request_id"] == request["request_id"]
        assert projected["retained_output"]["revision"] == checkpoint.ephemeral_commit_sha
        assert (await client.get(root + "/output-resolution")).json()["resolution"]["state"] in {
            "kept",
            "applied",
        }


@pytest.mark.asyncio
async def test_mcp_uses_same_service_with_authenticated_provenance(publication_db, monkeypatch):
    service, storage, run, checkpoint = await conflict_fixture(publication_db)
    await add_member(publication_db, run)
    token = SimpleNamespace(subject="user_member", scopes=["openid"], client_id="test_client")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    server, _ = create_mcp_app(
        settings=SimpleNamespace(
            switchboard_public_url="https://tin.test",
            clerk_frontend_api_url="https://clerk.tin.test",
        ),
        auth=SimpleNamespace(),
        runtime=lambda: SimpleNamespace(
            database=publication_db, storage=storage, output_resolution=service
        ),
    )
    args = {"run_id": str(run.id)}
    assert "Founder version" in str(await server.call_tool("compare_run_output", args))
    assert "retry_request" in str(await server.call_tool("get_run_output_resolution", args))
    request = request_for(storage, checkpoint)
    token.subject = "user_outsider"
    storage.reads.clear()
    for tool, params in [
        ("compare_run_output", args),
        ("get_run_output_resolution", args),
        ("resolve_run_output", {**args, **request.model_dump(mode="json")}),
    ]:
        with pytest.raises(ToolError, match="not_found: run not found"):
            await server.call_tool(tool, params)
    assert not storage.reads and storage.repo.writes == 0
    token.subject = "user_member"
    assert "applied" in str(
        await server.call_tool("resolve_run_output", {**args, **request.model_dump(mode="json")})
    )
    assert (await resolve(service, run, request))["replayed"]
    listing = str(await server.call_tool("list_project_runs", {"project_id": str(run.project_id)}))
    assert "output_resolution" in listing and "retained_output" in listing
    assert storage.repo.writes == 1
