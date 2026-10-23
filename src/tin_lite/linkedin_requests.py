"""Connection requests a member approves, sent one at a time on Tin's schedule (#450).

After an engagers run, the people who fit an ICP, engaged with the member's own posts and
aren't connected, invited before or a customer are proposed together in Decisions. Approving
queues them; nothing is sent then. A `social.linkedin_requests` run sends them one at a time
from the member's saved session:

- on weekdays, inside the member's window in the project's time zone;
- at least 15 minutes after the member's last request, from any project, plus a wait of up to
  25 minutes that each request draws once;
- under a daily and a weekly cap, halved in the member's first week of sending;
- only after a fresh read shows the person neither connected nor already invited.

A member can also choose people in People: choosing them approves them, in a batch of their
own. Either way, a member's requests on a project wait in one line, sent by one run: a batch
approved while a run still sends theirs joins it, behind what it already holds.

A request that may have gone out is never sent again. LinkedIn's warning, a signed-out session
or a refusal pauses the member's sending until they resume; LinkedIn's weekly limit holds it
until the next week. An approved request waits in the line, however long, until it is sent, the
member cancels it, or the person can't be asked any more (connected, invited, a customer or
forgotten); only a failed run lets go of what it held. The run finishes once nothing is left to
send. Who accepted is read by the member's own LinkedIn reads (profile viewers, daily, and
engagers): about once a day, for three weeks after each request went out.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from tin_lite import people_records as records
from tin_lite.schedules import supported_timezone

KEY = "social.linkedin_requests"
WORKFLOW_ID = UUID("9b3f6d2a-41c7-4e8b-a5d2-6c1e8f4b7a90")
INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "batch_id": {"type": "string", "format": "uuid"},
    },
    "required": ["project_id", "batch_id"],
}
CONSENT_VERSION = 1
FIT = 0.6
# People a member can choose at once in People or over MCP: a runaway guard well above a
# prospect list. The caps set the pace; chosen requests wait in the line until they go.
MAX_PICKED = 1000
CHECK_DAYS = 21
# Who accepted is read about once a day, whichever of the member's LinkedIn reads comes first.
READ_EVERY = timedelta(hours=20)
GAP = timedelta(minutes=15)
JITTER_SECONDS = 25 * 60
DEFAULTS = {
    "enabled": False,
    "consent_version": None,
    "daily_cap": 15,
    "weekly_cap": 60,
    "start_minute": 9 * 60,
    "end_minute": 17 * 60,
    "paused_reason": None,
    "paused_until": None,
}
# Requests that count against the member's caps: every one Tin tried to send.
ATTEMPTED = ("sending", "sent", "accepted", "not_pending", "uncertain", "not_sent", "refused")
# Reasons sending pauses until the member resumes it.
PAUSES = {
    "challenge": "LinkedIn asked to verify your account",
    "session_expired": "your saved LinkedIn session expired",
    "account_changed": "a different LinkedIn account is signed in to Chrome",
    "access_denied": "LinkedIn refused a request",
    "rate_limited": "LinkedIn asked Tin to slow down",
    "refused": "LinkedIn refused a request",
}


class RequestError(ValueError):
    """A change Tin refuses, with the sentence the member reads."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# Settings.


async def founder_timezone(conn, project_id: UUID) -> str:
    """The founder's time zone, read as the organic system reads it
    (`organic_system_activities.founder_timezone`): Start here's answer, then the newest saved
    schedule's, then the project's, else UTC."""

    candidates = (
        """SELECT input->>'timezone' FROM workflow_runs WHERE project_id = $1
           AND executor = 'growth.onboarding' AND input ? 'timezone'
           ORDER BY created_at DESC LIMIT 1""",
        """SELECT schedule->>'timezone' FROM project_workflows WHERE project_id = $1
           AND schedule IS NOT NULL AND status IN ('active', 'paused')
           ORDER BY created_at DESC LIMIT 1""",
        "SELECT timezone FROM projects WHERE id = $1",
    )
    for query in candidates:
        value = await conn.fetchval(query, project_id)
        if value and supported_timezone(value):
            return value
    return "UTC"


async def sending(conn, project_id: UUID, member: str) -> dict[str, Any]:
    """The member's sending on this project, in the founder's time zone."""

    row = await conn.fetchrow(
        """SELECT s.* FROM projects p
           LEFT JOIN linkedin_sending s ON s.project_id = p.id AND s.member = $2
           WHERE p.id = $1""",
        project_id,
        member,
    )
    if row is None:
        raise RequestError("not_found", "project not found")
    result = dict(DEFAULTS)
    # Whether the member ever set sending here; one who never did is on by default.
    result["set"] = row["member"] is not None
    if result["set"]:
        result.update({key: row[key] for key in DEFAULTS})
    result["timezone"] = await founder_timezone(conn, project_id)
    result["on"] = bool(result["enabled"] and result["consent_version"] == CONSENT_VERSION)
    until = result["paused_until"]
    # LinkedIn's weekly limit holds sending until a time; anything else until the member resumes.
    result["paused"] = bool(
        result["paused_reason"] and (until is None or until > datetime.now(UTC))
    )
    result["held"] = bool(result["paused"] and until is None)
    return result


