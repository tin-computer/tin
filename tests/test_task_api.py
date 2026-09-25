"""Interactive API turns retain their existing control/review contract and one bill."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from test_billing import ACTOR, fund
from test_billing import billed as billed
from test_checkpoint_contract import FakeStorage
from test_codex_api import post, result_event
from test_procedure_publication import publication_db as publication_db

from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.codex_api import PROCEDURE_CONTRACT, attempt_key, execution_profile
from tin_lite.codex_api_relay import CodexAPIRelay, router
from tin_lite.domain import SideEffectConflictError
from tin_lite.e2b_runtime import E2BRuntime, SandboxTaskResult
from tin_lite.procedures import SandboxProfile
from tin_lite.run_service import start_workflow_run
from tin_lite.task_api import auth_contract


async def task_fixture(f, monkeypatch):
    await fund(f)
    f.settings.codex_api_projects = {f.project.id}
    f.settings.luna_api_key = "synthetic"
    f.settings.sandbox_timeout_seconds = 900
    f.settings.switchboard_public_url = "https://tin.test"
    f.settings.forward_proxy_url = SimpleNamespace(
        get_secret_value=lambda: "http://proxy.test:8888"
    )
    builtin = next(w for w in BUILTIN_WORKFLOWS if w.key == "project.task")
    await f.db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=builtin.key,
        executor=builtin.executor,
        title=builtin.title,
        description=builtin.description,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha="d" * 40,
        version_label=builtin.version_label,
        definition=builtin.definition,
    )
    run = await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=await f.db.get_workflow(builtin.id),
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={
            "instruction": "Read the project, ask one question, then summarize.",
            "title": "Test task",
        },
        start_idempotency_key=str(uuid4()),
    )
    storage = FakeStorage()
    storage.get_repo = AsyncMock(return_value=SimpleNamespace())
    storage.head_sha = AsyncMock(return_value="a" * 40)
    sandboxes = SimpleNamespace(
        create=AsyncMock(return_value="task-sandbox"),
        kill=AsyncMock(),
        run_task_and_kill=AsyncMock(),
    )
    activities = TinActivities(
        database=f.db, storage=storage, sandboxes=sandboxes, settings=f.settings
    )
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *a: None)
    monkeypatch.setattr("tin_lite.activities.activity.info", lambda: SimpleNamespace(attempt=1))
    return run, activities, sandboxes


async def test_task_questions_retries_and_next_turn_charge_once(billed, monkeypatch):
    f = billed
    run, activities, sandboxes = await task_fixture(f, monkeypatch)
    wire = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            # gpt-6-sol: 5,000 x $2/M + 1,000 x $10/M = $0.02 per turn, so each turn shows.
            content=result_event(
                usage={
                    "input_tokens": 5000,
                    "output_tokens": 1000,
                    "total_tokens": 6000,
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                }
            ),
        )
    )
    relay = CodexAPIRelay(
        database=f.db, api_key="synthetic", client=httpx.AsyncClient(transport=wire)
    )
    app = FastAPI()
    app.state.runtime = SimpleNamespace(codex_api=relay)
    app.include_router(router)
    grants = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:

        async def execute(*, sandbox_id, run_input, on_event):
            assert run_input.isolated
            assert run_input.api_contract == PROCEDURE_CONTRACT
            assert run_input.proxy_url == "http://proxy.test:8888"
            grants.append(run_input.api_grant)
            if len(grants) > 1:
                assert (await post(client, run, grant=grants[0])).status_code == 403
            response = await post(client, run, grant=grants[-1])
            assert response.status_code == 200, response.text
            assert (await post(client, run, grant=grants[-1])).status_code == 409
            question = len(grants) == 1
            return SandboxTaskResult(
                outcome="needs_input" if question else "completed",
                summary="Ready",
                message="",
                question="Which section?" if question else None,
                has_changes=False,
            )

        sandboxes.run_task_and_kill.side_effect = execute
        payload = {"run_id": str(run.id), "turn_number": "1"}
        # Simulate losing the result-projection write after the supplier and sandbox completed.
        project = f.db.project_task_outcome
        fail_once = AsyncMock(side_effect=RuntimeError("projection unavailable"))
        monkeypatch.setattr(f.db, "project_task_outcome", fail_once)
        with pytest.raises(RuntimeError, match="projection unavailable"):
            await activities.run_project_task_turn(payload)
        monkeypatch.setattr(f.db, "project_task_outcome", project)
        assert await activities.run_project_task_turn(payload) == "needs_input"
        assert await activities.run_project_task_turn(payload) == "needs_input"
        assert sandboxes.create.await_count == sandboxes.run_task_and_kill.await_count == 1
        assert await f.billing.settle(run.id) is None
        await f.billing.reconcile()
        assert (
            await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 0
        )
        assert (await post(client, run, grant=grants[0])).status_code == 403
        # Pausing at a question and resuming preserves the same run budget/auth selection.
        await f.db.pool.execute("UPDATE workflow_runs SET status='paused' WHERE id=$1", run.id)
        assert await f.billing.settle(run.id) is None
        f.settings.codex_api_projects = set()
        await f.db.clear_task_control(run_id=run.id)
        assert (
            await activities.run_project_task_turn({**payload, "turn_number": "2"}) == "completed"
        )
        assert len(grants) == 2 and grants[0] != grants[1]
        assert await f.billing.settle(run.id) == 40_000_000
        assert await f.billing.settle(run.id) == 40_000_000
    await relay.close()
    assert (
        await f.db.pool.fetchval("SELECT count(*) FROM broker_grants WHERE run_id=$1", run.id) == 0
    )
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 1
    assert await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == 0
    assert (await f.db.get_run(run.id)).task_turn_number == 2
    assert (await f.db.get_run(run.id)).task_result == "Ready"


async def test_task_ambiguous_turn_is_not_restarted(billed, monkeypatch):
    run, activities, sandboxes = await task_fixture(billed, monkeypatch)
    sandboxes.run_task_and_kill.side_effect = TimeoutError("uncertain supplier attempt")
    payload = {"run_id": str(run.id), "turn_number": "1"}
    with pytest.raises(TimeoutError):
        await activities.run_project_task_turn(payload)
    with pytest.raises(SideEffectConflictError):
        await activities.run_project_task_turn(payload)
    assert sandboxes.create.await_count == sandboxes.run_task_and_kill.await_count == 1
    assert (await billed.db.get_effect(attempt_key(run.id, 1))).result["outcome"] == "failed"


async def test_task_recovery_preserves_direction_received_after_checkpoint(billed, monkeypatch):
    f = billed
    run, activities, sandboxes = await task_fixture(f, monkeypatch)
    original = await f.db.append_task_entry(
        run_id=run.id,
        kind="direction",
        source="founder",
        content="Summarize README.",
        author_clerk_user_id=ACTOR,
    )
    sandboxes.run_task_and_kill.return_value = SandboxTaskResult(
        outcome="completed", summary="Ready", message="Summary", question=None, has_changes=False
    )
    project = f.db.project_task_outcome
    monkeypatch.setattr(
        f.db, "project_task_outcome", AsyncMock(side_effect=RuntimeError("projection unavailable"))
    )
    payload = {"run_id": str(run.id), "turn_number": "1"}
    with pytest.raises(RuntimeError, match="projection unavailable"):
        await activities.run_project_task_turn(payload)
    later = await f.db.append_task_entry(
        run_id=run.id,
        kind="direction",
        source="founder",
        content="Also explain the audience.",
        author_clerk_user_id=ACTOR,
    )
    monkeypatch.setattr(f.db, "project_task_outcome", project)
    assert await activities.run_project_task_turn(payload) == "continue"
    entries = {entry.id: entry for entry in await f.db.list_task_entries(run_id=run.id)}
    assert entries[original.id].delivered_at is not None
    assert entries[later.id].delivered_at is None
    assert sandboxes.create.await_count == sandboxes.run_task_and_kill.await_count == 1


async def test_historical_task_keeps_oauth(billed, monkeypatch):
    run, _, _ = await task_fixture(billed, monkeypatch)
    # An old unbilled paused run has no API budget, even when the project is now enabled.
    await billed.db.pool.execute("DELETE FROM billing_run_budgets WHERE run_id=$1", run.id)
    async with billed.db.pool.acquire() as conn:
        auth = await auth_contract(billed.db, conn, run, billed.settings, legacy_turn=True)
    assert auth == {"mode": "chatgpt_oauth"}


async def unbudgeted_api_task(f, run):
    # An enabled project's unbilled task selects API at its first turn, not at admission.
    await f.db.pool.execute("DELETE FROM billing_run_budgets WHERE run_id=$1", run.id)
    await f.db.pool.execute("UPDATE billing_accounts SET run_billing_enabled=false")


async def test_first_turn_failure_before_auth_pin_retries_on_api(billed, monkeypatch):
    run, activities, sandboxes = await task_fixture(billed, monkeypatch)
    await unbudgeted_api_task(billed, run)
    sandboxes.run_task_and_kill.return_value = SandboxTaskResult(
        outcome="completed", summary="Ready", message="Summary", question=None, has_changes=False
    )
    require_run = activities._require_run
    failures = [ConnectionError("database unavailable")]

    async def flaky_require_run(run_id):
        if failures:
            raise failures.pop()
        return await require_run(run_id)

    activities._require_run = flaky_require_run
    payload = {"run_id": str(run.id), "turn_number": "1"}
    with pytest.raises(ConnectionError):
        await activities.run_project_task_turn(payload)
    # The failed first turn is not a pre-migration OAuth turn on retry.
    assert await activities.run_project_task_turn(payload) == "completed"
    receipt = await billed.db.get_effect(f"{run.id}:task_codex_auth")
    assert receipt.status == "completed"
    assert receipt.result["codex_auth"] == PROCEDURE_CONTRACT
    assert sandboxes.create.call_args.kwargs["profile"].isolated


async def test_unmarked_task_turn_receipt_keeps_oauth(billed, monkeypatch):
    run, activities, sandboxes = await task_fixture(billed, monkeypatch)
    await unbudgeted_api_task(billed, run)
    key = f"{run.id}:task_turn:1"
    async with billed.db.pool.acquire() as conn:
        await billed.db.start_effect(conn, execution_key=key, operation="project_task_turn")
    # A turn started before the API pilot never switches its historical OAuth pin.
    with pytest.raises(ValueError, match="retired"):
        await activities.run_project_task_turn({"run_id": str(run.id), "turn_number": "1"})
    receipt = await billed.db.get_effect(f"{run.id}:task_codex_auth")
    assert receipt.result["codex_auth"] == {"mode": "chatgpt_oauth"}
    sandboxes.create.assert_not_awaited()


def test_browser_has_separate_api_image_and_preserves_capabilities():
    pinned = SandboxProfile(profile="browser", timeout_seconds=1800, egress="open")
    effective = execution_profile(pinned, PROCEDURE_CONTRACT)
    assert effective.browser and effective.isolated and effective.open_egress
    runtime = E2BRuntime(
        api_key="test",
        template="default",
        browser_template="old-browser",
        timeout_seconds=900,
        egress_allow_hosts=("tin.test",),
    )
    assert runtime.template_for(effective) == "tin-lite-codex-browser-api"
    assert runtime.template_for(pinned) == "old-browser"
    assert effective.timeout_seconds == 1800
    assert pinned.definition()["profile"] == "browser"
