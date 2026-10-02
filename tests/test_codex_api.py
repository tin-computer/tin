from __future__ import annotations

import asyncio
import json
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from test_codex_isolation import load_sandbox_module
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db
from test_rollouts import base_values

from tin_lite.codex_api import (
    ATTEMPT,
    CONTRACT,
    PROCEDURE_CONTRACT_V5,
    USAGE,
    attempt_key,
    pinned_contract,
    run_api_attempt,
    select_contract,
    token_hash,
)
from tin_lite.codex_api_relay import CodexAPIRelay, request_body, request_identity, router
from tin_lite.domain import SideEffectConflictError
from tin_lite.e2b_runtime import E2BRuntime, SandboxProcedureInput, _run_secrets
from tin_lite.procedures import SandboxProfile
from tin_lite.run_usage import read_run_usage

GRANT = "synthetic-run-bound-grant-only-for-tests"
BODY = {"model": CONTRACT["model"], "stream": True, "input": "test"}
HEADERS = {
    "x-codex-turn-metadata": json.dumps(
        {
            "thread_id": "thread",
            "turn_id": "turn",
            "window_id": "window",
        }
    )
}


def result_event(**updates):
    response = {
        "id": "resp_test",
        "model": CONTRACT["model"],
        "service_tier": "default",
        "status": "completed",
        "output": [],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "input_tokens_details": {"cached_tokens": 40},
            "output_tokens_details": {"reasoning_tokens": 5},
        },
        **updates,
    }
    return (
        b"data: "
        + json.dumps({"type": "response.completed", "response": response}).encode()
        + b"\n\n"
    )


async def setup_relay(db, handler=None):
    _, _, run, _ = await activity_fixture(db)
    record = {
        "run_id": str(run.id),
        "outcome": "running",
        "contract": CONTRACT,
        "grant_sha256": token_hash(GRANT),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "project_id": str(run.project_id),
        "sandbox_id": run.sandbox_id,
        "generation": run.generation,
        "fencing_token": run.fencing_token,
        "thread_id": str(run.thread_id),
        "lease_owner": run.lease_owner,
    }
    async with db.pool.acquire() as conn:
        await db.start_effect(conn, execution_key=attempt_key(run.id), operation=ATTEMPT)
        await db.save_effect_progress(conn, execution_key=attempt_key(run.id), result=record)
    sent = []

    async def upstream(request):
        sent.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-master-key"
        assert "x-tin-codex-grant" not in request.headers
        assert request.url.host == "api.openai.com"
        if handler:
            return await handler(request)
        return httpx.Response(200, content=result_event(), headers={"x-request-id": "req_test"})

    relay = CodexAPIRelay(
        database=db,
        api_key="synthetic-master-key",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(upstream),
            follow_redirects=False,
        ),
    )
    app = FastAPI()
    app.state.runtime = SimpleNamespace(codex_api=relay)
    app.include_router(router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://tin.test")
    return run, relay, client, sent


async def post(client, run, body=None, grant=GRANT, operation="responses", headers=None):
    return await client.post(
        f"/internal/codex-api/{run.id}/v1/{operation}",
        json=body or BODY,
        headers={**HEADERS, "x-tin-codex-grant": grant, **(headers or {})},
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"model": "other"},
        {"stream": False},
        {"previous_response_id": "resp_other"},
        {"input": [{"type": "item_reference", "id": "msg_other"}]},
        {"input": [{"file_id": "file_other"}]},
        {"conversation": "conv_other"},
        {"background": True},
        {"tools": [{"type": "code_interpreter"}]},
        {"tools": [{"type": "namespace", "tools": [{"type": "file_search"}]}]},
        {"max_output_tokens": True},
    ],
)
def test_rejects_unbounded_or_cross_account_requests(updates):
    with pytest.raises(HTTPException):
        request_body(json.dumps({**BODY, **updates}).encode(), "responses")


def test_request_pinning_and_contextual_identity():
    raw = json.dumps(
        {**BODY, "store": True, "service_tier": "priority", "max_output_tokens": 99999}
    ).encode()
    body = request_body(raw, "responses")
    assert not body["store"] and body["service_tier"] == "default"
    assert body["max_output_tokens"] == CONTRACT["max_output_tokens"]
    assert request_identity(HEADERS, raw, "responses") == request_identity(
        HEADERS, raw, "responses"
    )
    assert request_identity(HEADERS, raw, "responses") != request_identity(
        HEADERS, b"new", "responses"
    )
    with pytest.raises(HTTPException):
        request_identity({}, raw, "responses")
    with pytest.raises(HTTPException):
        request_body(b"[" * 5000, "responses")
    schema = {
        **BODY,
        "tools": [
            {
                "type": "function",
                "name": "test",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": ["string", "null"]}},
                },
            }
        ],
    }
    assert request_body(json.dumps(schema).encode(), "responses")["tools"] == schema["tools"]


