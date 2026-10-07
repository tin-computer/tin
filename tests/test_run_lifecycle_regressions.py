"""Run lifecycle regressions: leases, retries, late results, failures and cleanup."""

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from e2b import SandboxException
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from test_billing import billed as billed
from test_code_schedules import occurrence, prepared
from test_growth_onboarding_picks import call, picks_args
from test_growth_onboarding_picks import harness as picks_harness
from test_private_workflows import app, structured
from test_procedure_publication import publication_db as publication_db
from test_project_connections import code_bridge
from test_project_deletion import ACTOR, fixture, seed_work
from test_workflow_code import SyntheticCompute, activate_code, setup, start

from tin_lite.code_project_files import CodeProjectFileError
from tin_lite.domain import RunStatus, SideEffectConflictError, StaleGenerationError
from tin_lite.e2b_runtime import CodeExecutionError, E2BRuntime
from tin_lite.project_deletion import delete_project


async def attach(f, run_id, sandbox_id):
    run = await f.db.get_run(UUID(str(run_id)))
    return await f.db.attach_sandbox(
        run_id=run.id,
        sandbox_id=sandbox_id,
        lease_owner=f"{run.id}:code_sandbox",
        expected_head_sha=f.storage.repo.head,
        ephemeral_branch=f"procedures/{run.id}/{run.generation}",
        require_active=True,
    )


async def holds_lease(f, run):
    return await f.db.validate_lease(
        project_id=run.project_id,
        thread_id=run.thread_id,
        generation=run.generation,
        lease_owner=run.lease_owner,
        fencing_token=run.fencing_token,
        sandbox_id=run.sandbox_id,
    )


async def test_concurrent_ad_hoc_runs_of_one_workflow_keep_their_own_leases(billed, monkeypatch):
    f = billed
    server, _common, _code = await setup(f, monkeypatch)
    active = await activate_code(f, server)
    first = await start(f, server, active)
    second = await start(f, server, active)
    older = await attach(f, first["id"], "sandbox-first")
    newer = await attach(f, second["id"], "sandbox-second")
    assert older.thread_id != newer.thread_id
    # Attaching the second run's sandbox used to revoke the first run's model and file access.
    assert await holds_lease(f, older) and await holds_lease(f, newer)


async def test_an_older_configuration_run_cannot_revoke_a_newer_ones_lease(billed, monkeypatch):
    f = billed
    server, _common, _code, configured, _handle = await prepared(f, monkeypatch)
    f.runtime.temporal.start_workflow = AsyncMock()
    selected = {"project_id": str(f.project.id), "project_workflow_id": configured["id"]}
    first = structured(
        await server.call_tool("start_project_workflow", {**selected, "request_id": str(uuid4())})
    )
    second = structured(
        await server.call_tool("start_project_workflow", {**selected, "request_id": str(uuid4())})
    )
    newer = await attach(f, second["id"], "sandbox-newer")
    with pytest.raises(StaleGenerationError):
        await attach(f, first["id"], "sandbox-older")
    assert await holds_lease(f, newer)
    # The newer generation still fences the older one, as before.
    older = await f.db.get_run(UUID(first["id"]))
    assert older.generation < newer.generation and not older.lease_active


async def test_concurrent_retries_of_one_failed_run_start_one_retry(billed, monkeypatch):
    f = billed
    _server, common, _code, configured, _handle = await prepared(f, monkeypatch)
    dispatched = await common.dispatch_scheduled_workflow(occurrence(configured))
    failed = UUID(dispatched["run_id"])
    await f.db.project_failure(run_id=failed, error_message="boom")
    f.runtime.temporal.start_workflow = AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://test"
    ) as client:
        url = f"/api/projects/{f.project.id}/workflows/{configured['id']}/retry"
        responses = await asyncio.gather(
            *[client.post(url, headers={"Idempotency-Key": str(uuid4())}) for _ in range(3)]
        )
    assert sorted(response.status_code for response in responses) == [202, 409, 409]
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE retry_of_run_id=$1", failed
        )
        == 1
    )


