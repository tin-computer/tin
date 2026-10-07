"""Code-only contracts and real DB/Temporal/MCP flow with synthetic compute/storage.

The opt-in live proof substitutes real E2B and code.storage, keeping all product DB
state in a disposable local schema. Clerk identity remains a test fixture.
"""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer, Worker
from test_billing import billed as billed
from test_private_workflows import ACTOR, fixture, mcp, structured
from test_procedure_publication import HistoryStorage
from test_procedure_publication import publication_db as publication_db
from test_project_codex_execution import temporal_env as temporal_env

from tin_lite.activities import TinActivities
from tin_lite.code_activities import CodeActivities
from tin_lite.code_storage import CodeStorage
from tin_lite.codex_execution import ProjectCodexExecution
from tin_lite.domain import RunStatus
from tin_lite.e2b_runtime import E2BRuntime
from tin_lite.private_workflows import validate_private_definition
from tin_lite.project_files import ProjectFileService
from tin_lite.publication import read_run_output
from tin_lite.workflow_code import (
    example_files,
    load_code_package,
    validate_code_definition,
    validate_code_resources,
    validate_code_result,
)
from tin_lite.workflow_packages import decode_workflow_source, export_workflow_package
from tin_lite.workflows import CodeWorkflow

KEY = "custom.order_report"
PATH = f"workflow_packages/{KEY}/workflow.json"
OUTPUT = "reports/custom/ORDER_REPORT.md"


def definition():
    return json.loads(example_files()[PATH])["definition"]


async def test_tin_owned_and_private_code_share_one_package_contract():
    storage = HistoryStorage()
    results = []
    for key in ["example.order_report", KEY]:
        files = {p: raw.encode() for p, raw in example_files(key).items()}
        path = f"workflow_packages/{key}/workflow.json"
        revision = storage.repo.edit(files)
        body, spec, resources = await load_code_package(
            storage=storage, repo_id=storage.repo.id, commit_sha=revision, definition_path=path
        )
        exported = export_workflow_package(body, resources)
        assert decode_workflow_source(exported[path], definition_path=path).definition == body
        if key == KEY:
            validate_private_definition(body)
        results.append((spec, resources))
    assert results[0] == results[1]


def test_code_contract_admits_the_raised_runaway_guards():
    body = definition()
    body["code"]["timeout_seconds"] = 900
    body["code"]["output"]["max_bytes"] = 1_000_000
    body["code"]["model_routes"] = {
        name: {
            "provider": "openai",
            "model": "gpt-6-luna",
            "max_calls": 16,
            "max_input_bytes": 256_000,
            "max_output_tokens": 32_000,
        }
        for name in ("first", "second")
    }
    spec = validate_code_definition(body)
    assert spec.timeout_seconds == 900 and spec.max_bytes == 1_000_000
    assert sum(route.max_calls for route in spec.model_routes) == 32
    body["code"]["model_routes"]["third"] = body["code"]["model_routes"]["first"]
    with pytest.raises(ValueError, match="at most 32 managed model calls"):
        validate_code_definition(body)


def test_larger_code_limits_fit_through_the_sandbox_bridge():
    import re
    from importlib.util import find_spec
    from pathlib import Path

    from tin_lite import code_models, code_project_files, workflow_code
    from tin_lite.e2b_runtime import CODE_BRIDGE_MAX_BYTES

    # code_runner imports Unix-only modules, so read its constants without importing it.
    source = Path(find_spec("tin_lite.code_runner").origin).read_text(encoding="utf-8")
    rpc = int(re.search(r"\nMAX_RPC = ([\d_]+)\n", source)[1])
    result = re.search(r"\nMAX_RESULT = ([\d_]+) \* 6 \+ 2048\n", source)
    assert rpc == CODE_BRIDGE_MAX_BYTES
    # validate_code_result's envelope bound for the largest declared output.
    assert int(result[1]) == workflow_code.MAX_OUTPUT_BYTES
    # A whole-limit file read crosses as base64, a model response as JSON of its own size.
    assert -(-code_project_files.MAX_FILE_BYTES // 3) * 4 + 64 < rpc
    assert code_models.MAX_RESPONSE_BYTES + 64 < rpc
    # The worker's 256 MiB address space comfortably holds a whole message and result.
    assert rpc + int(result[1]) * 6 + 2048 < 64 * 1024 * 1024
    assert "\nFILE_CALLS = 256\n" in source and "\nSERVICE_CALLS = 128\n" in source


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["code"].update(timeout_seconds=901),
        lambda d: d["code"].update(runtime="arbitrary-python"),
        lambda d: d["code"].update(entrypoint="../main.py"),
        lambda d: d["code"].update(files=["main.py", "main.py"]),
        lambda d: d["code"].update(model_routes=["free-looking-route"]),
        lambda d: d["code"]["output"].update(path="wiki/INDEX.md"),
        lambda d: d["code"]["output"].update(path="workflow_packages/custom.other/workflow.json"),
        lambda d: d["code"]["output"].update(max_bytes=1_000_001),
        lambda d: d.update(schedule_modes=["daily"]),
        lambda d: d.update(integration_requirements=[{"provider_key": "undeclared"}]),
        lambda d: d.update(procedure={}),
        lambda d: d.update(billing={"free": True}),
    ],
)
def test_code_contract_rejects_extra_authority(change):
    value = definition()
    change(value)
    with pytest.raises(ValueError):
        validate_private_definition(value)


