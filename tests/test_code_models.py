"""Managed model contract, durable recovery and funding with a real disposable DB.

Default tests use an SDK HTTP fixture and synthetic compute. The opt-in proof below
uses real isolated compute/storage and an actual supplier call, with local product state.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from openai import AsyncOpenAI
from pydantic import SecretStr
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer, Worker
from test_billing import billed as billed
from test_billing import fund
from test_private_workflows import ACTOR, structured
from test_procedure_publication import publication_db as publication_db
from test_project_codex_execution import temporal_env as temporal_env
from test_workflow_code import setup, start

from tin_lite.code_activities import CodeActivities
from tin_lite.code_models import (
    OPERATION,
    CodeModelError,
    model_terms,
    registered_routes,
    request_contract,
)
from tin_lite.codex_execution import ProjectCodexExecution
from tin_lite.domain import RunStatus
from tin_lite.model_providers import ModelRouter, OpenAIModelProvider, ProviderName
from tin_lite.model_usage import ModelUsageRecorder
from tin_lite.workflow_code import example_files, validate_code_definition
from tin_lite.workflow_costs import configured_terms
from tin_lite.workflows import CodeWorkflow

KEY = "custom.order_classification"
PATH = f"workflow_packages/{KEY}/workflow.json"
OUTPUT = "reports/custom/ORDER_CLASSIFICATION.md"
CLASSIFIED = {
    "orders": [
        {"id": "order-2", "category": "medium"},
        {"id": "order-3", "category": "large"},
    ]
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "orders": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "category": {"type": "string"}},
                "required": ["id", "category"],
            },
        }
    },
    "required": ["orders"],
}


def definition():
    return json.loads(example_files(KEY, model_steps=True)[PATH])["definition"]


def payload(**changes):
    return {
        "route": "classification",
        "step": "classify_orders",
        "instructions": "Classify the supplied fixture orders by size.",
        "data": [{"id": "order-2", "amount_cents": 1500}, {"id": "order-3", "amount_cents": 2400}],
        "output_schema": SCHEMA,
        **changes,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "anthropic"},
        {"model": "gpt-unknown"},
        {"provider": {}},
        {"base_url": "https://arbitrary.example"},
        {"max_calls": True},
        {"max_calls": 5},
        {"max_input_bytes": 32_001},
        {"max_output_tokens": 4097},
    ],
)
def test_declared_model_routes_reject_unsupported_authority(change):
    body = definition()
    body["code"]["model_routes"]["classification"].update(change)
    with pytest.raises(ValueError):
        validate_code_definition(body)


def test_model_route_errors_say_whether_the_shape_or_the_model_is_wrong():
    from tin_lite.private_workflows import authoring_guide

    extra = definition()
    extra["code"]["model_routes"]["classification"]["capabilities"] = ["json"]
    with pytest.raises(
        ValueError,
        match="route 'classification' keys must be exactly provider, model, max_calls, "
        "max_input_bytes, max_output_tokens",
    ):
        validate_code_definition(extra)
    unknown = definition()
    unknown["code"]["model_routes"]["classification"]["model"] = "gpt-5"
    with pytest.raises(
        ValueError,
        match="unsupported or unpriced model; supported provider/model pairs: "
        "openai/gpt-6-luna, openai/gpt-6-sol",
    ):
        validate_code_definition(unknown)
    guide = authoring_guide(settings=SimpleNamespace(), project_id=uuid4())
    assert guide["code_contract"]["models"]["routes"] == [
        {"provider": "openai", "model": "gpt-6-luna"},
        {"provider": "openai", "model": "gpt-6-sol"},
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"route": "undeclared"},
        {"step": ""},
        {"step": "x" * 81},
        {"data": "x" * 32_000},
        {"data": float("nan")},
        {"instructions": ""},
        {"output_schema": {"$ref": "http://169.254.169.254/"}},
        {
            "output_schema": {
                "type": "object",
                "properties": {"x": {"type": "array", "items": {"type": "string"}}},
                "required": ["x"],
                "additionalProperties": False,
            }
        },
        {"output_schema": {**SCHEMA, "description": 1}},
        {"run_id": str(uuid4())},
    ],
)
def test_requests_are_closed_bounded_and_cannot_fetch_schema_references(change):
    with pytest.raises(CodeModelError, match="declared contract"):
        request_contract(validate_code_definition(definition()), payload(**change))


def test_configured_estimate_is_cached_and_changes_with_definition_inputs_and_prices():
    body = definition()
    terms = model_terms(body)
    first = configured_terms(terms, body, {"minimum_cents": 1000})
    assert first == configured_terms(model_terms(body), body, {"minimum_cents": 1000})
    assert first["maximum_nanos"] >= 10_000_000
    assert first["funding"] == "per_operation_v1"
    assert (
        first["estimate"]["id"]
        != configured_terms(terms, body, {"minimum_cents": 0})["estimate"]["id"]
    )
    changed = deepcopy(body)
    changed["code"]["model_routes"]["classification"]["max_calls"] = 2
    assert (
        first["estimate"]["id"]
        != configured_terms(model_terms(changed), changed, {})["estimate"]["id"]
    )
    prices = deepcopy(terms)
    prices["service_pricing"]["id"] = "future-card"
    assert (
        first["estimate"]["id"]
        != configured_terms(prices, body, {"minimum_cents": 1000})["estimate"]["id"]
    )


def sdk_router(f, *, invalid=False, uncertain=False, calls=None, outputs=None):
    calls = calls if calls is not None else []

    def wire(request):
        data = json.loads(request.content)
        calls.append(data)
        if uncertain:
            raise httpx.ReadTimeout("test supplier lost acknowledgement", request=request)
        output = outputs[len(calls) - 1] if outputs is not None else CLASSIFIED
        text = json.dumps({"unexpected": True} if invalid else output)
        return httpx.Response(
            200,
            json={
                "id": f"resp-code-{len(calls)}",
                "object": "response",
                "created_at": 123,
                "status": "completed",
                "model": data["model"],
                "service_tier": "default",
                "output": [
                    {
                        "type": "message",
                        "id": "msg1",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                ],
                # gpt-6-sol: 5,000 x $2/M + 500 x $10/M = $0.015, settled as a visible $0.02.
                "usage": {
                    "input_tokens": 5000,
                    "output_tokens": 500,
                    "total_tokens": 5500,
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    client = AsyncOpenAI(
        api_key="fixture-server-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
    )
    router = ModelRouter(
        providers={ProviderName.OPENAI: OpenAIModelProvider(api_key="fixture", client=client)},  # noqa: S106
        routes=registered_routes(),
        recorder=ModelUsageRecorder(f.db),
    )
    return router, calls


class ModelCompute:
    def __init__(self, *, lose_once=False, result_invalid=False):
        self.calls = 0
        self.creates = 0
        self.killed = []
        self.lose_once, self.result_invalid = lose_once, result_invalid
        self.saved = asyncio.Event()

    async def create(self, **kwargs):
        assert kwargs["code_only"]
        self.creates += 1
        return f"synthetic-code-model-{self.creates}"

    async def run_code_and_kill(self, *, packet, model_call, **kwargs):
        assert packet["model_client"] is True
        assert set(packet) == {
            "model_client",
            "files",
            "entrypoint",
            "timeout_seconds",
            "context",
            "inputs",
        }
        self.calls += 1
        response = await model_call(payload())
        self.saved.set()
        if self.lose_once and self.calls == 1:
            raise RuntimeError("simulated worker loss after durable model completion")
        return json.dumps(
            {
                "path": OUTPUT,
                "content": ""
                if self.result_invalid
                else "# Classified orders\n\n" + json.dumps(response["parsed"]),
            }
        ).encode()

    async def kill(self, sandbox_id):
        self.killed.append(sandbox_id)


async def prepare(
    f, monkeypatch, *, compute=None, invalid=False, uncertain=False, funded=True, temporal=None
):
    if funded:
        await fund(f)
    compute = compute or ModelCompute()
    server, common, _ = await setup(f, monkeypatch, compute=compute, temporal=temporal)
    f.settings.luna_api_key = SecretStr("fixture-server-key")
    files = example_files(KEY, model_steps=True)
    manifest = json.loads(files[PATH])
    # Sol makes the fixture's actual metered usage visibly nonzero at cent settlement.
    manifest["definition"]["code"]["model_routes"]["classification"]["model"] = "gpt-6-sol"
    files[PATH] = json.dumps(manifest)
    f.revision = f.storage.repo.edit({p: raw.encode() for p, raw in files.items()})
    selection = {"project_id": str(f.project.id), "path": PATH, "revision": f.revision}
    validated = structured(await server.call_tool("validate_workflow_package", selection))
    assert validated["valid"] and validated["policy"] == "managed-code-model-v1"
    active = structured(
        await server.call_tool(
            "activate_workflow_package",
            {
                **selection,
                "expected_revision": None,
                "request_id": str(uuid4()),
            },
        )
    )
    router, calls = sdk_router(f, invalid=invalid, uncertain=uncertain)
    f.runtime.model_router = router
    return server, common, CodeActivities(common=common, model_router=router), active, calls


async def bound_model(f, code, run_id):
    run, workflow, project, spec, _ = await code.selected(run_id)
    run = await f.db.attach_sandbox(
        run_id=run.id,
        sandbox_id="bound-model-test",
        lease_owner=f"{run.id}:code_sandbox",
        expected_head_sha=f.storage.repo.head,
        ephemeral_branch=f"procedures/{run.id}/{run.generation}",
        require_active=True,
    )
    await f.db.mark_run_running(run.id)
    return {"run": run, "workflow": workflow, "spec": spec}


async def test_model_start_needs_funds_and_does_not_acquire_included_policy(billed, monkeypatch):
    f = billed
    server, _, code, active, calls = await prepare(f, monkeypatch, funded=False)
    estimate = structured(
        await server.call_tool(
            "estimate_workflow_run",
            {
                "project_id": str(f.project.id),
                "workflow_id": active["workflow_id"],
                "inputs": {},
            },
        )
    )
    assert estimate["approval_required"] is False and estimate["estimate"]["amount_nanos"] > 0
    with pytest.raises(ToolError, match="Add credits before starting"):
        await start(f, server, active)
    assert not calls
    assert not await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs")
    await code.models.router.close()


async def test_unenrolled_model_runs_fail_closed_and_start_replays_survive_key_removal(
    billed, monkeypatch
):
    f = billed
    server, _, code, active, calls = await prepare(f, monkeypatch)
    await f.db.pool.execute("UPDATE billing_accounts SET run_billing_enabled=false")
    with pytest.raises(ToolError, match="administrator to complete setup"):
        await start(f, server, active)
    assert not await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs")
    await f.db.pool.execute("UPDATE billing_accounts SET run_billing_enabled=true")
    request_id = str(uuid4())
    original = await start(f, server, active, request_id=request_id)
    f.settings.luna_api_key = None
    assert (await start(f, server, active, request_id=request_id))["id"] == original["id"]
    with pytest.raises(ToolError, match="model service is unavailable"):
        await start(f, server, active)
    assert not calls
    await code.models.router.close()


async def test_completed_call_replays_after_loss_and_charges_once(billed, monkeypatch):
    f, compute = billed, ModelCompute(lose_once=True)
    server, common, code, active, calls = await prepare(f, monkeypatch, compute=compute)
    run_id = (await start(f, server, active))["id"]
    with pytest.raises(RuntimeError, match="worker loss"):
        await ActivityEnvironment().run(code.execute, run_id)
    assert len(calls) == 1
    receipt = await f.db.get_effect(f"{run_id}:procedure_artifact_persist")
    assert (
        receipt.result["failure_reason"] == "Code workflow failed during execution (RuntimeError)."
    )
    assert "simulated worker loss" not in json.dumps(receipt.result)
    progress = await f.db.get_run(UUID(run_id))
    assert progress.progress_summary.startswith("Completed 1 managed step")
    assert progress.progress_percent is None
    # Cold service/activity instances recover only from durable Postgres receipts.
    await code.models.router.close()
    fresh_router, again = sdk_router(f)
    recovered = CodeActivities(common=common, model_router=fresh_router)
    await ActivityEnvironment().run(recovered.execute, run_id)
    await recovered.publish(run_id)
    await recovered.project(run_id)
    run = await f.db.get_run(UUID(run_id))
    assert run.status == RunStatus.SUCCEEDED and not again
    assert compute.calls == 2 and "synthetic-code-model-1" in compute.killed
    await f.billing.settle(run.id)
    await f.billing.settle(run.id)
    charge = await f.billing.run_charge(run.id, ACTOR)
    assert charge["charged_usd"] == "0.02"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 1
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM billing_ledger WHERE run_id=$1 AND kind='charge'", run.id
        )
        == 1
    )
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM effect_receipts WHERE operation=$1", OPERATION
        )
        == 1
    )
    await fresh_router.close()


async def test_checkpoint_write_failure_keeps_its_reason_and_reuses_paid_model(billed, monkeypatch):
    f = billed
    server, common, code, active, calls = await prepare(f, monkeypatch)
    run_id = (await start(f, server, active))["id"]
    stage = f.storage.stage_native_output
    monkeypatch.setattr(
        f.storage, "stage_native_output", AsyncMock(side_effect=ConnectionError("private payload"))
    )
    with pytest.raises(ConnectionError):
        await ActivityEnvironment().run(code.execute, run_id)
    receipt = await f.db.get_effect(f"{run_id}:procedure_artifact_persist")
    assert receipt.result["failure_reason"] == (
        "Code workflow failed during saving the result checkpoint (ConnectionError)."
    )
    assert "private payload" not in json.dumps(receipt.result)
    project_failure = AsyncMock()
    monkeypatch.setattr(common, "project_codex_procedure_failure", project_failure)
    await code.failure(run_id)
    assert project_failure.await_args.args[0]["reason"] == receipt.result["failure_reason"]
    monkeypatch.setattr(f.storage, "stage_native_output", stage)
    await ActivityEnvironment().run(code.execute, run_id)
    assert len(calls) == 1
    assert (await f.db.get_effect(f"{run_id}:procedure_artifact_persist")).status == "completed"
    await code.models.router.close()


async def test_step_fingerprint_call_limit_and_active_authority(billed, monkeypatch):
    f = billed
    server, _, code, active, calls = await prepare(f, monkeypatch)
    run_id = (await start(f, server, active))["id"]
    bound = await bound_model(f, code, run_id)
    async with f.db.pool.acquire() as conn:
        original = await code.models.generate(conn=conn, **bound, payload=payload())
        assert original["parsed"] == CLASSIFIED
        with pytest.raises(CodeModelError, match="different request"):
            await code.models.generate(conn=conn, **bound, payload=payload(data=[]))
        with pytest.raises(CodeModelError, match="model-call limit"):
            await code.models.generate(conn=conn, **bound, payload=payload(step="second"))
        # Replays still enforce current membership and the exact sandbox lease.
        with pytest.raises(CodeModelError, match="permission"):
            await code.models.generate(
                conn=conn,
                **{**bound, "run": replace(bound["run"], sandbox_id="stale")},
                payload=payload(),
            )
        await conn.execute(
            "DELETE FROM project_memberships WHERE project_id=$1 AND clerk_user_id=$2",
            f.project.id,
            ACTOR,
        )
        with pytest.raises(CodeModelError, match="permission"):
            await code.models.generate(conn=conn, **bound, payload=payload())
    assert len(calls) == 1
    await code.models.router.close()


@pytest.mark.parametrize("supplier_schema_failure", [True, False])
async def test_validation_failure_preserves_incurred_usage_and_settlement(
    billed, monkeypatch, supplier_schema_failure
):
    f = billed
    compute = ModelCompute(result_invalid=not supplier_schema_failure)
    server, _, code, active, calls = await prepare(
        f, monkeypatch, compute=compute, invalid=supplier_schema_failure
    )
    run_id = (await start(f, server, active))["id"]
    with pytest.raises(Exception, match="validation"):
        await ActivityEnvironment().run(code.execute, run_id)
    await ActivityEnvironment().run(code.failure, run_id)
    run = await f.db.get_run(UUID(run_id))
    assert run.status == RunStatus.FAILED and f.storage.repo.writes == 0
    usage = await f.db.pool.fetchrow(
        "SELECT result FROM effect_receipts WHERE operation='native_model_usage_v1'"
    )
    record = json.loads(usage["result"])
    assert record["outcome"] == (
        "invalid_output" if supplier_schema_failure else "response_received"
    )
    assert record["usage"]["total_tokens"] == 5500 and len(calls) == 1
    await f.billing.settle(run.id)
    assert (await f.billing.run_charge(run.id, ACTOR))["charged_usd"] == "0.02"
    await code.models.router.close()


async def test_uncertain_model_is_not_repurchased_and_remains_unknown(billed, monkeypatch):
    f = billed
    server, _, code, active, calls = await prepare(f, monkeypatch, uncertain=True)
    run_id = (await start(f, server, active))["id"]
    bound = await bound_model(f, code, run_id)
    async with f.db.pool.acquire() as conn:
        for _ in range(2):
            with pytest.raises(CodeModelError, match="unconfirmed"):
                await code.models.generate(conn=conn, **bound, payload=payload())
        with pytest.raises(CodeModelError, match="unconfirmed"):
            await code.models.generate(conn=conn, **bound, payload=payload(step="bypass"))
    assert len(calls) == 1
    row = await f.db.pool.fetchrow("SELECT status,observed_nanos FROM billing_operations")
    assert row["status"] == "pending" and row["observed_nanos"] is None
    await code.models.router.close()


async def test_a_run_tin_includes_calls_its_model_without_a_budget(billed, monkeypatch):
    # Admission includes an approved onboarding setup step: it records billing-included and
    # no budget row, and billing funds its model calls itself. Nothing refuses the call.
    f = billed
    server, _, code, active, calls = await prepare(f, monkeypatch)
    run_id = (await start(f, server, active))["id"]
    bound = await bound_model(f, code, run_id)
    await f.db.pool.execute("DELETE FROM billing_run_budgets WHERE run_id=$1", UUID(run_id))
    await f.db.pool.execute("UPDATE billing_accounts SET reserved_nanos=0")
    included = f"billing-included:{run_id}"
    async with f.db.pool.acquire() as conn:
        await f.db.start_effect(conn, execution_key=included, operation="included_workflow_v1")
        await f.db.complete_effect(
            conn,
            execution_key=included,
            result={"run_id": run_id, "root_run_id": run_id, "reason": "onboarding"},
        )
        response = await code.models.generate(conn=conn, **bound, payload=payload())
    assert response["parsed"] and len(calls) == 1
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0
    await code.models.router.close()


async def test_the_packages_declared_runtime_bounds_a_model_call(billed, monkeypatch):
    # No separate, shorter limit: a call slower than the package may run is cut at that
    # runtime and stays unconfirmed, never bought twice.
    f = billed
    server, _, code, active, calls = await prepare(f, monkeypatch)
    run_id = (await start(f, server, active))["id"]
    bound = await bound_model(f, code, run_id)
    bound["spec"] = replace(bound["spec"], timeout_seconds=0.2)
    generate = code.models.router.generate

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)
        return await generate(*args, **kwargs)

    monkeypatch.setattr(code.models.router, "generate", slow)
    async with f.db.pool.acquire() as conn:
        for _ in range(2):
            with pytest.raises(CodeModelError, match="unconfirmed"):
                await code.models.generate(conn=conn, **bound, payload=payload())
    assert not calls
    await code.models.router.close()


async def test_worker_restart_reuses_completed_model_call(billed, temporal_env, monkeypatch):
    f, compute = billed, ModelCompute(lose_once=True)
    server, common, code, active, calls = await prepare(
        f, monkeypatch, compute=compute, temporal=temporal_env.client
    )

    def worker(activities):
        return Worker(
            temporal_env.client,
            task_queue=f.settings.task_queue,
            workflows=[CodeWorkflow, ProjectCodexExecution],
            activities=[
                common.resolve_codex_project,
                activities.execute,
                activities.publish,
                activities.review,
                activities.approve,
                activities.project,
                activities.failure,
            ],
            graceful_shutdown_timeout=timedelta(seconds=1),
        )

    async with worker(code):
        run_id = (await start(f, server, active))["id"]
        await asyncio.wait_for(compute.saved.wait(), 30)
    await code.models.router.close()
    router, new_calls = sdk_router(f)
    fresh = CodeActivities(common=common, model_router=router)
    async with worker(fresh):
        run = await f.db.get_run(UUID(run_id))
        handle = temporal_env.client.get_workflow_handle(run.temporal_workflow_id)
        await asyncio.wait_for(handle.result(), 60)
        await Replayer(workflows=[CodeWorkflow, ProjectCodexExecution]).replay_workflow(
            await handle.fetch_history()
        )
    assert len(calls) == 1 and not new_calls and compute.calls == 2
    run = await f.db.get_run(UUID(run_id))
    assert run.status == RunStatus.SUCCEEDED
    await f.billing.settle(run.id)
    assert (await f.billing.run_charge(run.id, ACTOR))["charged_usd"] == "0.02"
    await router.close()


@asynccontextmanager
async def http_mcp(f, monkeypatch):
    """Actual MCP HTTP/auth middleware; only Clerk token verification is synthetic."""
    from mcp.server.auth.middleware.auth_context import get_access_token

    from tin_lite.auth import AuthContext
    from tin_lite.mcp_server import create_mcp_app

    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", get_access_token)

    class TestAuth:
        async def authenticate_oauth_token(self, token):
            if token != "slice-b-local-identity":  # noqa: S105 — synthetic local identity
                return None
            return AuthContext(
                clerk_user_id=ACTOR,
                token_type="oauth_token",  # noqa: S106 — token category
                scopes=frozenset({"openid"}),
                client_id="slice-b-proof",
                resource=f"{f.settings.switchboard_public_url.rstrip('/')}/mcp",
            )

    _, app = create_mcp_app(settings=f.settings, auth=TestAuth(), runtime=lambda: f.runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://tin.test",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer slice-b-local-identity",
            },
        ) as client,
    ):

        class Wire:
            next_id = 0

            async def call_tool(self, name, arguments):
                self.next_id += 1
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": self.next_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                assert response.status_code == 200
                result = response.json()["result"]
                if result.get("isError"):
                    raise ToolError(str(result["content"]))
                return SimpleNamespace(structured_content=result["structuredContent"])

        yield Wire()


@pytest.mark.skipif(
    os.environ.get("TIN_LITE_CODE_MODEL_LIVE_PROOF") != "1",
    reason="opt-in real E2B/code.storage and one paid server-held model call",
)
@pytest.mark.parametrize("real_supplier", [False, True])
async def test_live_model_client_recovers_after_worker_restart(
    billed, temporal_env, monkeypatch, tmp_path, real_supplier
):
    """Local DB/Temporal and test OAuth/funding; real E2B/storage, optional real model."""
    from dotenv import dotenv_values

    from tin_lite.code_storage import CodeStorage
    from tin_lite.e2b_runtime import E2BRuntime

    f = billed
    env = dotenv_values(".env")
    await fund(f)
    storage = CodeStorage(
        organization=env.get("TIN_LITE_CODE_STORAGE_ORG") or "tin",
        private_key=env["CODE_STORAGE_API_KEY"],
    )
    repo_id = f"projects/{f.project.id}"
    repo = await storage.ensure_repo(repo_id)
    await f.db.pool.execute(
        "UPDATE projects SET state_repo_id=$2 WHERE id=$1", f.project.id, repo_id
    )
    f.project = await f.db.get_project(f.project.id)

    class InterruptedCompute(E2BRuntime):
        saved = asyncio.Event()
        model_callbacks = 0
        sandbox_ids = []

        async def run_code_and_kill(self, *, model_call, **kwargs):
            self.sandbox_ids.append(kwargs["sandbox_id"])

            async def lose_after_save(payload):
                response = await model_call(payload)
                self.model_callbacks += 1
                if self.model_callbacks == 1:
                    self.saved.set()
                    raise RuntimeError("simulated loss before delivering the saved model result")
                return response

            return await super().run_code_and_kill(model_call=lose_after_save, **kwargs)

    compute = InterruptedCompute(
        api_key=env["E2B_API_KEY"],
        template="tin-lite-codex",
        isolated_template=env.get("TIN_LITE_E2B_ISOLATED_TEMPLATE") or "tin-lite-codex-isolated",
        timeout_seconds=60,
        egress_allow_hosts=(),
        usage_database=f.db,
    )
    _, common, _ = await setup(
        f, monkeypatch, temporal=temporal_env.client, compute=compute, storage=storage
    )
    if real_supplier:
        key = env["TIN_LITE_LUNA_API_KEY"]
        calls = []

        async def count_request(request):
            calls.append(request.url.path)

        client = AsyncOpenAI(
            api_key=key,
            max_retries=0,
            timeout=25,
            http_client=httpx.AsyncClient(event_hooks={"request": [count_request]}),
        )
        router = ModelRouter(
            providers={ProviderName.OPENAI: OpenAIModelProvider(api_key=key, client=client)},
            routes=registered_routes(),
            recorder=ModelUsageRecorder(f.db),
        )
    else:
        key = "fixture-server-key"
        router, calls = sdk_router(f)
    f.settings.luna_api_key = SecretStr(key)
    f.runtime.model_router = router
    code = CodeActivities(common=common, model_router=router)

    def worker(activities):
        return Worker(
            temporal_env.client,
            task_queue=f.settings.task_queue,
            workflows=[CodeWorkflow, ProjectCodexExecution],
            activities=[
                common.resolve_codex_project,
                activities.execute,
                activities.publish,
                activities.review,
                activities.approve,
                activities.project,
                activities.failure,
            ],
            graceful_shutdown_timeout=timedelta(seconds=1),
        )

    async with http_mcp(f, monkeypatch) as wire:
        files = example_files(KEY, model_steps=True)
        # Prove the model client does not open direct network or credential access.
        main = f"workflow_packages/{KEY}/main.py"
        files[main] = files[main].replace(
            "async def run(ctx, inputs):",
            """async def run(ctx, inputs):
    import os
    import socket
    assert not any(k in os.environ for k in (
        'OPENAI_API_KEY', 'TIN_LITE_LUNA_API_KEY', 'E2B_API_KEY', 'CODE_STORAGE_API_KEY'))
    try:
        Path('/root/tin-code/packet.json').read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError('controller files readable')
    with socket.socket() as direct:
        direct.settimeout(1)
        assert direct.connect_ex(('1.1.1.1', 443)) != 0
    print('TIN_MODEL_REQUEST')  # Untrusted stdout cannot invoke the control channel.
