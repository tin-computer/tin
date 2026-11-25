"""Gemini transport for a run-bound agent harness (Antigravity), not a public API proxy.

The sandbox's harness calls this route as if it were the Gemini API: its "API key" is the
run's grant and its base URL is `/internal/agent-api/{run_id}/gemini`. Admission, receipts and
spending are the Codex relay's (CodexAPIRelay.admit with the Gemini route): every request is
admitted against the run's pinned contract and budget before Tin's key reaches Google, and the
usage Google reports is priced on the run's pinned card before the final event reaches the
harness.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from tin_lite.agent_harness import AGENT_CARDS, CONTRACTS, is_agent_contract
from tin_lite.codex_api_relay import (
    MAX_EVENT_BYTES,
    MAX_STREAM_BYTES,
    PARALLEL_REJECTIONS,
    STREAM_SECONDS,
    CodexAPIRelay,
    RelayRejected,
    RelayRoute,
    _log_rejection,
)
from tin_lite.usage_capture import count, object_value

logger = logging.getLogger(__name__)

router = APIRouter()
UPSTREAM = "https://generativelanguage.googleapis.com/v1beta/models"
OPERATIONS = frozenset({"streamGenerateContent", "generateContent"})
_TARGET = re.compile(r"([a-z0-9][a-z0-9.\-]{0,79}):([A-Za-z]{1,40})")
# Every model any agent card prices; a run's own pinned card is checked after admission.
KNOWN_MODELS = frozenset(model for card in AGENT_CARDS for model in card["models"])
MAX_BODY_BYTES = max(value["max_request_bytes"] for value in CONTRACTS)
# Google-hosted state or execution the shared key must not reach: another project's cached
# contents or uploaded files, or tools whose price the card does not carry.
FORBIDDEN_KEYS = frozenset({"cachedContent", "fileUri"})
# Antigravity escalates its tool-call safety rater to Gemini 3.8 Flash with the whole
# trajectory, about a fifth of a run's cost. Tin's sandbox, fence and run grants are the
# boundary, and agy 1.5.16 carries on when this call is refused (its Flash-Lite first pass
# still runs), so the relay refuses it before admission and buys nothing.
REFUSED_MODELS = frozenset({"gemini-3.8-flash"})
# How long a Gemini stream may go without a byte. Google sends a function call's arguments in
# one piece, so an agent writing a long file through a tool streams nothing while it
# generates: staging 2026-11-25, an analyst writing its report went silent for over 180 s
# (the client's default read timeout) and the relay cut it as stream_unconfirmed, twice. The
# whole stream stays bounded by STREAM_SECONDS.
STREAM_IDLE_SECONDS = 900
_REFUSED_BODY = json.dumps(
    {
        "error": {
            "code": 400,
            "message": "This model is not available on Tin's agent route.",
            "status": "INVALID_ARGUMENT",
        }
    }
)
ALLOWED_TOOLS = frozenset({"functionDeclarations", "googleSearch", "urlContext"})
# How often the harness may resend bytes Google refused outright (a 429 or 5xx bills nothing).
MAX_REFUSED_RETRIES = 8


def _camel(key: str) -> str:
    """The lowerCamel name Google's JSON parser also maps a snake_case key to."""
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _one_spelling(value: dict, what: str) -> None:
    # Google accepts both spellings of a field, so a snake_case key could slip past a check.
    if any("_" in key for key in value):
        raise HTTPException(400, f"Unsupported field spelling in the agent {what}")


def split_target(target: str) -> tuple[str, str]:
    match = _TARGET.fullmatch(target)
    if match is None or match.group(1) not in KNOWN_MODELS or match.group(2) not in OPERATIONS:
        raise HTTPException(404, "Unsupported agent model operation")
    return match.group(1), match.group(2)