def test_source_is_parsed_without_execution_and_resources_are_bounded(tmp_path):
    marker = tmp_path / "must-not-exist"
    value = definition()
    value["code"]["files"] = ["main.py"]
    spec = validate_code_definition(value)
    raw = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
    validate_code_resources(spec, {"main.py": raw})
    assert not marker.exists()
    for raw in [b"if:", b"\xff", b" " * 64001]:
        with pytest.raises(ValueError):
            validate_code_resources(spec, {"main.py": raw})


@pytest.mark.parametrize(
    "result",
    [
        {"path": "../outside", "content": "text"},
        {"path": OUTPUT, "content": ""},
        {"path": OUTPUT, "content": "é" * 4001},
        {"path": OUTPUT, "content": "a\x00b"},
        {"path": OUTPUT, "content": ["not", "text"]},
        {"path": OUTPUT, "content": "valid", "billing": "free"},
    ],
)
def test_result_validation(result):
    with pytest.raises(ValueError):
        validate_code_result(json.dumps(result).encode(), validate_code_definition(definition()))


class CheckpointStorage(HistoryStorage):
    async def stage_native_output(self, *, content, path, **kwargs):
        head = self.repo.head
        revision = self.repo.edit({path: content})
        self.repo.head = head
        self.branch_revision = revision
        return revision


class SyntheticCompute:
    def __init__(self, result=None):
        self.calls = 0
        self.alive = False
        self.result = result or {"path": OUTPUT, "content": "# Fixture report\nTotal: 3900 cents\n"}

    async def create(self, **kwargs):
        assert kwargs["code_only"] and kwargs["profile"].isolated
        self.alive = True
        return "synthetic-code-sandbox"

    async def run_code_and_kill(self, *, packet, **kwargs):
        assert set(packet) == {
            "files",
            "entrypoint",
            "timeout_seconds",
            "context",
            "inputs",
            "model_client",
        }
        assert packet["model_client"] is True
        self.calls += 1
        self.alive = False
        return json.dumps(self.result).encode()

    async def kill(self, sandbox_id):
        self.alive = False


async def setup(f, monkeypatch, *, temporal=None, compute=None, storage=None):
    storage = storage or CheckpointStorage()
    f.storage = f.runtime.storage = storage
    f.runtime.project_files = ProjectFileService(database=f.db, storage=storage)
    if temporal:
        f.runtime.temporal = temporal
    f.runtime.sandboxes = compute or SyntheticCompute()
    if isinstance(storage, HistoryStorage):
        f.revision = storage.repo.edit({p: raw.encode() for p, raw in example_files().items()})
    server = mcp(f, monkeypatch)
    common = TinActivities(
        database=f.db, storage=storage, sandboxes=f.runtime.sandboxes, settings=f.settings
    )
    return server, common, CodeActivities(common=common)


async def activate_code(f, server, *, revision=None):
    selection = {"project_id": str(f.project.id), "path": PATH, "revision": revision or f.revision}
    validated = structured(await server.call_tool("validate_workflow_package", selection))
    assert validated["valid"], validated
    assert validated["policy"] == "bounded-code-v1"
    active = structured(
        await server.call_tool(
            "activate_workflow_package",
            {**selection, "expected_revision": None, "request_id": str(uuid4())},
        )
    )
    return active


async def start(f, server, active, *, inputs=None, request_id=None):
    return structured(
        await server.call_tool(
            "start_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": active["workflow_id"],
                "request_id": request_id or str(uuid4()),
                "inputs": inputs or {"minimum_cents": 1000},
            },
        )
    )


