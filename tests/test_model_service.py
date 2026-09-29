"""Real provider wire mapping and durable accounting; no paid requests or production DB."""
# ruff: noqa: S106 -- fake credentials for an in-memory HTTP transport

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.domain import RunStatus
from tin_lite.model_providers import (
    GeminiModelProvider,
    MessageRole,
    ModelCapability,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    ModelRoute,
    ModelRouter,
    ModelUsage,
    OpenRouterModelProvider,
    ProviderName,
    ReasoningEffort,
    _optional_int,
)
from tin_lite.model_usage import OPERATION, ModelUsageRecorder, model_usage_scope, model_usage_step

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
REQUEST = ModelRequest(
    messages=(ModelMessage(MessageRole.USER, "private founder context"),),
    system="private instructions",
    max_output_tokens=100,
    reasoning_effort=ReasoningEffort.HIGH,
    output_schema=SCHEMA,
)
ROUTE = ModelRoute(
    key="test-explicit-route",
    provider=ProviderName.OPENROUTER,
    model="vendor/test-model",
    capabilities=frozenset(ModelCapability),
)


def response_body(text='{"answer":"good"}', *, choices=True):
    return {
        "id": "generation-test",
        "object": "chat.completion",
        "created": 123,
        "model": ROUTE.model,
        "choices": (
            [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ]
            if choices
            else []
        ),
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 6,
            "total_tokens": 16,
            "prompt_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
            "completion_tokens_details": {"reasoning_tokens": 4},
        },
    }


def provider_for(handler):
    client = AsyncOpenAI(
        api_key="fake-server-key",
        base_url="https://openrouter.ai/api/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return OpenRouterModelProvider(api_key="unused", client=client)


async def test_openrouter_wire_contract_and_server_only_key():
    def handle(request):
        assert request.url == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fake-server-key"
        body = json.loads(request.content)
        assert "fake-server-key" not in request.content.decode()
        assert body["model"] == ROUTE.model
        assert body["provider"] == {"require_parameters": True, "allow_fallbacks": False}
        assert body["reasoning"] == {"effort": "high"}
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["messages"][0] == {"role": "system", "content": REQUEST.system}
        assert body["max_tokens"] == 100 and body["stream"] is False
        return httpx.Response(200, json=response_body())

    provider = provider_for(handle)
    try:
        result = await provider.generate(model=ROUTE.model, request=REQUEST)
        assert result.provider == ProviderName.OPENROUTER
        assert result.parsed == {"answer": "good"}
        assert asdict(result.usage) == {
            "input_tokens": 10,
            "output_tokens": 6,
            "total_tokens": 16,
            "cached_input_tokens": 3,
            "reasoning_tokens": 4,
            "cache_write_input_tokens": 2,
        }
    finally:
        await provider.close()


@pytest.mark.parametrize("text,choices", [("invalid", True), ("", True), ("", False)])
async def test_rejected_output_retains_trusted_usage(text, choices):
    provider = provider_for(
        lambda _: httpx.Response(200, json=response_body(text, choices=choices))
    )
    try:
        with pytest.raises(ModelProviderError) as error:
            await provider.generate(model=ROUTE.model, request=REQUEST)
        assert error.value.observation.usage.total_tokens == 16
        assert error.value.observation.request_id == "generation-test"
        assert "fake-server-key" not in str(error.value)
    finally:
        await provider.close()


async def test_openrouter_does_not_retry_server_error():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "private upstream details"}})

    provider = provider_for(handle)
    try:
        with pytest.raises(ModelProviderError) as error:
            await provider.generate(model=ROUTE.model, request=REQUEST)
        assert error.value.observation is None
        assert "private upstream" not in str(error.value)
        assert len(requests) == 1
    finally:
        await provider.close()


def test_gemini_explicitly_disables_sdk_retries(monkeypatch):
    options = {}

    def client(**kwargs):
        options.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("tin_lite.model_providers.genai.Client", client)
    GeminiModelProvider(api_key="fake-server-key")
    assert options["http_options"].retry_options.attempts == 1


@pytest.mark.parametrize("value", [None, True, False, -1, 1.2, "10"])
def test_unknown_or_invalid_usage_is_not_zero(value):
    assert _optional_int(value) is None
    assert _optional_int(0) == 0


async def fixture(db, handler=None):
    _, _, run, _ = await activity_fixture(db)
    await db.pool.execute("UPDATE workflow_runs SET executor='content.plan' WHERE id=$1", run.id)
    calls = []

    def handle(request):
        calls.append(request)
        return handler(request) if handler else httpx.Response(200, json=response_body())

    provider = provider_for(handle)
    router = ModelRouter(
        providers={ProviderName.OPENROUTER: provider},
        routes=(ROUTE,),
        recorder=ModelUsageRecorder(db),
    )
    return run, router, calls