def gemini_body(raw: bytes, contract) -> dict:
    """The request, checked against the run's contract and with its output bound applied."""
    if len(raw) > contract["max_request_bytes"]:
        raise RelayRejected(
            413, "Agent request exceeds its pinned context bound", reason="context_bound"
        )
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "Invalid agent model request") from None
    if not isinstance(body, dict) or not isinstance(body.get("contents"), list):
        raise HTTPException(400, "Invalid agent model request")

    def check(value):
        if isinstance(value, dict):
            if any(_camel(key) in FORBIDDEN_KEYS for key in value):
                raise HTTPException(400, "Stored provider objects are not supported by this route")
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    try:
        check(body)
    except RecursionError:
        raise HTTPException(400, "Agent model request is nested too deeply") from None
    tools = body.get("tools", [])
    if not isinstance(tools, list) or any(
        not isinstance(tool, dict) or not tool or not set(tool) <= ALLOWED_TOOLS for tool in tools
    ):
        raise HTTPException(400, "Unsupported tool on the agent model route")
    _one_spelling(body, "request")
    config = body.get("generationConfig", {})
    if not isinstance(config, dict):
        raise HTTPException(400, "Invalid agent generation config")
    _one_spelling(config, "generation config")
    if config.get("candidateCount", 1) != 1:
        raise HTTPException(400, "The agent route returns one candidate")
    bound = contract["max_output_tokens"]
    requested = config.get("maxOutputTokens", bound)
    if type(requested) is not int or requested < 1:
        raise HTTPException(400, "Invalid agent output bound")
    config = {**config, "maxOutputTokens": min(requested, bound)}
    thinking = config.get("thinkingConfig")
    if thinking is not None:
        if not isinstance(thinking, dict):
            raise HTTPException(400, "Invalid agent thinking config")
        _one_spelling(thinking, "thinking config")
        budget = thinking.get("thinkingBudget")
        if budget is not None and (type(budget) is not int or budget < -1):
            raise HTTPException(400, "Invalid agent thinking budget")
        if budget is not None and budget > bound:
            config["thinkingConfig"] = {**thinking, "thinkingBudget": bound}
    body["generationConfig"] = config
    return body


def gemini_route(model: str) -> RelayRoute:
    def validate(raw, operation, contract):
        card_models = AGENT_CARDS[0]["models"]
        if model != contract["model"] and model not in card_models:
            raise HTTPException(400, "Agent model does not match the pinned route")
        return gemini_body(raw, contract)

    return RelayRoute(
        provider="gemini",
        execution_kind="agent_gemini_api",
        billed_operations=OPERATIONS,
        accepts=is_agent_contract,
        validate=validate,
        requested_model=lambda operation, contract: model,
        requested_tier=lambda operation: "standard",
    )


def usage_facts(metadata: dict, queries: int) -> dict:
    """Gemini's usage in the shape the card prices. Prompt tokens include cached ones and
    tool-use prompt tokens are input; output is candidates plus thinking."""
    prompt = count(metadata.get("promptTokenCount"))
    tool_prompt = count(metadata.get("toolUsePromptTokenCount")) or 0
    cached = count(metadata.get("cachedContentTokenCount")) or 0
    candidates = count(metadata.get("candidatesTokenCount")) or 0
    thoughts = count(metadata.get("thoughtsTokenCount")) or 0
    inputs = None if prompt is None else prompt + tool_prompt
    outputs = candidates + thoughts
    return {
        "requests": 1,
        "input_tokens": inputs,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": outputs,
        "reasoning_tokens": thoughts,
        "total_tokens": None if inputs is None else inputs + outputs,
        "provider_total_tokens": count(metadata.get("totalTokenCount")),
        "search_queries": queries,
    }


def search_queries(message: dict) -> int:
    total = 0
    for candidate in message.get("candidates") or ():
        grounding = object_value(object_value(candidate).get("groundingMetadata"))
        queries = grounding.get("webSearchQueries")
        if isinstance(queries, list):
            total += len(queries)
    return total