async def test_mcp_code_zero_balance_and_duplicate_publication(billed, monkeypatch):
    f = billed
    server, _common, code = await setup(f, monkeypatch)
    active = await activate_code(f, server)
    with pytest.raises(ToolError):
        await start(f, server, active, inputs={"minimum_cents": -1})
    request_id = str(uuid4())
    first = await start(f, server, active, request_id=request_id)
    again = await start(f, server, active, request_id=request_id)
    assert first["id"] == again["id"]
    run_id = first["id"]
    env = ActivityEnvironment()
    await env.run(code.execute, run_id)
    await env.run(code.execute, run_id)
    await asyncio.gather(env.run(code.publish, run_id), env.run(code.publish, run_id))
    assert not await code.review(run_id)
    await code.project(run_id)
    await code.project(run_id)
    run = await f.db.get_run(UUID(run_id))
    assert run.status == RunStatus.SUCCEEDED and not f.runtime.sandboxes.alive
    assert f.runtime.sandboxes.calls == 1 and f.storage.repo.writes == 1
    output = await read_run_output(storage=f.storage, run=run, repo_id=f.project.state_repo_id)
    assert b"3900" in output.content
    charge = await f.billing.run_charge(run.id, ACTOR)
    assert charge["charged_usd"] == "0.00" and charge["reason"] == "bounded-code-v1"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 0
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0
    assert await f.db.pool.fetchval("SELECT balance_nanos FROM billing_accounts") == 0
    events = await f.db.list_product_activity(project_id=f.project.id)
    assert sum(e.event_type == "code_workflow_ready" for e in events) == 1


async def test_unknown_temporal_start_of_included_code_run_recovers_same_id(billed, monkeypatch):
    from temporalio.common import WorkflowIDReusePolicy

    from tin_lite.billing_recovery import recover_dispatches

    f = billed
    server, _common, _code = await setup(f, monkeypatch)
    active = await activate_code(f, server)
    f.runtime.temporal.start_workflow.side_effect = TimeoutError()
    for _ in range(2):
        with pytest.raises(ToolError, match="unconfirmed"):
            await start(f, server, active)
    run, child = [
        await f.db.get_run(row["id"])
        for row in await f.db.pool.fetch(
            "SELECT id FROM workflow_runs WHERE executor='workflow.code' ORDER BY created_at"
        )
    ]
    assert run.status == child.status == RunStatus.PENDING
    # A run prepared under a billing parent is dispatched by that parent, never recovered.
    await f.db.pool.execute(
        """UPDATE effect_receipts SET result=jsonb_set(result,'{parent_run_id}',to_jsonb($2::text))
           WHERE execution_key=$1""",
        f"billing-included:{child.id}",
        str(run.id),
    )
    await f.db.pool.execute(
        "UPDATE workflow_runs SET created_at=now()-interval '1 minute' WHERE id=ANY($1::uuid[])",
        [run.id, child.id],
    )
    f.runtime.temporal.start_workflow.side_effect = None
    f.runtime.temporal.start_workflow.reset_mock()
    await recover_dispatches(f.runtime, f.settings)
    assert f.runtime.temporal.start_workflow.await_count == 1
    kwargs = f.runtime.temporal.start_workflow.call_args.kwargs
    assert kwargs["id"] == run.temporal_workflow_id
    assert kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 0


async def test_invalid_output_never_publishes(publication_db, monkeypatch):
    f = await fixture(publication_db)
    compute = SyntheticCompute({"path": OUTPUT, "content": "x" * 8001})
    server, _common, code = await setup(f, monkeypatch, compute=compute)
    active = await activate_code(f, server)
    run_id = (await start(f, server, active))["id"]
    with pytest.raises(Exception, match="validation"):
        await ActivityEnvironment().run(code.execute, run_id)
    assert not compute.alive and f.storage.repo.writes == 0
    assert (await f.db.get_effect(f"{run_id}:procedure_artifact_persist")).status == "failed"


async def test_code_stop_fences_compute_and_retry(publication_db, monkeypatch):
    f = await fixture(publication_db)
    server, _common, code = await setup(f, monkeypatch)
    f.runtime.temporal.get_workflow_handle = lambda _: SimpleNamespace(cancel=AsyncMock())
    active = await activate_code(f, server)
    run_id = (await start(f, server, active))["id"]
    stopped = structured(await server.call_tool("stop_procedure", {"run_id": run_id}))
    assert stopped["status"] == "stopped"
    with pytest.raises(Exception, match="validation"):
        await ActivityEnvironment().run(code.execute, run_id)
    assert f.runtime.sandboxes.calls == 0 and f.storage.repo.writes == 0