async def set_sending(
    conn,
    project_id: UUID,
    member: str,
    *,
    enabled: bool,
    consent_version: int | None = None,
    limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Turn sending on (only with the current consent) or off, or change its limits."""

    if enabled and consent_version != CONSENT_VERSION:
        raise RequestError("consent_required", "Tick the box to agree to sending first.")
    limits = dict(limits or {})
    if set(limits) - {"daily_cap", "weekly_cap", "start_minute", "end_minute"}:
        raise RequestError("invalid", "Unknown sending limit.")
    current = await sending(conn, project_id, member)
    merged = {key: limits.get(key, current[key]) for key in limits or {}}
    values = {**{k: current[k] for k in ("daily_cap", "weekly_cap", "start_minute", "end_minute")}}
    values.update(merged)
    if not (
        1 <= values["daily_cap"] <= 25
        and 1 <= values["weekly_cap"] <= 100
        and 0 <= values["start_minute"] < values["end_minute"] <= 1440
    ):
        raise RequestError("invalid", "Choose limits within 1–25 a day, 1–100 a week.")
    await conn.execute(
        """INSERT INTO linkedin_sending (project_id, member, enabled, consent_version,
               consented_at, daily_cap, weekly_cap, start_minute, end_minute)
           VALUES ($1, $2, $3, $4, CASE WHEN $3 THEN now() END, $5, $6, $7, $8)
           ON CONFLICT (project_id, member) DO UPDATE SET
               enabled = EXCLUDED.enabled,
               consent_version = CASE WHEN EXCLUDED.enabled THEN EXCLUDED.consent_version
                                      ELSE linkedin_sending.consent_version END,
               consented_at = CASE WHEN EXCLUDED.enabled AND NOT linkedin_sending.enabled
                                   THEN now() ELSE linkedin_sending.consented_at END,
               daily_cap = EXCLUDED.daily_cap, weekly_cap = EXCLUDED.weekly_cap,
               start_minute = EXCLUDED.start_minute, end_minute = EXCLUDED.end_minute,
               updated_at = now()""",
        project_id,
        member,
        enabled,
        consent_version if enabled else current["consent_version"],
        values["daily_cap"],
        values["weekly_cap"],
        values["start_minute"],
        values["end_minute"],
    )
    return await sending(conn, project_id, member)


async def pause(conn, member: str, reason: str, *, until: datetime | None = None) -> None:
    """Hold the member's sending on every project: until they resume, or until `until`."""

    await conn.execute(
        """UPDATE linkedin_sending SET paused_reason = $2, paused_at = now(), paused_until = $3,
               updated_at = now()
           WHERE member = $1""",
        member,
        reason[:60],
        until,
    )


async def resume(conn, project_id: UUID, member: str) -> dict[str, Any]:
    await conn.execute(
        """UPDATE linkedin_sending SET paused_reason = NULL, paused_at = NULL,
               paused_until = NULL, updated_at = now()
           WHERE member = $1""",
        member,
    )
    return await sending(conn, project_id, member)


# Pacing.


def jitter(request_id: UUID | str) -> timedelta:
    """The extra wait one request draws, the same every time it is asked."""

    digest = hashlib.sha256(str(request_id).encode()).digest()
    return timedelta(seconds=int.from_bytes(digest[:4], "big") % (JITTER_SECONDS + 1))


def _at(day: date, minute: int, zone: ZoneInfo) -> datetime:
    moment = datetime.combine(day, time(), tzinfo=zone) + timedelta(minutes=minute)
    return moment.astimezone(UTC)


def _caps(settings: dict, first: datetime | None, moment: datetime) -> tuple[int, int]:
    daily, weekly = settings["daily_cap"], settings["weekly_cap"]
    if first is None or moment - first < timedelta(days=7):
        return math.ceil(daily / 2), math.ceil(weekly / 2)
    return daily, weekly


def next_slot(
    now: datetime,
    settings: dict,
    attempts: list[datetime],
    request_id: UUID | str,
    *,
    first: datetime | None = None,
) -> datetime | None:
    """The earliest moment this request may be sent: after the gap since the member's last
    request, inside a weekday window, under the caps. None past a few weeks of searching."""

    zone = ZoneInfo(settings["timezone"])
    moment = now
    if attempts:
        moment = max(moment, max(attempts) + GAP + jitter(request_id))
    paused_until = settings.get("paused_until")
    if paused_until and moment < paused_until:
        moment = paused_until
    for _ in range(80):
        local = moment.astimezone(zone)
        day = local.date()
        start = _at(day, settings["start_minute"], zone)
        end = _at(day, settings["end_minute"], zone)
        if local.weekday() >= 5 or moment >= end:
            following = day + timedelta(days=1)
            while following.weekday() >= 5:
                following += timedelta(days=1)
            moment = _at(following, settings["start_minute"], zone)
            continue
        if moment < start:
            moment = start
            continue
        daily, weekly = _caps(settings, first, moment)
        local_days = [a.astimezone(zone).date() for a in attempts]
        week = day.isocalendar()[:2]
        if sum(1 for d in local_days if d.isocalendar()[:2] == week) >= weekly:
            monday = day + timedelta(days=7 - local.weekday())
            moment = _at(monday, settings["start_minute"], zone)
            continue
        if sum(1 for d in local_days if d == day) >= daily:
            moment = end
            continue
        return moment
    return None


def projection(
    now: datetime,
    settings: dict,
    attempts: list[datetime],
    request_ids: list,
    *,
    first: datetime | None = None,
) -> list[datetime]:
    """When each queued request would go, in order, if each were sent at its slot."""

    planned, sent, moment = [], list(attempts), now
    for request_id in request_ids:
        earliest = first or (min(sent) if sent else None)
        slot = next_slot(moment, settings, sent, request_id, first=earliest)
        if slot is None:
            break
        planned.append(slot)
        sent.append(slot)
        moment = slot
    return planned


async def member_attempts(
    conn, member: str, now: datetime
) -> tuple[list[datetime], datetime | None]:
    """The member's requests from every project over the last eight days, and their first."""

    rows = await conn.fetch(
        """SELECT attempted_at FROM linkedin_requests
           WHERE member = $1 AND attempted_at > $2 ORDER BY attempted_at""",
        member,
        now - timedelta(days=8),
    )
    first = await conn.fetchval(
        "SELECT min(attempted_at) FROM linkedin_requests WHERE member = $1", member
    )
    return [row["attempted_at"] for row in rows], first


# Proposals.


async def propose(conn, project_id: UUID, member: str, *, run_id: UUID | None = None) -> int:
    """Add everyone newly eligible to the member's open batch; returns how many were added."""

    rows = await conn.fetch(
        """SELECT r.id, i.value AS target FROM records r
           JOIN record_identifiers i ON i.record_id = r.id AND i.kind = 'linkedin_member'
           WHERE r.project_id = $1 AND r.merged_into IS NULL
             -- They engaged with this member's own posts, or viewed their profile.
             AND EXISTS (
                 SELECT 1 FROM record_events e
                 JOIN records m ON m.id = e.record_id
                 JOIN workflow_runs w ON w.id = e.run_id
                 WHERE e.project_id = $1 AND COALESCE(m.merged_into, m.id) = r.id
                   AND e.kind = ANY($4::text[])
                   AND w.started_by_clerk_user_id = $2)
             AND EXISTS (SELECT 1 FROM record_values v WHERE v.record_id = r.id
                         AND v.field = 'icp_fit' AND v.value_number >= $3)
             AND EXISTS (SELECT 1 FROM record_relationships x WHERE x.record_id = r.id
                         AND x.member = $2 AND x.state = 'none')
             AND NOT EXISTS (SELECT 1 FROM record_values v WHERE v.record_id = r.id
                             AND v.field = 'customer' AND v.value_text = 'yes')
             AND NOT EXISTS (SELECT 1 FROM linkedin_requests q
                             WHERE q.project_id = $1 AND q.record_id = r.id AND q.member = $2)
           ORDER BY r.created_at, r.id""",
        project_id,
        member,
        FIT,
        list(records.SIGNALS),
    )
    if not rows:
        return 0
    batch = await conn.fetchval(
        """SELECT id FROM linkedin_request_batches
           WHERE project_id = $1 AND member = $2 AND status = 'open'""",
        project_id,
        member,
    )
    if batch is None:
        batch = await conn.fetchval(
            """INSERT INTO linkedin_request_batches (id, project_id, member, source_run_id)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (project_id, member) WHERE status = 'open' DO NOTHING
               RETURNING id""",
            uuid4(),
            project_id,
            member,
            run_id,
        ) or await conn.fetchval(
            """SELECT id FROM linkedin_request_batches
               WHERE project_id = $1 AND member = $2 AND status = 'open'""",
            project_id,
            member,
        )
    added = 0
    for row in rows:
        added += bool(
            await conn.fetchval(
                """INSERT INTO linkedin_requests (id, project_id, batch_id, record_id, member,
                       target)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (batch_id, record_id) DO NOTHING RETURNING true""",
                uuid4(),
                project_id,
                batch,
                row["id"],
                member,
                row["target"],
            )
        )
    return added


# People a member picks themselves.


async def pickable(
    conn, project_id: UUID, member: str, *, only: list[UUID] | None = None
) -> dict[UUID, str]:
    """The people the member can ask now, with each one's LinkedIn profile ID: someone Tin can
    name on LinkedIn, not a customer, not the member's connection or invitee as far as Tin
    knows, and not asked by the member before. A request lapsed, declined or never sent doesn't
    count; one waiting in the member's batch in Decisions moves to theirs."""

    rows = await conn.fetch(
        """SELECT r.id, min(i.value) AS target FROM records r
           JOIN record_identifiers i ON i.record_id = r.id AND i.kind = 'linkedin_member'
           WHERE r.project_id = $1 AND r.merged_into IS NULL AND r.type = 'person'
             AND ($3::uuid[] IS NULL OR r.id = ANY($3::uuid[]))
             AND i.value ~ '^ACo[A-Za-z0-9_-]{20,80}$'
             AND NOT EXISTS (SELECT 1 FROM record_values v WHERE v.record_id = r.id
                             AND v.field = 'customer' AND v.value_text = 'yes')
             AND NOT EXISTS (SELECT 1 FROM record_relationships x WHERE x.record_id = r.id
                             AND x.member = $2 AND x.state IN ('connected', 'pending'))
             AND NOT EXISTS (
                 SELECT 1 FROM linkedin_requests q JOIN records m ON m.id = q.record_id
                 WHERE q.project_id = $1 AND q.member = $2
                   AND COALESCE(m.merged_into, m.id) = r.id
                   AND q.status NOT IN ('proposed', 'declined', 'lapsed', 'not_sent'))
           GROUP BY r.id""",
        project_id,
        member,
        only,
    )
    return {row["id"]: row["target"] for row in rows}


async def waiting(conn, project_id: UUID, member: str) -> set[UUID]:
    """The people whose request from the member waits to go out."""

    rows = await conn.fetch(
        """SELECT DISTINCT COALESCE(m.merged_into, m.id) AS person
           FROM linkedin_requests q JOIN records m ON m.id = q.record_id
           WHERE q.project_id = $1 AND q.member = $2 AND q.status IN ('queued', 'sending')""",
        project_id,
        member,
    )
    return {row["person"] for row in rows}


async def pick(
    conn, project_id: UUID, member: str, people: list[UUID], *, request_id: UUID
) -> dict:
    """Queue requests to the people the member chose in People: choosing them is the approval,
    so they wait in a batch of their own, approved, for a sending run like any other. The same
    request ID repeats the same choice."""

    chosen = list(dict.fromkeys(people))
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"linkedin-picks:{project_id}:{member}",
        )
        repeated = await conn.fetchrow(
            "SELECT * FROM linkedin_request_batches WHERE decision_request_id = $1", request_id
        )
        if repeated is not None:
            if repeated["project_id"] != project_id or repeated["member"] != member:
                raise RequestError("conflict", "This choice was already recorded.")
            queued = await conn.fetchval(
                "SELECT count(*) FROM linkedin_requests WHERE batch_id = $1", repeated["id"]
            )
            return {
                "status": repeated["status"],
                "batch_id": repeated["id"],
                "queued": queued,
                "repeated": True,
            }
        if not chosen:
            raise RequestError("invalid", "Choose at least one person.")
        if len(chosen) > MAX_PICKED:
            raise RequestError("invalid", f"Choose up to {MAX_PICKED} people at a time.")
        if not (await sending(conn, project_id, member))["on"]:
            raise RequestError("sending_off", "Turn on sending on the LinkedIn card first.")
        targets = await pickable(conn, project_id, member, only=chosen)
        if set(chosen) - set(targets):
            raise RequestError(
                "invalid",
                "Someone you chose can't get a request from you now. Choose again in People.",
            )
        batch_id = uuid4()
        decided_at = await conn.fetchval(
            """INSERT INTO linkedin_request_batches (id, project_id, member, status, decided_at,
                   decision_request_id)
               VALUES ($1, $2, $3, 'approved', now(), $4) RETURNING decided_at""",
            batch_id,
            project_id,
            member,
            request_id,
        )
        # Requests go in the order they were queued: here, the order the member chose.
        for offset, record_id in enumerate(chosen):
            queued_at = decided_at + timedelta(milliseconds=offset)
            # Someone already waiting in the member's batch in Decisions moves over.
            moved = await conn.fetchval(
                """UPDATE linkedin_requests q SET batch_id = $3, record_id = $4,
                       status = 'queued', queued_at = $5
                   FROM records m
                   WHERE m.id = q.record_id AND q.project_id = $1 AND q.member = $2
                     AND q.status = 'proposed' AND COALESCE(m.merged_into, m.id) = $4
                   RETURNING true""",
                project_id,
                member,
                batch_id,
                record_id,
                queued_at,
            )
            if not moved:
                await conn.execute(
                    """INSERT INTO linkedin_requests (id, project_id, batch_id, record_id,
                           member, target, status, queued_at)
                       VALUES ($1, $2, $3, $4, $5, $6, 'queued', $7)""",
                    uuid4(),
                    project_id,
                    batch_id,
                    record_id,
                    member,
                    targets[record_id],
                    queued_at,
                )
    return {"status": "approved", "batch_id": batch_id, "queued": len(chosen), "repeated": False}


