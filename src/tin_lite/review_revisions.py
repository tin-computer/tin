"""Approval never uses a saved output that a one-off task has revised or is revising.

A run's approval and delivery stay pinned to the copy it saved. A task's changes reach the
project through their own exact-diff approval, not as a new version of that run, so a run
cannot adopt them. While such a revision waits, approval is refused until it is resolved;
once it is applied, the older copy may still be kept in Tin but never published.
"""

from __future__ import annotations

from typing import Any

from tin_lite.content_delivery import CHOICE_WORKFLOW_IDS
from tin_lite.domain import RunStatus


async def approval_conflict(database: Any, run: Any, delivery: str | None) -> str | None:
    """Why approving this run now would use an outdated copy, or None when it would not."""
    if run.status != RunStatus.NEEDS_INPUT or run.review_decision is not None:
        return None
    revision = await database.output_revision(run_id=run.id)
    if revision is None:
        return None
    task = f"“{revision['title']}”" if revision.get("title") else "A one-off task"
    if revision["state"] == "waiting":
        return (
            f"A revision of this draft is waiting in {task}. Review it first; "
            "approving now would use the older copy."
        )
    if run.workflow_id in CHOICE_WORKFLOW_IDS and delivery != "none":
        return (
            f"{task} revised this draft after it was saved, so publishing here would send "
            "the older copy. The revised copy is in Files; keep this one in Tin instead."
        )
    return None
