"""Saved schedules: Postgres and the Temporal clock stay in agreement about what runs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from temporalio.service import RPCError, RPCStatusCode
from test_procedure_publication import publication_db as publication_db
from test_workflow_definitions import SCHEDULE, fixture, http_app, mcp

from tin_lite import growth_onboarding
from tin_lite.activities import TinActivities
from tin_lite.organic_system_activities import founder_timezone
from tin_lite.project_workflow_operations import sync_project_workflow
from tin_lite.schedules import (
    WorkflowSchedule,
    next_run_after,
    require_saveable_schedule,
    supported_timezone,
)
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs


class Clock:
    """A Temporal client double that records the schedule definitions Tin sends."""

    def __init__(self) -> None:
        self.definitions: list = []
        self.paused: list[str] = []
        self.unpaused: list[str] = []
        self.fail_update: Exception | None = None
        self.fail_pause: Exception | None = None

    def get_schedule_handle(self, schedule_id: str):
        clock = self

        class Handle:
            async def update(self, updater):
                if clock.fail_update is not None:
                    raise clock.fail_update
                clock.definitions.append(updater(None).schedule)

            async def pause(self, note=None):
                if clock.fail_pause is not None:
                    raise clock.fail_pause
                clock.paused.append(schedule_id)

            async def unpause(self, note=None):
                clock.unpaused.append(schedule_id)

            async def delete(self):
                return None

        return Handle()

    async def create_schedule(self, schedule_id, definition):
        self.definitions.append(definition)


def _rejected(message: str) -> RPCError:
    return RPCError(message, RPCStatusCode.INVALID_ARGUMENT, b"")


def _dispatcher(f) -> TinActivities:
    return TinActivities(database=f.db, storage=f.storage, sandboxes=None, settings=f.settings)


async def _runs(f) -> int:
    return await f.db.pool.fetchval(
        "SELECT count(*) FROM workflow_runs WHERE project_workflow_id = $1", f.configured.id
    )


# C1: a failed Temporal sync must not leave the old clock driving the new configuration.


async def test_failed_sync_is_inert_pausable_and_repaired_by_resume(publication_db, caplog):
    f = await fixture(publication_db)
    clock = Clock()
    f.runtime.temporal = clock
    moved = {**SCHEDULE, "local_time": "10:00"}
    clock.fail_update = _rejected("Invalid schedule spec: unknown time zone")
    caplog.set_level(logging.WARNING)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=http_app(f)), base_url="http://test"
    ) as client:
        saved = await client.put(
            f"/api/projects/{f.project.id}/workflows/{f.configured.id}",
            json={
                "name": f.configured.name,
                "inputs": f.configured.inputs,
                "schedule": moved,
                "expected_settings_revision": f.configured.settings_revision,
            },
        )
        assert saved.status_code == 502
        failed = await f.db.get_project_workflow(f.configured.id)
        assert failed.status == "failed"
        # Nothing is due while Temporal holds a different calendar.
        assert failed.next_run_at is None
        # The old Temporal calendar is stopped as far as Temporal allows, and the operator
        # can see why the sync failed.
        assert clock.paused == [f"tin-lite-project-workflow:{f.configured.id}"]
        assert "unknown time zone" in caplog.text

        # An occurrence of the old calendar is a clean no-op, not a retried error.
        occurrence = {
            "project_workflow_id": str(f.configured.id),
            "scheduled_for": datetime.now(UTC).isoformat(),
            "occurrence_id": "old-calendar",
        }
        assert await _dispatcher(f).dispatch_scheduled_workflow(occurrence) == {}
        assert await _runs(f) == 0

        paused = await client.post(
            f"/api/projects/{f.project.id}/workflows/{f.configured.id}/pause"
        )
        assert paused.status_code == 200, paused.text
        assert paused.json()["status"] == "paused"

        clock.fail_update = None
        resumed = await client.post(
            f"/api/projects/{f.project.id}/workflows/{f.configured.id}/resume"
        )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "active"
    # Resume sends the saved calendar, not just an unpause of whatever Temporal still holds.
    sent = clock.definitions[-1]
    assert sent.state.paused is False
    assert sent.spec.calendars[0].hour[0].start == 10


async def test_failed_sync_pauses_through_mcp_when_the_schedule_never_existed(
    publication_db, monkeypatch
):
    f = await fixture(publication_db)
    clock = Clock()
    f.runtime.temporal = clock
    await f.db.project_workflow_failed(
        project_workflow_id=f.configured.id,
        error_message="RPCError: schedule synchronization failed",
    )
    clock.fail_pause = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    server = mcp(f, monkeypatch)
    await server.call_tool(
        "set_project_workflow_paused",
        {
            "project_id": str(f.project.id),
            "project_workflow_id": str(f.configured.id),
            "paused": True,
        },
    )
    assert (await f.db.get_project_workflow(f.configured.id)).status == "paused"


async def test_a_mid_save_configuration_does_not_dispatch(publication_db):
    f = await fixture(publication_db)
    await f.db.pool.execute(
        "UPDATE project_workflows SET status = 'provisioning' WHERE id = $1", f.configured.id
    )
    occurrence = {
        "project_workflow_id": str(f.configured.id),
        "scheduled_for": datetime.now(UTC).isoformat(),
        "occurrence_id": "during-save",
    }
    assert await _dispatcher(f).dispatch_scheduled_workflow(occurrence) == {}
    assert await _runs(f) == 0


# C5: a pause that lands while a settings save is in flight survives the save.


@pytest.mark.parametrize("surface", ["http", "mcp"])
async def test_pause_during_a_settings_save_is_kept(publication_db, monkeypatch, surface):
    f = await fixture(publication_db)
    clock = Clock()
    f.runtime.temporal = clock
    db = f.db
    read = db.get_project_workflow
    raced = False

    async def pause_after_the_editor_read(project_workflow_id, **kwargs):
        nonlocal raced
        current = await read(project_workflow_id, **kwargs)
        if not raced:
            raced = True
            await db.set_project_workflow_paused(
                project_workflow_id=f.configured.id, project_id=f.project.id, paused=True
            )
        return current

    monkeypatch.setattr(db, "get_project_workflow", pause_after_the_editor_read)
    renamed = "Renamed while paused"
    if surface == "http":
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http_app(f)), base_url="http://test"
        ) as client:
            response = await client.put(
                f"/api/projects/{f.project.id}/workflows/{f.configured.id}",
                json={
                    "name": renamed,
                    "inputs": f.configured.inputs,
                    "schedule": SCHEDULE,
                    "expected_settings_revision": f.configured.settings_revision,
                },
            )
        assert response.status_code == 200, response.text
    else:
        await mcp(f, monkeypatch).call_tool(
            "update_project_workflow",
            {
                "project_id": str(f.project.id),
                "project_workflow_id": str(f.configured.id),
                "name": renamed,
                "inputs": f.configured.inputs,
                "expected_settings_revision": f.configured.settings_revision,
            },
        )
    saved = await read(f.configured.id)
    assert saved.name == renamed
    assert saved.status == "paused"
    assert clock.definitions[-1].state.paused is True


async def test_update_keeps_the_pause_it_found(publication_db):
    f = await fixture(publication_db)
    await f.db.set_project_workflow_paused(
        project_workflow_id=f.configured.id, project_id=f.project.id, paused=True
    )
    updated = await f.db.update_project_workflow(
        project_workflow_id=f.configured.id,
        project_id=f.project.id,
        name="Still paused",
        inputs=f.configured.inputs,
        schedule=SCHEDULE,
        expected_settings_revision=f.configured.settings_revision,
        clerk_user_id="user_phase1",
        changed_fields=["name"],
        workflow_key=f.configured.workflow_key,
        workflow_title=f.configured.workflow_title,
    )
    assert updated.status == "paused"
    clock = Clock()
    synced = await sync_project_workflow(
        runtime=SimpleNamespace(database=f.db, temporal=clock),
        settings=f.settings,
        configured=updated,
        previous_schedule=SCHEDULE,
    )
    assert synced.status == "paused"
    assert clock.definitions[-1].state.paused is True


# C2: only IANA zone names Temporal runs are saved; older saved names keep working.


@pytest.mark.parametrize(
    "name", ["localtime", "posixrules", "Factory", "right/UTC", "posix/Europe/Berlin", "UTC "]
)
def test_new_schedules_refuse_host_and_alternate_zone_files(name):
    assert not supported_timezone(name)
    schedule = WorkflowSchedule.model_construct(
        cadence="daily", weekdays=[], local_time="09:00", timezone=name, start_at=None, end_at=None
    )
    with pytest.raises(ValueError, match="IANA timezone"):
        require_saveable_schedule(schedule)


@pytest.mark.parametrize("name", ["Europe/Berlin", "America/New_York", "US/Eastern", "UTC"])
def test_iana_names_and_links_remain_supported(name):
    assert supported_timezone(name)


def test_a_saved_schedule_with_a_now_refused_zone_still_loads_and_projects():
    stored = {"cadence": "daily", "local_time": "09:00", "timezone": "localtime"}
    schedule = WorkflowSchedule.model_validate(stored)
    assert next_run_after(schedule) is not None
    # Keeping it unchanged in an edit is not a new save of the zone.
    require_saveable_schedule(schedule, previous=schedule.model_dump(mode="json"))


async def test_http_refuses_a_localtime_schedule_but_keeps_an_unchanged_legacy_one(
    publication_db,
):
    f = await fixture(publication_db)
    f.runtime.temporal = Clock()
    legacy = {**SCHEDULE, "timezone": "localtime"}
    await f.db.pool.execute(
        "UPDATE project_workflows SET schedule = $2::jsonb WHERE id = $1",
        f.configured.id,
        __import__("json").dumps(WorkflowSchedule.model_validate(legacy).model_dump(mode="json")),
    )
    configured = await f.db.get_project_workflow(f.configured.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=http_app(f)), base_url="http://test"
    ) as client:
        created = await client.post(
            f"/api/projects/{f.project.id}/workflows",
            json={
                "workflow_id": str(f.workflow.id),
                "name": "Local clock",
                "inputs": f.configured.inputs,
                "schedule": legacy,
                "request_id": str(uuid4()),
            },
        )
        renamed = await client.put(
            f"/api/projects/{f.project.id}/workflows/{f.configured.id}",
            json={
                "name": "Renamed legacy",
                "inputs": configured.inputs,
                "schedule": configured.schedule,
                "expected_settings_revision": configured.settings_revision,
            },
        )
    assert created.status_code == 422
    assert "IANA timezone" in created.text
    assert renamed.status_code == 200, renamed.text


def test_onboarding_refuses_a_timezone_schedules_cannot_use():
    project_id = uuid4()
    for bad in ("localtime", "posix/Europe/Berlin", "right/UTC", "Factory"):
        with pytest.raises(WorkflowInputError, match="timezone"):
            normalize_workflow_inputs(
                schema=growth_onboarding.INPUT_SCHEMA,
                project_id=project_id,
                inputs={"timezone": bad},
            )
    for good in ("America/New_York", "UTC", "Etc/GMT+5"):
        normalize_workflow_inputs(
            schema=growth_onboarding.INPUT_SCHEMA,
            project_id=project_id,
            inputs={"timezone": good},
        )


async def test_founder_timezone_skips_a_zone_schedules_cannot_use():
    answers = iter(["localtime", "Europe/Berlin"])

    async def fetchval(*_args):
        return next(answers)

    async def get_project(_project_id):
        return None

    database = SimpleNamespace(pool=SimpleNamespace(fetchval=fetchval), get_project=get_project)
    assert await founder_timezone(database, uuid4()) == "Europe/Berlin"


# C4: a schedule that has already ended is not reported as now on the calendar.


async def test_a_schedule_whose_end_has_passed_is_refused(publication_db, monkeypatch):
    f = await fixture(publication_db)
    f.runtime.temporal = Clock()
    ended = {**SCHEDULE, "end_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
    with pytest.raises(ValueError, match="end has passed"):
        require_saveable_schedule(WorkflowSchedule.model_validate(ended))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=http_app(f)), base_url="http://test"
    ) as client:
        created = await client.post(
            f"/api/projects/{f.project.id}/workflows",
            json={
                "workflow_id": str(f.workflow.id),
                "name": "Already over",
                "inputs": f.configured.inputs,
                "schedule": ended,
                "request_id": str(uuid4()),
            },
        )
    assert created.status_code == 422
    with pytest.raises(Exception, match="end has passed"):
        await mcp(f, monkeypatch).call_tool(
            "create_project_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": str(f.workflow.id),
                "name": "Already over",
                "inputs": f.configured.inputs,
                "request_id": str(uuid4()),
                "schedule": ended,
            },
        )


def test_schedule_shape_errors_are_still_validation_errors():
    with pytest.raises(ValidationError):
        WorkflowSchedule(cadence="daily", local_time="9am", timezone="UTC")


# C3: next_run_at is the occurrence Temporal dispatches across DST changes. Expected values
# were read from a Temporal 1.32 dev server's describe().info.next_action_times.


@pytest.mark.parametrize(
    ("timezone", "local_time", "after", "expected"),
    [
        # Berlin repeats 02:00-02:59 on 25 October 2026; Temporal fires 02:30 once, at CET.
        (
            "Europe/Berlin",
            "02:30",
            "2026-10-24T00:30:00+00:00",
            ["2026-10-25T01:30:00+00:00", "2026-10-26T01:30:00+00:00"],
        ),
        # ...and fires 01:00 a second time an hour later.
        (
            "Europe/Berlin",
            "01:00",
            "2026-10-24T00:00:00+00:00",
            [
                "2026-10-24T23:00:00+00:00",
                "2026-10-25T00:00:00+00:00",
                "2026-10-26T00:00:00+00:00",
            ],
        ),
        # New York fires both copies of the repeated 01:30.
        (
            "America/New_York",
            "01:30",
            "2026-10-31T06:00:00+00:00",
            ["2026-11-01T05:30:00+00:00", "2026-11-01T06:30:00+00:00"],
        ),
        # Lord Howe's missing 02:00 (a 30-minute jump) fires at 02:30 local instead.
        (
            "Australia/Lord_Howe",
            "02:00",
            "2026-10-02T16:00:00+00:00",
            ["2026-10-03T15:30:00+00:00", "2026-10-04T15:00:00+00:00"],
        ),
        # A missing 02:30 in Los Angeles is skipped.
        (
            "America/Los_Angeles",
            "02:30",
            "2027-03-13T11:00:00+00:00",
            ["2027-03-15T09:30:00+00:00"],
        ),
    ],
)
def test_next_run_matches_temporal_across_dst(timezone, local_time, after, expected):
    schedule = WorkflowSchedule(cadence="daily", local_time=local_time, timezone=timezone)
    cursor = datetime.fromisoformat(after)
    projected = []
    for _ in expected:
        cursor = next_run_after(schedule, cursor)
        projected.append(cursor.isoformat())
    assert projected == expected


@pytest.mark.parametrize(
    ("timezone", "local_time", "day", "months", "after", "expected"),
    [
        # A monthly run at a missing local time is skipped for that month: New York has no
        # 02:30 on 14 March 2027, so the next March 14 run is in 2028.
        (
            "America/New_York",
            "02:30",
            14,
            [3],
            "2027-02-01T00:00:00+00:00",
            ["2028-03-14T06:30:00+00:00"],
        ),
        # Both copies of New York's repeated 01:30 fire, as for a daily schedule.
        (
            "America/New_York",
            "01:30",
            7,
            [11],
            "2027-10-01T00:00:00+00:00",
            [
                "2027-11-07T05:30:00+00:00",
                "2027-11-07T06:30:00+00:00",
                "2028-11-07T06:30:00+00:00",
            ],
        ),
        # Sydney repeats 02:00-02:59 on 4 April 2027; 02:30 fires once, at AEST.
        (
            "Australia/Sydney",
            "02:30",
            4,
            [4],
            "2027-03-01T00:00:00+00:00",
            ["2027-04-03T16:30:00+00:00", "2028-04-03T16:30:00+00:00"],
        ),
        # Quarterly: the 15th of January, April, July and October.
        (
            "Asia/Kolkata",
            "23:45",
            15,
            [1, 4, 7, 10],
            "2027-01-16T00:00:00+00:00",
            ["2027-04-15T18:15:00+00:00", "2027-07-15T18:15:00+00:00"],
        ),
    ],
)
def test_monthly_next_run_matches_temporal(timezone, local_time, day, months, after, expected):
    schedule = WorkflowSchedule(
        cadence="monthly",
        day_of_month=day,
        months=months,
        local_time=local_time,
        timezone=timezone,
    )
    cursor = datetime.fromisoformat(after)
    projected = []
    for _ in expected:
        cursor = next_run_after(schedule, cursor)
        projected.append(cursor.isoformat())
    assert projected == expected