async def unpick(conn, batch_id: UUID) -> bool:
    """Undo a choice whose sending run never started: nothing was approved, so the batch goes
    and its people can be chosen again. Anyone it took from the member's batch in Decisions
    waits there again. Returns whether anything changed."""

    batch = await conn.fetchrow(
        "SELECT * FROM linkedin_request_batches WHERE id = $1 FOR UPDATE", batch_id
    )
    if batch is None or batch["status"] != "approved" or batch["send_run_id"] is not None:
        return False
    if await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM linkedin_requests WHERE batch_id = $1
           AND (attempted_at IS NOT NULL OR status <> 'queued'))""",
        batch_id,
    ):
        return False
    # The choice wrote its own requests as it was made; older ones came from Decisions.
    await conn.execute(
        """UPDATE linkedin_requests SET status = 'proposed', queued_at = NULL, batch_id = b.id
           FROM linkedin_request_batches b
           WHERE linkedin_requests.batch_id = $1 AND linkedin_requests.created_at < $2
             AND b.project_id = $3 AND b.member = $4 AND b.status = 'open'""",
        batch_id,
        batch["decided_at"],
        batch["project_id"],
        batch["member"],
    )
    await conn.execute("DELETE FROM linkedin_request_batches WHERE id = $1", batch_id)
    return True


async def plan(conn, project_id: UUID, member: str, request_ids: list, now: datetime) -> list:
    """When each of these requests would go, after everything the member already queued."""

    settings = await sending(conn, project_id, member)
    attempts, first = await member_attempts(conn, member, now)
    queued = [
        row["id"]
        for row in await conn.fetch(
            "SELECT id FROM linkedin_requests WHERE member = $1 AND status = 'queued' "
            "ORDER BY queued_at, id",
            member,
        )
    ]
    mine = set(request_ids)
    ahead = [request_id for request_id in queued if request_id not in mine]
    return projection(now, settings, attempts, [*ahead, *request_ids], first=first)[len(ahead) :]


async def open_batches(conn, project_id: UUID, member: str, now: datetime) -> list[dict]:
    """The member's batch waiting in Decisions, best fit first, with when each would go."""

    batch = await conn.fetchrow(
        """SELECT id, created_at FROM linkedin_request_batches
           WHERE project_id = $1 AND member = $2 AND status = 'open'""",
        project_id,
        member,
    )
    if batch is None:
        return []
    people = [
        dict(row)
        for row in await conn.fetch(
            """SELECT q.id AS request_id, q.record_id,
                   (SELECT value_text FROM record_values v
                    WHERE v.record_id = q.record_id AND v.field = 'name') AS name,
                   (SELECT value_text FROM record_values v
                    WHERE v.record_id = q.record_id AND v.field = 'headline') AS headline,
                   (SELECT value_text FROM record_values v
                    WHERE v.record_id = q.record_id AND v.field = 'icp') AS icp,
                   (SELECT value_number FROM record_values v
                    WHERE v.record_id = q.record_id AND v.field = 'icp_fit') AS fit
               FROM linkedin_requests q
               WHERE q.batch_id = $1 AND q.status = 'proposed'
                 -- Someone marked a customer since they were proposed drops out.
                 AND NOT EXISTS (SELECT 1 FROM records m JOIN record_values v
                                 ON v.record_id = COALESCE(m.merged_into, m.id)
                                 WHERE m.id = q.record_id AND v.field = 'customer'
                                   AND v.value_text = 'yes')
               ORDER BY fit DESC NULLS LAST, name""",
            batch["id"],
        )
    ]
    if not people:
        return []
    # What each did last: a reaction counts on the post's date, as in People.
    engaged: dict = {}
    for row in await conn.fetch(
        """SELECT COALESCE(r.merged_into, r.id) AS person, e.kind, e.occurred_at,
               e.recorded_at, e.detail->>'posted_at' AS posted_at
           FROM record_events e JOIN records r ON r.id = e.record_id
           WHERE e.project_id = $1 AND e.kind = ANY($3::text[])
             AND COALESCE(r.merged_into, r.id) = ANY($2::uuid[])""",
        project_id,
        [p["record_id"] for p in people],
        list(records.SIGNALS),
    ):
        at = records._when(row)
        if row["person"] not in engaged or at > engaged[row["person"]]["at"]:
            engaged[row["person"]] = {"kind": row["kind"], "at": at}
    slots = await plan(conn, project_id, member, [p["request_id"] for p in people], now)
    return [
        {
            "id": batch["id"],
            "created_at": batch["created_at"],
            "people": [{**p, "engaged": engaged.get(p["record_id"])} for p in people],
            "first_send": slots[0] if slots else None,
            "last_send": slots[-1] if slots else None,
        }
    ]


