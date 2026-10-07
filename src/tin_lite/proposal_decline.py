"""Turn down anything waiting for approval in Decisions.

A draft, report or proposal only takes effect when a member approves it. Discarding it records
the run's review as declined and ends the waiting run. Nothing it proposed is used, published or
applied, and its files stay readable in Files; nothing is deleted. A one-off task's proposal is
discarded by stopping the task instead. Decisions (HTTP) and MCP both discard through
`discard_review`, so the two never drift.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

ONLY_REVIEWS = "Only a decision with something to approve can be discarded."
# Workflow key -> what the proposal is, in the Activity line.
PROPOSALS = {"style.capture": "writing style guide", "brand.capture": "brand guide"}


def discard_summary(workflow: Any) -> str:
    kind = PROPOSALS.get(workflow.key) if workflow and workflow.project_id is None else None
    if kind:
        return f"You discarded the proposed {kind}. The current guide is unchanged."
    return "You discarded this. It stays readable in Files; nothing was published or applied."


async def decline_proposal(*, database: Any, run_id: UUID, actor: str) -> Any:
    run = await database.get_run(run_id)
    if run is None or not await database.has_project_access(
        project_id=run.project_id, clerk_user_id=actor
    ):
        raise LookupError("run not found")
    workflow = await database.get_workflow(run.workflow_id)
    return await database.decline_review(
        run_id=run.id, clerk_user_id=actor, summary=discard_summary(workflow)
    )


async def discard_review(*, runtime: Any, run_id: UUID, actor: str) -> Any:
    """Discard what a run has waiting in Decisions, for a member who asked to.

    Raises LookupError for a run the actor can't see or that has nothing waiting, ValueError
    when what waits is not something to approve, and the task errors when a one-off task's
    stop is refused or not delivered. Discarding again returns the run as it was left.
    """
    from tin_lite.domain import PROJECT_TASK_WORKFLOW_NAME, RunStatus

    database = runtime.database
    run = await database.get_run(run_id)
    if run is None or not await database.has_project_access(
        project_id=run.project_id, clerk_user_id=actor
    ):
        raise LookupError("run not found")
    decision = await database.get_pending_decision_for_run(run_id=run.id)
    if decision is None:
        if run.review_decision == "declined" or (
            run.executor == PROJECT_TASK_WORKFLOW_NAME and run.status is RunStatus.STOPPED
        ):
            return run
        raise LookupError("nothing from this run is waiting in Decisions")
    if decision["kind"] != "review":
        raise ValueError(ONLY_REVIEWS)
    if decision["workflow_name"] == PROJECT_TASK_WORKFLOW_NAME:
        from tin_lite.project_task_control import stop_project_task

        return await stop_project_task(runtime=runtime, run_id=run.id, clerk_user_id=actor)
    return await decline_proposal(database=database, run_id=run.id, actor=actor)
