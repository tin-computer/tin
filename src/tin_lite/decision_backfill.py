"""Name decisions saved before outputs carried their own heading and first sentence.

Such a decision shows an empty card under the workflow's name. Listing Decisions reads a few
of them at the revision each decision pins and stores the heading and one sentence in
Postgres, so every later listing reads Postgres only. Each decision is read once: storing its
line clears the generic text that selected it, whether or not the document has a heading.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from tin_lite.content_delivery import display_title, review_line

PER_LISTING = 3
READ_TIMEOUT_SECONDS = 5
# Keep in step with _GENERIC_REVIEW_TEXT_SQL in db.py, which selects the same decisions.
GENERIC_REVIEW_LINE = re.compile(r"^[^.]*\bis ready for your review\.\s*")

logger = logging.getLogger(__name__)


def backfilled_line(raw: bytes, previous: str) -> str:
    """The saved sentence that replaced the generic one; any later sentence (where approval
    delivers the draft) stays after it. A line that was never generic is kept as it is."""
    if not GENERIC_REVIEW_LINE.match(previous or ""):
        return previous or ""
    rest = GENERIC_REVIEW_LINE.sub("", previous, count=1).strip()
    return " ".join(part for part in (review_line(raw), rest) if part)


async def backfill_decision_text(
    *, database: Any, storage: Any, project: Any, limit: int = PER_LISTING
) -> int:
    """Store the heading and card line of at most `limit` older decisions; return how many."""
    rows = await database.decisions_without_output_text(project_id=project.id, limit=limit)
    stored = 0
    for row in rows:
        try:
            async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                raw = await storage.read_canonical_artifact(
                    repo_id=project.state_repo_id, commit_sha=row["revision"], path=row["path"]
                )
        except Exception:
            # The next listing tries again; the card keeps the workflow's name meanwhile.
            logger.warning(
                "An older decision's output could not be read yet",
                extra={"run_id": str(row["run_id"])},
            )
            continue
        if await database.store_decision_output_text(
            decision_id=row["decision_id"],
            run_id=row["run_id"],
            revision=row["revision"],
            previous_explanation=row["explanation"],
            workflow_title=row["workflow_title"],
            title=display_title(raw),
            explanation=backfilled_line(raw, row["explanation"]),
        ):
            stored += 1
    return stored
