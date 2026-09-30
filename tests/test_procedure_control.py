"""Real SQL stop fences and the shared HTTP/MCP procedure control contract."""
# ruff: noqa: S106 -- synthetic grant values, never provider credentials

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.domain import RunStatus, SideEffectConflictError, StaleGenerationError
from tin_lite.mcp_server import _run_allowed_actions, create_mcp_app
from tin_lite.procedure_control import stop_procedure


async def fixture(db, monkeypatch, *, created=False):
    activities, storage, run, checkpoint = await activity_fixture(
        db, isolated=True, created=created
    )
    member = "test-member"
    monkeypatch.setattr(
        db,
        "has_project_access",
        AsyncMock(side_effect=lambda **kw: kw["clerk_user_id"] == member),
    )
    handle = SimpleNamespace(cancel=AsyncMock())
    runtime = SimpleNamespace(
        database=db,
        sandboxes=SimpleNamespace(kill=AsyncMock()),
        temporal=SimpleNamespace(get_workflow_handle=Mock(return_value=handle)),
    )
    return SimpleNamespace(
        db=db,
        activities=activities,
        storage=storage,
        run=run,
        checkpoint=checkpoint,
        runtime=runtime,
        handle=handle,
        member=member,
    )


async def stop(f):
    return await stop_procedure(runtime=f.runtime, run_id=f.run.id, actor=f.member)


async def test_stop_is_idempotent_revokes_grants_and_preserves_verified_output(
    publication_db,
    monkeypatch,
):
    f = await fixture(publication_db, monkeypatch, created=True)
    await f.db.pool.execute(
        "INSERT INTO broker_grants(token_hash,run_id,sandbox_id,expires_at) "
        "VALUES($1,$2,$3,now()+interval '1 minute')",
        "historical-synthetic-hash",
        f.run.id,
        f.run.sandbox_id,
    )
    first, second = await asyncio.gather(stop(f), stop(f))
    assert first == second
    assert first["status"] == "stopped"
    assert not first["cleanup_pending"] and not first["cancellation_pending"]
    current = await f.db.get_run(f.run.id)
    assert not current.lease_active and current.finished_at is not None
    assert current.retained_output["ephemeral_commit_sha"] == f.checkpoint.ephemeral_commit_sha
    assert (
        await f.db.pool.fetchval("SELECT count(*) FROM broker_grants WHERE run_id=$1", f.run.id)
        == 0
    )
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events WHERE run_id=$1 "
            "AND event_type='codex_procedure_stopped'",
            f.run.id,
        )
        == 1
    )
    with pytest.raises(StaleGenerationError):
        await f.activities.commit_codex_procedure_artifact(str(f.run.id))
    await f.activities.project_codex_procedure_failure({"run_id": str(f.run.id)})
    assert (await f.db.get_run(f.run.id)).status == RunStatus.STOPPED
    assert f.storage.repo.writes == 0
    f.runtime.sandboxes.kill.assert_awaited_with(f.run.sandbox_id)


@pytest.mark.parametrize("required", [False, True])
async def test_stopped_procedure_cannot_reacquire_even_with_legacy_call(
    publication_db,
    monkeypatch,
    required,
):
    f = await fixture(publication_db, monkeypatch)
    await stop(f)
    with pytest.raises(RuntimeError):
        await f.db.attach_sandbox(
            run_id=f.run.id,
            sandbox_id="replacement",
            lease_owner=f.run.lease_owner,
            expected_head_sha=f.run.expected_head_sha,
            ephemeral_branch=f.run.ephemeral_branch,
            require_active=required,
        )
    with pytest.raises(StaleGenerationError):
        await f.activities.create_codex_procedure_sandbox(str(f.run.id))
    assert not (await f.db.get_run(f.run.id)).lease_active


