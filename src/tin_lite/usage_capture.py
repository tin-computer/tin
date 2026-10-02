"""Trusted usage observations, never money balances or editable artifact claims."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

_connection = ContextVar("usage_effect_connection", default=None)
_scope = ContextVar("external_usage_scope", default=None)


@contextmanager
def effect_connection(database, conn):
    token = _connection.set((database, conn))
    try:
        yield
    finally:
        _connection.reset(token)


def borrowed_connection(database):
    current = _connection.get()
    return current[1] if current and current[0] is database else None


@contextmanager
def external_usage_scope(database, conn, run_id, step, *, maximum_usd=None):
    token = _scope.set((database, conn, UUID(str(run_id)), step, maximum_usd))
    try:
        yield
    finally:
        _scope.reset(token)


def count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def dollars(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        return str(amount) if amount.is_finite() and 0 <= amount <= 10**9 else None
    except (InvalidOperation, ValueError):
        return None


def object_value(value):
    return value if isinstance(value, dict) else {}


def billable_search_calls(output):
    if not isinstance(output, list):
        return None
    searches = 0
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        # A completed response can contain an ignored over-limit search attempt.
        # Charge only confirmed completed actions; Tin absorbs uncertain tool fees.
        if item.get("status") in {"failed", "in_progress", "searching", "incomplete"}:
            continue
        action = object_value(item.get("action")).get("type")
        if item.get("status") != "completed" or action not in {
            "search",
            "open_page",
            "find_in_page",
        }:
            return None
        searches += action == "search"
    return searches


class ObservationAlreadyRecorded(RuntimeError):
    """A metered request for this run step was already attempted.

    Its outcome may be unknown, so it is never silently repeated; callers convert
    this into a terminal run error.
    """


def observation_key(run_id, step, endpoint):
    return f"usage:{run_id}:external:{hashlib.sha256(f'{step}:{endpoint}'.encode()).hexdigest()}"


async def begin_observation(provider, category, endpoint, *, request=None):
    scope = _scope.get()
    if scope is None:
        return None
    database, conn, run_id, step, maximum_usd = scope
    key = observation_key(run_id, step, endpoint)
    existing = await database.get_effect(key, conn=conn)
    if existing is not None:
        # The owning workflow reconciles its output. An observation is not permission
        # to repeat a request whose outcome is unknown.
        raise ObservationAlreadyRecorded(
            "External request already observed; recover its owning effect"
        )
    record = {
        "version": 1,
        "run_id": str(run_id),
        "provider": provider,
        "category": category,
        "endpoint": endpoint,
        "step": step,
        "attempted_at": datetime.now(UTC).isoformat(),
        "outcome": "unconfirmed",
        "usage": None,
        "reported_cost_usd": None,
    }
    if category == "model":
        # The call's output cap, so a response that stopped at it is visible.
        record["max_output_tokens"] = count((request or {}).get("max_output_tokens", 16_384))
    billing = getattr(database, "billing", None)
    if billing is not None:
        from tin_lite.service_pricing import amount_nanos, model_maximum

        if category == "model":
            import json

            request = request or {}
            searches = request.get("max_tool_calls", 8) if request.get("tools") else 0
            input_bound = (
                len(json.dumps(request, ensure_ascii=False).encode()) + 4096 + searches * 16_384
            )
            maximum = lambda terms: model_maximum(  # noqa: E731
                terms,
                provider=provider,
                model=request.get("model"),
                input_tokens=input_bound,
                output_tokens=request.get("max_output_tokens", 16_384),
                searches=searches,
            )
        else:
            maximum = amount_nanos(maximum_usd)
        await billing.begin_operation(
            conn,
            run_id=run_id,
            operation_id=key,
            kind="native_model" if category == "model" else "tool",
            maximum=maximum,
        )
    await database.start_effect(conn, execution_key=key, operation="external_usage_v1")
    await database.save_effect_progress(conn, execution_key=key, result=record)
    return database, conn, key, record


async def observe_failure(observation, *, kind, status_code=None):
    """Keep bounded transport facts, not errors/headers/content or guessed supplier cost."""
    if observation is None:
        return
    if kind not in {"timeout", "connection", "http", "api"}:
        raise ValueError("Unknown provider failure kind")
    db, conn, key, record = observation
    failure = {"kind": kind, "observed_at": datetime.now(UTC).isoformat()}
    if type(status_code) is int and 100 <= status_code <= 599:
        failure["status_code"] = status_code
    # Acceptance/usage remain unconfirmed. This does not release funds or authorize retry.
    await db.save_effect_progress(conn, execution_key=key, result={**record, "failure": failure})


async def observe_response(observation, response):
    if observation is None:
        return
    db, conn, key, record = observation
    raw = object_value(response.get("usage"))
    inputs = object_value(raw.get("input_tokens_details"))
    outputs = object_value(raw.get("output_tokens_details"))
    tools = response.get("output")
    status = response.get("status")
    incomplete = object_value(response.get("incomplete_details")).get("reason")
    stop = incomplete if status == "incomplete" else status
    record = {
        **record,
        "model": str(response.get("model") or "")[:150] or None,
        "service_tier": str(response.get("service_tier") or "")[:40] or None,
        # The Responses API's own stop signal: its status, or why it is incomplete.
        "stop_reason": stop if isinstance(stop, str) and 0 < len(stop) <= 64 else None,
        "output_truncated": (
            status == "incomplete" and incomplete == "max_output_tokens"
            if isinstance(status, str)
            else None
        ),
        "outcome": "response_received",
        "observed_at": datetime.now(UTC).isoformat(),
        "usage": {
            "input_tokens": count(raw.get("input_tokens")),
            "output_tokens": count(raw.get("output_tokens")),
            "total_tokens": count(raw.get("total_tokens")),
            "cached_input_tokens": count(inputs.get("cached_tokens")),
            "cache_write_input_tokens": count(inputs.get("cache_write_tokens")),
            "reasoning_tokens": count(outputs.get("reasoning_tokens")),
            "billable_web_search_calls": billable_search_calls(tools),
            "unpriced_web_search_actions": (
                sum(
                    isinstance(item, dict)
                    and item.get("type") == "web_search_call"
                    and item.get("status") != "completed"
                    for item in tools
                )
                if isinstance(tools, list)
                else None
            ),
            "web_search_calls": (
                sum(
                    isinstance(item, dict) and item.get("type") == "web_search_call"
                    for item in tools
                )
                if isinstance(tools, list)
                else None
            ),
        },
    }
    await db.complete_effect(conn, execution_key=key, result=record)
    await _bill_observation(db, conn, key, record, "native_model")


async def observe_tool(observation, task):
    if observation is None:
        return
    db, conn, key, record = observation
    record = {
        **record,
        "outcome": "response_received",
        "observed_at": datetime.now(UTC).isoformat(),
        "reported_cost_usd": dollars(task.get("cost")),
        "usage": {"requests": 1},
    }
    await db.complete_effect(
        conn,
        execution_key=key,
        result=record,
    )
    await _bill_observation(db, conn, key, record, "tool")


async def recover_tool_observation(db, conn, *, run_id, step, endpoint, cost):
    """Recover a lost response from exact provider task metadata, never an artifact.

    The caller already matched the provider task to its pinned request. Do not
    manufacture an observation for historical calls or rewrite completed evidence.
    """
    if getattr(db, "billing", None) is None:
        return
    key = observation_key(run_id, step, endpoint)
    async with db.effect_lock(key, "external_usage_v1", conn=conn) as (locked, existing):
        if existing is None:
            return
        record = existing.result or {}
        if (
            record.get("provider") not in {"dataforseo", "gak", "google_ads"}
            or record.get("category") != "tool"
        ):
            return
        if existing.status == "completed":
            await _bill_observation(db, locked, key, record, "tool")
        elif dollars(cost) is not None:
            await observe_tool((db, locked, key, record), {"cost": cost})


async def _bill_observation(db, conn, key, record, kind):
    if getattr(db, "billing", None) is None:
        return
    from tin_lite.billing_contracts import object_value, receipt_charge

    budget = await conn.fetchval(
        "SELECT terms FROM billing_run_budgets WHERE run_id=$1", UUID(record["run_id"])
    )
    priced = receipt_charge(object_value(budget), kind, record) if budget else None
    if priced is not None:
        await db.billing.observe_operation(
            conn, operation_id=key, nanos=priced[0], observation=priced[1]
        )


async def observe_sandbox(database, sandbox_id, *, info=None, ended=False, absent=False):
    """Record provider start facts and observed cleanup, not a supplier invoice.

    A provider expiration deadline is never treated as actual shutdown time. Missing
    start/cleanup facts stay unknown; repeated cleanup cannot extend an earlier interval.
    """
    if database is None:
        return
    conn = borrowed_connection(database)
    key = f"sandbox-usage:{sandbox_id}"
    async with database.effect_lock(key, "sandbox_usage_v1", conn=conn) as (locked, existing):
        if existing and existing.status == "completed":
            return
        record = dict(existing.result or {}) if existing else {}
        if not record:
            metadata = getattr(info, "metadata", None) or {}
            if not metadata.get("run_id"):
                return
            run_id = UUID(metadata["run_id"])
            run = await database.get_run(run_id, conn=locked)
            if run is None:
                return
            start = getattr(info, "started_at", None)
            record = {
                "version": 1,
                "run_id": str(run_id),
                "provider": "e2b",
                "profile": metadata.get("profile", "default"),
                "started_at": start.isoformat() if isinstance(start, datetime) else None,
                "cpu_count": count(getattr(info, "cpu_count", None)),
                "memory_mb": count(getattr(info, "memory_mb", None)),
                "observed_wall_seconds": None,
                "outcome": "running",
            }
            await database.start_effect(locked, execution_key=key, operation="sandbox_usage_v1")
        if ended or absent:
            record["outcome"] = "deleted" if ended else "already_absent"
            if ended and record.get("started_at"):
                now = datetime.now(UTC)
                start = datetime.fromisoformat(record["started_at"])
                if start.tzinfo is not None and start <= now:
                    record["observed_wall_seconds"] = round((now - start).total_seconds(), 3)
                    record["cleanup_observed_at"] = now.isoformat()
            await database.complete_effect(locked, execution_key=key, result=record)
        else:
            await database.save_effect_progress(locked, execution_key=key, result=record)