async def test_a_scheduled_code_run_is_retried_after_a_transient_failure(billed, monkeypatch):
    f = billed
    _server, common, _code, configured, _handle = await prepared(f, monkeypatch)
    dispatched = await common.dispatch_scheduled_workflow(occurrence(configured))
    failed = UUID(dispatched["run_id"])
    next_run_at = (await f.db.get_project_workflow(UUID(configured["id"]))).next_run_at
    common._temporal.start_workflow = AsyncMock()
    message = "Code workflow failed during execution (ConnectError: connection refused)."
    await f.db.project_failure(run_id=failed, error_message=message)
    retry_id = await common._auto_retry_after_transient_failure(
        failed, message=message, restarted=False
    )
    assert retry_id is not None
    retry = await f.db.get_run(retry_id)
    assert retry.retry_of_run_id == failed and retry.trigger_source == "schedule"
    # A retry is not an occurrence: the saved schedule's next run is untouched.
    assert (await f.db.get_project_workflow(UUID(configured["id"]))).next_run_at == next_run_at


async def test_a_paused_schedule_gets_no_automatic_retry(billed, monkeypatch):
    f = billed
    _server, common, _code, configured, _handle = await prepared(f, monkeypatch)
    dispatched = await common.dispatch_scheduled_workflow(occurrence(configured))
    failed = UUID(dispatched["run_id"])
    common._temporal.start_workflow = AsyncMock()
    message = "Code workflow failed during execution (ConnectError: connection refused)."
    await f.db.project_failure(run_id=failed, error_message=message)
    await f.db.pool.execute(
        "UPDATE project_workflows SET status='paused' WHERE id=$1", UUID(configured["id"])
    )
    assert (
        await common._auto_retry_after_transient_failure(failed, message=message, restarted=False)
        is None
    )
    assert not await f.db.pool.fetchval(
        "SELECT count(*) FROM workflow_runs WHERE retry_of_run_id=$1", failed
    )


async def test_a_late_memory_result_cannot_revive_a_finished_run(publication_db):
    f = await fixture(publication_db)
    work = await seed_work(f)
    await delete_project(f.runtime, project_id=f.project.id, actor=ACTOR, request_id=uuid4())
    before = await f.db.pool.fetchval(
        "SELECT memory_commit_sha FROM projects WHERE id=$1", f.project.id
    )
    for status in ("stopped", "failed", "superseded"):
        await f.db.pool.execute(
            "UPDATE workflow_runs SET status=$2, error_message='x' WHERE id=$1",
            work.run_id,
            status,
        )
        with pytest.raises(SideEffectConflictError):
            await f.db.project_memory_success(
                run_id=work.run_id,
                canonical_commit_sha="b" * 40,
                artifact_ref="code.storage://projects/acme@b/wiki/INDEX.md",
                artifact_path="wiki/INDEX.md",
                memory_index="# Memory\n",
            )
        row = await f.db.pool.fetchrow(
            "SELECT status, error_message FROM workflow_runs WHERE id=$1", work.run_id
        )
        assert dict(row) == {"status": status, "error_message": "x"}
    assert (
        await f.db.pool.fetchval("SELECT memory_commit_sha FROM projects WHERE id=$1", f.project.id)
        == before
    )


class FailingCompute(SyntheticCompute):
    def __init__(self, error):
        super().__init__()
        self.error = error

    async def run_code_and_kill(self, *, packet, **kwargs):
        self.calls += 1
        raise self.error


async def failed_execution(f, monkeypatch, error):
    compute = FailingCompute(error)
    server, common, code = await setup(f, monkeypatch, compute=compute)
    active = await activate_code(f, server)
    run_id = (await start(f, server, active))["id"]
    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(code.execute, run_id)
    assert caught.value.non_retryable
    receipt = await f.db.get_effect(f"{run_id}:procedure_artifact_persist")
    assert receipt.status == "failed" and receipt.error_message == str(caught.value)
    await code.failure(run_id)
    run = await f.db.get_run(UUID(run_id))
    assert run.status == RunStatus.FAILED
    return run, str(caught.value)


