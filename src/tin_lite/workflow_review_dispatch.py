"""Recover accepted review commands without putting feedback into Temporal history."""

import asyncio
import logging

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode


async def dispatch_reviews(runtime, settings):
    from tin_lite.workflows import CodexProcedureWorkflow

    rows = await runtime.database.pool.fetch(
        "SELECT * FROM workflow_review_commands WHERE dispatch_state<>'received' "
        "ORDER BY created_at LIMIT 20"
    )
    for row in rows:
        try:
            if row["coordinator_run_id"]:
                coordinator = await runtime.database.get_run(row["coordinator_run_id"])
                handle = runtime.temporal.get_workflow_handle(coordinator.temporal_workflow_id)
                # A declined proposal ends its waiting run the same way a stopped revision does.
                if row["action"] in {"cancel", "decline"}:
                    try:
                        await handle.cancel()
                    except RPCError as exc:
                        if exc.status != RPCStatusCode.NOT_FOUND:
                            raise
                elif row["action"] == "approve":
                    await handle.signal("approve")
                else:
                    await handle.signal("request_revision", str(row["id"]))
            elif row["action"] == "revise":
                successor = await runtime.database.get_run(row["successor_run_id"])
                try:
                    if successor.status.value == "stopped":
                        await runtime.database.pool.execute(
                            "UPDATE workflow_review_commands SET dispatch_state='received' "
                            "WHERE id=$1",
                            row["id"],
                        )
                        continue
                    await runtime.temporal.start_workflow(
                        CodexProcedureWorkflow.run,
                        str(successor.id),
                        id=successor.temporal_workflow_id,
                        task_queue=settings.task_queue,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    pass
            await runtime.database.pool.execute(
                "UPDATE workflow_review_commands SET dispatch_state=$2, dispatched_at=now() "
                "WHERE id=$1 AND dispatch_state<>'received'",
                row["id"],
                "received"
                if row["action"] in {"approve", "cancel", "decline"}
                or not row["coordinator_run_id"]
                else "dispatched",
            )
        except Exception:
            logging.getLogger(__name__).warning("Review dispatch pending; it will retry.")
    await dispatch_x_revisions(runtime, settings)


async def dispatch_x_revisions(runtime, settings):
    """The admitted run is the intent, including on self-hosts without a billing loop."""
    from tin_lite.workflows import XFeedbackWorkflow

    rows = await runtime.database.pool.fetch(
        "SELECT r.id FROM workflow_runs r JOIN projects p ON p.id=r.project_id "
        "WHERE r.executor='social.x_revise' AND r.status='pending' AND p.deleted_at IS NULL "
        "AND r.created_at<now()-interval '30 seconds' ORDER BY r.created_at LIMIT 20"
    )
    for row in rows:
        run = await runtime.database.get_run(row["id"])
        try:
            await runtime.temporal.start_workflow(
                XFeedbackWorkflow.run,
                str(run.id),
                id=run.temporal_workflow_id,
                task_queue=settings.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            pass


async def review_reconciliation_loop(runtime, settings):
    while True:
        try:
            await dispatch_reviews(runtime, settings)
        except Exception:
            logging.getLogger(__name__).warning("Review reconciliation pending; it will retry.")
        await asyncio.sleep(15)