async def test_code_temporal_flow_and_replay(publication_db, temporal_env, monkeypatch):
    f = await fixture(publication_db)
    server, common, code = await setup(f, monkeypatch, temporal=temporal_env.client)
    active = await activate_code(f, server)
    async with Worker(
        temporal_env.client,
        task_queue=f.settings.task_queue,
        workflows=[CodeWorkflow, ProjectCodexExecution],
        activities=[
            common.resolve_codex_project,
            code.execute,
            code.publish,
            code.review,
            code.approve,
            code.project,
            code.failure,
        ],
    ):
        run_id = (await start(f, server, active))["id"]
        run = await f.db.get_run(UUID(run_id))
        handle = temporal_env.client.get_workflow_handle(run.temporal_workflow_id)
        await asyncio.wait_for(handle.result(), 30)
        history = await handle.fetch_history()
        await Replayer(workflows=[CodeWorkflow, ProjectCodexExecution]).replay_workflow(history)
    assert (await f.db.get_run(UUID(run_id))).status == RunStatus.SUCCEEDED
    # Authored files and fixture records do not enter history.
    history_json = history.to_json()
    assert "amount_cents" not in history_json and "minimum_cents" not in history_json


@pytest.mark.skipif(
    os.environ.get("TIN_LITE_CODE_LIVE_PROOF") != "1",
    reason="opt-in real E2B/code.storage proof; uses a new isolated test repository",
)
async def test_live_code_mcp_to_durable_artifact(billed, temporal_env, monkeypatch, tmp_path):
    """Real HTTP MCP, local Postgres/Temporal, real E2B/storage; synthetic OAuth verifier."""
    import httpx
    from dotenv import dotenv_values

    from tin_lite.auth import AuthContext
    from tin_lite.mcp_server import create_mcp_app

    f = billed
    env = dotenv_values(".env")
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
    compute = E2BRuntime(
        api_key=env["E2B_API_KEY"],
        template="tin-lite-codex",
        isolated_template=env.get("TIN_LITE_E2B_ISOLATED_TEMPLATE") or "tin-lite-codex-isolated",
        timeout_seconds=60,
        egress_allow_hosts=(),
        usage_database=f.db,
    )
    _server, common, code = await setup(
        f, monkeypatch, temporal=temporal_env.client, compute=compute, storage=storage
    )
    # Undo setup's direct-dispatch identity seam. HTTP must authenticate at the MCP boundary.
    from mcp.server.auth.middleware.auth_context import get_access_token

    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", get_access_token)

    class TestAuth:
        async def authenticate_oauth_token(self, token):
            if token != "slice-a-test-identity":  # noqa: S105 — local test identity
                return None
            return AuthContext(
                clerk_user_id=ACTOR,
                token_type="oauth_token",  # noqa: S106 — token category
                scopes=frozenset({"openid"}),
                client_id="slice-a-local-proof",
                resource=f"{f.settings.switchboard_public_url.rstrip('/')}/mcp",
            )

    server, app = create_mcp_app(settings=f.settings, auth=TestAuth(), runtime=lambda: f.runtime)
    next_id = 0
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://tin.test",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer slice-a-test-identity",
            },
            timeout=60,
        ) as client,
    ):

        class Wire:
            async def call_tool(self, name, arguments):
                nonlocal next_id
                next_id += 1
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": next_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                assert response.status_code == 200, response.text
                result = response.json()["result"]
                if result.get("isError"):
                    raise ToolError(str(result["content"]))
                return SimpleNamespace(structured_content=result["structuredContent"])

        wire = Wire()
        init = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "slice-a-proof", "version": "1"},
                },
            },
        )
        assert init.status_code == 200
        files = example_files()
        committed = structured(
            await wire.call_tool(
                "commit_project_changes",
                {
                    "project_id": str(f.project.id),
                    "expected_revision": await storage.head_sha(repo, "main"),
                    "request_id": str(uuid4()),
                    "message": "Slice A code-only fixture package",
                    "changes": [
                        {"operation": "upsert", "path": p, "content": raw}
                        for p, raw in files.items()
                    ],
                },
            )
        )
        active = await activate_code(f, wire, revision=committed["revision"])
        with pytest.raises(ToolError):
            await start(f, wire, active, inputs={"minimum_cents": -1})
        async with Worker(
            temporal_env.client,
            task_queue=f.settings.task_queue,
            workflows=[CodeWorkflow, ProjectCodexExecution],
            activities=[
                common.resolve_codex_project,
                code.execute,
                code.publish,
                code.review,
                code.approve,
                code.project,
                code.failure,
            ],
        ):
            request_id = str(uuid4())
            run_id = (await start(f, wire, active, request_id=request_id))["id"]
            assert (await start(f, wire, active, request_id=request_id))["id"] == run_id
            run = await f.db.get_run(UUID(run_id))
            handle = temporal_env.client.get_workflow_handle(run.temporal_workflow_id)
            await asyncio.wait_for(handle.result(), 180)
            history = await handle.fetch_history()
            await Replayer(workflows=[CodeWorkflow, ProjectCodexExecution]).replay_workflow(history)
        run = await f.db.get_run(UUID(run_id))
        assert run.status == RunStatus.SUCCEEDED
        assert not await compute.is_running(run.sandbox_id)
        output = structured(await wire.call_tool("read_run_output", {"run_id": run_id}))
        assert "3900 cents" in output["content"] and "Selected orders: 2" in output["content"]
        assert (
            await storage.read_canonical_artifact(
                repo_id=repo_id, commit_sha=run.canonical_commit_sha, path=OUTPUT
            )
            == output["content"].encode()
        )
        # Re-executing completed trusted stages cannot create another artifact/model call.
        await ActivityEnvironment().run(code.execute, run_id)
        await code.publish(run_id)
        await code.project(run_id)
        assert await storage.head_sha(repo, "main") == run.canonical_commit_sha
        charge = await f.billing.run_charge(run.id, ACTOR)
        assert charge["charged_usd"] == "0.00"
        assert await f.db.pool.fetchval("SELECT balance_nanos FROM billing_accounts") == 0
        assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0
        assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 0
        operations = await f.db.pool.fetch("SELECT operation FROM effect_receipts")
        assert not any("model" in r["operation"] or "codex" in r["operation"] for r in operations)
        proof = {
            "run_id": run_id,
            "sandbox_id": run.sandbox_id,
            "sandbox_deleted": True,
            "repo_id": repo_id,
            "definition_revision": committed["revision"],
            "artifact_revision": run.canonical_commit_sha,
            "artifact_path": OUTPUT,
            "charge": charge,
            "mcp": "HTTP with test OAuth identity",
            "temporal": "local dev server; history replay passed",
            "postgres": "local disposable schema",
        }
        proof_path = tmp_path / "slice-a-proof.json"
        proof_path.parent.mkdir(exist_ok=True)
        await asyncio.to_thread(proof_path.write_text, json.dumps(proof, indent=2))
        await asyncio.to_thread(
            proof_path.with_name("code-workflow-history.json").write_text, history.to_json()
        )
        print(json.dumps(proof, indent=2))