async def invoke(db, run, router, step="draft"):
    async with db.pool.acquire() as conn:
        with model_usage_scope(run_id=run.id, step=step, conn=conn):
            return await router.generate(ROUTE.key, REQUEST)


async def receipts(db):
    rows = await db.pool.fetch(
        "SELECT status, result FROM effect_receipts WHERE operation=$1", OPERATION
    )
    return [(row["status"], json.loads(row["result"])) for row in rows]


async def test_concurrent_duplicate_has_one_observation_and_one_paid_attempt(publication_db):
    db = publication_db
    run, router, calls = await fixture(db)
    try:
        results = await asyncio.gather(
            invoke(db, run, router), invoke(db, run, router), return_exceptions=True
        )
        assert sum(isinstance(r, ModelProviderError) for r in results) == 1
        assert len(calls) == 1
        [(status, record)] = await receipts(db)
        assert status == "completed" and record["outcome"] == "response_received"
        assert record["run_id"] == str(run.id) and record["project_id"] == str(run.project_id)
        assert record["definition_commit_sha"] == run.definition_commit_sha
        assert record["usage"]["total_tokens"] == 16
        encoded = json.dumps(record)
        for secret in (
            "private founder context",
            "private instructions",
            "fake-server-key",
            '"good"',
        ):
            assert secret not in encoded
    finally:
        await router.close()


async def test_invalid_output_is_accounted_and_not_rebought(publication_db):
    db = publication_db
    run, router, calls = await fixture(
        db, lambda _: httpx.Response(200, json=response_body("oops"))
    )
    try:
        for _ in range(2):
            with pytest.raises(ModelProviderError):
                await invoke(db, run, router)
        [(status, record)] = await receipts(db)
        assert status == "completed" and record["outcome"] == "invalid_output"
        assert record["usage"]["total_tokens"] == 16 and len(calls) == 1
    finally:
        await router.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_uncertain_request_retains_intent_and_is_not_rebought(publication_db, cancel):
    db = publication_db

    def fail(_):
        if cancel:
            raise asyncio.CancelledError()
        raise httpx.ReadTimeout("private provider body and credential must not be stored")

    run, router, calls = await fixture(db, fail)
    try:
        with pytest.raises(asyncio.CancelledError if cancel else ModelProviderError):
            await invoke(db, run, router)
        with pytest.raises(ModelProviderError, match="already attempted"):
            await invoke(db, run, router)
        [(status, record)] = await receipts(db)
        assert status == "started" and record["outcome"] == "unconfirmed"
        assert record["usage"] == asdict(ModelUsage()) and len(calls) == 1
        assert record["failure_reason"] == (
            "execution_interrupted" if cancel else "provider_timeout"
        )
        assert "private provider" not in json.dumps(record)
    finally:
        await router.close()


async def test_accounting_write_failure_does_not_allow_another_purchase(
    publication_db, monkeypatch
):
    db = publication_db
    run, router, calls = await fixture(db)
    try:
        monkeypatch.setattr(db, "complete_effect", AsyncMock(side_effect=ConnectionError))
        with pytest.raises(ConnectionError):
            await invoke(db, run, router)
        with pytest.raises(ModelProviderError, match="already attempted"):
            await invoke(db, run, router)
        [(status, record)] = await receipts(db)
        assert status == "started" and record["outcome"] == "unconfirmed" and len(calls) == 1
    finally:
        await router.close()


async def test_usage_reuses_activity_connection_even_when_pool_is_full(publication_db):
    db = publication_db
    run, router, calls = await fixture(db)
    try:
        async with AsyncExitStack() as stack:
            connections = [await stack.enter_async_context(db.pool.acquire()) for _ in range(10)]
            conn = connections[0]
            async with db.effect_lock("owning-activity", "test", conn=conn):
                with model_usage_scope(run_id=run.id, step="character", conn=conn):
                    for label in ("draft", "repair1", "refine"):
                        with model_usage_step(label):
                            await asyncio.wait_for(router.generate(ROUTE.key, REQUEST), timeout=2)
        assert len(calls) == 3
        assert {r["step"] for _, r in await receipts(db)} == {
            "character:draft",
            "character:repair1",
            "character:refine",
        }
    finally:
        await router.close()


async def test_unbound_and_inactive_runs_cannot_use_runtime_model_service(publication_db):
    db = publication_db
    run, router, calls = await fixture(db)
    try:
        with pytest.raises(ValueError, match="trusted run usage scope"):
            await router.generate(ROUTE.key, REQUEST)
        await db.pool.execute(
            "UPDATE workflow_runs SET status=$2 WHERE id=$1", run.id, RunStatus.STOPPED.value
        )
        with pytest.raises(ValueError, match="active run"):
            await invoke(db, run, router)
        assert not calls and not await receipts(db)
    finally:
        await router.close()