@pytest.mark.parametrize("phase", ["locked", "uncertain", "published", "review", "finished"])
async def test_stop_never_recalls_started_publication(publication_db, monkeypatch, phase):
    f = await fixture(publication_db, monkeypatch)
    key = f"{f.run.id}:procedure_canonical_commit"
    if phase == "locked":
        async with f.db.effect_lock(key, "procedure_canonical_commit"):
            with pytest.raises(SideEffectConflictError, match="in progress"):
                await stop(f)
    else:
        if phase == "uncertain":
            f.storage.repo.lose_response = True
            with pytest.raises(ApplicationError, match="PublicationPendingError"):
                await f.activities.commit_codex_procedure_artifact(str(f.run.id))
            assert f.storage.repo.writes == 1
        elif phase == "published":
            await f.activities.commit_codex_procedure_artifact(str(f.run.id))
        else:
            await f.db.pool.execute(
                "UPDATE workflow_runs SET status=$2 WHERE id=$1",
                f.run.id,
                "needs_input" if phase == "review" else "succeeded",
            )
        with pytest.raises(SideEffectConflictError):
            await stop(f)
        if phase == "uncertain":
            await f.activities.commit_codex_procedure_artifact(str(f.run.id))
            await f.activities.project_codex_procedure_result(str(f.run.id))
            assert f.storage.repo.writes == 1
            assert (await f.db.get_run(f.run.id)).status == RunStatus.SUCCEEDED
    f.handle.cancel.assert_not_awaited()
    f.runtime.sandboxes.kill.assert_not_awaited()