class GeminiAgentRelay(CodexAPIRelay):
    """Admission and settlement are inherited; only Gemini's transport differs."""

    async def fingerprint(self, run_id, model, operation, raw) -> str:
        """Identical bytes are one purchase, except after Google refused them outright: the
        harness retries a refused request, which bought nothing. An unconfirmed attempt or
        received response with the same bytes is never bought again."""
        digest = hashlib.sha256(model.encode() + b"\0" + operation.encode() + b"\0" + raw)
        base = digest.hexdigest()
        rows = await self.db.pool.fetch(
            """SELECT status, result->>'outcome' AS outcome FROM effect_receipts
               WHERE execution_key = ANY($1::text[])""",
            [f"{run_id}:codex-api:{base}:{n}" for n in range(MAX_REFUSED_RETRIES + 1)],
        )
        if len(rows) > MAX_REFUSED_RETRIES or any(
            row["status"] != "completed" or row["outcome"] != "upstream_rejected" for row in rows
        ):
            raise HTTPException(409, "This model request was already attempted; do not repurchase")
        return f"{base}:{len(rows)}"

    async def settle_rejected(self, key, record, status):
        facts = {
            **record,
            "outcome": "upstream_rejected",
            "observed_at": datetime.now(UTC).isoformat(),
            "upstream_status": status,
        }
        async with self.db.pool.acquire() as conn:
            await self.db.complete_effect(conn, execution_key=key, result=facts)
            billing = getattr(self.db, "billing", None)
            from tin_lite.codex_api_pricing import price_response

            priced = price_response(record.get("pricing"), facts)
            if billing is not None and priced is not None:
                await billing.observe_operation(
                    conn, operation_id=key, nanos=priced[0], observation=priced[1]
                )

    async def observe_gemini(self, key, record, *, metadata, queries, model_version, request_id):
        tier = metadata.get("serviceTier")
        facts = {
            **record,
            "outcome": "response_received",
            "observed_at": datetime.now(UTC).isoformat(),
            "request_id": request_id[:200] if isinstance(request_id, str) else None,
            "model": record["requested_model"],
            "model_version": model_version[:150] if isinstance(model_version, str) else None,
            "service_tier": tier[:100] if isinstance(tier, str) else None,
            "usage": usage_facts(metadata, queries),
        }
        async with self.db.pool.acquire() as conn:
            await self.db.complete_effect(conn, execution_key=key, result=facts)
            billing = getattr(self.db, "billing", None)
            from tin_lite.codex_api_pricing import price_response

            priced = price_response(record.get("pricing"), facts)
            if billing is not None and priced is not None:
                await billing.observe_operation(
                    conn, operation_id=key, nanos=priced[0], observation=priced[1]
                )

    async def relay_gemini(self, run_id, grant, raw, target):
        model, operation = split_target(target)
        # The widest bounds before admission; the run's own contract is checked inside it.
        gemini_body(raw, max(CONTRACTS, key=lambda value: value["max_request_bytes"]))
        fingerprint = await self.fingerprint(run_id, model, operation, raw)
        route = gemini_route(model)
        key, record = await self.admit(
            run_id, grant, fingerprint, operation, request_bytes=len(raw), raw=raw, route=route
        )
        body = route.validate(raw, operation, record["contract"])
        stream = operation == "streamGenerateContent"
        try:
            upstream = await self.client.send(
                self.client.build_request(
                    "POST",
                    f"{UPSTREAM}/{model}:{operation}",
                    params={"alt": "sse"} if stream else None,
                    json=body,
                    headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
                    timeout=httpx.Timeout(15, read=STREAM_IDLE_SECONDS)
                    if stream
                    else httpx.USE_CLIENT_DEFAULT,
                ),
                stream=True,
            )
        except httpx.HTTPError:
            # Nothing confirms Google never received it: the intent stays unconfirmed.
            raise RelayRejected(
                502, "Agent model request outcome is unconfirmed", reason="upstream_unreachable"
            ) from None
        if upstream.status_code != 200:
            status = upstream.status_code
            await upstream.aclose()
            logger.warning(
                "Agent API upstream rejected run=%s model=%s upstream_status=%s",
                run_id,
                model,
                status,
            )
            # Google bills nothing for a refused request; the harness may retry it.
            await self.settle_rejected(key, record, status)
            raise RelayRejected(
                429 if status == 429 else 502,
                "Google rejected the agent's model request",
                reason="rate_limited" if status == 429 else "upstream_rejected",
                upstream_status=status,
            )
        request_id = upstream.headers.get("x-request-id")
        if not stream:
            try:
                payload = bytearray()
                async for chunk in upstream.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_EVENT_BYTES:
                        raise ValueError("response exceeds its bound")
                parsed = json.loads(payload)
                metadata = object_value(parsed.get("usageMetadata"))
                if not metadata:
                    raise ValueError("response has no usage")
                await self.observe_gemini(
                    key,
                    record,
                    metadata=metadata,
                    queries=search_queries(parsed),
                    model_version=parsed.get("modelVersion"),
                    request_id=request_id,
                )
                return Response(bytes(payload), media_type="application/json")
            except (ValueError, AttributeError, httpx.HTTPError):
                await self.note_rejection(
                    run_id, grant, status=502, reason="stream_unconfirmed", operation=operation
                )
                raise HTTPException(502, "Agent model outcome is unconfirmed") from None
            finally:
                await upstream.aclose()
        return self.stream_response(run_id, grant, key, record, upstream, operation, request_id)

    def stream_response(self, run_id, grant, key, record, upstream, operation, request_id):
        # As on the Codex route, one task reads and settles the provider stream whether or not
        # the harness stays; the response only forwards its events.
        events: asyncio.Queue = asyncio.Queue()
        failed = object()
        forwarding = {"open": True, "queued": 0}

        def forward(item, size):
            if not forwarding["open"]:
                return
            forwarding["queued"] += size
            if forwarding["queued"] > MAX_STREAM_BYTES:
                raise RuntimeError("Agent API stream exceeds its bound")
            events.put_nowait(item)

        async def read():
            ended = False
            try:
                async with asyncio.timeout(STREAM_SECONDS):
                    await read_events()
                ended = True
            except (TimeoutError, httpx.HTTPError, ValueError, RuntimeError, RecursionError):
                await self.note_rejection(
                    run_id, grant, status=502, reason="stream_unconfirmed", operation=operation
                )
            except Exception:  # noqa: BLE001 — e.g. settling failed; the receipt stays unconfirmed
                logger.warning("Agent API stream was not settled run=%s", run_id)
            finally:
                events.put_nowait(None if ended else failed)
                await upstream.aclose()

        async def read_events():
            buffer = b""
            metadata, queries, version = None, 0, None
            pending = []
            async for chunk in upstream.aiter_bytes():
                buffer += chunk
                if len(buffer) > MAX_EVENT_BYTES:
                    raise RuntimeError("Agent API event exceeds its bound")
                while boundary := re.search(rb"\r?\n\r?\n", buffer):
                    event, buffer = buffer[: boundary.start()], buffer[boundary.end() :]
                    data = b"\n".join(
                        line[5:].lstrip()
                        for line in event.splitlines()
                        if line.startswith(b"data:")
                    )
                    if data:
                        message = json.loads(data)
                        if not isinstance(message, dict):
                            raise ValueError("Invalid agent stream event")
                        if usage := object_value(message.get("usageMetadata")):
                            metadata = usage
                        queries = max(queries, search_queries(message))
                        version = message.get("modelVersion") or version
                    # Gemini's stream has no terminal event: hold the newest event back until
                    # the next one arrives, so the last reaches the harness only after the
                    # usage it carries is settled.
                    if pending:
                        forward(pending.pop(), len(event))
                    pending.append(event + b"\n\n")
            if buffer.strip() or metadata is None:
                raise RuntimeError("Agent API stream ended without its usage")
            await self.observe_gemini(
                key,
                record,
                metadata=metadata,
                queries=queries,
                model_version=version,
                request_id=request_id,
            )
            for item in pending:
                forward(item, len(item))

        reader = asyncio.create_task(read())
        self._readers.add(reader)
        reader.add_done_callback(self._readers.discard)

        async def stream():
            try:
                while (item := await events.get()) is not None:
                    if item is failed:
                        yield (
                            b'data: {"error":{"code":502,"status":"UNAVAILABLE",'
                            b'"message":"Agent model stream outcome is unconfirmed"}}\n\n'
                        )
                        return
                    yield item
            finally:
                forwarding["open"] = False

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
        )


