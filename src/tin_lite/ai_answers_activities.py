"""Trusted activity that measures AI answers; Temporal carries identifiers only.

No workflow calls this yet. The organic audit will, once its v11 policy lands: it saves the
panel with ``save_request`` and then runs ``ai_answers_measure`` with the run id and a stage
name. The prompts, brand and answers stay in effect receipts in Postgres; the activity
returns counts and cost only.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite.ai_answers import (
    AIAnswersClient,
    AIAnswersRequest,
    CostCeilingExceeded,
    call_outcome,
    measure,
)
from tin_lite.billing_contracts import BillingError
from tin_lite.usage_capture import external_usage_scope

OPERATION = "ai.answers"
HEARTBEAT_SECONDS = 20
STAGE = re.compile(r"[a-z0-9_-]{1,40}")


def receipt_key(run_id, stage: str, name: str) -> str:
    if not STAGE.fullmatch(stage):
        raise ValueError("The AI answers stage must be a short lowercase identifier.")
    return f"ai-answers:{UUID(str(run_id))}:{stage}:{name}"


async def save_request(database, *, run_id, stage: str, inputs: dict) -> dict:
    """Validate a panel and save it for the run's stage before the activity starts.

    Saving the same panel again is a no-op; a different panel for a saved stage is refused.
    """
    request = AIAnswersRequest.from_inputs(inputs).as_inputs()
    key = receipt_key(run_id, stage, "request")
    async with database.effect_lock(key, OPERATION) as (conn, existing):
        if existing and existing.status == "completed":
            if existing.result != request:
                raise ValueError("A different AI answers panel is already saved for this stage.")
            return existing.result
        await database.start_effect(conn, execution_key=key, operation=OPERATION)
        await database.complete_effect(conn, execution_key=key, result=request)
    return request


class ReceiptLedger:
    """Each paid DataForSEO request gets a receipt before dispatch and a reservation.

    A completed receipt is reused on retry. An attempt without an outcome is reported as
    unknown and never sent again; its unconfirmed observation stays with billing.
    """

    def __init__(self, database, *, run_id, stage: str) -> None:
        self.db, self.run_id, self.stage = database, UUID(str(run_id)), stage

    async def once(self, name, *, reserve_usd: Decimal, call):
        key = receipt_key(self.run_id, self.stage, name)
        async with self.db.effect_lock(key, OPERATION) as (conn, existing):
            if existing and existing.status == "completed":
                return existing.result
            await self.db.start_effect(conn, execution_key=key, operation=OPERATION)
            if existing and (existing.result or {}).get("attempted_at"):
                result = {"status": "unknown", "reason": "unconfirmed_previous_request"}
            else:
                run = await self.db.get_run(self.run_id, conn=conn)
                if run is None or run.status.value in {"failed", "stopped", "succeeded"}:
                    raise ApplicationError("The run is no longer active.", non_retryable=True)
                await self.db.save_effect_progress(
                    conn,
                    execution_key=key,
                    result={
                        "attempted_at": datetime.now(UTC).isoformat(),
                        "reservation_usd": str(reserve_usd),
                    },
                )
                try:
                    with external_usage_scope(
                        self.db,
                        conn,
                        self.run_id,
                        f"ai_answers:{self.stage}:{name}",
                        maximum_usd=str(reserve_usd),
                    ):
                        result = await call_outcome(call)
                except BillingError:
                    result = {"status": "failed", "reason": "spending_limit"}
                except Exception:  # noqa: BLE001 - a paid attempt's outcome is now unknown.
                    activity.logger.warning("AI answers request outcome is unknown.")
                    result = {"status": "unknown", "reason": "provider_result_unavailable"}
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return result


async def _with_heartbeats(operation, details: dict):
    task = asyncio.ensure_future(operation)
    try:
        while True:
            activity.heartbeat(details)
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
            if task in done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class AIAnswersActivities:
    def __init__(self, *, database, settings, client=None, clock=None, sleep=None) -> None:
        self.db = database
        login, password = (
            getattr(settings, "dataforseo_login", None),
            getattr(settings, "dataforseo_password", None),
        )
        self.client = client or (
            AIAnswersClient(login.get_secret_value(), password.get_secret_value())
            if login and password
            else None
        )
        self.options = {
            **({"clock": clock} if clock else {}),
            **({"sleep": sleep} if sleep else {}),
        }

    @activity.defn(name="ai_answers_measure")
    async def ai_answers_measure(self, control: dict) -> dict:
        """Measure the saved panel for ``control = {"run_id", "stage"}``; return counts only."""
        run_id, stage = str(UUID(str(control["run_id"]))), control["stage"]
        result_key = receipt_key(run_id, stage, "result")
        saved = await self.db.get_effect(result_key)
        if saved and saved.status == "completed":
            return saved.result["summary"]
        run = await self.db.get_run(UUID(run_id))
        if run is None or run.status.value in {"failed", "stopped", "succeeded"}:
            raise ApplicationError("The run is no longer active.", non_retryable=True)
        panel = await self.db.get_effect(receipt_key(run_id, stage, "request"))
        if panel is None or panel.status != "completed":
            raise ApplicationError("No AI answers panel is saved.", non_retryable=True)
        if self.client is None:
            raise ApplicationError("DataForSEO is not configured.", non_retryable=True)
        request = AIAnswersRequest.from_inputs(panel.result)
        try:
            result = await _with_heartbeats(
                measure(
                    self.client,
                    request,
                    ledger=ReceiptLedger(self.db, run_id=run_id, stage=stage),
                    tag=f"tin-ai:{run_id}:{stage}",
                    **self.options,
                ),
                {"run_id": run_id, "stage": stage},
            )
        except CostCeilingExceeded as exc:
            result = {
                "rows": [],
                "summary": {
                    "status": "rejected",
                    "reason": "cost_ceiling",
                    "estimate_usd": str(exc.estimate),
                    "max_cost_usd": str(exc.ceiling),
                    "cost_usd": "0",
                },
            }
        else:
            result["summary"] = {"status": "measured", **result["summary"]}
        async with self.db.effect_lock(result_key, OPERATION) as (conn, existing):
            if existing and existing.status == "completed":
                return existing.result["summary"]
            await self.db.start_effect(conn, execution_key=result_key, operation=OPERATION)
            await self.db.complete_effect(conn, execution_key=result_key, result=result)
        return result["summary"]


async def read_result(database, *, run_id, stage: str) -> dict | None:
    """The saved rows and summary for a measured stage, for the caller that reports them."""
    receipt = await database.get_effect(receipt_key(run_id, stage, "result"))
    return receipt.result if receipt and receipt.status == "completed" else None
