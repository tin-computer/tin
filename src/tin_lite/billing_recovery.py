"""Bounded reconciliation on the existing switchboard, not a new execution engine."""

import asyncio
import logging

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from tin_lite.billing_payments import StripePayments
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


async def billing_reconciliation_loop(runtime, settings):
    if runtime.database.billing is None:
        return
    while True:
        try:
            studio = getattr(runtime, "studio", None)
            if studio is not None:
                await studio.reconcile_usage()
            await runtime.database.billing.reconcile()
            await recover_dispatches(runtime, settings)
            await StripePayments(billing=runtime.database.billing, settings=settings).reconcile()
        except Exception:
            # No raw provider exceptions, payment details or credentials in logs.
            logger.warning("Billing reconciliation is pending; it will retry.")
        await asyncio.sleep(15)
