"""People from a published visitor journal: the people who told the product who they are.

A `product.visitor_journeys` report names people only by an email they gave the product, by
signing up or in a form. After the report is published, each named person becomes (or joins)
a People record recognized by that email, with what they were on the site (`web_tier`), the
latest summary (`web_summary`) and, once per window end date, a `visited` event holding the
summary, where they stopped, the suggested action and a replay link. A new person's work found
on the public web (1.4.0) fills `headline`; a run never adds their LinkedIn profile as an
identifier, so a namesake found by search can't merge two records. Anonymous visitors never get
a record, and a person the founder asked Tin to forget is skipped.

Every write is idempotent (one record per email, a `visited` key per person and day), so a
retried publication adds nothing. A failure here is logged and never fails the run that
already published its report.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from uuid import UUID

from tin_lite import people_records as records
from tin_lite import visitor_journal

KEY = "product.visitor_journeys"

logger = logging.getLogger(__name__)


def event_key(project_id: UUID, email: str, day: str) -> str:
    digest = hashlib.sha256(f"{project_id}:{email}".encode()).hexdigest()[:24]
    return f"visited:{digest}:{day}"


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def write(conn, project_id: UUID, run_id: UUID, outcome: visitor_journal.Outcome) -> int:
    """Write the report's named people; returns how many records it touched."""
    if not outcome.people:
        return 0
    await records.ensure_fields(conn, project_id)
    day = outcome.window_end[:10]
    touched = 0
    for person in outcome.people:
        record_id = await records.upsert_person(
            conn, project_id, {"email": person.email}, source=KEY, run_id=run_id
        )
        if record_id is None:
            continue  # forgotten
        values = [
            records.Value("web_tier", person.tier, person.stopped_at),
            records.Value("web_summary", person.summary),
        ]
        # A name or headline another source already set (a LinkedIn profile, a member) stays.
        current = await records.current(conn, project_id, record_id)
        work = person.work
        for key, value in (("name", person.name), ("headline", work.headline if work else "")):
            if value and not await conn.fetchval(
                "SELECT value_text FROM record_values WHERE record_id = $1 AND field = $2",
                current,
                key,
            ):
                values.append(records.Value(key, value))
        await records.set_values(
            conn, project_id, record_id, values, set_by_kind="run", set_by=str(run_id)
        )
        await records.add_event(
            conn,
            project_id,
            record_id,
            kind="visited",
            event_key=event_key(project_id, person.email, day),
            occurred_at=_when(person.last_seen),
            detail={
                "tier": person.tier,
                "summary": person.summary,
                "stopped_at": person.stopped_at,
                "action": person.action,
                **({"replay_url": person.replay_url} if person.replay_url else {}),
            },
            run_id=run_id,
        )
        touched += 1
    return touched


async def record(*, database, run, content: bytes) -> None:
    """After publication: put the report's named people in People. Never fails the run."""
    try:
        outcome = visitor_journal.read(content, work=visitor_journal.work_allowed(run.input))
        async with database.pool.acquire() as conn:
            touched = await write(conn, run.project_id, run.id, outcome)
        if touched:
            await database.add_activity(
                run_id=run.id,
                event_type="visitor_people_recorded",
                details={"people": touched},
                dedupe_key=f"{run.id}:visitor_people",
            )
    except Exception as exc:  # noqa: BLE001 — the report is already published
        logger.warning("Visitor journeys run %s could not update People: %s", run.id, type(exc))
