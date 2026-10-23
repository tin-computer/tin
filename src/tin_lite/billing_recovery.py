"""Bounded reconciliation on the existing switchboard, not a new execution engine."""

import asyncio
import json
import logging

import httpx
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from tin_lite.billing_payments import StripePayments
from tin_lite.workflow_definitions import resolve_execution_contract
from tin_lite.workflows import registered_workflow_implementations

logger = logging.getLogger(__name__)


async def recover_dispatches(runtime, settings):
    # Included runs have no budget; their receipt is the dispatch intent. Only receipts
    # that record no billing parent qualify, so a prepared child is never started alone.
    rows = await runtime.database.pool.fetch(
        """SELECT r.id FROM workflow_runs r LEFT JOIN billing_run_budgets b ON b.run_id=r.id
           LEFT JOIN effect_receipts e ON e.execution_key='billing-included:'||r.id::text
             AND e.operation='included_workflow_v1' AND e.status='completed'
           WHERE r.status='pending'
             AND ((b.status='reserved' AND b.root_run_id=b.run_id)
                  OR e.result->'parent_run_id'='null'::jsonb)
             AND r.created_at<now()-interval '30 seconds' AND r.trigger_source<>'schedule'
             AND r.review_source_run_id IS NULL
           ORDER BY r.created_at LIMIT 20"""
    )
    for row in rows:
        run = await runtime.database.get_run(row["id"])
        implementation = registered_workflow_implementations().get(run.executor)
        if implementation is None:
            continue
        try:
            await runtime.temporal.start_workflow(
                implementation.run,
                str(run.id),
                id=run.temporal_workflow_id,
                task_queue=settings.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            pass


async def resume_funded_schedules(runtime, settings):
    """A schedule paused because credits ran out starts again once its workspace can pay for
    its next run, however the credits arrived: a top-up, a refund reversal or an adjustment.
    A schedule the founder paused has no such reason on it and stays paused."""
    from tin_lite.failures import BILLING_FOUNDER_TEXT
    from tin_lite.project_workflow_operations import set_schedule_paused

    reason = BILLING_FOUNDER_TEXT["insufficient_funds"][1]
    db = runtime.database
    rows = await db.pool.fetch(
        """SELECT id FROM project_workflows
           WHERE status='paused' AND last_error=$1 AND schedule IS NOT NULL
           ORDER BY updated_at LIMIT 20""",
        reason,
    )
    for row in rows:
        configured = await db.get_project_workflow(row["id"])
        workflow = configured and await db.get_workflow(configured.workflow_id)
        if workflow is None or configured.status != "paused" or configured.last_error != reason:
            continue
        try:
            workflow = await resolve_execution_contract(
                storage=runtime.storage,
                workflow=workflow,
                project_id=configured.project_id,
                revision=configured.definition_commit_sha,
                input_schema=configured.input_schema,
            )
            async with db.pool.acquire() as conn:
                if not await db.billing.schedule_affordable(
                    conn,
                    project_id=configured.project_id,
                    definition=workflow.definition,
                    inputs=configured.inputs,
                ):
                    continue
            # The founder may have paused or edited it since it was read: then it is theirs.
            current = await db.get_project_workflow(configured.id)
            if current is None or current.updated_at != configured.updated_at:
                continue
            await set_schedule_paused(
                runtime=runtime, settings=settings, configured=current, paused=False
            )
        except (LookupError, RuntimeError, ValueError):
            # Not resumable yet, such as a readiness issue; the next pass looks again.
            continue
        await db.pool.execute(
            """INSERT INTO activity_events
               (project_id,event_type,details,summary,audience,dedupe_key)
               VALUES($1,'workflow_schedule_resumed',$2::jsonb,$3,'product',$4)
               ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING""",
            configured.project_id,
            json.dumps({"project_workflow_id": str(configured.id), "reason": "credits_added"}),
            f"{configured.name} schedule resumed: credits were added."[:1000],
            f"schedule-funded:{configured.id}:{configured.updated_at.isoformat()}",
        )


async def billing_reconciliation_loop(runtime, settings):
    if runtime.database.billing is None:
        return
    # One Stripe connection pool for the loop's lifetime; cancellation on shutdown closes it.
    async with httpx.AsyncClient(timeout=25) as stripe_client:
        payments = StripePayments(
            billing=runtime.database.billing, settings=settings, client=stripe_client
        )
        while True:
            try:
                studio = getattr(runtime, "studio", None)
                if studio is not None:
                    await studio.reconcile_usage()
                await runtime.database.billing.reconcile()
                await recover_dispatches(runtime, settings)
                await payments.reconcile()
                await resume_funded_schedules(runtime, settings)
            except Exception:
                # No raw provider exceptions, payment details or credentials in logs.
                logger.warning("Billing reconciliation is pending; it will retry.")
            await asyncio.sleep(15)