async def decide(
    conn,
    project_id: UUID,
    batch_id: UUID,
    member: str,
    *,
    request_id: UUID,
    approve: bool,
    people: list[UUID] | None = None,
) -> dict:
    """Approve the chosen people (queued, sent later) or discard the batch. Only the member
    whose LinkedIn would send decides; the same request ID repeats the same decision."""

    async with conn.transaction():
        batch = await conn.fetchrow(
            "SELECT * FROM linkedin_request_batches WHERE id = $1 AND project_id = $2 FOR UPDATE",
            batch_id,
            project_id,
        )
        if batch is None or batch["member"] != member:
            raise RequestError("not_found", "These requests are no longer waiting.")
        if batch["status"] != "open":
            if batch["decision_request_id"] == request_id:
                return {"status": batch["status"], "batch_id": batch_id, "repeated": True}
            raise RequestError("conflict", "These requests were already decided.")
        proposed = {
            row["record_id"]
            for row in await conn.fetch(
                """SELECT q.record_id FROM linkedin_requests q WHERE q.batch_id = $1
                   AND q.status = 'proposed'
                   AND NOT EXISTS (SELECT 1 FROM records m JOIN record_values v
                                   ON v.record_id = COALESCE(m.merged_into, m.id)
                                   WHERE m.id = q.record_id AND v.field = 'customer'
                                     AND v.value_text = 'yes')""",
                batch_id,
            )
        }
        chosen = set(people or []) if approve else set()
        if approve:
            if not chosen:
                raise RequestError("invalid", "Choose at least one person.")
            if chosen - proposed:
                raise RequestError("invalid", "Someone you chose is no longer in this batch.")
            if not (await sending(conn, project_id, member))["on"]:
                raise RequestError("sending_off", "Turn on sending on the LinkedIn card first.")
        await conn.execute(
            """UPDATE linkedin_requests SET status = 'queued', queued_at = now()
               WHERE batch_id = $1 AND status = 'proposed' AND record_id = ANY($2::uuid[])""",
            batch_id,
            list(chosen),
        )
        await conn.execute(
            """UPDATE linkedin_requests SET status = 'declined', settled_at = now()
               WHERE batch_id = $1 AND status = 'proposed'""",
            batch_id,
        )
        status = "approved" if approve else "discarded"
        await conn.execute(
            """UPDATE linkedin_request_batches SET status = $2, decided_at = now(),
                   decision_request_id = $3
               WHERE id = $1""",
            batch_id,
            status,
            request_id,
        )
    return {"status": status, "batch_id": batch_id, "queued": len(chosen), "repeated": False}


