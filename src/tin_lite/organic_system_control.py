"""Membership-gated controls shared by HTTP and MCP, with Postgres stop fences."""

from uuid import UUID

from temporalio.service import RPCError, RPCStatusCode

from tin_lite import organic_system
from tin_lite.db import SideEffectConflictError


async def deliver_control(handle, signal=None):
    try:
        if signal:
            await handle.signal(signal)
        else:
            await handle.cancel()
    except RPCError as exc:
        # A stop can win before a prepared child starts. Its Postgres fence still
        # rejects activity execution if Temporal starts it just after this check.
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise


async def stop_system(*, runtime, run_id, actor):
    db = runtime.database
    run = await db.get_run(run_id)
    if (
        run is None
        or run.executor != organic_system.KEY
        or not await db.has_project_access(project_id=run.project_id, clerk_user_id=actor)
    ):
        raise LookupError("run not found")
    # Same short lock as child preparation: after this fence, no new child can appear.
    async with db.effect_lock(f"traffic:{run.id}:control", organic_system.KEY):
        stopped = await db._stop_paid_report(
            run_id=run.id, project_id=run.project_id, actor=actor, workflow_key=organic_system.KEY
        )
    facts = await organic_system.system_facts(database=db, project_id=run.project_id, run_id=run.id)
    pending = []
    for row in facts["steps"]:
        if not row["run_id"] or row["status"] not in {
            "pending",
            "running",
            "stopped",
        }:
            continue
        # A draft already waiting for review stays readable and reviewable. Stopping
        # the parent removes its automatic delivery promise; it is not unfinished compute.
        child = await db.get_run(UUID(row["run_id"]))
        try:
            if child.executor == "codex.procedure":
                from tin_lite.procedure_control import stop_procedure

                await stop_procedure(runtime=runtime, run_id=child.id, actor=actor)
            else:
                await db._stop_paid_report(
                    run_id=child.id,
                    project_id=run.project_id,
                    actor=actor,
                    workflow_key=row["workflow_key"],
                )
                await deliver_control(
                    runtime.temporal.get_workflow_handle(child.temporal_workflow_id), "stop"
                )
        except SideEffectConflictError:
            # Publication/delivery already reserved cannot be recalled. Keep its run
            # visible and let it finish; do not terminate it via parent cancellation.
            pending.append(str(child.id))
    await deliver_control(runtime.temporal.get_workflow_handle(run.temporal_workflow_id))
    return {
        "id": str(stopped.id),
        "status": stopped.status.value,
        "finishing_run_ids": pending,
        "notice": "Reserved delivery may finish. Accepted provider requests may still cost.",
    }