async def test_code_review_contract_and_immutable_files(publication_db, monkeypatch):
    f = await fixture(publication_db)
    server, _common, code = await setup(f, monkeypatch)
    manifest = json.loads(f.storage.repo.trees[f.revision][PATH][1])
    manifest["definition"]["human_review"] = {"eligible": True, "reason": "Review the report."}
    f.revision = f.storage.repo.edit({PATH: json.dumps(manifest).encode()})
    active = await activate_code(f, server)
    run_id = (await start(f, server, active))["id"]
    # A later edit to the same source does not change an admitted run's source or review.
    f.storage.repo.edit({f"workflow_packages/{KEY}/main.py": b'raise RuntimeError("new version")'})
    pinned = await code.selected(run_id)
    assert b"new version" not in pinned[4]["main.py"]
    await ActivityEnvironment().run(code.execute, run_id)
    await code.publish(run_id)
    assert await code.review(run_id)
    run = await f.db.get_run(UUID(run_id))
    assert run.status == RunStatus.NEEDS_INPUT
    assert (
        await read_run_output(storage=f.storage, run=run, repo_id=f.project.state_repo_id)
    ).content
    with pytest.raises(RuntimeError, match="review"):
        await code.project(run_id)
    await code.approve(run_id)
    await code.project(run_id)
    assert (await f.db.get_run(UUID(run_id))).status == RunStatus.SUCCEEDED


