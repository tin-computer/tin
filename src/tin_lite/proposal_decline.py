"""Turn down a proposed writing style or brand guide.

A proposal only takes effect when a member approves it. Discarding it records the run's
review as declined, ends the waiting run and leaves the current guide exactly as it is. The
proposed files stay readable in Files; nothing is deleted.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

# Workflow key -> what the proposal is, in the Activity line.
PROPOSALS = {"style.capture": "writing style guide", "brand.capture": "brand guide"}


async def decline_proposal(*, database: Any, run_id: UUID, actor: str) -> Any:
    run = await database.get_run(run_id)
    if run is None or not await database.has_project_access(
        project_id=run.project_id, clerk_user_id=actor
    ):
        raise LookupError("run not found")
    workflow = await database.get_workflow(run.workflow_id)
    kind = PROPOSALS.get(workflow.key) if workflow and workflow.project_id is None else None
    if kind is None:
        raise ValueError("Only a proposed writing style or brand guide can be discarded.")
    return await database.decline_review(
        run_id=run.id,
        clerk_user_id=actor,
        summary=f"You discarded the proposed {kind}. The current guide is unchanged.",
    )