async def test_real_receipts_usage_projection_and_no_duplicate_purchase(publication_db):
    run, relay, client, sent = await setup_relay(publication_db)
    try:
        response = await post(client, run)
        assert response.status_code == 200 and b"response.completed" in response.content
        assert (await post(client, run)).status_code == 409
        assert len(sent) == 1
        readout = await read_run_usage(database=publication_db, run=run)
        api = [o for o in readout["own"]["observations"] if o["kind"] == "codex_openai_api"]
        assert len(api) == 1 and api[0]["usage"]["total_tokens"] == 110
        assert api[0]["usage"]["cache_write_input_tokens"] is None
        assert readout["own"]["totals"]["total_cost_usd"] is None
        assert not any(o["kind"] == "codex_chatgpt_oauth" for o in readout["own"]["observations"])
        assert "grant" not in json.dumps(readout) and "synthetic-master" not in json.dumps(readout)
        receipt = await publication_db.pool.fetchval(
            "SELECT result FROM effect_receipts WHERE operation=$1", USAGE
        )
        assert '"request_id": "req_test"' in receipt and '"input":' not in receipt
    finally:
        await client.aclose()
        await relay.close()


@pytest.mark.parametrize("mode", ["disconnect", "timeout", "rejected", "invalid", "redirect"])
async def test_unknown_attempts_never_automatically_repurchase(publication_db, mode):
    async def handler(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("do-not-expose-provider-secret", request=request)
        return httpx.Response(
            503 if mode == "rejected" else 307 if mode == "redirect" else 200,
            content=b"data: {}\n\n" if mode == "disconnect" else b"data: invalid\n\n",
            headers={"location": "https://attacker.test"},
        )

    run, relay, client, sent = await setup_relay(publication_db, handler)
    try:
        response = await post(client, run)
        assert "do-not-expose" not in response.text
        retry = await post(client, run, {**BODY, "input": "different"})
        assert retry.status_code == 409 and len(sent) == 1
        facts = await publication_db.pool.fetchrow(
            "SELECT status, result FROM effect_receipts WHERE operation=$1", USAGE
        )
        assert facts["status"] == "started" and json.loads(facts["result"])["usage"] is None
    finally:
        await client.aclose()
        await relay.close()


@pytest.mark.parametrize("mutation", ["grant", "run", "stop", "fence", "sandbox", "expiry"])
async def test_grants_fail_closed_before_spending(publication_db, mutation):
    run, relay, client, sent = await setup_relay(publication_db)
    try:
        grant = GRANT
        if mutation == "grant":
            grant = "other-grant-that-is-not-valid-for-this-run"
        elif mutation == "run":
            run = replace(run, id=uuid4())
        elif mutation == "stop":
            await publication_db.pool.execute(
                "UPDATE workflow_runs SET lease_active=false WHERE id=$1", run.id
            )
        elif mutation in {"fence", "sandbox"}:
            column = "fencing_token" if mutation == "fence" else "sandbox_id"
            value = run.fencing_token + 1 if mutation == "fence" else "new-sandbox"
            await publication_db.pool.execute(
                f"UPDATE workflow_runs SET {column}=$2 WHERE id=$1",  # noqa: S608 — fixed two columns
                run.id,
                value,
            )
        else:
            await publication_db.pool.execute(
                """UPDATE effect_receipts SET result=jsonb_set(result,'{expires_at}',
                   '"2000-01-01T00:00:00+00:00"') WHERE execution_key=$1""",
                attempt_key(run.id),
            )
        assert (await post(client, run, grant=grant)).status_code == 403
        assert not sent
    finally:
        await client.aclose()
        await relay.close()


async def test_concurrent_duplicate_and_request_bound(publication_db):
    run, relay, client, sent = await setup_relay(publication_db)
    try:
        replies = await asyncio.gather(post(client, run), post(client, run))
        assert sorted(r.status_code for r in replies) == [200, 409]
        for index in range(1, CONTRACT["max_requests"]):
            assert (await post(client, run, {**BODY, "input": str(index)})).status_code == 200
        assert (await post(client, run, {**BODY, "input": "excess"})).status_code == 429
        assert len(sent) == CONTRACT["max_requests"]
    finally:
        await client.aclose()
        await relay.close()


@pytest.mark.parametrize("operation", ["responses", "responses/compact"])
async def test_compaction_crlf_and_missing_usage_stays_unknown(publication_db, operation):
    async def handler(_request):
        return httpx.Response(
            200,
            content=(
                result_event(usage=None).replace(b"\n", b"\r\n")
                if operation == "responses"
                else json.dumps({"id": "cmp_one", "usage": None}).encode()
            ),
        )

    run, relay, client, _ = await setup_relay(publication_db, handler)
    try:
        assert (await post(client, run, operation=operation)).status_code == 200
        facts = json.loads(
            await publication_db.pool.fetchval(
                "SELECT result FROM effect_receipts WHERE operation=$1", USAGE
            )
        )
        assert facts["usage"]["total_tokens"] is None
    finally:
        await client.aclose()
        await relay.close()


def test_config_and_environment_never_include_provider_key(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-6-sol"\n[mcp_servers.tin]\nurl="https://tin.test/tools"\n')
    url = f"https://tin.test/internal/codex-api/{uuid4()}/v1"
    module = load_sandbox_module("codex_api_config")
    module.configure(
        config,
        {
            "TIN_CODEX_API_URL": url,
            "TIN_CODEX_API_GRANT": GRANT,
            "TIN_PROCEDURE_ISOLATED": "1",
        },
    )
    parsed = tomllib.loads(config.read_text())
    assert parsed["model_provider"] == "tin_api" and "tin" in parsed["mcp_servers"]
    assert parsed["model_providers"]["tin_api"]["stream_max_retries"] == 0
    assert GRANT not in config.read_text()
    values = {**base_values()}
    input = SandboxProcedureInput(
        **values,
        context={},
        output_path="report.md",
        output_max_bytes=1000,
        api_url=url,
        api_grant=GRANT,
        isolated=True,
    )
    runtime = E2BRuntime(
        api_key="test", template="test", timeout_seconds=900, egress_allow_hosts=("tin.test",)
    )
    env = runtime._run_env(sandbox_id="sandbox", run_input=input)
    assert env["TIN_CODEX_API_GRANT"] == GRANT
    assert not any(name in env for name in ("OPENAI_API_KEY", "TIN_LITE_LUNA_API_KEY"))
    assert GRANT in _run_secrets(input)
    with pytest.raises(ValueError):
        runtime._run_env(sandbox_id="sandbox", run_input=replace(input, isolated=False))


def test_studio_shell_policy_delegates_only_voice_capability(tmp_path):
    module = load_sandbox_module("codex_api_config")
    env = {
        "TIN_PROCEDURE_STUDIO": "1",
        "TIN_PROCEDURE_ISOLATED": "1",
        "TIN_CODEX_API_URL": f"https://tin.test/internal/codex-api/{uuid4()}/v1",
        "TIN_CODEX_API_GRANT": "controller-only",
        "TIN_RUN_TOOLS_URL": "https://tin.test/internal/run-tools/mcp",
        "TIN_RUN_TOOLS_GRANT": 'run-voice-"capability',
        "FAL_KEY": "provider-only",
    }
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-6-sol"\n')
    module.configure(config, env)
    policy = tomllib.loads(config.read_text())["shell_environment_policy"]
    assert policy == {
        "inherit": "none",
        "set": {key: env[key] for key in ("TIN_RUN_TOOLS_URL", "TIN_RUN_TOOLS_GRANT")},
    }
    assert "controller-only" not in config.read_text()
    assert "provider-only" not in config.read_text()
    assert config.stat().st_mode & 0o777 == 0o600
    assert module.studio_shell_policy({}) == ""
    with pytest.raises(ValueError, match="voice capability"):
        module.studio_shell_policy({**env, "TIN_RUN_TOOLS_GRANT": ""})


async def test_pinned_auth_survives_flags_and_ambiguous_attempt_not_restarted(publication_db):
    _, _, run, _ = await activity_fixture(publication_db)
    db = publication_db
    async with db.pool.acquire() as conn:
        assert await pinned_contract(db, run.id, conn=conn) == {"mode": "chatgpt_oauth"}
        selected = await select_contract(
            db=db,
            conn=conn,
            run=run,
            procedure=SimpleNamespace(sandbox=SandboxProfile(profile="isolated")),
            settings=SimpleNamespace(codex_api_projects={run.project_id}, luna_api_key="test"),
        )
        key = f"{run.id}:procedure_sandbox_create"
        await db.start_effect(conn, execution_key=key, operation="procedure_sandbox_create")
        await db.complete_effect(conn, execution_key=key, result={"codex_auth": selected})
        assert selected == PROCEDURE_CONTRACT_V5  # Unpinned isolated runs now select v5.
        assert await pinned_contract(db, run.id, conn=conn) == selected
        input = SandboxProcedureInput(
            **{**base_values()},
            context={},
            output_path="report.md",
            output_max_bytes=1000,
            isolated=True,
            timeout_seconds=900,
        )
        call = AsyncMock(side_effect=RuntimeError("uncertain"))
        with pytest.raises(RuntimeError):
            await run_api_attempt(
                db=db, conn=conn, run=run, sandbox_id=run.sandbox_id, run_input=input, call=call
            )
        with pytest.raises(SideEffectConflictError):
            await run_api_attempt(
                db=db, conn=conn, run=run, sandbox_id=run.sandbox_id, run_input=input, call=call
            )
        assert call.await_count == 1

        supplied = call.call_args.args[0]
        assert supplied.api_grant
        receipt = await db.get_effect(attempt_key(run.id), conn=conn)
        assert receipt.result["outcome"] == "failed"
        assert supplied.api_grant not in json.dumps(receipt.result)


async def test_hosted_defaults_do_not_reinterpret_an_older_unbudgeted_run(publication_db):
    _, _, run, _ = await activity_fixture(publication_db)
    async with publication_db.pool.acquire() as conn:
        selected = await select_contract(
            db=publication_db,
            conn=conn,
            run=run,
            procedure=SimpleNamespace(sandbox=SandboxProfile(profile="isolated")),
            settings=SimpleNamespace(
                billing_hosted_defaults_enabled=True, codex_api_projects=set(), luna_api_key="test"
            ),
        )
        assert selected == {"mode": "chatgpt_oauth"}


@pytest.mark.parametrize("boundary", ["membership", "billing", "token_limit"])
async def test_authority_and_token_stop_before_dispatch(publication_db, boundary):
    run, relay, client, sent = await setup_relay(publication_db)
    try:
        if boundary == "membership":
            await publication_db.pool.execute(
                "UPDATE workflow_runs SET started_by_clerk_user_id='user_revoked' WHERE id=$1",
                run.id,
            )
        elif boundary == "billing":
            project = await publication_db.get_project(run.project_id)
            await publication_db.pool.execute(
                """INSERT INTO billing_accounts(workspace_id, mode, admin_clerk_user_id)
                   VALUES ($1,'test','user_test')""",
                project.workspace_id,
            )
        else:
            key, record = await relay.admit(run.id, GRANT, "earlier", "responses")
            await relay.observe(
                key,
                record,
                {
                    "usage": {
                        "total_tokens": CONTRACT["max_observed_tokens"],
                    }
                },
            )
        response = await post(client, run)
        assert response.status_code == (429 if boundary == "token_limit" else 403)
        assert not sent
    finally:
        await client.aclose()
        await relay.close()


async def test_four_outer_activity_locks_do_not_deadlock_relay(publication_db):
    from contextlib import AsyncExitStack

    run, relay, client, sent = await setup_relay(publication_db)
    try:
        async with AsyncExitStack() as stack:
            for index in range(4):
                await stack.enter_async_context(
                    publication_db.effect_lock(f"outer:{index}", "procedure_artifact_persist")
                )
            response = await asyncio.wait_for(post(client, run), timeout=3)
            assert response.status_code == 200 and len(sent) == 1
    finally:
        await client.aclose()
        await relay.close()


async def test_usage_recorded_before_terminal_event_and_even_for_incomplete_output(publication_db):
    async def handler(_request):
        return httpx.Response(200, content=result_event(status="incomplete"))

    run, relay, client, sent = await setup_relay(publication_db, handler)
    try:
        response = await relay.relay(run.id, GRANT, json.dumps(BODY).encode(), "responses", HEADERS)
        async for event in response.body_iterator:
            if b"response.completed" in event:
                receipt = await publication_db.pool.fetchrow(
                    "SELECT status, result FROM effect_receipts WHERE operation=$1", USAGE
                )
                assert receipt["status"] == "completed"
                assert json.loads(receipt["result"])["response_status"] == "incomplete"
                assert json.loads(receipt["result"])["usage"]["total_tokens"] == 110
        assert len(sent) == 1
    finally:
        await client.aclose()
        await relay.close()