async def test_code_stale_lease_and_conflicting_output_are_retained(publication_db, monkeypatch):
    f = await fixture(publication_db)
    server, _common, code = await setup(f, monkeypatch)
    active = await activate_code(f, server)
    run_id = (await start(f, server, active))["id"]
    await ActivityEnvironment().run(code.execute, run_id)
    f.storage.repo.edit({OUTPUT: b"Founder version\n"})
    with pytest.raises(Exception, match="file changed"):
        await code.publish(run_id)
    await code.failure(run_id)
    run = await f.db.get_run(UUID(run_id))
    assert run.status == RunStatus.FAILED and run.retained_output["reason"] == "output_conflict"
    retained = await read_run_output(
        storage=f.storage, run=run, repo_id=f.project.state_repo_id, source="retained"
    )
    assert b"3900" in retained.content and f.storage.repo.writes == 0
    assert f.storage.repo.trees[f.storage.repo.head][OUTPUT][1] == b"Founder version\n"


async def test_stop_during_code_execution_kills_and_fences_result(publication_db, monkeypatch):
    class WaitingCompute(SyntheticCompute):
        def __init__(self):
            super().__init__()
            self.started, self.stopped = asyncio.Event(), asyncio.Event()

        async def run_code_and_kill(self, **kwargs):
            self.started.set()
            await self.stopped.wait()
            return await super().run_code_and_kill(**kwargs)

        async def kill(self, sandbox_id):
            self.stopped.set()
            await super().kill(sandbox_id)

    f = await fixture(publication_db)
    compute = WaitingCompute()
    server, _common, code = await setup(f, monkeypatch, compute=compute)
    f.runtime.temporal.get_workflow_handle = lambda _: SimpleNamespace(cancel=AsyncMock())
    active = await activate_code(f, server)
    run_id = (await start(f, server, active))["id"]
    task = asyncio.create_task(ActivityEnvironment().run(code.execute, run_id))
    await asyncio.wait_for(compute.started.wait(), 5)
    stopped = structured(await server.call_tool("stop_procedure", {"run_id": run_id}))
    assert stopped["status"] == "stopped"
    with pytest.raises(Exception, match="lease|active"):
        await task
    assert not compute.alive and f.storage.repo.writes == 0
    assert (await f.db.get_run(UUID(run_id))).status == RunStatus.STOPPED


@pytest.mark.skipif(
    os.environ.get("TIN_LITE_CODE_LIVE_PROOF") != "1", reason="opt-in real E2B test"
)
@pytest.mark.parametrize("scenario", ["isolation", "symlink", "timeout"])
async def test_live_code_runtime_bounds(scenario):
    from dotenv import dotenv_values

    from tin_lite.procedures import SandboxProfile

    env = dotenv_values(".env")
    compute = E2BRuntime(
        api_key=env["E2B_API_KEY"],
        template="tin-lite-codex",
        isolated_template=env.get("TIN_LITE_E2B_ISOLATED_TEMPLATE") or "tin-lite-codex-isolated",
        timeout_seconds=60,
        egress_allow_hosts=(),
    )
    sources = {
        "isolation": """import os, socket, subprocess, sys
from pathlib import Path

def run(ctx, inputs):
    assert os.getuid() != 0
    assert set(os.environ) <= {'PATH', 'LANG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0',
                               'GIT_CONFIG_VALUE_0', 'GIT_CONFIG_KEY_1', 'GIT_CONFIG_VALUE_1'}
    try:
        Path('/root/tin-code/packet.json').read_bytes()
        raise AssertionError('controller was readable')
    except PermissionError:
        pass
    for host in ['1.1.1.1', '169.254.169.254', '127.0.0.1']:
        try:
            socket.create_connection((host, 80), timeout=1)
            raise AssertionError('network was available')
        except OSError:
            pass
    # Detached descendants share the worker UID and must be killed before result reads.
    subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(120)'],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {'path':'reports/custom/ORDER_REPORT.md', 'content':'Isolation checks passed.'}
""",
        "symlink": """from pathlib import Path

def run(ctx, inputs):
    Path('unsafe').symlink_to('/root/tin-code/packet.json')
    return {'path':'reports/custom/ORDER_REPORT.md', 'content':'must not escape'}
""",
        "timeout": """def run(ctx, inputs):
    while True:
        pass
""",
    }
    probe = f"slice-a-{scenario}-{uuid4()}"
    sandbox_id = await compute.create(
        execution_key=probe,
        run_id=probe,
        profile=SandboxProfile(profile="isolated", timeout_seconds=60),
        code_only=True,
    )
    try:
        call = compute.run_code_and_kill(
            sandbox_id=sandbox_id,
            packet={
                "files": {"main.py": sources[scenario]},
                "entrypoint": "main.py",
                "timeout_seconds": 1 if scenario == "timeout" else 10,
                "context": {"run_id": probe, "created_at": "2026-09-15T00:00:00Z"},
                "inputs": {},
            },
        )
        if scenario == "isolation":
            raw = await call
            assert (
                validate_code_result(raw, validate_code_definition(definition()))
                == b"Isolation checks passed."
            )
        else:
            with pytest.raises(RuntimeError, match="execution limits"):
                await call
        assert not await compute.is_running(sandbox_id)
    finally:
        await compute.kill(sandbox_id)