async def reopen(conn, batch_id: UUID) -> bool:
    """Undo an approval whose sending run never started: its people wait in Decisions again,
    including any it declined. Returns whether anything changed."""

    batch = await conn.fetchrow(
        "SELECT * FROM linkedin_request_batches WHERE id = $1 FOR UPDATE", batch_id
    )
    if batch is None or batch["status"] != "approved" or batch["send_run_id"] is not None:
        return False
    # The decision set every row it touched at the same moment it was decided.
    if await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM linkedin_requests WHERE batch_id = $1
           AND (attempted_at IS NOT NULL OR status NOT IN ('queued', 'declined')
                OR COALESCE(queued_at, settled_at) <> $2))""",
        batch_id,
        batch["decided_at"],
    ):
        return False
    # An engagers run may have opened a new batch since: its people join that one.
    target = await conn.fetchval(
        """SELECT id FROM linkedin_request_batches
           WHERE project_id = $1 AND member = $2 AND status = 'open'""",
        batch["project_id"],
        batch["member"],
    )
    await conn.execute(
        """UPDATE linkedin_requests SET status = 'proposed', queued_at = NULL,
               settled_at = NULL, batch_id = $2
           WHERE batch_id = $1""",
        batch_id,
        target or batch_id,
    )
    if target is None:
        await conn.execute(
            """UPDATE linkedin_request_batches SET status = 'open', decided_at = NULL,
                   decision_request_id = NULL
               WHERE id = $1""",
            batch_id,
        )
    else:
        await conn.execute(
            "UPDATE linkedin_request_batches SET status = 'discarded' WHERE id = $1", batch_id
        )
    return True


# What the member sees: this week, and what waits.


async def week(conn, project_id: UUID, member: str, now: datetime) -> dict:
    settings = await sending(conn, project_id, member)
    zone = ZoneInfo(settings["timezone"])
    local = now.astimezone(zone)
    monday = _at(local.date() - timedelta(days=local.weekday()), 0, zone)
    row = await conn.fetchrow(
        """SELECT
               count(*) FILTER (WHERE status IN ('sent', 'accepted', 'not_pending')
                                AND sent_at >= $3) AS sent,
               count(*) FILTER (WHERE status = 'accepted' AND settled_at >= $3) AS accepted,
               count(*) FILTER (WHERE status = 'queued') AS queued,
               count(*) FILTER (WHERE status = 'sent' AND settled_at IS NULL) AS pending
           FROM linkedin_requests WHERE project_id = $1 AND member = $2""",
        project_id,
        member,
        monday,
    )
    queued = [
        r["id"]
        for r in await conn.fetch(
            "SELECT id FROM linkedin_requests WHERE member = $1 AND status = 'queued' "
            "ORDER BY queued_at, id LIMIT 1",
            member,
        )
    ]
    next_send = None
    if queued and settings["on"] and not settings["held"]:
        attempts, first = await member_attempts(conn, member, now)
        next_send = next_slot(now, settings, attempts, queued[0], first=first)
    return {
        **{key: row[key] for key in ("sent", "accepted", "queued", "pending")},
        "next_send": next_send,
    }


# One line per member: the run already sending a member's requests sends the next batch too.


async def _line_lock(conn, project_id: UUID, member: str) -> None:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"linkedin-line:{project_id}:{member}",
    )


async def sending_run(conn, batch_id: UUID) -> UUID | None:
    """The run that sends this approved batch, or None when a new one should start.

    A batch joins the member's run on the project while that run still has requests waiting,
    so they go after the ones it holds. A run with none left is finishing, so a new run
    starts instead. Call in a transaction and record a new run on the batch before it
    commits: the member's line stays locked until then, so two approvals at once still share
    one run."""

    batch = await conn.fetchrow(
        "SELECT * FROM linkedin_request_batches WHERE id = $1 FOR UPDATE", batch_id
    )
    if batch is None or batch["send_run_id"] is not None:
        return batch["send_run_id"] if batch else None
    await _line_lock(conn, batch["project_id"], batch["member"])
    run_id = await conn.fetchval(
        """SELECT b.send_run_id FROM linkedin_request_batches b
           JOIN workflow_runs w ON w.id = b.send_run_id
           JOIN linkedin_requests q ON q.batch_id = b.id
           WHERE b.project_id = $1 AND b.member = $2 AND w.status IN ('pending', 'running')
             AND q.status = 'queued'
           ORDER BY w.created_at DESC LIMIT 1""",
        batch["project_id"],
        batch["member"],
    )
    if run_id is not None:
        await conn.execute(
            "UPDATE linkedin_request_batches SET send_run_id = $2 WHERE id = $1", batch_id, run_id
        )
    return run_id


# What a sending run does next. It sends every batch it holds (`send_run_id`).


async def next_step(conn, run_id: UUID, now: datetime) -> dict:
    """Send the run's next request, wait, or finish once nothing is left to send.

    Returns {"action": "send", "request_id"}, {"action": "wait", "seconds"} or
    {"action": "done"}. Requests go in the order they were queued. Sending waits while the
    member's sending is off or paused; a wait is never longer than six hours, so a change is
    noticed. Decided under the member's line lock, so a batch never joins a run that has just
    finished. Who accepted is read by the member's LinkedIn reads, not here (`reading_due`);
    runs from before that asked for {"action": "check"}.
    """

    async with conn.transaction():
        held = await conn.fetchrow(
            """SELECT DISTINCT project_id, member FROM linkedin_request_batches
               WHERE send_run_id = $1""",
            run_id,
        )
        if held is None:
            return {"action": "done"}
        await _line_lock(conn, held["project_id"], held["member"])
        queued = await conn.fetchrow(
            """SELECT q.id FROM linkedin_requests q
               JOIN linkedin_request_batches b ON b.id = q.batch_id
               WHERE b.send_run_id = $1 AND q.status = 'queued'
               ORDER BY q.queued_at, q.id LIMIT 1""",
            run_id,
        )
        if queued is None:
            return {"action": "done"}
        settings = await sending(conn, held["project_id"], held["member"])
        if not settings["on"] or settings["held"]:
            return {"action": "wait", "seconds": 6 * 3600}
        attempts, first = await member_attempts(conn, held["member"], now)
        slot = next_slot(now, settings, attempts, queued["id"], first=first)
        if slot is None:
            return {"action": "wait", "seconds": 6 * 3600}
        if slot <= now + timedelta(seconds=30):
            return {"action": "send", "request_id": str(queued["id"])}
        return {"action": "wait", "seconds": _seconds(slot - now)}


def _seconds(delta: timedelta) -> int:
    return max(30, min(6 * 3600, math.ceil(delta.total_seconds())))


async def reserve(conn, request_id: UUID, now: datetime) -> dict:
    """Under the member's lock: the request's turn has come, so mark it attempted before the
    send, or say how long to wait. Returns {"go": True, ...} or {"wait": seconds}."""

    request = await conn.fetchrow("SELECT * FROM linkedin_requests WHERE id = $1", request_id)
    if request is None or request["status"] not in {"queued", "sending"}:
        return {"skip": True}
    details = {
        "go": True,
        "project_id": request["project_id"],
        "record_id": request["record_id"],
        "member": request["member"],
        "target": request["target"],
        "batch_id": request["batch_id"],
    }
    if request["status"] == "sending":
        # Reserved before an interruption: its turn was already taken and counted.
        return details
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"linkedin-requests:{request['member']}",
    )
    # Someone marked a customer since it was queued isn't asked.
    if await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM records m JOIN record_values v
                          ON v.record_id = COALESCE(m.merged_into, m.id)
                          WHERE m.id = $1 AND v.field = 'customer' AND v.value_text = 'yes')""",
        request["record_id"],
    ):
        await conn.execute(
            """UPDATE linkedin_requests SET status = 'declined', settled_at = now()
               WHERE id = $1 AND status = 'queued'""",
            request_id,
        )
        return {"skip": True}
    settings = await sending(conn, request["project_id"], request["member"])
    if not settings["on"] or settings["held"]:
        return {"wait": 6 * 3600}
    attempts, first = await member_attempts(conn, request["member"], now)
    slot = next_slot(now, settings, attempts, request_id, first=first)
    if slot is None or slot > now + timedelta(seconds=60):
        return {"wait": _seconds((slot or now + timedelta(hours=6)) - now)}
    # Only a request still waiting goes: one cancelled meanwhile stays cancelled.
    if not await conn.fetchval(
        """UPDATE linkedin_requests SET status = 'sending', attempted_at = $2
           WHERE id = $1 AND status = 'queued' RETURNING true""",
        request_id,
        now,
    ):
        return {"skip": True}
    return details


