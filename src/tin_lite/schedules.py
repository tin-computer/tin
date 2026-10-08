from __future__ import annotations

import re
from copy import copy
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleCalendarSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
)
from temporalio.service import RPCError, RPCStatusCode

from tin_lite.settings import Settings

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
# A monthly schedule runs on a day every month has, so it never skips a short month.
LAST_MONTHLY_DAY = 28


MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def monthly_words(schedule: dict) -> str:
    """'the 1st of every month' or 'the 15th of January, April, July and October'."""
    day = int(schedule.get("day_of_month") or 1)
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    names = [MONTH_NAMES[month - 1] for month in schedule.get("months") or []]
    if not names:
        return f"the {day}{suffix} of every month"
    listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return f"the {day}{suffix} of {listed}"


class ScheduledWorkflowSkip(RuntimeError):
    """An occurrence became stale or overlaps another accepted occurrence."""


class WorkflowSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cadence: Literal["daily", "weekly", "monthly"]
    weekdays: list[
        Literal[
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]
    ] = Field(default_factory=list, max_length=7)
    # Monthly only: the day of the month, and the months it runs in (empty: every month).
    # Four months such as [1, 4, 7, 10] make a quarterly schedule.
    day_of_month: int | None = Field(default=None, ge=1, le=LAST_MONTHLY_DAY)
    months: list[int] = Field(default_factory=list, max_length=12)
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(min_length=1, max_length=100)
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone name") from exc
        return value

    @model_validator(mode="after")
    def valid_weekdays(self) -> WorkflowSchedule:
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValueError("schedule end must be after its start")
        self.weekdays = list(dict.fromkeys(self.weekdays))
        if any(not 1 <= month <= 12 for month in self.months):
            raise ValueError("months must be numbers from 1 to 12")
        self.months = sorted(set(self.months))
        if self.cadence == "weekly" and not self.weekdays:
            raise ValueError("weekly schedules require at least one weekday")
        if self.cadence != "weekly" and self.weekdays:
            raise ValueError(f"{self.cadence} schedules do not accept weekdays")
        if self.cadence == "monthly" and self.day_of_month is None:
            raise ValueError("monthly schedules require a day_of_month from 1 to 28")
        if self.cadence != "monthly" and (self.day_of_month is not None or self.months):
            raise ValueError(f"{self.cadence} schedules do not accept day_of_month or months")
        return self

    @model_serializer(mode="wrap")
    def _omit_unused_monthly_fields(self, handler: SerializerFunctionWrapHandler) -> dict:
        # Daily and weekly schedules keep the exact stored shape they had before monthly
        # existed, so saved rows, receipts and an earlier release read them unchanged.
        data = handler(self)
        if self.cadence != "monthly":
            data.pop("day_of_month", None)
            data.pop("months", None)
        return data


# Names that load from a zoneinfo directory but are not IANA zones a schedule can use:
# `localtime` is the host's own zone, `posix/` and `right/` are alternate trees (`right/`
# counts leap seconds, so its times drift by them) and `Factory` has no real offset.
_UNSUPPORTED_ZONES = frozenset({"localtime", "posixrules", "Factory"})
_ZONE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*")


def supported_timezone(value: str) -> bool:
    """Whether a new schedule may be saved in this IANA zone (links such as US/Eastern count)."""
    if (
        value in _UNSUPPORTED_ZONES
        or value.startswith(("posix/", "right/"))
        or not _ZONE_NAME.fullmatch(value)
    ):
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def require_saveable_schedule(
    schedule: WorkflowSchedule | None, *, previous: dict | None = None
) -> None:
    """Refuse a new or changed schedule Temporal cannot run or that would never run again.

    Reads stay lenient: a schedule saved before these checks keeps loading and dispatching,
    and an edit that leaves it exactly as saved is not refused for it.
    """
    if schedule is None:
        return
    if previous is not None:
        try:
            unchanged = schedule == WorkflowSchedule.model_validate(previous)
        except ValueError:
            unchanged = False
        if unchanged:
            return
    if not supported_timezone(schedule.timezone):
        raise ValueError(
            "timezone must be an IANA timezone name such as Europe/Berlin or America/New_York"
        )
    if next_run_after(schedule) is None:
        raise ValueError("schedule end has passed; choose an end after its next run")