""",
        )
        committed = structured(
            await wire.call_tool(
                "commit_project_changes",
                {
                    "project_id": str(f.project.id),
                    "expected_revision": await storage.head_sha(repo, "main"),
                    "request_id": str(uuid4()),
                    "message": "Slice B managed model recovery proof",
                    "changes": [
                        {"operation": "upsert", "path": path, "content": raw}
                        for path, raw in files.items()
                    ],
                },
            )
        )
        selection = {
            "project_id": str(f.project.id),
            "path": PATH,
            "revision": committed["revision"],
        }
        validated = structured(await wire.call_tool("validate_workflow_package", selection))
        assert validated["valid"] and validated["runtime_available"]
        active = structured(
            await wire.call_tool(
                "activate_workflow_package",
                {**selection, "expected_revision": None, "request_id": str(uuid4())},
            )
        )
        async with worker(code):
            run_id = (await start(f, wire, active))["id"]
            await asyncio.wait_for(compute.saved.wait(), 100)
        await router.close()
        # No provider is configured in the replacement process. Only the saved result
        # can satisfy this call; there is no SDK client available to buy it twice.
        cold_router = ModelRouter(providers={}, routes=())
        fresh = CodeActivities(common=common, model_router=cold_router)
        async with worker(fresh):
            run = await f.db.get_run(UUID(run_id))
            handle = temporal_env.client.get_workflow_handle(run.temporal_workflow_id)
            await asyncio.wait_for(handle.result(), 100)
            history = await handle.fetch_history()
            await Replayer(workflows=[CodeWorkflow, ProjectCodexExecution]).replay_workflow(history)
        run = await f.db.get_run(UUID(run_id))
        assert run.status == RunStatus.SUCCEEDED
        assert len(calls) == 1 and compute.model_callbacks == 2
        assert len(set(compute.sandbox_ids)) == 2
        for sandbox_id in compute.sandbox_ids:
            assert not await compute.is_running(sandbox_id)
        output = structured(await wire.call_tool("read_run_output", {"run_id": run_id}))
        assert "order-2 | 1500 | medium" in output["content"]
        assert "order-3 | 2400 | large" in output["content"]
        assert (
            await storage.read_canonical_artifact(
                repo_id=repo_id, commit_sha=run.canonical_commit_sha, path=OUTPUT
            )
            == output["content"].encode()
        )
        await f.billing.settle(run.id)
        charge = await f.billing.run_charge(run.id, ACTOR)
        operations = await f.db.pool.fetch("SELECT status,observed_nanos FROM billing_operations")
        assert len(operations) == 1 and operations[0]["observed_nanos"] > 0
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM effect_receipts WHERE operation=$1 AND status='completed'",
                OPERATION,
            )
            == 1
        )
        proof = {
            "run_id": run_id,
            "repo_id": repo_id,
            "artifact_revision": run.canonical_commit_sha,
            "artifact_path": OUTPUT,
            "sandboxes_deleted": compute.sandbox_ids,
            "supplier": "real OpenAI" if real_supplier else "SDK HTTP fixture",
            "supplier_calls": len(calls),
            "model_callbacks": compute.model_callbacks,
            "observed_nanos": operations[0]["observed_nanos"],
            "charge": charge,
            "mcp": "HTTP with synthetic OAuth identity",
            "temporal": "local; worker restarted with no provider; history replay passed",
            "postgres_and_funding": "disposable local schema and Stripe wire fixture",
        }
        await asyncio.to_thread(
            (tmp_path / "slice-b-proof.json").write_text, json.dumps(proof, indent=2)
        )
        print(json.dumps(proof, indent=2))