async def cancel(conn, project_id: UUID, member: str, person_id: UUID) -> int:
    """Take the member's request to this person out of the queue before it goes. It reads as
    declined, so they can be chosen again; one LinkedIn is being asked right now can't be."""

    cancelled = await conn.fetch(
        """UPDATE linkedin_requests q SET status = 'declined', settled_at = now()
           FROM records m
           WHERE m.id = q.record_id AND q.project_id = $1 AND q.member = $2
             AND COALESCE(m.merged_into, m.id) = $3 AND q.status = 'queued'
           RETURNING q.id""",
        project_id,
        member,
        person_id,
    )
    if cancelled:
        return len(cancelled)
    if person_id in await waiting(conn, project_id, member):
        raise RequestError("conflict", "Tin is sending this request right now.")
    raise RequestError("not_found", "No request to them is waiting to go out.")


async def queued(conn, project_id: UUID, member: str, person_id: UUID, now: datetime):
    """The member's request to this person waiting to go out: when they queued it and when it
    would go (None while sending is off or paused), or None without one."""

    row = await conn.fetchrow(
        """SELECT q.id, q.status, q.queued_at FROM linkedin_requests q
           JOIN records m ON m.id = q.record_id
           WHERE q.project_id = $1 AND q.member = $2 AND COALESCE(m.merged_into, m.id) = $3
             AND q.status IN ('queued', 'sending')
           ORDER BY q.queued_at, q.id LIMIT 1""",
        project_id,
        member,
        person_id,
    )
    if row is None:
        return None
    found = {"queued_at": row["queued_at"], "sending": row["status"] == "sending", "at": None}
    settings = await sending(conn, project_id, member)
    if found["sending"] or not settings["on"] or settings["held"]:
        return found
    attempts, first = await member_attempts(conn, member, now)
    ahead = [
        item["id"]
        for item in await conn.fetch(
            "SELECT id FROM linkedin_requests WHERE member = $1 AND status = 'queued' "
            "AND (queued_at, id) <= ($2, $3) ORDER BY queued_at, id",
            member,
            row["queued_at"],
            row["id"],
        )
    ]
    slots = projection(now, settings, attempts, ahead, first=first)
    found["at"] = slots[-1] if len(slots) == len(ahead) else None
    return found


