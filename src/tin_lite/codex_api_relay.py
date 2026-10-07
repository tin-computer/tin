"""Bounded Responses transport for a run-bound Codex controller, not a public API proxy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
from datetime import UTC, datetime
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from tin_lite.billing_contracts import BillingError
from tin_lite.codex_api import (
    ATTEMPT,
    CONTRACT,
    DIAGRAM_CONTRACT,
    MULTI_TOOL_CONTRACTS,
    SESSION_CONTRACT,
    STOP_MESSAGES,
    USAGE,
    attempt_key,
    decode_record,
    is_api_contract,
    is_procedure_contract,
    is_session_contract,
    record_relay_rejection,
    token_hash,
)
from tin_lite.codex_api_pricing import price_response
from tin_lite.codex_web_evidence import WebEvidence
from tin_lite.usage_capture import count, object_value
from tin_lite.workflow_costs import session_funded

logger = logging.getLogger(__name__)

router = APIRouter()
MAX_EVENT_BYTES = 2 * 1024 * 1024
UPSTREAM = "https://api.openai.com/v1"


class AdmissionStopped(HTTPException):
    def __init__(self, status, detail, *, key, reason):
        super().__init__(status, detail)
        self.key, self.reason = key, reason


class RelayRejected(HTTPException):
    """A fixed rejection that also names an allowlisted reason for logs and the attempt."""

    def __init__(self, status, detail, *, reason, upstream_status=None):
        super().__init__(status, detail)
        self.reason = reason
        self.upstream_status = upstream_status


CONTRACT_REJECTION = "This API request exceeds its reserved execution contract"
# Historical v1/v2 observations count every web_search_call as one search.
MULTI_TOOL_PROTOCOLS = frozenset(contract["protocol"] for contract in MULTI_TOOL_CONTRACTS)


def web_usage(output, protocol):
    if not isinstance(output, list):
        return {"web_search_calls": None}
    calls = [
        item for item in output if isinstance(item, dict) and item.get("type") == "web_search_call"
    ]
    if protocol not in MULTI_TOOL_PROTOCOLS:
        return {"web_search_calls": len(calls)}  # Preserve historical observations.
    counts = {"search": 0, "open_page": 0, "find_in_page": 0}
    for item in calls:
        action = object_value(item.get("action")).get("type")
        if item.get("status") != "completed" or action not in counts:
            return {"web_search_calls": None}  # Incomplete/unknown is not free usage.
        counts[action] += 1
    # The tool-call fee applies to search actions, not opening/finding in pages.
    # Keep page actions visible as metadata; their model tokens are priced normally.
    return {
        "web_search_calls": counts["search"],
        "web_open_pages": counts["open_page"],
        "web_find_calls": counts["find_in_page"],
    }


def request_body(raw: bytes, operation: str, contract=CONTRACT):
    if len(raw) > contract["max_request_bytes"]:
        raise RelayRejected(
            413, "Codex API request exceeds its pinned context bound", reason="context_bound"
        )
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "Invalid Codex API request") from None
    if not isinstance(body, dict) or body.get("model") != contract["model"]:
        raise HTTPException(400, "Codex API model does not match the pinned route")
    # This pilot sends explicit context, not shared-account stored objects. Do not
    # expose another project's responses/files/vector stores using the common key.
    forbidden = {"previous_response_id", "conversation", "file_id", "vector_store_ids"}

    def check(value):
        if isinstance(value, dict):
            if any(value.get(key) is not None for key in forbidden):
                raise HTTPException(400, "Stored provider objects are not supported by this route")
            if isinstance(value.get("type"), str) and value["type"] in {
                "item_reference",
                "input_file",
                "file_search",
                "code_interpreter",
            }:
                raise HTTPException(400, "Provider-hosted files and execution are not supported")
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    try:
        check(body)
    except RecursionError:
        raise HTTPException(400, "Codex API request is nested too deeply") from None
    if contract == DIAGRAM_CONTRACT:

        def without_images(value):
            if isinstance(value, list):
                return [without_images(item) for item in value]
            if not isinstance(value, dict):
                return value
            result = {key: without_images(item) for key, item in value.items()}
            url = value.get("image_url")
            if (
                value.get("type") == "input_image"
                and isinstance(url, str)
                and url.startswith(
                    ("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")
                )
            ):
                result["image_url"] = ""
            return result

        if (
            len(json.dumps(without_images(body), separators=(",", ":")).encode())
            > contract["max_non_image_bytes"]
        ):
            raise RelayRejected(
                413, "Codex API text exceeds its pinned context bound", reason="text_bound"
            )
    if body.get("background") not in (None, False):
        raise HTTPException(400, "Background API execution is not supported")
    if operation == "responses":
        if body.get("stream") is not True:
            raise HTTPException(400, "Codex API responses must use streaming")
        tools = body.get("tools", [])

        def supported(tool, *, nested=False):
            if not isinstance(tool, dict):
                return False
            kind = tool.get("type")
            if not isinstance(kind, str):
                return False
            # Codex groups local MCP functions in namespaces, including on the
            # isolated v1 route. This is packaging, not provider-hosted execution;
            # validate every member without changing the pinned execution bounds.
            if kind == "namespace" and not nested:
                members = tool.get("tools")
                return isinstance(members, list) and all(
                    supported(member, nested=True) for member in members
                )
            return kind in (
                {"function", "custom"} if nested else {"function", "custom", "web_search"}
            )

        if not isinstance(tools, list) or not all(supported(tool) for tool in tools):
            raise HTTPException(400, "Unsupported tool on the Codex API route")
        inputs = body.get("input")
        for item in inputs if isinstance(inputs, list) else ():
            if isinstance(item, dict) and item.get("type") == "additional_tools":
                additional = item.get("tools")
                if not isinstance(additional, list) or not all(supported(t) for t in additional):
                    raise HTTPException(400, "Unsupported additional Codex API tool")
        if is_procedure_contract(contract) and not any(
            tool.get("type") == "web_search" for tool in tools
        ):
            # The pinned CLI omits hosted search for custom providers. Tin supplies
            # OpenAI's native Responses tool, executed upstream (not a shell/MCP
            # credential path). Its usage is already reserved and observed here.
            body["tools"] = [*tools, {"type": "web_search"}]
        requested = body.get("max_output_tokens", contract["max_output_tokens"])
        if type(requested) is not int or requested < 1:
            raise HTTPException(400, "Invalid Codex output bound")
        body.update(
            store=False,
            service_tier="default",
            max_output_tokens=min(requested, contract["max_output_tokens"]),
        )
        if contract in MULTI_TOOL_CONTRACTS:
            # Do not cut off search -> open -> find within one model step. Run
            # reservations, usage settlement, token limits and timeouts remain.
            body.pop("max_tool_calls", None)
            include = body.get("include", [])
            if not isinstance(include, list) or not all(
                isinstance(value, str) for value in include
            ):
                raise HTTPException(400, "Invalid Codex response includes")
            body["include"] = list(dict.fromkeys([*include, "web_search_call.results"]))
        else:
            body["max_tool_calls"] = 1  # Historical v1/v2 grants retain their terms.
    elif operation != "responses/compact":
        raise HTTPException(404, "Unsupported Codex API operation")
    return body


def request_identity(headers, raw, operation):
    # Codex 0.156.1 uses its THREAD id for x-client-request-id, not a per-call id.
    # Within our single-turn controller, a new model step changes explicit context.
    # Bind identical-request rejection to the actual turn/window as well as bytes.
    try:
        metadata = json.loads(headers.get("x-codex-turn-metadata", "{}"))
    except (ValueError, RecursionError):
        raise HTTPException(400, "Invalid Codex request identity") from None
    if not isinstance(metadata, dict):
        raise HTTPException(400, "Invalid Codex request identity")
    identity = [metadata.get(name) for name in ("thread_id", "turn_id", "window_id")]
    if any(not isinstance(value, str) or not 1 <= len(value) <= 200 for value in identity):
        raise HTTPException(400, "Codex controller request identity is required")
    fingerprint = hashlib.sha256(
        json.dumps([operation, *identity], separators=(",", ":")).encode() + b"\0" + raw
    ).hexdigest()
    return fingerprint


class CodexAPIRelay:
    def __init__(self, *, database, api_key: str, client: httpx.AsyncClient | None = None):
        self.db = database
        self._api_key = api_key
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(180, connect=15), follow_redirects=False, trust_env=False
        )

    async def close(self):
        await self.client.aclose()

    async def note_rejection(self, run_id, grant, **facts):
        """Persist the rejection's cause on the attempt; never mask the rejection itself."""
        try:
            await asyncio.wait_for(
                record_relay_rejection(self.db.pool, run_id, grant, **facts), timeout=5
            )
        except Exception:  # noqa: BLE001 — logging already names the cause
            logger.warning("Codex API rejection cause was not recorded run=%s", run_id)

    async def admit(self, run_id, grant, fingerprint, operation, *, request_bytes=0, raw=None):
        try:
            return await self._admit(
                run_id, grant, fingerprint, operation, request_bytes=request_bytes, raw=raw
            )
        except AdmissionStopped as exc:
            # Admission rolled back and released its connection/run lock. Keep only
            # our allowlisted code, never the request body or provider error text.
            await self.db.pool.execute(
                """UPDATE effect_receipts SET result=result || $2::jsonb
                   WHERE execution_key=$1 AND operation=$3 AND status='started'
                     AND result->>'grant_sha256'=$4 AND NOT result ? 'stop_reason'""",
                exc.key,
                json.dumps({"stop_reason": exc.reason}),
                ATTEMPT,
                token_hash(grant),
            )
            raise

    async def _admit(self, run_id, grant, fingerprint, operation, *, request_bytes=0, raw=None):
        async with self.db.pool.acquire() as conn, conn.transaction():
            # Serialize short admissions, NOT model streaming or the activity's
            # session-long advisory lock. Stop/replacement lock the same run row.
            run = await conn.fetchrow("SELECT * FROM workflow_runs WHERE id=$1 FOR UPDATE", run_id)
            turn_number = (
                run["task_turn_number"] + 1
                if run is not None and run["executor"] == "project.task"
                else None
            )
            key = (
                f"{run_id}:task_turn:{turn_number}:codex-api:{fingerprint}"
                if turn_number is not None
                else f"{run_id}:codex-api:{fingerprint}"
            )
            row = await conn.fetchrow(
                "SELECT * FROM effect_receipts WHERE execution_key=$1",
                attempt_key(run_id, turn_number),
            )
            record = decode_record(row["result"]) if row else {}
            if (
                run is None
                or row is None
                or row["operation"] != ATTEMPT
                or row["status"] != "started"
                or record.get("outcome") != "running"
                or not is_api_contract(record.get("contract"))
                or not secrets.compare_digest(record.get("grant_sha256", ""), token_hash(grant))
                or datetime.fromisoformat(record["expires_at"]) <= datetime.now(UTC)
                or run["status"] not in {"pending", "running"}
                or (
                    turn_number is not None
                    and (
                        record.get("turn_number") != turn_number
                        or run["task_control"] in {"pause", "stop"}
                    )
                )
                or not run["lease_active"]
                or any(
                    str(run[name]) != str(record.get(name))
                    for name in (
                        "project_id",
                        "sandbox_id",
                        "generation",
                        "fencing_token",
                        "thread_id",
                        "lease_owner",
                    )
                )
            ):
                raise HTTPException(403, "Codex API grant is expired or no longer active")
            contract = record["contract"]
            if record.get("stop_reason") in STOP_MESSAGES:
                raise HTTPException(409, STOP_MESSAGES[record["stop_reason"]])

            def stopped(exc):
                if exc.code in STOP_MESSAGES:
                    return AdmissionStopped(
                        exc.status,
                        exc.diagnostic(),
                        key=attempt_key(run_id, turn_number),
                        reason=exc.code,
                    )
                return RelayRejected(exc.status, exc.diagnostic(), reason=exc.code)

            if raw is not None:
                # Validate against this run's contract before any paid intent. The
                # HTTP body ceiling alone must not upgrade a historical v1 grant.
                request_body(raw, operation, contract)
            if run["started_by_clerk_user_id"] and not await conn.fetchval(
                """SELECT EXISTS(SELECT 1 FROM project_memberships
                   WHERE project_id=$1 AND clerk_user_id=$2)""",
                run["project_id"],
                run["started_by_clerk_user_id"],
            ):
                raise HTTPException(403, "The initiating member no longer has project access")
            enrolled = await conn.fetchval(
                """SELECT EXISTS(SELECT 1 FROM projects p JOIN billing_accounts b
                   ON b.workspace_id=p.workspace_id WHERE p.id=$1 AND b.run_billing_enabled)""",
                run["project_id"],
            )
            budget = decode_record(
                await conn.fetchval("SELECT terms FROM billing_run_budgets WHERE run_id=$1", run_id)
            )
            if not budget:
                from tin_lite.free_workflows import api_terms_for_included

                budget = await api_terms_for_included(self.db, run_id, conn=conn) or {}
            billing = getattr(self.db, "billing", None)
            if enrolled and (
                billing is None
                or budget.get("kind") != "codex_api"
                or budget.get("pricing") != record.get("pricing")
            ):
                raise HTTPException(403, "Codex API execution requires its reserved credit budget")
            if enrolled:
                if operation != "responses":
                    raise RelayRejected(422, CONTRACT_REJECTION, reason="operation_not_allowed")
                if request_bytes > budget["request_maximum_input_bytes"]:
                    raise RelayRejected(422, CONTRACT_REJECTION, reason="request_too_large")
                if budget.get("codex_contract", CONTRACT) != contract:
                    raise RelayRejected(422, CONTRACT_REJECTION, reason="contract_mismatch")
            if await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM effect_receipts WHERE execution_key=$1)", key
            ):
                raise HTTPException(
                    409, "This model request was already attempted; do not repurchase"
                )
            request_number = None
            if is_session_contract(contract):
                if not enrolled or billing is None or not session_funded(budget):
                    raise HTTPException(403, "This session requires its funded spending budget")
                try:
                    await billing.begin_operation(
                        conn, run_id=run_id, operation_id=key, kind="codex_api", maximum=None
                    )
                except BillingError as exc:
                    raise stopped(exc) from None
            else:
                rows = await conn.fetch(
                    """SELECT status, result FROM effect_receipts
                       WHERE operation=$1 AND result->>'run_id'=$2 LIMIT $3""",
                    USAGE,
                    str(run_id),
                    contract["max_requests"] + 1,
                )
                if any(item["status"] != "completed" for item in rows):
                    raise HTTPException(409, "A prior API request is active or unconfirmed")
                if len(rows) >= contract["max_requests"]:
                    raise stopped(
                        BillingError("request_limit", "Codex API request limit reached", 429)
                    )
                tokens = sum(
                    (decode_record(item["result"]).get("usage") or {}).get("total_tokens") or 0
                    for item in rows
                )
                if tokens >= contract["max_observed_tokens"]:
                    raise stopped(BillingError("token_limit", "Codex API token limit reached", 429))
                if enrolled:
                    for item in rows:
                        previous = decode_record(item["result"])
                        priced = price_response(budget["pricing"], previous)
                        if priced is None or priced[0] > budget["request_maximum_nanos"]:
                            raise HTTPException(
                                409, "Prior API usage is unresolved or exceeded its reservation"
                            )
                    try:
                        await billing.begin_operation(
                            conn,
                            run_id=run_id,
                            operation_id=key,
                            kind="codex_api",
                            maximum=lambda terms: terms["request_maximum_nanos"],
                        )
                    except BillingError as exc:
                        raise stopped(exc) from None
                request_number = len(rows) + 1
            observation = {
                "version": 1,
                "execution_kind": "codex_openai_api",
                "run_id": str(run_id),
                "project_id": str(run["project_id"]),
                "generation": run["generation"],
                "definition_commit_sha": run["definition_commit_sha"],
                "provider": "openai",
                "requested_model": contract["model"],
                "requested_service_tier": "default" if operation == "responses" else None,
                "protocol": contract["protocol"],
                "contract": contract,
                "endpoint": operation,
                "request_fingerprint": fingerprint,
                "request_number": request_number,
                "attempted_at": datetime.now(UTC).isoformat(),
                "outcome": "unconfirmed",
                "usage": None,
                "supplier_cost": None,
                "pricing": record.get("pricing"),
            }
            await self.db.start_effect(conn, execution_key=key, operation=USAGE)
            await self.db.save_effect_progress(conn, execution_key=key, result=observation)
        return key, observation

    async def observe(self, key, record, response, *, request_id=None):
        usage = object_value(response.get("usage"))
        inputs = object_value(usage.get("input_tokens_details"))
        outputs = object_value(usage.get("output_tokens_details"))
        output = response.get("output")
        facts = {
            **record,
            "outcome": "response_received",
            "observed_at": datetime.now(UTC).isoformat(),
            "request_id": request_id[:200] if isinstance(request_id, str) else None,
            "response_id": response.get("id")[:200]
            if isinstance(response.get("id"), str)
            else None,
            "response_object": "response.compaction"
            if response.get("object") == "response.compaction"
            else None,
            "model": response.get("model")[:150]
            if isinstance(response.get("model"), str)
            else None,
            "response_status": response.get("status")
            if response.get("status") in {"completed", "incomplete", "failed"}
            else None,
            "service_tier": response.get("service_tier")[:100]
            if isinstance(response.get("service_tier"), str)
            else None,
            "usage": {
                "requests": 1,
                "input_tokens": count(usage.get("input_tokens")),
                "output_tokens": count(usage.get("output_tokens")),
                "total_tokens": count(usage.get("total_tokens")),
                "cached_input_tokens": count(inputs.get("cached_tokens")),
                "cache_write_input_tokens": count(inputs.get("cache_write_tokens")),
                "reasoning_tokens": count(outputs.get("reasoning_tokens")),
                **web_usage(output, record.get("protocol")),
            },
        }
        async with self.db.pool.acquire() as conn:
            await self.db.complete_effect(conn, execution_key=key, result=facts)
            billing = getattr(self.db, "billing", None)
            priced = price_response(record.get("pricing"), facts)
            if billing is not None and priced is not None:
                await billing.observe_operation(
                    conn, operation_id=key, nanos=priced[0], observation=priced[1]
                )

    async def relay(self, run_id, grant, raw, operation, headers):
        request_body(raw, operation, SESSION_CONTRACT)
        fingerprint = request_identity(headers, raw, operation)
        upstream_headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if operation == "responses" else "application/json",
        }
        # Preserve only the pinned Codex protocol negotiation, never arbitrary auth,
        # account/project headers or sandbox-provided upstream destinations.
        for name in ("x-codex-beta-features", "x-openai-internal-codex-responses-lite"):
            if value := headers.get(name):
                if len(value) > 200:
                    raise HTTPException(400, "Invalid Codex protocol header")
                upstream_headers[name] = value
        key, record = await self.admit(
            run_id, grant, fingerprint, operation, request_bytes=len(raw), raw=raw
        )
        # Carry the actual admitted bounds; protocol alone does not identify the
        # diagram image allowance. No customer/sandbox value supplies this record.
        contract = record["contract"]
        body = request_body(raw, operation, contract)
        if is_procedure_contract(contract):
            # Lite disallows provider-hosted search. Standard Responses accepts
            # the same additional_tools/context items and SSE protocol unchanged.
            upstream_headers.pop("x-openai-internal-codex-responses-lite", None)
        try:
            upstream = await self.client.send(
                self.client.build_request(
                    "POST", f"{UPSTREAM}/{operation}", json=body, headers=upstream_headers
                ),
                stream=True,
            )
        except httpx.HTTPError:
            # Intent stays unconfirmed. HTTPX and Codex are configured not to retry.
            raise RelayRejected(
                502, "Codex API request outcome is unconfirmed", reason="upstream_unreachable"
            ) from None
        if upstream.status_code != 200:
            status = upstream.status_code
            await upstream.aclose()
            # Provider bodies can echo prompt content; keep only the status code.
            logger.warning(
                "Codex API upstream rejected run=%s operation=%r upstream_status=%s",
                run_id,
                operation,
                status,
            )
            raise RelayRejected(
                429 if status == 429 else 502,
                "OpenAI rejected the Codex API request; the attempt will not be replayed",
                reason="upstream_rejected",
                upstream_status=status,
            )

        if operation == "responses/compact":
            try:
                payload = bytearray()
                async for chunk in upstream.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_EVENT_BYTES:
                        raise HTTPException(502, "Codex compaction response exceeds its bound")
                parsed = json.loads(payload)
                if not isinstance(parsed, dict):
                    raise ValueError("invalid compact response")
                await self.observe(
                    key, record, parsed, request_id=upstream.headers.get("x-request-id")
                )
                return Response(bytes(payload), media_type="application/json")
            except (ValueError, httpx.HTTPError):
                raise HTTPException(502, "Codex compaction outcome is unconfirmed") from None
            finally:
                await upstream.aclose()

        async def stream():
            buffer = b""
            observed = False
            evidence = WebEvidence() if contract in MULTI_TOOL_CONTRACTS else None
            inserted = 0
            sequence = 0
            try:
                async for chunk in upstream.aiter_bytes():
                    buffer += chunk
                    if len(buffer) > MAX_EVENT_BYTES:
                        raise RuntimeError("Codex API event exceeds its bound")
                    while boundary := re.search(rb"\r?\n\r?\n", buffer):
                        event, buffer = buffer[: boundary.start()], buffer[boundary.end() :]
                        data = b"\n".join(
                            line[5:].lstrip()
                            for line in event.splitlines()
                            if line.startswith(b"data:")
                        )
                        if data and data != b"[DONE]":
                            message = json.loads(data)
                            if not isinstance(message, dict):
                                raise ValueError("Invalid Codex stream event")
                            if message.get("type") in {
                                "response.completed",
                                "response.incomplete",
                                "response.failed",
                            }:
                                response = message.get("response")
                                if isinstance(response, dict) and not observed:
                                    await self.observe(
                                        key,
                                        record,
                                        response,
                                        request_id=upstream.headers.get("x-request-id"),
                                    )
                                    observed = True
                                    if response.get("status") == "failed":
                                        # OpenAI can fail a response inside a 200 stream; name
                                        # the cause by code only, never the provider's text.
                                        await self.note_rejection(
                                            run_id,
                                            grant,
                                            **failed_response_cause(response),
                                            operation=operation,
                                        )
                            if evidence is not None:
                                original_index = message.get("output_index")
                                if type(original_index) is int:
                                    message["output_index"] = original_index + inserted
                                for adapted in evidence.events(message):
                                    if (
                                        adapted is not message
                                        and adapted.get("type") == "response.output_item.done"
                                        and "output_index" not in adapted
                                    ):
                                        inserted += 1
                                        adapted["output_index"] = (original_index or 0) + inserted
                                    adapted["sequence_number"] = sequence
                                    sequence += 1
                                    yield b"data: " + json.dumps(adapted).encode() + b"\n\n"
                                continue
                        # Persist final usage BEFORE returning the completed event to Codex.
                        yield event + b"\n\n"
                if buffer.strip() or not observed:
                    raise RuntimeError("Codex API stream ended without a terminal response")
            except (httpx.HTTPError, ValueError, RuntimeError, RecursionError):
                # Never expose provider exception text, partial private body, or credentials.
                await self.note_rejection(
                    run_id, grant, status=502, reason="stream_unconfirmed", operation=operation
                )
                yield (
                    b'data: {"type":"error",'
                    b'"message":"Codex API stream outcome is unconfirmed"}\n\n'
                )
            finally:
                await upstream.aclose()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(upstream.aclose),
        )


