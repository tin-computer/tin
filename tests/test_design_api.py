"""Legacy design orchestration, real credit ledger, synthetic supplier wire."""

import json
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
from tin_lite.codex_api import PROCEDURE_CONTRACT, attempt_key
from tin_lite.codex_api_relay import CodexAPIRelay, router
from tin_lite.domain import SideEffectConflictError
from tin_lite.e2b_runtime import SandboxProcedureResult
from tin_lite.run_service import start_workflow_run


async def design_run(f):
    await fund(f)
    f.settings.codex_api_projects = {f.project.id}
    f.settings.luna_api_key = "synthetic"
    builtin = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.design_md")
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
    workflow = await f.db.get_workflow(builtin.id)
    run = await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        start_idempotency_key=str(uuid4()),
    )
    storage = FakeStorage()
    storage.get_repo = AsyncMock(return_value=SimpleNamespace())
    storage.head_sha = AsyncMock(return_value="a" * 40)
    sandboxes = SimpleNamespace(
        create=AsyncMock(return_value="design-sandbox"),
        kill=AsyncMock(),
        is_running=AsyncMock(return_value=False),
        run_and_kill=AsyncMock(),
        run_procedure_and_kill=AsyncMock(),
    )
    f.settings.switchboard_public_url = "https://tin.test"
    f.settings.forward_proxy_url = SimpleNamespace(
        get_secret_value=lambda: "http://proxy.test:8888"
    )
    activities = TinActivities(
        database=f.db, storage=storage, sandboxes=sandboxes, settings=f.settings
    )
    return run, activities, storage, sandboxes


async def test_design_api_pins_and_charges_supplier_once(billed, monkeypatch):
    f = billed
    run, activities, storage, sandboxes = await design_run(f)
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *a: None)
    await activities.create_design_sandbox(str(run.id))
    receipt = await f.db.get_effect(f"{run.id}:sandbox_create")
    assert receipt.result["codex_auth"] == PROCEDURE_CONTRACT
    assert receipt.result["context"]["output"]["path"] == "DESIGN.md"
    assert sandboxes.create.call_args.kwargs["profile"].isolated
    # Configuration changes cannot downgrade an admitted run or change its image.
    f.settings.codex_api_projects = set()
    await activities.create_design_sandbox(str(run.id))
    assert sandboxes.create.await_count == 1
    budget = json.loads(
        await f.db.pool.fetchval("SELECT terms FROM billing_run_budgets WHERE run_id=$1", run.id)
    )
    assert budget["kind"] == "codex_api" and budget["funding"] == "per_operation_v1"
    assert await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == 0
    sent = []

    def wire(request):
        sent.append(request)
        return httpx.Response(
            200,
            content=result_event(
                # 5,000 x $2/M + 1,000 x $10/M = $0.02 at gpt-6-sol: a visible cent charge.
                usage={
                    "input_tokens": 5000,
                    "output_tokens": 1000,
                    "total_tokens": 6000,
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                }
            ),
        )

    relay = CodexAPIRelay(
        database=f.db,
        api_key="synthetic-master-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
    )
    app = FastAPI()
    app.state.runtime = SimpleNamespace(codex_api=relay)
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:

        async def execute(*, sandbox_id, run_input):
            assert not hasattr(run_input, "broker_grant")
            assert run_input.proxy_url == "http://proxy.test:8888"
            assert run_input.no_proxy == "tin.test"
            assert run_input.isolated and run_input.api_contract == PROCEDURE_CONTRACT
            assert run_input.project_revision == "a" * 40
            assert (await post(client, run, grant=run_input.api_grant)).status_code == 200
            assert (await post(client, run, grant=run_input.api_grant)).status_code == 409
            storage.recoverable_artifact = b"# Product design\n"
            return SandboxProcedureResult("e" * 40, "Design saved", "Ready")

        sandboxes.run_procedure_and_kill.side_effect = execute
        await activities.persist_design_artifact(str(run.id))
        await activities.persist_design_artifact(str(run.id))
    await relay.close()
    assert len(sent) == 1 and sandboxes.run_procedure_and_kill.await_count == 1
    sandboxes.run_and_kill.assert_not_awaited()
    assert (
        await f.db.pool.fetchval("SELECT count(*) FROM broker_grants WHERE run_id=$1", run.id) == 0
    )
    assert (await f.db.get_effect(attempt_key(run.id))).status == "completed"
    await activities.commit_design_canonically(str(run.id))
    await activities.project_design_result(str(run.id))
    await activities.project_design_result(str(run.id))
    assert (await f.db.get_run(run.id)).status.value == "succeeded"
    assert await f.billing.settle(run.id) == 20_000_000
    assert await f.billing.settle(run.id) == 20_000_000
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 1
    assert await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == 0
    assert storage.canonical_commits == 1


