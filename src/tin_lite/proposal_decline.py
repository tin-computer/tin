"""Turn down anything waiting for approval in Decisions.

A draft, report or proposal only takes effect when a member approves it. Discarding it records
the run's review as declined and ends the waiting run. Nothing it proposed is used, published or
applied, and its files stay readable in Files; nothing is deleted. A one-off task's proposal is
discarded by stopping the task instead (see the decisions API).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

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