async def test_prepared_github_delivery_cannot_be_cancelled(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    await f.db.pool.execute(
        "INSERT INTO integration_call_receipts "
        "(execution_key, project_id, run_id, provider_key, capability, "
        "request_fingerprint, status) "
        "VALUES ($1,$2,$3,'infra.github','pull_request.write',$4,'started')",
        f"{f.run.id}:procedure_pull_request",
        f.run.project_id,
        f.run.id,
        "a" * 64,
    )
    with pytest.raises(SideEffectConflictError, match="reconciled"):
        await stop(f)
    assert (await f.db.get_run(f.run.id)).lease_active
    f.runtime.sandboxes.kill.assert_not_awaited()


@pytest.mark.parametrize("failure", ["temporal", "sandbox", "both", "not_found"])
async def test_cleanup_attempts_both_sides_and_is_retryable(publication_db, monkeypatch, failure):
    f = await fixture(publication_db, monkeypatch)
    if failure in {"temporal", "both"}:
        f.handle.cancel.side_effect = RuntimeError("sensitive provider error")
    elif failure == "not_found":
        f.handle.cancel.side_effect = RPCError("missing", RPCStatusCode.NOT_FOUND, b"")
    if failure in {"sandbox", "both"}:
        f.runtime.sandboxes.kill.side_effect = RuntimeError("sensitive provider error")
    result = await stop(f)
    assert result["cancellation_pending"] == (failure in {"temporal", "both"})
    assert result["cleanup_pending"] == (failure in {"sandbox", "both"})
    assert "sensitive" not in str(result)
    f.handle.cancel.assert_awaited_once()
    f.runtime.sandboxes.kill.assert_awaited_once()
    f.handle.cancel.side_effect = f.runtime.sandboxes.kill.side_effect = None
    again = await stop(f)
    assert not again["cleanup_pending"] and not again["cancellation_pending"]


async def test_active_worker_observes_stop_without_temporal_cancel_delivery(
    publication_db,
    monkeypatch,
):
    f = await fixture(publication_db, monkeypatch)
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda details: None)
    monkeypatch.setattr("tin_lite.activities.SANDBOX_HEARTBEAT_SECONDS", 0.01)
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    task = asyncio.create_task(
        f.activities._await_with_heartbeats(
            operation(),
            details={},
            procedure_run=f.run,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    # Simulate the API dying after its DB commit but before contacting Temporal/E2B.
    await f.db.stop_codex_procedure(
        run_id=f.run.id,
        project_id=f.run.project_id,
        actor=f.member,
        expected_generation=f.run.generation,
    )
    with pytest.raises(StaleGenerationError):
        await asyncio.wait_for(task, timeout=1)
    assert cleaned.is_set()


async def test_generation_and_executor_cannot_be_substituted(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    with pytest.raises(SideEffectConflictError, match="generation"):
        await f.db.stop_codex_procedure(
            run_id=f.run.id,
            project_id=f.run.project_id,
            actor=f.member,
            expected_generation=f.run.generation + 1,
        )
    with pytest.raises(LookupError):
        await f.db.stop_codex_procedure(
            run_id=f.run.id,
            project_id=uuid4(),
            actor=f.member,
        )
    await f.db.pool.execute(
        "UPDATE workflow_runs SET executor='project.task' WHERE id=$1",
        f.run.id,
    )
    with pytest.raises(LookupError):
        await stop(f)
    f.runtime.sandboxes.kill.assert_not_awaited()


async def test_http_and_mcp_authorize_and_use_the_same_control(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    token = SimpleNamespace(subject="outsider", scopes=["openid"], client_id="test-client")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    monkeypatch.setattr(f.db, "record_mcp_usage", AsyncMock())
    monkeypatch.setattr(f.db, "record_tin_user", AsyncMock())
    server, _ = create_mcp_app(
        settings=SimpleNamespace(
            switchboard_public_url="https://tin.test",
            clerk_frontend_api_url="https://clerk.test",
        ),
        auth=SimpleNamespace(),
        runtime=lambda: f.runtime,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = f.runtime
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=token.subject,
        token_type="session_token",  # noqa: S106
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://tin.test",
    ) as client:
        path = f"/api/workflows/runs/{f.run.id}/stop-procedure"
        assert (await client.post(path)).status_code == 404
        with pytest.raises(ToolError, match="not_found: run not found"):
            await server.call_tool("stop_procedure", {"run_id": str(f.run.id)})
        f.handle.cancel.assert_not_awaited()
        token.subject = f.member
        response = await client.post(path)
        assert response.status_code == 200
        result = await server.call_tool("stop_procedure", {"run_id": str(f.run.id)})
        assert response.json() == result.structured_content
        f.runtime.sandboxes.kill.assert_awaited_with(f.run.sandbox_id)


async def test_cancel_discovery_preserves_review_and_native_controls(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    assert _run_allowed_actions(f.run) == ["cancel"]
    assert _run_allowed_actions(replace(f.run, canonical_commit_sha="a" * 40)) == []
    assert _run_allowed_actions(replace(f.run, status=RunStatus.STOPPED)) == []
    assert _run_allowed_actions(
        replace(
            f.run,
            status=RunStatus.NEEDS_INPUT,
            review_required=True,
        )
    ) == ["approve"]


async def test_stop_during_creation_kills_the_unattached_sandbox(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    f.activities._settings = SimpleNamespace(
        codex_api_projects={f.run.project_id}, luna_api_key="synthetic"
    )
    monkeypatch.setattr(f.storage, "head_sha", AsyncMock(return_value="a" * 40))

    async def create(**kwargs):
        await stop(f)
        return "created-after-stop"

    monkeypatch.setattr(f.activities._sandboxes, "create", create)
    kill = AsyncMock()
    monkeypatch.setattr(f.activities._sandboxes, "kill", kill)
    with pytest.raises(RuntimeError, match="lease"):
        await f.activities.create_codex_procedure_sandbox(str(f.run.id))
    kill.assert_awaited_once_with("created-after-stop")
    current = await f.db.get_run(f.run.id)
    assert current.status == RunStatus.STOPPED and not current.lease_active


async def test_duplicate_e2b_cleanup_accepts_a_race_after_connect(monkeypatch):
    from e2b import SandboxNotFoundException

    from tin_lite.e2b_runtime import E2BRuntime

    sandbox = SimpleNamespace(kill=AsyncMock(side_effect=SandboxNotFoundException("gone")))
    monkeypatch.setattr(
        "tin_lite.e2b_runtime.AsyncSandbox.connect",
        AsyncMock(return_value=sandbox),
    )
    runner = E2BRuntime(
        api_key="synthetic",
        template="test",
        timeout_seconds=60,
        egress_allow_hosts=(),
    )
    await runner.kill("test-sandbox")


async def test_stop_needs_only_one_available_database_connection(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    async with AsyncExitStack() as stack:
        for _ in range(f.db.pool.get_max_size() - 1):
            await stack.enter_async_context(f.db.pool.acquire())
        result = await asyncio.wait_for(stop(f), timeout=2)
    assert result["status"] == "stopped"