def failed_response_cause(response):
    """A failed streamed response's cause: OpenAI's rate limit, or a generic upstream failure."""
    error = response.get("error")
    if isinstance(error, dict) and error.get("code") == "rate_limit_exceeded":
        return {"status": 429, "reason": "rate_limited", "upstream_status": 200}
    return {"status": 502, "reason": "upstream_failed", "upstream_status": 200}


@router.post("/internal/codex-api/{run_id}/v1/{operation:path}", include_in_schema=False)
async def relay_codex_api(run_id: UUID, operation: str, request: Request):
    relay = getattr(request.app.state.runtime, "codex_api", None)
    grant = request.headers.get("x-tin-codex-grant", "")
    if relay is None or not 32 <= len(grant) <= 200:
        raise HTTPException(403, "Codex API access is unavailable")
    if request.headers.get("content-encoding", "identity") != "identity":
        raise HTTPException(415, "Compressed Codex requests are not supported")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > DIAGRAM_CONTRACT["max_request_bytes"]:
            _log_rejection(run_id, operation, 413, "Codex API request exceeds its context bound")
            await relay.note_rejection(
                run_id, grant, status=413, reason="context_bound", operation=operation
            )
            raise HTTPException(413, "Codex API request exceeds its context bound")
    try:
        return await asyncio.wait_for(
            relay.relay(run_id, grant, bytes(raw), operation, request.headers), timeout=200
        )
    except TimeoutError:
        _log_rejection(run_id, operation, 504, "Codex API request outcome is unconfirmed")
        await relay.note_rejection(
            run_id, grant, status=504, reason="relay_timeout", operation=operation
        )
        raise HTTPException(504, "Codex API request outcome is unconfirmed") from None
    except HTTPException as exc:
        reason = getattr(exc, "reason", None)
        _log_rejection(run_id, operation, exc.status_code, exc.detail, reason)
        await relay.note_rejection(
            run_id,
            grant,
            status=exc.status_code,
            reason=reason,
            operation=operation,
            upstream_status=getattr(exc, "upstream_status", None),
        )
        raise


def _log_rejection(run_id, operation, status, detail, reason=None):
    # Details are Tin's own fixed messages; never log request bodies or grants.
    logger.warning(
        "Codex API relay rejected run=%s operation=%r status=%s reason=%s detail=%s",
        run_id,
        operation,
        status,
        reason,
        detail,
    )
