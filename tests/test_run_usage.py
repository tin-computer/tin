"""Usage readouts use real disposable Postgres; provider/compute calls are mocked."""

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_private_workflows import app, mcp, structured
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.e2b_runtime import E2BRuntime
from tin_lite.luna import OpenAIResponsesClient
from tin_lite.run_usage import observation, read_run_usage, totals
from tin_lite.usage_capture import (
    begin_observation,
    borrowed_connection,
    count,
    dollars,
    external_usage_scope,
    observe_response,
    observe_sandbox,
    observe_tool,
    recover_tool_observation,
)


async def receipt(db, run, operation, facts, *, key=None, complete=True):
    key = key or f"{run.id}:{uuid4()}"
    async with db.effect_lock(key, operation) as (conn, _):
        await db.start_effect(conn, execution_key=key, operation=operation)
        method = db.complete_effect if complete else db.save_effect_progress
        await method(conn, execution_key=key, result={"run_id": str(run.id), **facts})
    return key


@pytest.mark.parametrize("value", [None, True, False, -1, "no", "NaN", "Infinity", 10**15])
def test_bad_amounts_are_unknown(value):
    assert dollars(value) is None
    assert count(value) is None
    assert dollars(0) == "0" and count(0) == 0


async def test_responses_captured_before_invalid_output_and_never_rebought(publication_db):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "test-response",
                "object": "response",
                "model": "test-model",
                "created_at": 123,
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 8,
                    "output_tokens": 2,
                    "total_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 3},
                    "output_tokens_details": {"reasoning_tokens": 1},
                },
            },
        )

    client = OpenAIResponsesClient(
        api_key="fake",
        model="test-model",  # noqa: S106
        base_url="https://provider.test/v1",
        timeout_seconds=5,
        transport=httpx.MockTransport(handle),
    )
    try:
        async with db.effect_lock("outer", "test") as (conn, _):
            with external_usage_scope(db, conn, run.id, "geo-one"):
                result = await client.create({"input": "private prompt"})
                assert not result["output"]  # semantic validation would reject it
                with pytest.raises(RuntimeError, match="already observed"):
                    await client.create({"input": "same request"})
        assert len(calls) == 1
        view = await read_run_usage(database=db, run=run)
        item = view["own"]["observations"][0]
        assert item["usage"]["total_tokens"] == 10
        assert item["outcome"] == "response_received"
        assert item["provider_reported_cost_usd"] is None
        assert "private prompt" not in json.dumps(view)
        # The receipt keeps the call's cap and the Responses API's stop signal.
        [record] = [
            json.loads(row["result"])
            for row in await db.pool.fetch(
                "SELECT result FROM effect_receipts WHERE operation='external_usage_v1'"
            )
        ]
        assert record["max_output_tokens"] == 16_384
        assert record["stop_reason"] == "completed" and record["output_truncated"] is False
    finally:
        await client.close()


