"""A member's first-degree LinkedIn connections, with their current company and position.

`social.linkedin_connections` reads the member's own connections list with their saved
LinkedIn session, newest first, and keeps every connection as a People record (matched on
their profile ID or public slug) with their name and headline, the member's `connected`
relationship and a `connected` event dated when LinkedIn says they connected.

LinkedIn's list has no company or position, and from December 2026 its data export has
neither. So each run also reads up to `POSITIONS_PER_RUN` profiles, one request each with
pauses: connections never read first, newest first, then those read longest ago, once their
read is `STALE_DAYS` old. A member with more connections than that gets their companies and
positions over a few daily runs; the summary says how many are still to come.

The list is rendered to project Files as `people/linkedin-connections.csv`, in the columns of
LinkedIn's own `Connections.csv` (without the email addresses LinkedIn no longer exports),
plus headline, location and when the profile was read.

Only counts pass through Temporal. Names, headlines and positions stay in Postgres and in the
rendered files; where the next read starts stays in its read's receipt.
"""

from __future__ import annotations

import csv
import io
import json
import math
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from tin_lite import linkedin_engagers as engagers
from tin_lite import people_records as records
from tin_lite.domain import LINKEDIN_CONNECTIONS_WORKFLOW_NAME
from tin_lite.linkedin_connections import PAGE_SIZE, PAGES_PER_READ, PROFILES_PER_READ
from tin_lite.people_records import Value

KEY = LINKEDIN_CONNECTIONS_WORKFLOW_NAME
WORKFLOW_ID = UUID("5d2c8e41-7b3a-4f96-9e1d-2a6b0c4f8e73")
# Reads of the list per run, each up to ten pages of 40: up to 8,000 connections, newest first.
LIST_READS = 20
MAX_CONNECTIONS = LIST_READS * PAGES_PER_READ * PAGE_SIZE
# Reads of profiles per run, 20 profiles each: a hundred profiles a day on a daily schedule.
POSITION_READS = 5
POSITIONS_PER_RUN = POSITION_READS * PROFILES_PER_READ
# A position read this long ago is read again, once everyone has been read once.
STALE_DAYS = 30
CSV_PATH = "people/linkedin-connections.csv"
SUMMARY_PATH = "people/linkedin-connections.md"
PATHS = {"linkedin-connections.csv": CSV_PATH, "linkedin-connections.md": SUMMARY_PATH}
LIMITS = {"linkedin-connections.csv": 8_000_000, "linkedin-connections.md": 64_000}
INPUT_SCHEMA: dict[str, Any] = engagers.INPUT_SCHEMA
NO_POSITION = "LinkedIn showed no current position"


def connected_key(member: str, record_id: UUID) -> str:
    return f"linkedin:connected:{member}:{record_id}"


async def record_connections(
    conn, *, project_id: UUID, run_id: UUID, member: str, connections: list[dict]
) -> dict[str, int]:
    """Write a read's connections as records, values, the member's relationship and one
    `connected` event each. It's the run's starter's own list, so they're all connected."""

    counts = {"people": 0, "new": 0, "forgotten": 0}
    for connection in connections:
        identifiers = {"linkedin_member": connection["member"]}
        if connection.get("slug"):
            identifiers["linkedin_slug"] = connection["slug"]
        record_id = await records.upsert_person(
            conn, project_id, identifiers, source=KEY, run_id=run_id
        )
        if record_id is None:
            counts["forgotten"] += 1
            continue
        name = " ".join(filter(None, (connection["first_name"], connection["last_name"])))
        values = [
            Value(
                "headline",
                connection["headline"] or None,
                "" if connection["headline"] else "LinkedIn showed none",
            )
        ]
        if name:
            values.append(Value("name", name, ""))
        await records.set_values(
            conn, project_id, record_id, values, set_by_kind="run", set_by=str(run_id)
        )
        await records.set_relationship(
            conn,
            project_id,
            record_id,
            member,
            "connected",
            set_by=str(run_id),
            reason="In your LinkedIn connections",
        )
        counts["people"] += 1
        target = await records.current(conn, project_id, record_id)
        counts["new"] += await records.add_event(
            conn,
            project_id,
            target,
            kind="connected",
            event_key=connected_key(member, target),
            occurred_at=datetime.fromisoformat(connection["connected_at"]),
            # LinkedIn's own columns, so the export can repeat them.
            detail={
                "first_name": connection["first_name"],
                "last_name": connection["last_name"],
            },
            run_id=run_id,
        )
    return counts