def _go_date(zone: ZoneInfo, y: int, mo: int, d: int, h: int, m: int, s: int) -> datetime:
    """Go's time.Date(y, mo, d, h, m, s, 0, zone), which places Temporal's calendar times.

    Overflowing fields carry. A repeated local time resolves to one of its two instants and a
    nonexistent one to an instant with another local hour, chosen from the offsets on either
    side exactly as Go does.
    """
    wall = datetime(y + (mo - 1) // 12, (mo - 1) % 12 + 1, 1, tzinfo=UTC) + timedelta(
        days=d - 1, hours=h, minutes=m, seconds=s
    )
    offset = wall.astimezone(zone).utcoffset() or timedelta(0)
    moved = (wall - offset).astimezone(zone).utcoffset() or timedelta(0)
    if moved != offset:
        offset = moved
    return wall - offset


def _days_in_month(month: int, year: int) -> int:
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 + ((0b1010110101010 >> month) & 1)


def _temporal_next(
    zone: ZoneInfo,
    hour: int,
    minute: int,
    weekdays: set[int] | None,
    after: datetime,
    *,
    days: set[int] | None = None,
    months: set[int] | None = None,
) -> datetime:
    """Port of Temporal's compiledCalendar.next (service/worker/scheduler/calendar.go).

    Temporal walks local calendar fields and places them with Go's time.Date, so across a
    DST change it fires a repeated time once or twice and skips or shifts a missing one,
    depending on the zone. Following the same steps keeps next_run_at and skip-once on the
    occurrence Temporal actually dispatches. `weekdays` uses Python's Monday=0 numbering;
    `days` (day of month) and `months` (1-12) default to every one, as in Temporal.
    """
    zero, hour_step = timedelta(0), timedelta(hours=1)
    ts = after.astimezone(zone)
    y, mo, d, h, m, s = ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second
    # Inside the second copy of a repeated hour.
    dst_offset = hour_step if (after - hour_step).astimezone(zone).hour == h else zero
    s += 1
    while True:
        if s >= 60:
            m, s = m + 1, 0
        if m >= 60:
            before = _go_date(zone, y, mo, d, h, 0, 0)
            h, m = h + 1, 0
            # Moving one hour on skipped a repeated hour: try it again with an offset.
            if dst_offset == zero and _go_date(zone, y, mo, d, h, 0, 0) - before > hour_step:
                h, dst_offset = h - 1, hour_step
            else:
                dst_offset = zero
        if h >= 24:
            d, h = d + 1, 0
        if d > _days_in_month(mo, y):
            mo, d = mo + 1, 1
        if mo > 12:
            y, mo = y + 1, 1
        if y > after.year + 2:
            raise RuntimeError("schedule has no next occurrence")
        restart = False
        while months is not None and mo not in months:
            mo, d, h, m, s, dst_offset = mo + 1, 1, 0, 0, 0, zero
            if mo > 12:
                restart = True
                break
        while not restart and (
            (days is not None and d not in days)
            or (
                weekdays is not None
                and _go_date(zone, y, mo, d, h, m, s).astimezone(zone).weekday() not in weekdays
            )
        ):
            d, h, m, s, dst_offset = d + 1, 0, 0, 0, zero
            if d > _days_in_month(mo, y):
                restart = True
                break
        while not restart and h != hour:
            h, m, s, dst_offset = h + 1, 0, 0, zero
            restart = h >= 24
        while not restart and m != minute:
            m, s = m + 1, 0
            restart = m >= 60
        while not restart and s != 0:
            s += 1
            restart = s >= 60
        if restart:
            continue
        candidate = _go_date(zone, y, mo, d, h, m, s)
        # A missing local time was jumped over: its instant carries a different local hour.
        if candidate.astimezone(zone).hour != h:
            h, m, s = h + 1, 0, 0
            continue
        return candidate + dst_offset


def next_run_after(schedule: WorkflowSchedule, after: datetime | None = None) -> datetime | None:
    """The first occurrence Temporal dispatches strictly after `after` (default: now)."""
    threshold = (after or datetime.now(UTC)).astimezone(UTC)
    if schedule.start_at:
        # Temporal starts one second before the range so its first second can still match.
        threshold = max(threshold, schedule.start_at.astimezone(UTC) - timedelta(seconds=1))
    hour, minute = (int(value) for value in schedule.local_time.split(":"))
    weekdays = (
        {WEEKDAYS.index(day) for day in schedule.weekdays} if schedule.cadence == "weekly" else None
    )
    monthly = schedule.cadence == "monthly"
    candidate = _temporal_next(
        ZoneInfo(schedule.timezone),
        hour,
        minute,
        weekdays,
        threshold,
        days={schedule.day_of_month} if monthly and schedule.day_of_month else None,
        months=set(schedule.months) if monthly and schedule.months else None,
    )
    if schedule.end_at and candidate >= schedule.end_at:
        return None
    return candidate