async def test_revoked_file_access_is_reported_as_revoked_not_as_validation(billed, monkeypatch):
    run, reason = await failed_execution(
        billed, monkeypatch, CodeProjectFileError("file_access_revoked")
    )
    assert reason.startswith("file_access_revoked:")
    assert "validation" not in reason and run.error_message == reason


async def test_a_package_that_exits_non_zero_fails_once_with_its_reason(billed, monkeypatch):
    error = CodeExecutionError(
        "Code workflow failed: the package exited without a valid result.", deterministic=True
    )
    run, reason = await failed_execution(billed, monkeypatch, error)
    assert reason == str(error) and run.error_message == str(error)
    summary = await billed.db.pool.fetchval(
        "SELECT summary FROM activity_events WHERE run_id=$1 AND event_type='workflow_run_failed'",
        run.id,
    )
    assert summary == "Workflow run failed before it finished."


async def test_an_unconfirmed_sandbox_result_is_still_retried(billed, monkeypatch):
    compute = FailingCompute(
        CodeExecutionError("Code workflow failed: the sandbox could not return its result.")
    )
    server, _common, code = await setup(billed, monkeypatch, compute=compute)
    active = await activate_code(billed, server)
    run_id = (await start(billed, server, active))["id"]
    with pytest.raises(CodeExecutionError):
        await ActivityEnvironment().run(code.execute, run_id)
    receipt = await billed.db.get_effect(f"{run_id}:procedure_artifact_persist")
    assert receipt.status == "started"
    assert receipt.result["failure_reason"] == str(compute.error)


@pytest.mark.parametrize(
    "failure,deterministic",
    [
        (dict(exit=1), True),
        (dict(result=RuntimeError("private sandbox read failure")), False),
        (dict(result=TimeoutError("private timeout details")), False),
    ],
)
async def test_only_a_package_exit_is_deterministic(monkeypatch, failure, deterministic):
    runtime, run, _replies, outcomes, package = code_bridge(monkeypatch)
    package.requests = 0
    for name, value in failure.items():
        setattr(package, name, value)
    with pytest.raises(CodeExecutionError) as failed:
        await runtime.run_code_and_kill(**run)
    assert failed.value.deterministic is deterministic


async def test_kill_tolerates_a_sandbox_another_stop_is_already_deleting(monkeypatch):
    runtime = E2BRuntime(
        api_key="synthetic", template="default", timeout_seconds=60, egress_allow_hosts=()
    )
    connect = AsyncMock(side_effect=SandboxException("409: Sandbox abc is being deleted"))
    monkeypatch.setattr("tin_lite.e2b_runtime.AsyncSandbox.connect", connect)
    await runtime.kill("abc")
    connect.side_effect = SandboxException("409: Sandbox abc is being paused")
    with pytest.raises(SandboxException):
        await runtime.kill("abc")
    connect.side_effect = SandboxException("500: internal error")
    with pytest.raises(SandboxException):
        await runtime.kill("abc")


async def test_only_an_onboarding_approval_talks_about_schedules(publication_db, monkeypatch):
    h = await picks_harness(publication_db, monkeypatch, executor="codex.procedure")
    approved = await call(h, "approve_workflow_run", run_id=str(h.run.id))
    assert approved["review_decision"] == "approval_signaled"
    assert "schedules" not in approved["note"] and "first runs" not in approved["note"]


async def test_an_onboarding_approval_still_explains_its_setup(publication_db, monkeypatch):
    onboarding = await picks_harness(publication_db, monkeypatch)
    await call(onboarding, "record_onboarding_picks", **picks_args(onboarding))
    approved = await call(onboarding, "approve_workflow_run", run_id=str(onboarding.run.id))
    assert "saves the schedules" in approved["note"]
