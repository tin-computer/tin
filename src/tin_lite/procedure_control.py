"""One run-bound procedure stop, shared by HTTP, MCP and the technical-fix alias."""

import asyncio
from uuid import UUID

from temporalio.service import RPCError, RPCStatusCode


async def stop_procedure(*, runtime, run_id, actor, workflow_key=None):
    run = await runtime.database.get_run(run_id)
    if run is None or not await runtime.database.has_project_access(
        project_id=run.project_id, clerk_user_id=actor
    ):
        raise LookupError("run not found")
    if run.review_source_run_id is not None and run.status.value in {"failed", "stopped"}:
        from tin_lite.domain import SideEffectConflictError
        from tin_lite.workflow_review_store import ReviewConflict
        from tin_lite.workflow_reviews import WorkflowReviews

        reviews = WorkflowReviews(runtime=runtime, settings=None)
        view = await reviews.view(run.id, actor)
        try:
            stopped = await reviews.cancel_failed_revision(
                run_id=run.id, actor=actor, token=view["review_token"]
            )
        except ReviewConflict as exc:
            raise SideEffectConflictError(str(exc)) from exc
        return {
            "id": str(stopped.id),
            "status": stopped.status.value,
            "notice": "Revision stopped. The previous copy remains readable.",
        }
    if run.executor == "social.x_draft":
        return await stop_x_draft(runtime=runtime, run=run, actor=actor)
    if run.executor not in {"codex.procedure", "workflow.code"}:
        from tin_lite.domain import SideEffectConflictError

        raise SideEffectConflictError(
            "stop_procedure stops code workflows, Codex procedures and X drafts, not "
            f"{run.executor} runs. Use that workflow's own stop action."
        )
    stopped = await runtime.database.stop_codex_procedure(
        run_id=run.id,
        project_id=run.project_id,
        actor=actor,
        expected_generation=run.generation,
        workflow_key=workflow_key,
    )
    if stopped.review_source_run_id is not None:
        from tin_lite.workflow_reviews import WorkflowReviews

        reviews = WorkflowReviews(runtime=runtime, settings=None)
        view = await reviews.view(stopped.id, actor)
        await reviews.cancel_failed_revision(
            run_id=stopped.id, actor=actor, token=view["review_token"]
        )

    async def cancel():
        try:
            async with asyncio.timeout(15):
                await runtime.temporal.get_workflow_handle(stopped.temporal_workflow_id).cancel()
        except RPCError as exc:
            # A stop may win before Temporal starts. Activity admission still rejects it.
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise

    async def cleanup():
        if stopped.sandbox_id is not None:
            async with asyncio.timeout(15):
                await runtime.sandboxes.kill(stopped.sandbox_id)

    # A Temporal outage must not prevent killing the paid compute, or vice versa.
    # Repeating Stop retries both without another product event. Never return upstream errors.
    outcomes = await asyncio.gather(cancel(), cleanup(), return_exceptions=True)
    pending = [isinstance(outcome, BaseException) for outcome in outcomes]
    notice = (
        "The run is stopped. Cleanup is pending; retry Stop to confirm cleanup."
        if any(pending)
        else "Stopped. Saved output is retained."
    )
    if stopped.executor != "workflow.code":
        notice += " Already accepted provider work may still incur costs."
    return {
        "id": str(stopped.id),
        "status": stopped.status.value,
        "cancellation_pending": pending[0],
        "cleanup_pending": pending[1],
        "notice": notice,
    }


async def stop_x_draft(*, runtime, run, actor):
    """Stop an X draft: no further step starts and an unfinished composition stops.

    A voice guide already being learned or waiting in Decisions stays there as its own
    decision, like a draft a stopped organic system leaves for review. Nothing is posted.
    """
    from tin_lite import x_draft
    from tin_lite.organic_system_control import deliver_control

    db = runtime.database
    # The locks each step holds while it starts a child: after this fence, none can appear.
    async with db.effect_lock(f"x-draft:{run.id}:style", x_draft.KEY) as (conn, _):
        async with db.effect_lock(f"x-draft:{run.id}:compose", x_draft.KEY, conn=conn):
            stopped = await db._stop_paid_report(
                run_id=run.id, project_id=run.project_id, actor=actor, workflow_key=x_draft.KEY
            )
    waiting = None
    for row in (await x_draft.facts(db, stopped))["steps"]:
        if not row["run_id"]:
            continue
        child = await db.get_run(UUID(row["run_id"]))
        if row["step"] == "compose" and child.status.value in {"pending", "running"}:
            await stop_procedure(runtime=runtime, run_id=child.id, actor=actor)
        elif child.status.value == "pending":
            # Prepared for the parent to start; a stopped parent never will.
            await db.pool.execute(
                "UPDATE workflow_runs SET status='stopped', finished_at=now() "
                "WHERE id=$1 AND status='pending'",
                child.id,
            )
        elif child.status.value in {"running", "needs_input"}:
            waiting = str(child.id)
    await deliver_control(runtime.temporal.get_workflow_handle(stopped.temporal_workflow_id))
    return {
        "id": str(stopped.id),
        "status": stopped.status.value,
        "voice_guide_run_id": waiting,
        "notice": "Stopped. Nothing was posted."
        + (
            " The proposed X voice guide stays in Decisions to approve or discard."
            if waiting
            else ""
        ),
    }