async def drop_customer(conn, project_id: UUID, person_id: UUID) -> int:
    """Someone just marked a customer isn't asked: every member's request to them that is
    still waiting reads as declined."""

    result = await conn.execute(
        """UPDATE linkedin_requests q SET status = 'declined', settled_at = now()
           FROM records m
           WHERE m.id = q.record_id AND q.project_id = $1
             AND COALESCE(m.merged_into, m.id) = $2 AND q.status = 'queued'""",
        project_id,
        person_id,
    )
    return int(result.split()[-1])


async def settle(
    conn, request_id: UUID, outcome: str, *, run_id: UUID, invitation: str | None = None
) -> str:
    """Record what one send came to on the request and on the person. Returns its status."""

    request = await conn.fetchrow("SELECT * FROM linkedin_requests WHERE id = $1", request_id)
    status = {
        "sent": "sent",
        "connected": "connected",
        "invited": "invited",
        "unknown_person": "unknown_person",
        "refused": "refused",
        "uncertain": "uncertain",
    }.get(outcome)
    if outcome == "limit" or (outcome in PAUSES and outcome != "refused"):
        # Nothing went out: the request waits its turn again.
        await conn.execute(
            "UPDATE linkedin_requests SET status = 'queued', attempted_at = NULL WHERE id = $1",
            request_id,
        )
        return "queued"
    if status is None:
        status = "uncertain"
    now = datetime.now(UTC)
    await conn.execute(
        """UPDATE linkedin_requests SET status = $2::text, invitation = $3::text,
               sent_at = CASE WHEN $2::text = 'sent' THEN $4::timestamptz END,
               settled_at = CASE WHEN $2::text IN ('connected', 'invited', 'unknown_person',
                                                    'refused')
                                 THEN $4::timestamptz END
           WHERE id = $1""",
        request_id,
        status,
        invitation,
        now,
    )
    relationship = {"sent": "pending", "invited": "pending", "connected": "connected"}.get(status)
    if relationship:
        await _relationship(conn, request, relationship, run_id)
    if status == "sent":
        await records.add_event(
            conn,
            request["project_id"],
            request["record_id"],
            kind="invited",
            event_key=f"linkedin:invited:{request['id']}",
            occurred_at=now,
            detail={"member": request["member"]},
            run_id=run_id,
        )
    return status


