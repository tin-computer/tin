"""One week of a project's system: what ran each day and what is still to come.

The System page draws this as a calendar. Runs come from Postgres. Upcoming occurrences come
from each saved schedule through `next_run_after`, the same arithmetic that predicts Temporal's
next dispatch, so the calendar and the schedule rows never disagree about a time.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from tin_lite.domain import ProjectWorkflow, RunStatus
from tin_lite.schedules import WorkflowSchedule, next_run_after

OccurrenceState = Literal["planned", "paused", "held", "skipped"]

# A daily schedule has seven occurrences a week; the bound only guards a malformed spec.
MAX_OCCURRENCES_PER_SCHEDULE = 31


@dataclass(frozen=True)
class Occurrence:
    project_workflow_id: UUID
    at: datetime
    state: OccurrenceState


def week_window(start: date, timezone: str) -> tuple[datetime, datetime]:
    """The seven local days from `start`, as UTC instants; DST makes a day 23 or 25 hours."""
    zone = ZoneInfo(timezone)
    begin = datetime.combine(start, time(), zone)
    end = datetime.combine(start + timedelta(days=7), time(), zone)
    return begin.astimezone(UTC), end.astimezone(UTC)


def upcoming_occurrences(
    configured: Iterable[ProjectWorkflow],
    *,
    start: datetime,
    end: datetime,
    now: datetime,
) -> list[Occurrence]:
    """Every scheduled occurrence from now (or `start`, if later) until `end`.

    A paused schedule keeps its slots, marked paused. A slot Skip once moved past is skipped.
    While a configuration's latest run waits for review, Temporal skips overlapping
    occurrences, so its slots are held: they run only if the founder answers first.
    """
    after = max(start, now) - timedelta(seconds=1)
    occurrences: list[Occurrence] = []
    for item in configured:
        if not item.schedule or item.status == "archived":
            continue
        try:
            schedule = WorkflowSchedule.model_validate(item.schedule)
        except ValidationError:
            # A stored schedule this host cannot read (a zone file it lacks) has no slots here;
            # its row and its Temporal schedule are unaffected.
            continue
        at = next_run_after(schedule, after)
        for _ in range(MAX_OCCURRENCES_PER_SCHEDULE):
            if at is None or at >= end:
                break
            occurrences.append(
                Occurrence(project_workflow_id=item.id, at=at, state=_state(item, at))
            )
            at = next_run_after(schedule, at)
    return sorted(
        occurrences, key=lambda occurrence: (occurrence.at, str(occurrence.project_workflow_id))
    )


def _state(item: ProjectWorkflow, at: datetime) -> OccurrenceState:
    if item.status == "paused":
        return "paused"
    if item.skip_scheduled_for is not None and at == item.skip_scheduled_for:
        return "skipped"
    if item.last_run_status == RunStatus.NEEDS_INPUT:
        return "held"
    return "planned"