async def test_design_uncertain_attempt_is_not_purchased_again(billed, monkeypatch):
    f = billed
    run, activities, storage, sandboxes = await design_run(f)
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *a: None)
    await activities.create_design_sandbox(str(run.id))
    sandboxes.run_procedure_and_kill.side_effect = TimeoutError("provider interrupted")
    with pytest.raises(TimeoutError):
        await activities.persist_design_artifact(str(run.id))
    with pytest.raises(SideEffectConflictError):
        await activities.persist_design_artifact(str(run.id))
    assert sandboxes.run_procedure_and_kill.await_count == 1
    # A late durable checkpoint is still recoverable, without a second model call.
    storage.recoverable_artifact = b"# Product design\n"
    await activities.persist_design_artifact(str(run.id))
    assert sandboxes.run_procedure_and_kill.await_count == 1


async def test_design_legacy_create_receipt_keeps_oauth(billed):
    f = billed
    run, activities, _, sandboxes = await design_run(f)
    key = f"{run.id}:sandbox_create"
    async with f.db.pool.acquire() as conn:
        await f.db.start_effect(conn, execution_key=key, operation="sandbox_create")
    # Historical OAuth pins are never upgraded or allowed to create an unsafe runner.
    with pytest.raises(ValueError, match="retired"):
        await activities.create_design_sandbox(str(run.id))
    assert (await f.db.get_effect(key)).result["codex_auth"] == {"mode": "chatgpt_oauth"}
    sandboxes.create.assert_not_awaited()


async def test_design_preflight_failure_does_not_fall_back_to_oauth(billed):
    f = billed
    run, activities, _, sandboxes = await design_run(f)
    f.settings.luna_api_key = None
    for _ in range(2):
        with pytest.raises(ValueError, match="server-side OpenAI"):
            await activities.create_design_sandbox(str(run.id))
    sandboxes.create.assert_not_awaited()
    f.settings.luna_api_key = "synthetic"
    f.settings.codex_api_projects = set()  # The paid admission already fixed API auth.
    await activities.create_design_sandbox(str(run.id))
    assert sandboxes.create.call_args.kwargs["profile"].isolated


async def test_design_early_failure_does_not_fall_back_to_oauth(billed, monkeypatch):
    f = billed
    run, activities, _, sandboxes = await design_run(f)
    failures = [ConnectionError("database connection reset")]
    require_run = activities._require_run

    async def flaky_require_run(run_id):
        if failures:
            raise failures.pop()
        return await require_run(run_id)

    monkeypatch.setattr(activities, "_require_run", flaky_require_run)
    with pytest.raises(ConnectionError):
        await activities.create_design_sandbox(str(run.id))
    sandboxes.create.assert_not_awaited()
    await activities.create_design_sandbox(str(run.id))
    assert sandboxes.create.call_args.kwargs["profile"].isolated
    receipt = await f.db.get_effect(f"{run.id}:sandbox_create")
    assert receipt.result["codex_auth"] == PROCEDURE_CONTRACT