async def need_positions(conn, project_id: UUID, member: str, *, limit: int) -> list[str]:
    """Profile IDs of this member's connections whose company and position to read next:
    never read, newest connection first, then read longest ago once STALE_DAYS old. A value a
    member set themselves is theirs, so that person isn't read."""

    rows = await conn.fetch(
        """WITH mine AS (
               SELECT COALESCE(r.merged_into, r.id) AS person, max(e.occurred_at) AS connected
               FROM record_events e
               JOIN records r ON r.id = e.record_id
               JOIN workflow_runs w ON w.id = e.run_id
               WHERE e.project_id = $1 AND e.kind = 'connected'
                 AND w.started_by_clerk_user_id = $2
               GROUP BY 1)
           SELECT i.value AS member, v.set_at
           FROM mine
           JOIN record_identifiers i ON i.record_id = mine.person AND i.kind = 'linkedin_member'
           LEFT JOIN record_values v ON v.record_id = mine.person AND v.field = 'position'
           WHERE v.record_id IS NULL
              -- A position Tin only read from a headline is still read from the profile.
              OR v.set_by = $5
              OR (v.set_by_kind = 'run' AND v.set_at < now() - make_interval(days => $3))
           ORDER BY (CASE WHEN v.set_by = $5 THEN NULL ELSE v.set_at END) NULLS FIRST,
                    mine.connected DESC, i.value
           LIMIT $4""",
        project_id,
        member,
        STALE_DAYS,
        limit,
        records.COMPANY_WRITER,
    )
    return [row["member"] for row in rows]


async def record_positions(conn, *, project_id: UUID, run_id: UUID, people: list[dict]) -> int:
    """Write each read profile's company, position, location and headline; returns how many
    showed a current position. Every profile read gets a value, empty with a reason when
    LinkedIn showed nothing, so its read time is known."""

    found = 0
    for person in people:
        if not person["found"]:
            continue
        record_id = await records.upsert_person(
            conn, project_id, {"linkedin_member": person["member"]}, source=KEY, run_id=run_id
        )
        if record_id is None:
            continue
        has_position = bool(person["company"] or person["position"])
        found += has_position
        values = [
            Value("company", person["company"] or None, "" if person["company"] else NO_POSITION),
            Value(
                "position", person["position"] or None, "" if person["position"] else NO_POSITION
            ),
            Value("location", person["location"] or None, ""),
        ]
        if person["headline"]:
            values.append(Value("headline", person["headline"], ""))
        # A read that finds the same position is still the latest read of it.
        await records.set_values(
            conn,
            project_id,
            record_id,
            values,
            set_by_kind="run",
            set_by=str(run_id),
            refresh=True,
        )
    return found


# The rendered list.

COLUMNS = (
    "First Name",
    "Last Name",
    "URL",
    "Company",
    "Position",
    "Connected On",
    "Headline",
    "Location",
    "Profile read",
)