async def test_checkpoint_recovers_without_recomputing_after_worker_loss(
    publication_db, monkeypatch
):
    f = await fixture(publication_db)
    server, _common, code = await setup(f, monkeypatch)
    active = await activate_code(f, server)
    run_id = (await start(f, server, active))["id"]
    complete = f.db.complete_procedure_persist

    async def lost_completion(*args, **kwargs):
        raise RuntimeError("synthetic worker loss after storage checkpoint")

    monkeypatch.setattr(f.db, "complete_procedure_persist", lost_completion)
    with pytest.raises(RuntimeError, match="worker loss"):
        await ActivityEnvironment().run(code.execute, run_id)
    assert f.storage.branch_revision and f.runtime.sandboxes.calls == 1
    monkeypatch.setattr(f.db, "complete_procedure_persist", complete)
    await ActivityEnvironment().run(code.execute, run_id)
    assert f.runtime.sandboxes.calls == 1
    # A successful canonical commit whose reply is lost reconciles before any second write.
    f.storage.repo.lose_response = True
    with pytest.raises(RuntimeError, match="reconciliation"):
        await code.publish(run_id)
    assert f.storage.repo.writes == 1
    await code.publish(run_id)
    await code.project(run_id)
    assert f.storage.repo.writes == 1
    assert (await f.db.get_run(UUID(run_id))).status == RunStatus.SUCCEEDED


async def test_revoked_member_cannot_begin_code_compute(publication_db, monkeypatch):
    f = await fixture(publication_db)
    server, _common, code = await setup(f, monkeypatch)
    active = await activate_code(f, server)
    run_id = (await start(f, server, active))["id"]
    await f.db.pool.execute("DELETE FROM project_memberships WHERE project_id=$1", f.project.id)
    with pytest.raises(LookupError, match="member"):
        await ActivityEnvironment().run(code.execute, run_id)
    assert f.runtime.sandboxes.calls == 0


async def test_tin_owned_code_uses_same_executor_without_private_ownership(
    publication_db, monkeypatch
):
    f = await fixture(publication_db)
    server, _common, code = await setup(f, monkeypatch)
    key = "example.order_report"
    files = {p: raw.encode() for p, raw in example_files(key).items()}
    revision = f.storage.repo.edit(files)
    path = f"workflow_packages/{key}/workflow.json"
    body = decode_workflow_source(files[path], definition_path=path).definition
    workflow = await f.db.upsert_registry_workflow(
        workflow_id=uuid4(),
        key=key,
        title=body["title"],
        description=body["description"],
        executor="workflow.code",
        definition_repo_id="registry/workflows",
        definition_path=path,
        current_commit_sha=revision,
        version_label=body["version"],
        definition=body,
    )
    assert workflow.project_id is None
    f.settings.private_workflow_projects = ()
    run_id = (await start(f, server, {"workflow_id": str(workflow.id)}))["id"]
    await ActivityEnvironment().run(code.execute, run_id)
    await code.publish(run_id)
    assert not await code.review(run_id)
    await code.project(run_id)
    assert (await f.db.get_run(UUID(run_id))).status == RunStatus.SUCCEEDED
    assert f.runtime.sandboxes.calls == 1


def test_output_names_accept_only_the_run_date_and_a_slug():
    from datetime import UTC, datetime

    from tin_lite.workflow_code import output_path_allowed

    value = definition()
    value["code"]["output"]["path"] = "reports/custom/{date}-{slug}.md"
    spec = validate_code_definition(value)
    created_at = datetime(2026, 9, 28, 17, tzinfo=UTC)
    assert output_path_allowed(spec, "reports/custom/2026-09-28-order-report.md", created_at)
    for path in [
        "reports/custom/2026-09-27-order-report.md",
        "reports/custom/2026-09-28-Order-Report.md",
        "reports/custom/2026-09-28-order--report.md",
        "reports/custom/2026-09-28-" + "a" * 81 + ".md",
        "reports/other/2026-09-28-order-report.md",
        "reports/custom/2026-09-28-../x.md",
    ]:
        assert not output_path_allowed(spec, path, created_at), path
    assert not output_path_allowed(spec, "reports/custom/2026-09-28-x.md", None)
    fixed = validate_code_definition(definition())
    assert output_path_allowed(fixed, OUTPUT) and not output_path_allowed(fixed, OUTPUT + "x")
    for template in [
        "reports/{date}/x.md",
        "reports/custom/{date}-{date}.md",
        "reports/custom/{run_id}.md",
        "reports/custom/{slug.md",
    ]:
        value["code"]["output"]["path"] = template
        with pytest.raises(ValueError):
            validate_code_definition(value)