async def _relationship(conn, request, value: str, run_id: UUID) -> None:
    try:
        await records.set_relationship(
            conn,
            request["project_id"],
            request["record_id"],
            request["member"],
            value,
            set_by=str(run_id),
        )
    except records.RecordError as error:
        if error.code != "record_gone":
            raise


# Who accepted. The member's own LinkedIn reads (profile viewers, daily, and engagers) read it
# once a day while any of their requests on the project waits for an answer.


async def checking(conn, project_id: UUID, member: str, now: datetime) -> list[dict]:
    """The member's requests on the project still waiting for an answer, at most 20, oldest
    first; those past three weeks stop being read."""

    await conn.execute(
        """UPDATE linkedin_requests SET settled_at = now()
           WHERE project_id = $1 AND member = $2 AND status IN ('sent', 'uncertain')
             AND settled_at IS NULL AND attempted_at < $3""",
        project_id,
        member,
        now - timedelta(days=CHECK_DAYS),
    )
    rows = await conn.fetch(
        """SELECT id, target, status FROM linkedin_requests
           WHERE project_id = $1 AND member = $2 AND status IN ('sent', 'uncertain')
             AND settled_at IS NULL
           ORDER BY attempted_at, id LIMIT 20""",
        project_id,
        member,
    )
    return [dict(row) for row in rows]


async def reading_due(conn, project_id: UUID, member: str, now: datetime) -> bool:
    """Whether a LinkedIn read of the member's should read who accepted: a request of theirs
    waits for an answer, and the last read was about a day ago, or never."""

    last = await conn.fetchval(
        "SELECT max(checked_at) FROM linkedin_request_batches WHERE project_id = $1 "
        "AND member = $2",
        project_id,
        member,
    )
    if last is not None and last > now - READ_EVERY:
        return False
    return await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM linkedin_requests
                          WHERE project_id = $1 AND member = $2
                            AND status IN ('sent', 'uncertain') AND settled_at IS NULL
                            AND attempted_at >= $3)""",
        project_id,
        member,
        now - timedelta(days=CHECK_DAYS),
    )


async def read_at(conn, project_id: UUID, member: str, now: datetime) -> None:
    """Note that Tin read who accepted, or tried to, at the time the read was decided."""

    await conn.execute(
        "UPDATE linkedin_request_batches SET checked_at = $3 WHERE project_id = $1 "
        "AND member = $2 AND status = 'approved'",
        project_id,
        member,
        now,
    )


async def checked(
    conn, project_id: UUID, member: str, states: dict[str, str], now: datetime, *, run_id: UUID
) -> dict:
    """Record a relationship read by the run `run_id`: accepted, still pending, or no longer
    pending."""

    counts = {"accepted": 0, "pending": 0, "ended": 0}
    for row in await checking(conn, project_id, member, now):
        state = states.get(row["target"])
        request = await conn.fetchrow("SELECT * FROM linkedin_requests WHERE id = $1", row["id"])
        if state == "connected":
            await conn.execute(
                "UPDATE linkedin_requests SET status = 'accepted', settled_at = now(), "
                "sent_at = COALESCE(sent_at, attempted_at) WHERE id = $1",
                row["id"],
            )
            await _relationship(conn, request, "connected", run_id)
            try:
                await records.add_event(
                    conn,
                    request["project_id"],
                    request["record_id"],
                    kind="accepted",
                    event_key=f"linkedin:accepted:{request['id']}",
                    occurred_at=datetime.now(UTC),
                    detail={"member": request["member"]},
                    run_id=run_id,
                )
            except records.RecordError as error:
                if error.code != "record_gone":
                    raise
            counts["accepted"] += 1
        elif state == "invited":
            if row["status"] == "uncertain":
                # It went out after all.
                await conn.execute(
                    "UPDATE linkedin_requests SET status = 'sent', sent_at = attempted_at "
                    "WHERE id = $1",
                    row["id"],
                )
                await _relationship(conn, request, "pending", run_id)
            counts["pending"] += 1
        elif state == "none":
            # Sent and no longer pending (declined or withdrawn), or uncertain and never sent.
            await conn.execute(
                "UPDATE linkedin_requests SET status = $2, settled_at = now() WHERE id = $1",
                row["id"],
                "not_pending" if row["status"] == "sent" else "not_sent",
            )
            await _relationship(conn, request, "none", run_id)
            counts["ended"] += 1
    await read_at(conn, project_id, member, now)
    return counts