@router.post("/internal/agent-api/{run_id}/gemini/v1beta/models/{target}", include_in_schema=False)
async def relay_gemini(run_id: UUID, target: str, request: Request):
    relay = getattr(request.app.state.runtime, "agent_api", None)
    # The harness sends its configured API key, which is this run's grant.
    grant = request.headers.get("x-goog-api-key", "")
    if relay is None or not 32 <= len(grant) <= 200:
        raise HTTPException(403, "Agent API access is unavailable")
    if request.headers.get("content-encoding", "identity") != "identity":
        raise HTTPException(415, "Compressed agent requests are not supported")
    if target.partition(":")[0] in REFUSED_MODELS:
        # Not the run's cause and never bought: answered like Google would, nothing recorded.
        return Response(_REFUSED_BODY, status_code=400, media_type="application/json")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            _log_rejection(run_id, target, 413, "Agent request exceeds its context bound")
            await relay.note_rejection(
                run_id, grant, status=413, reason="context_bound", operation=target
            )
            raise HTTPException(413, "Agent request exceeds its context bound")
    try:
        return await asyncio.wait_for(
            relay.relay_gemini(run_id, grant, bytes(raw), target), timeout=200
        )
    except TimeoutError:
        _log_rejection(run_id, target, 504, "Agent API request outcome is unconfirmed")
        await relay.note_rejection(
            run_id, grant, status=504, reason="relay_timeout", operation=target
        )
        raise HTTPException(504, "Agent API request outcome is unconfirmed") from None
    except HTTPException as exc:
        reason = getattr(exc, "reason", None)
        _log_rejection(run_id, target, exc.status_code, exc.detail, reason)
        if reason in PARALLEL_REJECTIONS or reason in {"rate_limited", "upstream_rejected"}:
            # A subagent waiting on the others, or a refusal Google billed nothing for and the
            # harness retries: neither is the run's cause.
            raise
        await relay.note_rejection(
            run_id,
            grant,
            status=exc.status_code,
            reason=reason,
            operation=target,
            upstream_status=getattr(exc, "upstream_status", None),
        )
        raise