class TemporalScheduleService:
    def __init__(self, *, client: Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def create(
        self, *, project_workflow_id: str, schedule: WorkflowSchedule, paused: bool = False
    ) -> str:
        schedule_id = self.schedule_id(project_workflow_id)
        definition = self._definition(
            project_workflow_id=project_workflow_id,
            schedule=schedule,
            paused=paused,
        )
        try:
            await self._client.create_schedule(schedule_id, definition)
        except ScheduleAlreadyRunningError:
            await self.update(
                project_workflow_id=project_workflow_id,
                schedule=schedule,
                paused=paused,
            )
        return schedule_id

    async def update(
        self,
        *,
        project_workflow_id: str,
        schedule: WorkflowSchedule,
        paused: bool,
    ) -> None:
        definition = self._definition(
            project_workflow_id=project_workflow_id,
            schedule=schedule,
            paused=paused,
        )
        try:
            await self._client.get_schedule_handle(self.schedule_id(project_workflow_id)).update(
                lambda _input: ScheduleUpdate(definition)
            )
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            await self._client.create_schedule(self.schedule_id(project_workflow_id), definition)

    async def pause(self, project_workflow_id: str) -> None:
        try:
            await self._client.get_schedule_handle(self.schedule_id(project_workflow_id)).pause(
                note="Paused from Tin"
            )
        except RPCError as exc:
            # A schedule whose first sync failed was never created; nothing can fire.
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise

    async def remove_legacy_review_timeout(
        self, project_workflow_id: str, *, apply: bool = False
    ) -> bool:
        """Migrate a stored action without replacing its calendar, policy or pause state."""
        handle = self._client.get_schedule_handle(self.schedule_id(project_workflow_id))

        def updated(schedule: Schedule) -> ScheduleUpdate | None:
            action = schedule.action
            if (
                not isinstance(action, ScheduleActionStartWorkflow)
                or action.workflow != "tin.scheduled_dispatch"
                or action.id != f"tin-scheduled-dispatch:{project_workflow_id}"
            ):
                raise ValueError("Schedule is not the expected Tin dispatcher")
            if action.execution_timeout != timedelta(hours=24):
                if action.execution_timeout not in (None, timedelta(0)):
                    raise ValueError("Schedule has an unexpected execution timeout")
                return None
            # Retain raw payloads and SDK encoding metadata from describe().
            action = copy(action)
            action.execution_timeout = None
            migrated = copy(schedule)
            migrated.action = action
            return ScheduleUpdate(migrated)

        if not apply:
            return updated((await handle.describe()).schedule) is not None
        changed = False

        def update(current):
            nonlocal changed
            result = updated(current.description.schedule)
            changed = result is not None
            return result

        await handle.update(update)
        return changed

    async def resume(self, project_workflow_id: str) -> None:
        await self._client.get_schedule_handle(self.schedule_id(project_workflow_id)).unpause(
            note="Resumed from Tin"
        )

    async def delete(self, project_workflow_id: str) -> None:
        try:
            await self._client.get_schedule_handle(self.schedule_id(project_workflow_id)).delete()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise

    @staticmethod
    def schedule_id(project_workflow_id: str) -> str:
        return f"tin-lite-project-workflow:{project_workflow_id}"

    def _definition(
        self,
        *,
        project_workflow_id: str,
        schedule: WorkflowSchedule,
        paused: bool,
    ) -> Schedule:
        hour, minute = (int(value) for value in schedule.local_time.split(":"))
        day_ranges = (
            [ScheduleRange((WEEKDAYS.index(day) + 1) % 7) for day in schedule.weekdays]
            if schedule.cadence == "weekly"
            else [ScheduleRange(0, 6, 1)]
        )
        monthly = {}
        if schedule.cadence == "monthly" and schedule.day_of_month:
            monthly["day_of_month"] = [ScheduleRange(schedule.day_of_month)]
            if schedule.months:
                monthly["month"] = [ScheduleRange(month) for month in schedule.months]
        return Schedule(
            action=ScheduleActionStartWorkflow(
                "tin.scheduled_dispatch",
                project_workflow_id,
                id=f"tin-scheduled-dispatch:{project_workflow_id}",
                task_queue=self._settings.task_queue,
                # Catch-up bounds late starts, not the lifetime of a reviewable child.
                # Keep the dispatcher alive until that child finishes (including review).
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        minute=[ScheduleRange(minute)],
                        hour=[ScheduleRange(hour)],
                        day_of_week=day_ranges,
                        **monthly,
                    )
                ],
                time_zone_name=schedule.timezone,
                start_at=schedule.start_at,
                end_at=schedule.end_at,
            ),
            policy=SchedulePolicy(
                overlap=ScheduleOverlapPolicy.SKIP,
                catchup_window=timedelta(hours=24),
                pause_on_failure=False,
            ),
            state=ScheduleState(paused=paused),
        )