async def test_missing_cancelled_and_malformed_observations_stay_unknown(publication_db):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    async with db.effect_lock("outer", "test") as (conn, _):
        with external_usage_scope(db, conn, run.id, "cancelled"):
            await begin_observation("openai", "model", "responses")
        with external_usage_scope(db, conn, run.id, "malformed"):
            observed = await begin_observation("openai", "model", "responses")
            await observe_response(
                observed,
                {
                    "usage": [1],
                    "output": {"bad": True},
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            )
        for stage, task in [
            ("bad-result", {"cost": 0.42}),
            ("missing-cost", {}),
            ("real-zero", {"cost": 0}),
        ]:
            with external_usage_scope(db, conn, run.id, stage):
                observed = await begin_observation("dataforseo", "tool", "test")
                await observe_tool(observed, task)
    view = await read_run_usage(database=db, run=run)
    assert len(view["own"]["observations"]) == 5
    assert view["inclusive_totals"]["known_provider_reported_cost_usd"] == "0.42"
    assert view["inclusive_totals"]["total_cost_usd"] is None
    assert view["own"]["observations"][0]["outcome"] == "unconfirmed"
    assert all(item["usage"]["total_tokens"] is None for item in view["own"]["observations"])
    records = {
        json.loads(row["result"])["step"]: json.loads(row["result"])
        for row in await db.pool.fetch(
            "SELECT result FROM effect_receipts WHERE operation='external_usage_v1'"
        )
    }
    # A cut-off response says so even when its usage is unreadable.
    assert records["malformed"]["stop_reason"] == "max_output_tokens"
    assert records["malformed"]["output_truncated"] is True
    assert records["cancelled"]["max_output_tokens"] == 16_384
    assert "max_output_tokens" not in records["bad-result"]  # Tool receipts are unchanged.


async def test_sandbox_observation_borrows_connection_and_does_not_extend_cleanup(publication_db):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    info = SimpleNamespace(
        metadata={"run_id": str(run.id), "profile": "isolated"},
        started_at=datetime.now(UTC) - timedelta(seconds=100),
        end_at=datetime.now(UTC) + timedelta(hours=1),
        cpu_count=2,
        memory_mb=1024,
    )
    async with AsyncExitStack() as stack:
        # Fill the pool. Observability must not recursively acquire another connection.
        conns = [await stack.enter_async_context(db.pool.acquire()) for _ in range(10)]
        async with db.effect_lock("outer", "test", conn=conns[0]) as (conn, _):
            assert borrowed_connection(db) is conn
            await asyncio.wait_for(observe_sandbox(db, "sandbox-a", info=info), timeout=2)
            await asyncio.wait_for(observe_sandbox(db, "sandbox-a", ended=True), timeout=2)
    first = await db.get_effect("sandbox-usage:sandbox-a")
    await observe_sandbox(db, "sandbox-a", ended=True)
    assert (await db.get_effect("sandbox-usage:sandbox-a")).result == first.result
    await observe_sandbox(db, "sandbox-b", info=info)
    await observe_sandbox(db, "sandbox-b", absent=True)
    await observe_sandbox(db, "never-observed", absent=True)
    view = await read_run_usage(database=db, run=run)
    items = view["own"]["observations"]
    assert len(items) == 2
    assert 100 <= float(items[0]["observed_wall_seconds"]) < 120
    assert items[1]["observed_wall_seconds"] is None
    assert items[0]["reference_estimate_usd"] is not None
    assert view["inclusive_totals"]["known_provider_reported_cost_usd"] is None
    assert borrowed_connection(db) is None


async def test_usage_failure_cannot_strand_sandbox(publication_db):
    engine = E2BRuntime(
        api_key="fake",
        template="test",
        timeout_seconds=60,  # noqa: S106
        egress_allow_hosts=(),
        usage_database=publication_db,
    )
    sandbox = SimpleNamespace(
        sandbox_id="test", get_info=AsyncMock(side_effect=ValueError("bad")), kill=AsyncMock()
    )
    await engine._delete_sandbox(sandbox)
    sandbox.kill.assert_awaited_once()


async def test_usage_cancellation_still_kills_sandbox(publication_db):
    engine = E2BRuntime(
        api_key="fake",
        template="test",
        timeout_seconds=60,  # noqa: S106
        egress_allow_hosts=(),
        usage_database=publication_db,
    )
    sandbox = SimpleNamespace(
        sandbox_id="test",
        get_info=AsyncMock(side_effect=asyncio.CancelledError()),
        kill=AsyncMock(),
    )
    with pytest.raises(asyncio.CancelledError):
        await engine._delete_sandbox(sandbox)
    sandbox.kill.assert_awaited_once()


async def test_e2b_false_delete_does_not_invent_shutdown_time(publication_db):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    engine = E2BRuntime(
        api_key="fake",
        template="test",
        timeout_seconds=60,  # noqa: S106
        egress_allow_hosts=(),
        usage_database=db,
    )
    sandbox = SimpleNamespace(
        sandbox_id="already-gone",
        get_info=AsyncMock(
            return_value=SimpleNamespace(
                metadata={"run_id": str(run.id)},
                started_at=datetime.now(UTC) - timedelta(seconds=100),
                cpu_count=2,
                memory_mb=1024,
            )
        ),
        kill=AsyncMock(return_value=False),
    )
    await engine._delete_sandbox(sandbox)
    view = await read_run_usage(database=db, run=run)
    item = view["own"]["observations"][0]
    assert item["outcome"] == "already_absent"
    assert item["observed_wall_seconds"] is None and item["reference_estimate_usd"] is None


async def test_legacy_modern_overlap_is_not_double_counted(publication_db):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    await receipt(
        db,
        run,
        "organic.keyword_plan",
        {"status": "completed", "value": {"usage": {"total_tokens": 50}}},
        key=f"keyword:{run.id}:seeds",
    )
    await receipt(
        db,
        run,
        "native_model_usage_v1",
        {"step": "keyword:seeds", "usage": {"total_tokens": 50}, "provider": "openai"},
    )
    await receipt(
        db,
        run,
        "organic.audit",
        {"status": "completed", "value": {"reported_cost_usd": ".25"}},
        key=f"organic:{run.id}:crawl_submit",
    )
    await receipt(
        db, run, "organic.audit", {"reservation_usd": "100"}, key=f"organic:{run.id}:budget"
    )
    view = await read_run_usage(database=db, run=run)
    assert len(view["own"]["observations"]) == 2
    assert view["inclusive_totals"]["known_provider_reported_cost_usd"] == "0.25"
    assert (
        view["inclusive_totals"]["tokens_by_execution_kind"]["native_model_service"]["total_tokens"]
        == 50
    )


async def child_run(db, parent, *, project_id=None, step="audit"):
    child = uuid4()
    await db.pool.execute(
        """INSERT INTO workflow_runs
        (id, project_id, workflow_id, executor, definition_commit_sha, temporal_workflow_id,
         thread_id, status, start_idempotency_key, generation)
        VALUES ($1,$2,$3,'organic.audit',$4,$5,$5,'failed',$6,1)""",
        child,
        project_id or parent.project_id,
        parent.workflow_id,
        parent.definition_commit_sha,
        str(child),
        f"system:{parent.id}:{step}",
    )
    return await db.get_run(child)


async def test_recovered_task_cost_fills_ambiguous_observation_once(publication_db):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    await receipt(
        db,
        run,
        "external_usage_v1",
        {
            "step": "crawl_submit",
            "category": "tool",
            "provider": "dataforseo",
            "outcome": "unconfirmed",
            "reported_cost_usd": None,
        },
        complete=False,
    )
    await receipt(
        db,
        run,
        "organic.audit",
        {"status": "completed", "value": {"reported_cost_usd": ".15"}},
        key=f"organic:{run.id}:crawl_submit",
    )
    view = await read_run_usage(database=db, run=run)
    assert len(view["own"]["observations"]) == 1
    assert view["inclusive_totals"]["known_provider_reported_cost_usd"] == "0.15"


async def test_gak_tool_observations_keep_their_provider_at_zero_cost(publication_db):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    async with db.effect_lock("outer", "test") as (conn, _):
        with external_usage_scope(db, conn, run.id, "ideas"):
            observed = await begin_observation("gak", "tool", "api/v1/keywords/ideas")
            await observe_tool(observed, {"cost": "0"})
        with external_usage_scope(db, conn, run.id, "volume"):
            await begin_observation("gak", "tool", "api/v1/keywords/volume")
    view = await read_run_usage(database=db, run=run)
    confirmed, pending = sorted(view["own"]["observations"], key=lambda item: item["outcome"])
    assert confirmed["provider"] == pending["provider"] == "gak"
    assert confirmed["kind"] == pending["kind"] == "tool"
    assert confirmed["outcome"] == "response_received"
    assert confirmed["provider_reported_cost_usd"] == "0"
    assert confirmed["usage"]["requests"] == 1
    assert pending["outcome"] == "unconfirmed" and pending["provider_reported_cost_usd"] is None
    assert view["inclusive_totals"]["known_provider_reported_cost_usd"] == "0"
    assert view["inclusive_totals"]["total_cost_usd"] is None


async def test_recovery_accepts_a_gak_tool_record_without_a_provider_cost():
    from test_gak import MemoryDB

    db = MemoryDB()
    conn = None
    run_id = db.run.id
    with external_usage_scope(db, conn, run_id, "ideas"):
        await begin_observation("gak", "tool", "api/v1/keywords/ideas")
    (receipt,) = db.effects.values()
    # Recovery is a billing-only path; without a run budget it prices nothing.
    db.billing = SimpleNamespace()
    assert receipt.status == "started" and receipt.result["outcome"] == "unconfirmed"
    for _ in range(2):
        await recover_tool_observation(
            db, conn, run_id=run_id, step="ideas", endpoint="api/v1/keywords/ideas", cost="0"
        )
    (receipt,) = db.effects.values()
    assert receipt.status == "completed" and receipt.result["provider"] == "gak"
    assert receipt.result["outcome"] == "response_received"
    assert receipt.result["reported_cost_usd"] == "0"
    # An unknown provider's receipt is never rewritten by the tool recovery path.
    db.billing = None
    with external_usage_scope(db, conn, run_id, "other"):
        await begin_observation("unknown", "tool", "api/v1/keywords/ideas")
    db.billing = SimpleNamespace()
    await recover_tool_observation(
        db, conn, run_id=run_id, step="other", endpoint="api/v1/keywords/ideas", cost="0"
    )
    assert [r.status for r in db.effects.values()] == ["completed", "started"]


async def test_parent_only_counts_owned_children_including_failures(publication_db):
    db = publication_db
    _, _, base, _ = await activity_fixture(db)
    parent = replace(base, executor="organic.traffic_system")
    owned = await child_run(db, parent)
    referenced = await child_run(db, parent, step="not-owned")
    other_project = await db.create_project(name="Other", state_repo_id="projects/other")
    foreign = await child_run(db, parent, project_id=other_project.id, step="technical")
    for step, child in [("audit", owned), ("keywords", referenced), ("technical", foreign)]:
        await receipt(
            db,
            parent,
            "organic.traffic_system",
            {"run_id": str(child.id)},
            key=f"traffic:{parent.id}:step:{step}",
        )
        await receipt(db, child, "native_model_usage_v1", {"usage": {"total_tokens": 12}})
    view = await read_run_usage(database=db, run=parent)
    assert [row["run_id"] for row in view["children"]] == [str(owned.id)]
    assert view["children"][0]["status"] == "failed"
    assert (
        view["inclusive_totals"]["tokens_by_execution_kind"]["native_model_service"]["total_tokens"]
        == 12
    )
    assert str(foreign.id) not in json.dumps(view)


@pytest.mark.parametrize("model", [None, "test-codex-model"])
def test_codex_cumulative_usage_is_not_api_cost_or_compute_time(model):
    item = observation(
        {"operation": "isolated_codex_attempt_v1", "execution_key": "private-key"},
        {
            "usage": {
                "model": model,
                "total": {"totalTokens": 20, "inputTokens": 16},
                "last": {"totalTokens": 5},
            },
            "elapsed_seconds": 7,
            "outcome": "failed",
            "error": "private upstream error",
            "secret": "credential",
        },
    )
    assert item["usage"]["total_tokens"] == 20
    assert item["model"] == model
    assert item["kind"] == "codex_chatgpt_oauth" and item["outcome"] == "failed"
    assert item["reference_estimate_usd"] is None
    assert item["observed_wall_seconds"] is None
    assert "private" not in json.dumps(item) and "credential" not in json.dumps(item)
    assert totals([])["known_provider_reported_cost_usd"] is None


async def test_http_mcp_parity_and_membership_before_usage_read(publication_db, monkeypatch):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    await db.record_tin_user("user_privateauthor")
    await db.grant_project_membership(project_id=run.project_id, clerk_user_id="user_privateauthor")
    f = SimpleNamespace(
        runtime=SimpleNamespace(database=db),
        settings=SimpleNamespace(
            switchboard_public_url="https://tin.test", clerk_frontend_api_url="https://clerk.test"
        ),
    )
    await receipt(db, run, "native_model_usage_v1", {"usage": {"total_tokens": 10}})
    server = mcp(f, monkeypatch)
    via_mcp = structured(await server.call_tool("get_run_usage", {"run_id": str(run.id)}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/workflows/runs/{run.id}/usage")
    assert response.status_code == 200 and response.json() == via_mcp
    assert response.headers["X-Tin-Read-Source"] == "postgres"
    reader = AsyncMock(side_effect=AssertionError("unauthorized usage read"))
    monkeypatch.setattr("tin_lite.run_usage.read_run_usage", reader)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, "user_outsider")), base_url="http://test"
    ) as client:
        assert (await client.get(f"/api/workflows/runs/{run.id}/usage")).status_code == 404
    with pytest.raises(ToolError, match="not_found: run not found"):
        await mcp(f, monkeypatch, "user_outsider").call_tool(
            "get_run_usage", {"run_id": str(run.id)}
        )
    reader.assert_not_awaited()


async def test_truncation_is_explicit(publication_db, monkeypatch):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    for _ in range(2):
        await receipt(db, run, "native_model_usage_v1", {"usage": {"total_tokens": 10}})
    monkeypatch.setattr("tin_lite.run_usage.MAX_OBSERVATIONS", 1)
    view = await read_run_usage(database=db, run=run)
    assert view["own"]["truncated"] and len(view["own"]["observations"]) == 1


def test_stage_readout_keeps_compaction_and_unknown_prices_separate():
    from tin_lite.run_usage import usage_stage

    assert (
        usage_stage({"endpoint": "/v1/responses/compact", "step": "draft"}, "codex_openai_api")
        == "context_compaction"
    )
    assert usage_stage({"step": "keyword:triage"}, "native_model_service") == "keyword:triage"
    assert (
        usage_stage({"step": "https://secret.example/?token=hidden"}, "native_model_service")
        == "model_unspecified"
    )
    assert usage_stage({}, "connected_api") == "connected_api"
