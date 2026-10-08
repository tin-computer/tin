"""Trusted run-scoped accounting for the native model service, not Codex OAuth usage.

The owning activity still receipts its output and decides how to recover. These receipts
retain observed usage even for rejected output and prevent an unconfirmed call being bought
again. They contain no prompts, model output, provider errors, or credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

import asyncpg

from tin_lite.db import Database
from tin_lite.domain import RunStatus
from tin_lite.model_providers import (
    ModelObservation,
    ModelProviderError,
    ModelRequest,
    ModelResult,
    ModelRoute,
    ModelUsage,
    model_failure_reason,
)

OPERATION = "native_model_usage_v1"


@dataclass(frozen=True)
class _CallScope:
    run_id: UUID
    step: str
    conn: asyncpg.Connection
    input_tokens: int | None = None


_scope: ContextVar[_CallScope | None] = ContextVar("native_model_usage_scope", default=None)


@contextmanager
def model_usage_scope(
    *,
    run_id: UUID | str,
    step: str,
    conn: asyncpg.Connection,
    input_tokens: int | None = None,
) -> Iterator[None]:
    """Set only by trusted activities, never by a client request or uploaded package.

    `input_tokens` replaces the default one-token-per-byte input reservation with the
    caller's own bound, which its pinned run terms must cover (code_models does this).
    """
    if not step or len(step) > 200:
        raise ValueError("model step must be a bounded stable identifier")
    if input_tokens is not None and (type(input_tokens) is not int or input_tokens < 1):
        raise ValueError("model input token bound must be a positive integer")
    token = _scope.set(
        _CallScope(run_id=UUID(str(run_id)), step=step, conn=conn, input_tokens=input_tokens)
    )
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def model_usage_step(label: str) -> Iterator[None]:
    """Distinguish bounded draft/repair calls inside one trusted native activity."""
    scope = _scope.get()
    token = _scope.set(replace(scope, step=f"{scope.step}:{label}") if scope else None)
    try:
        yield
    finally:
        _scope.reset(token)


class ModelUsageRecorder:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def generate(
        self,
        route: ModelRoute,
        request: ModelRequest,
        call: Callable[[], Awaitable[ModelResult]],
    ) -> ModelResult:
        scope = _scope.get()
        if scope is None:
            raise ValueError("native model service requires a trusted run usage scope")
        key = f"{scope.run_id}:model-usage:{hashlib.sha256(scope.step.encode()).hexdigest()}"
        # Reuse the activity's connection: nested pool acquisition can deadlock a busy worker.
        async with self.db.effect_lock(key, OPERATION, conn=scope.conn) as (conn, existing):
            if existing is not None:
                raise ModelProviderError(
                    "Model step was already attempted; recover the owning activity's result."
                )
            run = await self.db.get_run(scope.run_id, conn=conn)
            if run is None or run.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                raise ValueError("model usage scope is not an active run")
            record = {
                "version": 1,
                "execution_kind": "native_model_service",
                "run_id": str(run.id),
                "project_id": str(run.project_id),
                "workflow_id": str(run.workflow_id),
                "definition_commit_sha": run.definition_commit_sha,
                "step": scope.step,
                "route": route.key,
                "provider": route.provider.value,
                "requested_model": route.model,
                "request_sha256": hashlib.sha256(
                    json.dumps(asdict(request), sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "attempted_at": datetime.now(UTC).isoformat(),
                # The call's output cap, so a response that stopped at it is visible.
                "max_output_tokens": request.max_output_tokens,
                "outcome": "unconfirmed",
                "usage": asdict(ModelUsage()),
            }
            # Dispatch only after durable intent; a crash or cancellation leaves unknown, not zero.
            if self.db.billing is not None:
                # A conservative serialized-input bound and the actual output ceiling.
                # Tin absorbs any supplier overage; a customer charge cannot exceed its cap.
                from tin_lite.service_pricing import model_maximum

                input_bound = (
                    scope.input_tokens
                    if scope.input_tokens is not None
                    else len(json.dumps(asdict(request), ensure_ascii=False).encode()) + 4096
                )
                await self.db.billing.begin_operation(
                    conn,
                    run_id=run.id,
                    operation_id=key,
                    kind="native_model",
                    maximum=lambda terms: model_maximum(
                        terms,
                        provider=route.provider.value,
                        model=route.model,
                        input_tokens=input_bound,
                        output_tokens=request.max_output_tokens,
                    ),
                )
            await self.db.start_effect(conn, execution_key=key, operation=OPERATION)
            await self.db.save_effect_progress(conn, execution_key=key, result=record)
            try:
                result = await call()
            except BaseException as exc:
                if isinstance(exc, ModelProviderError) and exc.observation is not None:
                    await self._observed(conn, key, record, exc.observation, "invalid_output")
                else:
                    await self.db.save_effect_progress(
                        conn,
                        execution_key=key,
                        result={
                            **record,
                            "failure_reason": (
                                "execution_interrupted"
                                if isinstance(exc, asyncio.CancelledError)
                                else model_failure_reason(exc)
                            ),
                        },
                    )
                raise
            await self._observed(
                conn,
                key,
                record,
                ModelObservation(
                    result.provider,
                    result.model,
                    result.request_id,
                    result.usage,
                    result.service_tier,
                    result.stop_reason,
                    result.output_truncated,
                ),
                "response_received",
            )
            return result

    async def _observed(self, conn, key, record, observation, outcome) -> None:
        observed_record = {
            **record,
            "outcome": outcome,
            "observed_at": datetime.now(UTC).isoformat(),
            "model": observation.model,
            "request_id": observation.request_id,
            "service_tier": observation.service_tier,
            # The provider's own stop signal, and whether it said the output cap was reached.
            "stop_reason": observation.stop_reason,
            "output_truncated": observation.output_truncated,
            "usage": {**asdict(observation.usage), "web_search_calls": 0},
        }
        await self.db.complete_effect(
            conn,
            execution_key=key,
            result=observed_record,
        )
        if self.db.billing is not None:
            from tin_lite.billing_contracts import object_value, receipt_charge

            budget = await conn.fetchrow(
                "SELECT terms FROM billing_run_budgets WHERE run_id=$1", UUID(record["run_id"])
            )
            if budget:
                priced = receipt_charge(
                    object_value(budget["terms"]), "native_model", observed_record
                )
                if priced is not None:
                    await self.db.billing.observe_operation(
                        conn, operation_id=key, nanos=priced[0], observation=priced[1]
                    )