async def connections(conn, project_id: UUID, member: str) -> list[dict]:
    """The member's connections, newest first, with what Tin knows of each."""

    rows = await conn.fetch(
        """WITH mine AS (
               SELECT DISTINCT ON (COALESCE(r.merged_into, r.id))
                   COALESCE(r.merged_into, r.id) AS person, e.occurred_at AS connected,
                   e.detail
               FROM record_events e
               JOIN records r ON r.id = e.record_id
               JOIN workflow_runs w ON w.id = e.run_id
               WHERE e.project_id = $1 AND e.kind = 'connected'
                 AND w.started_by_clerk_user_id = $2
               ORDER BY COALESCE(r.merged_into, r.id), e.occurred_at DESC)
           SELECT mine.person, mine.connected, mine.detail,
               (SELECT jsonb_object_agg(i.kind, i.value) FROM record_identifiers i
                WHERE i.record_id = mine.person) AS identifiers,
               (SELECT jsonb_object_agg(v.field, jsonb_build_object(
                    'text', v.value_text, 'set_at', v.set_at, 'set_by', v.set_by))
                FROM record_values v WHERE v.record_id = mine.person
                  AND v.field IN ('name', 'headline', 'company', 'position', 'location'))
                   AS fields
           FROM mine
           JOIN records r ON r.id = mine.person AND r.merged_into IS NULL
           ORDER BY mine.connected DESC, mine.person""",
        project_id,
        member,
    )
    result = []
    for row in rows:
        identifiers = _json(row["identifiers"])
        fields = _json(row["fields"])
        detail = _json(row["detail"])
        text = {key: (value or {}).get("text") or "" for key, value in fields.items()}
        first, last = detail.get("first_name") or "", detail.get("last_name") or ""
        if not first and not last:
            first, _, last = (text.get("name") or "").partition(" ")
        position = fields.get("position") or {}
        # A position Tin only read from the headline isn't a profile read.
        read = position.get("set_at") if position.get("set_by") != records.COMPANY_WRITER else None
        result.append(
            {
                "first_name": first,
                "last_name": last,
                "url": records.profile_link(identifiers),
                "company": text.get("company", ""),
                "position": text.get("position", ""),
                "connected": row["connected"],
                "headline": text.get("headline", ""),
                "location": text.get("location", ""),
                "read": datetime.fromisoformat(read) if isinstance(read, str) else read,
            }
        )
    return result


def _json(value) -> dict:
    if value is None:
        return {}
    return json.loads(value) if isinstance(value, str) else dict(value)


def render(people: list[dict], *, notes: dict, now: datetime) -> dict[str, str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for person in people:
        writer.writerow(
            [
                engagers._cell(person["first_name"]),
                engagers._cell(person["last_name"]),
                person["url"],
                engagers._cell(person["company"]),
                engagers._cell(person["position"]),
                f"{person['connected']:%d %b %Y}",
                engagers._cell(person["headline"]),
                engagers._cell(person["location"]),
                f"{person['read']:%Y-%m-%d}" if person["read"] else "",
            ]
        )
    return {CSV_PATH: buffer.getvalue(), SUMMARY_PATH: summary_markdown(people, notes, now=now)}


def summary_markdown(people: list[dict], notes: dict, *, now: datetime) -> str:
    read = [p for p in people if p["read"]]
    placed = [p for p in read if p["company"] or p["position"]]
    waiting = len(people) - len(read)
    lines = [
        "# Your LinkedIn connections",
        "",
        f"Updated {now.astimezone(UTC):%-d %b %Y, %H:%M} UTC. "
        "Tin read your own LinkedIn connections, newest first, with your saved session. "
        f"The full list is in `{CSV_PATH}`, in the columns of LinkedIn's own Connections.csv, "
        "with each connection's current company and position.",
        "",
        *([f"**{notes['notice']}**", ""] if notes.get("notice") else []),
        f"- {sentence(notes)}",
        f"- {len(people)} connection{'s' if len(people) != 1 else ''} in the list.",
        f"- Company and position read for {len(read)}: {len(placed)} show a current position, "
        f"{len(read) - len(placed)} show none on their profile.",
    ]
    if waiting:
        days = math.ceil(waiting / POSITIONS_PER_RUN)
        lines.append(
            f"- {waiting} still to read. Tin reads {POSITIONS_PER_RUN} profiles a run, a few "
            f"seconds apart, so a daily run finishes in about {days} "
            f"day{'s' if days != 1 else ''}."
        )
    if notes.get("capped"):
        lines.append(
            f"- Tin read your newest {MAX_CONNECTIONS:,} connections; older ones aren't listed."
        )
    if notes.get("unnamed"):
        lines.append(
            f"- {notes['unnamed']} connection{'s' if notes['unnamed'] != 1 else ''} LinkedIn "
            "listed without a profile, most likely closed or hidden accounts."
        )
    lines += [
        "",
        "Email addresses aren't included: LinkedIn shows them only to connections a person "
        "allows, and Tin doesn't collect them.",
        "",
    ]
    return "\n".join(lines)


def sentence(notes: dict) -> str:
    """The run's one line: what it read."""

    people, new, positions = notes.get("people", 0), notes.get("new", 0), notes.get("positions", 0)
    text = f"{people} LinkedIn connection{'s' if people != 1 else ''} read"
    if new and new != people:
        text += f", {new} new since the last run"
    return text + f"; company and position read for {positions} profiles this run."