async def test_named_output_is_recorded_before_its_write_and_recovered(publication_db, monkeypatch):
    f = await fixture(publication_db)
    storage = CheckpointStorage()
    compute = SyntheticCompute()
    server, _common, code = await setup(f, monkeypatch, compute=compute, storage=storage)
    manifest = json.loads(example_files()[PATH])
    manifest["definition"]["code"]["output"]["path"] = "reports/custom/{date}-{slug}.md"
    f.revision = storage.repo.edit({PATH: json.dumps(manifest).encode()})
    active = await activate_code(f, server)
    run_id = (await start(f, server, active))["id"]
    run = await f.db.get_run(UUID(run_id))
    path = f"reports/custom/{run.created_at.date().isoformat()}-order-report.md"
    compute.result = {"path": path, "content": "# Orders\nTotal: 3900 cents\n"}
    complete = f.db.complete_procedure_persist

    async def lost_completion(*args, **kwargs):
        raise RuntimeError("synthetic worker loss after storage checkpoint")

    monkeypatch.setattr(f.db, "complete_procedure_persist", lost_completion)
    with pytest.raises(RuntimeError, match="worker loss"):
        await ActivityEnvironment().run(code.execute, run_id)
    saved = await f.db.get_effect(f"{run_id}:procedure_artifact_persist")
    assert saved.status == "started"
    assert saved.result == {
        "artifact_path": path,
        "failure_reason": "Code workflow failed during recording the saved result (RuntimeError).",
    }
    monkeypatch.setattr(f.db, "complete_procedure_persist", complete)
    await ActivityEnvironment().run(code.execute, run_id)
    assert compute.calls == 1
    await code.publish(run_id)
    await code.project(run_id)
    run = await f.db.get_run(UUID(run_id))
    assert run.status == RunStatus.SUCCEEDED and run.artifact_path == path
    assert run.artifact_ref.endswith(f"/{path}")


async def test_named_output_with_another_date_never_publishes(publication_db, monkeypatch):
    f = await fixture(publication_db)
    storage = CheckpointStorage()
    compute = SyntheticCompute({"path": "reports/custom/2001-01-01-x.md", "content": "# X\n"})
    server, _common, code = await setup(f, monkeypatch, compute=compute, storage=storage)
    manifest = json.loads(example_files()[PATH])
    manifest["definition"]["code"]["output"]["path"] = "reports/custom/{date}-{slug}.md"
    f.revision = storage.repo.edit({PATH: json.dumps(manifest).encode()})
    active = await activate_code(f, server)
    run_id = (await start(f, server, active))["id"]
    with pytest.raises(Exception, match="validation"):
        await ActivityEnvironment().run(code.execute, run_id)
    assert storage.repo.writes == 0


async def test_named_output_heading_titles_its_review(publication_db, monkeypatch):
    f = await fixture(publication_db)
    storage = CheckpointStorage()
    compute = SyntheticCompute()
    server, _common, code = await setup(f, monkeypatch, compute=compute, storage=storage)
    manifest = json.loads(example_files()[PATH])
    manifest["definition"]["code"]["output"]["path"] = "reports/custom/{date}-{slug}.md"
    manifest["definition"]["human_review"] = {"eligible": True, "reason": "Review the report."}
    f.revision = storage.repo.edit({PATH: json.dumps(manifest).encode()})
    active = await activate_code(f, server)
    run_id = (await start(f, server, active))["id"]
    run = await f.db.get_run(UUID(run_id))
    path = f"reports/custom/{run.created_at.date().isoformat()}-september-orders.md"
    compute.result = {"path": path, "content": "# September orders\nTotal: 3900 cents\n"}
    await ActivityEnvironment().run(code.execute, run_id)
    await code.publish(run_id)
    assert await code.review(run_id)
    run = await f.db.get_run(UUID(run_id))
    assert run.artifact_path == path and run.artifact_title == "September orders"
