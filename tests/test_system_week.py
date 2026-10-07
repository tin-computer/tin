"""The System calendar's week: local day bounds and the occurrences still to come."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

from tin_lite.domain import ProjectWorkflow, RunStatus
from tin_lite.system_week import upcoming_occurrences, week_window

LA = "America/Los_Angeles"
# Wednesday 7 October 2026, 12:18 in Los Angeles.
NOW = datetime(2026, 10, 7, 19, 18, tzinfo=UTC)


def saved(schedule: dict | None, **changes) -> ProjectWorkflow:
    base = ProjectWorkflow(
        id=uuid4(),
        project_id=uuid4(),
        workflow_id=uuid4(),
        workflow_key="content.refresh",
        workflow_title="Refresh a page",
        workflow_description="",
        version_label="1.0.0",
        definition_commit_sha="a" * 40,
        name="Weekly page refresh",
        inputs={},
        input_schema={},
        schedule=schedule,
        status="active",
        temporal_schedule_id=None,
        next_run_at=None,
        last_run_id=None,
        last_run_status=None,
        last_artifact_path=None,
        last_error=None,
        settings_revision=1,
        created_by_clerk_user_id="user",
        created_at=NOW,
        updated_at=NOW,
    )
    return replace(base, **changes)


def weekly(*weekdays: str, at: str = "10:00", timezone: str = LA, **extra) -> dict:
    return {
        "cadence": "weekly",
        "weekdays": list(weekdays),
        "local_time": at,
        "timezone": timezone,
        **extra,
    }


def this_week(timezone: str = LA) -> tuple[datetime, datetime]:
    return week_window(date(2026, 10, 5), timezone)


def test_a_week_is_seven_local_days_even_across_a_clock_change():
    begin, end = week_window(date(2026, 10, 5), LA)
    assert begin == datetime(2026, 10, 5, 7, tzinfo=UTC)
    assert end == datetime(2026, 10, 12, 7, tzinfo=UTC)
    # Los Angeles falls back on 1 November: that week is one hour longer.
    begin, end = week_window(date(2026, 10, 26), LA)
    assert (end - begin).total_seconds() == 7 * 24 * 3600 + 3600


def test_only_what_is_still_to_come_and_at_the_schedule_s_own_local_time():
    start, end = this_week()
    refresh = saved(weekly("monday", "thursday"))
    occurrences = upcoming_occurrences([refresh], start=start, end=end, now=NOW)
    # Monday already happened; Thursday 10:00 in Los Angeles is 17:00 UTC.
    assert [item.at for item in occurrences] == [datetime(2026, 10, 8, 17, tzinfo=UTC)]
    assert occurrences[0].state == "planned"


def test_a_later_week_lists_every_occurrence_and_a_daily_schedule_has_seven():
    start, end = week_window(date(2026, 10, 12), LA)
    daily = saved({"cadence": "daily", "local_time": "08:30", "timezone": "Europe/Istanbul"})
    occurrences = upcoming_occurrences([daily], start=start, end=end, now=NOW)
    assert len(occurrences) == 7
    assert all(start <= item.at < end for item in occurrences)


def test_paused_skipped_and_held_slots_stay_on_their_day():
    start, end = this_week()
    thursday = datetime(2026, 10, 8, 17, tzinfo=UTC)
    paused = saved(weekly("thursday"), status="paused")
    skipped = saved(weekly("thursday"), skip_scheduled_for=thursday)
    held = saved(weekly("thursday"), last_run_status=RunStatus.NEEDS_INPUT)
    states = {
        item.project_workflow_id: item.state
        for item in upcoming_occurrences([paused, skipped, held], start=start, end=end, now=NOW)
    }
    assert states == {paused.id: "paused", skipped.id: "skipped", held.id: "held"}


def test_unscheduled_archived_and_ended_schedules_have_no_slots():
    start, end = this_week()
    manual = saved(None)
    archived = saved(weekly("thursday"), status="archived")
    ended = saved(weekly("thursday", end_at="2026-10-08T00:00:00Z"))
    assert upcoming_occurrences([manual, archived, ended], start=start, end=end, now=NOW) == []


def test_a_stored_schedule_this_host_cannot_read_leaves_the_others_on_the_calendar():
    start, end = this_week()
    unreadable = saved({"cadence": "daily", "local_time": "09:00", "timezone": "Mars/Base"})
    readable = saved(weekly("thursday"))
    occurrences = upcoming_occurrences([unreadable, readable], start=start, end=end, now=NOW)
    assert [item.project_workflow_id for item in occurrences] == [readable.id]


def test_a_monthly_schedule_appears_only_in_its_week():
    monthly = saved(
        {"cadence": "monthly", "day_of_month": 14, "local_time": "09:00", "timezone": LA}
    )
    start, end = week_window(date(2026, 10, 12), LA)
    occurrences = upcoming_occurrences([monthly], start=start, end=end, now=NOW)
    assert [item.at for item in occurrences] == [datetime(2026, 10, 14, 16, tzinfo=UTC)]
    start, end = week_window(date(2026, 10, 19), LA)
    assert upcoming_occurrences([monthly], start=start, end=end, now=NOW) == []
