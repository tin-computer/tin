"""People records: one lasting record per person a project knows (#435).

A record is recognized by its identifiers (a LinkedIn member ID or profile slug, or an email the
person gave the project's product), holds typed
field values that each say who set them, when and why, and keeps an append-only timeline.
Only Tin's own services write here. When one read supplies identifiers of two records, they
merge: the older record survives, the newer points at it, and events stay where they were
written. Forgetting a person deletes the record and keeps a hash of each identifier, so no
later read adds them back. Members correct values (a run never replaces them) and write notes
about a person; Tin reads the notes the next time it scores them.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID, uuid4

PERSON = "person"
IDENTIFIER_KINDS = ("linkedin_member", "linkedin_slug", "email")
EVENT_KINDS = ("reacted", "commented", "invited", "accepted", "withdrawn", "visited")
RELATIONSHIPS = ("connected", "pending", "none")
# What a person was on the project's site or product when a run last read them.
WEB_TIERS = ("lead", "new_user", "user")
# Built-in fields: key → (type, label, choices).
FIELDS: dict[str, tuple[str, str, tuple[str, ...] | None]] = {
    "name": ("text", "Name", None),
    "headline": ("text", "Headline", None),
    "linkedin_relationship": ("choice", "On LinkedIn", RELATIONSHIPS),
    "icp": ("text", "ICP", None),
    "icp_fit": ("number", "ICP fit", None),
    "customer": ("choice", "Customer", ("yes", "no")),
    "web_tier": ("choice", "On the site", WEB_TIERS),
    "web_summary": ("text", "Last visit", None),
}
SET_BY = ("run", "member")
TEXT_LIMIT = 2000
REASON_LIMIT = 500
NOTE_LIMIT = 2000
# LinkedIn's own profile ID, as in /in/ACoAA… and urn:li:fsd_profile:ACoAA….
MEMBER_ID = re.compile(r"ACo[A-Za-z0-9_-]{20,80}")
EMAIL = re.compile(r"[^@\s]{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,63}")


class RecordError(ValueError):
    """A write Tin refuses: an unknown field, a value of the wrong type, a bad identifier."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Value:
    field: str
    value: str | float | None
    reason: str = ""


def linkedin_identifiers(value: str) -> dict[str, str]:
    """The identifiers in a LinkedIn profile URL or profile URN.

    /in/ACo… and urn:li:fsd_profile:ACo… name the member; any other /in/<slug> is the profile's
    public slug, which LinkedIn matches without regard to case.
    """

    value = (value or "").strip()
    if value.startswith(("urn:li:fsd_profile:", "urn:li:fs_miniProfile:")):
        member = value.rsplit(":", 1)[1]
        if MEMBER_ID.fullmatch(member):
            return {"linkedin_member": member}
        raise RecordError("invalid_identifier")
    parsed = urlsplit(value)
    match = re.fullmatch(r"/in/([^/]+)/?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.linkedin.com", "linkedin.com"}
        or parsed.port is not None
        or parsed.username
        or not match
    ):
        raise RecordError("invalid_identifier")
    identifier = unquote(match[1])
    if MEMBER_ID.fullmatch(identifier):
        return {"linkedin_member": identifier}
    if (
        not identifier
        or len(identifier) > 300
        or any(c in identifier for c in "/\\?#")
        or any(ord(c) < 32 for c in identifier)
    ):
        raise RecordError("invalid_identifier")
    return {"linkedin_slug": identifier.casefold()}


def profile_link(identifiers: dict[str, str]) -> str:
    """The person's LinkedIn profile URL: the public slug when Tin knows it."""

    value = identifiers.get("linkedin_slug") or identifiers.get("linkedin_member")
    return "https://www.linkedin.com/in/" + quote(value, safe="-._~") if value else ""


def suppression_hash(project_id: UUID, kind: str, value: str) -> str:
    return hashlib.sha256(f"{project_id}:{kind}:{value}".encode()).hexdigest()


def _pairs(identifiers: dict[str, str]) -> list[tuple[str, str]]:
    pairs = []
    for kind, value in identifiers.items():
        if kind not in IDENTIFIER_KINDS or not isinstance(value, str):
            raise RecordError("invalid_identifier")
        value = value.strip()
        if kind in ("linkedin_slug", "email"):
            value = value.casefold()
        if not value or len(value) > 300 or (kind == "email" and not EMAIL.fullmatch(value)):
            raise RecordError("invalid_identifier")
        pairs.append((kind, value))
    if not pairs:
        raise RecordError("invalid_identifier")
    return pairs


async def _lock(conn, project_id: UUID) -> None:
    # Recognizing a person reads, then writes identifiers: one writer per project at a time.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"records:{project_id}"
    )


async def ensure_fields(conn, project_id: UUID) -> None:
    await conn.executemany(
        """INSERT INTO record_fields (project_id, key, type, label, options, built_in)
           VALUES ($1, $2, $3, $4, $5::jsonb, true)
           ON CONFLICT (project_id, key) DO NOTHING""",
        [
            (project_id, key, kind, label, json.dumps(list(choices)) if choices else None)
            for key, (kind, label, choices) in FIELDS.items()
        ],
    )


async def current(conn, project_id: UUID, record_id: UUID) -> UUID | None:
    """The record a possibly merged record now lives in, or None once it is gone."""

    row = await conn.fetchrow(
        "SELECT id, merged_into FROM records WHERE project_id = $1 AND id = $2",
        project_id,
        record_id,
    )
    if row is None:
        return None
    return row["merged_into"] or row["id"]


async def upsert_person(
    conn,
    project_id: UUID,
    identifiers: dict[str, str],
    *,
    source: str,
    run_id: UUID | None = None,
) -> UUID | None:
    """The person's record, created when no identifier matches; None for a forgotten person."""

    pairs = _pairs(identifiers)
    kinds, values = [k for k, _ in pairs], [v for _, v in pairs]
    async with conn.transaction():
        await _lock(conn, project_id)
        if await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM record_suppressions
               WHERE project_id = $1 AND identifier_hash = ANY($2::text[]))""",
            project_id,
            [suppression_hash(project_id, k, v) for k, v in pairs],
        ):
            return None
        rows = await conn.fetch(
            """SELECT DISTINCT r.id, r.created_at FROM record_identifiers i
               JOIN records r ON r.id = i.record_id
               WHERE i.project_id = $1
                 AND (i.kind, i.value) IN (SELECT * FROM unnest($2::text[], $3::text[]))
               ORDER BY r.created_at, r.id""",
            project_id,
            kinds,
            values,
        )
        if rows:
            record_id = rows[0]["id"]
            if len(rows) > 1:
                await _merge(conn, record_id, [row["id"] for row in rows[1:]])
            await conn.execute("UPDATE records SET updated_at = now() WHERE id = $1", record_id)
        else:
            record_id = uuid4()
            await conn.execute(
                """INSERT INTO records (id, project_id, type, source, source_run_id)
                   VALUES ($1, $2, $3, $4, $5)""",
                record_id,
                project_id,
                PERSON,
                source[:100],
                run_id,
            )
        await conn.execute(
            """INSERT INTO record_identifiers (project_id, kind, value, record_id)
               SELECT $1, kind, value, $4 FROM unnest($2::text[], $3::text[]) AS t(kind, value)
               ON CONFLICT (project_id, kind, value) DO NOTHING""",
            project_id,
            kinds,
            values,
            record_id,
        )
        return record_id


async def _merge(conn, survivor: UUID, others: list[UUID]) -> None:
    await conn.execute(
        """UPDATE records SET merged_into = $2, updated_at = now()
           WHERE id = ANY($1::uuid[]) OR merged_into = ANY($1::uuid[])""",
        others,
        survivor,
    )
    await conn.execute(
        "UPDATE record_identifiers SET record_id = $2 WHERE record_id = ANY($1::uuid[])",
        others,
        survivor,
    )
    # The survivor keeps its own values. A field only the others had moves over: a member's
    # value first, then the newest.
    await conn.execute(
        """WITH moving AS (
               SELECT DISTINCT ON (field) record_id, field FROM record_values
               WHERE record_id = ANY($1::uuid[])
                 AND field NOT IN (SELECT field FROM record_values WHERE record_id = $2)
               ORDER BY field, set_by_kind = 'member' DESC, set_at DESC)
           UPDATE record_values v SET record_id = $2 FROM moving m
           WHERE v.record_id = m.record_id AND v.field = m.field""",
        others,
        survivor,
    )
    await conn.execute("DELETE FROM record_values WHERE record_id = ANY($1::uuid[])", others)
    await conn.execute(
        "UPDATE record_notes SET record_id = $2 WHERE record_id = ANY($1::uuid[])", others, survivor
    )


def _typed(spec: tuple[str, tuple[str, ...] | None], value) -> tuple[str | None, float | None]:
    kind, choices = spec
    if value is None:
        return None, None
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise RecordError("invalid_value")
        if not math.isfinite(value):
            raise RecordError("invalid_value")
        return None, float(value)
    if not isinstance(value, str):
        raise RecordError("invalid_value")
    text = " ".join(value.split())[:TEXT_LIMIT]
    if kind == "choice" and text not in (choices or ()):
        raise RecordError("invalid_value")
    return text or None, None


async def set_values(
    conn,
    project_id: UUID,
    record_id: UUID,
    values: list[Value],
    *,
    set_by_kind: str,
    set_by: str,
    definition: str = "",
    refresh: bool = False,
) -> int:
    """Write current values with their lineage; returns how many changed.

    A value a member set is never replaced by a run, and a write that changes nothing (same
    value, reason and definition) leaves the earlier one, with its time, in place, unless
    `refresh` asks for a new time: a score made again after a note, even unchanged, is current.
    """

    if set_by_kind not in SET_BY or not set_by:
        raise RecordError("invalid_author")
    rows = await conn.fetch(
        "SELECT key, type, options FROM record_fields WHERE project_id = $1", project_id
    )
    fields = {
        row["key"]: (row["type"], tuple(json.loads(row["options"])) if row["options"] else None)
        for row in rows
    }
    target = await current(conn, project_id, record_id)
    if target is None:
        raise RecordError("record_gone")
    changed = 0
    for item in values:
        if item.field not in fields:
            raise RecordError("unknown_field")
        text, number = _typed(fields[item.field], item.value)
        written = await conn.fetchval(
            """INSERT INTO record_values (project_id, record_id, field, value_text, value_number,
                   reason, set_by_kind, set_by, definition)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (record_id, field) DO UPDATE SET
                   value_text = EXCLUDED.value_text, value_number = EXCLUDED.value_number,
                   reason = EXCLUDED.reason, set_by_kind = EXCLUDED.set_by_kind,
                   set_by = EXCLUDED.set_by, definition = EXCLUDED.definition, set_at = now(),
                   -- A member's value keeps the run's value it replaced; a run's keeps none.
                   previous = CASE
                       WHEN EXCLUDED.set_by_kind = 'run' THEN NULL
                       WHEN record_values.set_by_kind = 'run' THEN jsonb_build_object(
                           'text', record_values.value_text,
                           'number', record_values.value_number,
                           'reason', record_values.reason, 'set_by', record_values.set_by,
                           'definition', record_values.definition,
                           'set_at', record_values.set_at)
                       ELSE record_values.previous END
               WHERE NOT (record_values.set_by_kind = 'member' AND EXCLUDED.set_by_kind = 'run')
                 AND ($10 OR (record_values.value_text, record_values.value_number,
                      record_values.reason, record_values.definition)
                     IS DISTINCT FROM (EXCLUDED.value_text, EXCLUDED.value_number,
                      EXCLUDED.reason, EXCLUDED.definition))
               RETURNING true""",
            project_id,
            target,
            item.field,
            text,
            number,
            " ".join((item.reason or "").split())[:REASON_LIMIT],
            set_by_kind,
            set_by[:200],
            definition[:200],
            refresh,
        )
        changed += bool(written)
    return changed


async def add_event(
    conn,
    project_id: UUID,
    record_id: UUID,
    *,
    kind: str,
    event_key: str,
    occurred_at: datetime | None = None,
    detail: dict[str, Any] | None = None,
    run_id: UUID | None = None,
) -> bool:
    """Add one timeline entry; a key already recorded adds nothing and returns False."""

    if kind not in EVENT_KINDS:
        raise RecordError("unknown_event")
    if not event_key or len(event_key) > 300:
        raise RecordError("invalid_event_key")
    target = await current(conn, project_id, record_id)
    if target is None:
        raise RecordError("record_gone")
    inserted = await conn.fetchval(
        """INSERT INTO record_events (id, project_id, record_id, kind, event_key, occurred_at,
               detail, run_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
           ON CONFLICT (project_id, event_key) DO NOTHING
           RETURNING true""",
        uuid4(),
        project_id,
        target,
        kind,
        event_key,
        occurred_at,
        json.dumps(detail or {}),
        run_id,
    )
    return bool(inserted)


# What members change: a value, back to the run's value it replaced, and notes.


async def set_by_member(
    conn, project_id: UUID, record_id: UUID, values: list[Value], *, member: str
) -> int:
    """A member's values, which no run replaces; each keeps the run's value it replaced."""

    await ensure_fields(conn, project_id)
    return await set_values(
        conn, project_id, record_id, values, set_by_kind="member", set_by=member
    )


async def restore_run_values(
    conn, project_id: UUID, record_id: UUID, fields: tuple[str, ...]
) -> None:
    """Put back the run's values members replaced in these fields. A field a member set with
    no run value before it goes empty, so the next run fills it."""

    target = await current(conn, project_id, record_id)
    if target is None:
        raise RecordError("record_gone")
    async with conn.transaction():
        await conn.execute(
            """UPDATE record_values SET value_text = previous->>'text',
                   value_number = (previous->>'number')::double precision,
                   reason = COALESCE(previous->>'reason', ''), set_by_kind = 'run',
                   set_by = previous->>'set_by',
                   definition = COALESCE(previous->>'definition', ''),
                   set_at = (previous->>'set_at')::timestamptz, previous = NULL
               WHERE record_id = $1 AND field = ANY($2::text[]) AND set_by_kind = 'member'
                 AND previous IS NOT NULL""",
            target,
            list(fields),
        )
        await conn.execute(
            """DELETE FROM record_values WHERE record_id = $1 AND field = ANY($2::text[])
               AND set_by_kind = 'member'""",
            target,
            list(fields),
        )


def _note_body(body) -> str:
    if not isinstance(body, str):
        raise RecordError("empty_note")
    # Keep the writer's line breaks; trim the ends and runs of blank lines.
    text = re.sub(r"\n{3,}", "\n\n", body.replace("\r\n", "\n").strip())
    if not text:
        raise RecordError("empty_note")
    if len(text) > NOTE_LIMIT:
        raise RecordError("note_too_long")
    return text


async def add_note(conn, project_id: UUID, record_id: UUID, body, *, author: str) -> UUID:
    text = _note_body(body)
    target = await current(conn, project_id, record_id)
    if target is None:
        raise RecordError("record_gone")
    note_id = uuid4()
    await conn.execute(
        """INSERT INTO record_notes (id, project_id, record_id, body, author)
           VALUES ($1, $2, $3, $4, $5)""",
        note_id,
        project_id,
        target,
        text,
        author[:200],
    )
    return note_id


async def _own_note(conn, project_id: UUID, record_id: UUID, note_id: UUID, author: str):
    target = await current(conn, project_id, record_id)
    if target is None:
        raise RecordError("record_gone")
    found = await conn.fetchval(
        "SELECT author FROM record_notes WHERE id = $1 AND project_id = $2 AND record_id = $3",
        note_id,
        project_id,
        target,
    )
    if found is None:
        raise RecordError("note_gone")
    if found != author:
        raise RecordError("not_author")


async def edit_note(
    conn, project_id: UUID, record_id: UUID, note_id: UUID, body, *, author: str
) -> None:
    """Change a note; only its author can."""

    text = _note_body(body)
    await _own_note(conn, project_id, record_id, note_id, author)
    await conn.execute(
        "UPDATE record_notes SET body = $2, updated_at = now() WHERE id = $1 AND body <> $2",
        note_id,
        text,
    )


async def delete_note(
    conn, project_id: UUID, record_id: UUID, note_id: UUID, *, author: str
) -> None:
    """Delete a note; only its author can."""

    await _own_note(conn, project_id, record_id, note_id, author)
    await conn.execute("DELETE FROM record_notes WHERE id = $1", note_id)


async def forget(conn, project_id: UUID, record_id: UUID) -> bool:
    """Delete a person and everything merged into them, and keep their identifiers' hashes."""

    async with conn.transaction():
        await _lock(conn, project_id)
        target = await current(conn, project_id, record_id)
        if target is None:
            return False
        ids = [
            row["id"]
            for row in await conn.fetch(
                "SELECT id FROM records WHERE project_id = $1 AND (id = $2 OR merged_into = $2)",
                project_id,
                target,
            )
        ]
        identifiers = await conn.fetch(
            "SELECT kind, value FROM record_identifiers WHERE record_id = ANY($1::uuid[])", ids
        )
        await conn.executemany(
            """INSERT INTO record_suppressions (project_id, identifier_hash) VALUES ($1, $2)
               ON CONFLICT DO NOTHING""",
            [
                (project_id, suppression_hash(project_id, row["kind"], row["value"]))
                for row in identifiers
            ],
        )
        await conn.execute("DELETE FROM record_events WHERE record_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM record_notes WHERE record_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM record_values WHERE record_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM record_identifiers WHERE record_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM records WHERE id = ANY($1::uuid[])", ids)
        return True


async def people(
    conn, project_id: UUID, *, limit: int = 5000, only: UUID | None = None
) -> list[dict[str, Any]]:
    """Current people with identifiers, values and event counts, newest first; `only` reads
    one."""

    rows = await conn.fetch(
        """SELECT r.id, r.source, r.created_at, r.updated_at,
               (SELECT jsonb_object_agg(i.kind, i.value) FROM record_identifiers i
                WHERE i.record_id = r.id) AS identifiers,
               (SELECT jsonb_object_agg(v.field, jsonb_build_object(
                    'text', v.value_text, 'number', v.value_number, 'reason', v.reason,
                    'definition', v.definition, 'set_by_kind', v.set_by_kind,
                    'set_by', v.set_by, 'set_at', v.set_at, 'previous', v.previous))
                FROM record_values v WHERE v.record_id = r.id) AS fields,
               (SELECT jsonb_object_agg(kind, n) FROM (
                    SELECT e.kind, count(*) AS n FROM record_events e
                    WHERE e.record_id = r.id
                       OR e.record_id IN (SELECT m.id FROM records m WHERE m.merged_into = r.id)
                    GROUP BY e.kind) counted) AS events
           FROM records r
           WHERE r.project_id = $1 AND r.type = $2 AND r.merged_into IS NULL
             AND ($4::uuid IS NULL OR r.id = $4)
           ORDER BY r.created_at DESC, r.id
           LIMIT $3""",
        project_id,
        PERSON,
        limit,
        only,
    )
    result = []
    for row in rows:
        identifiers = json.loads(row["identifiers"]) if row["identifiers"] else {}
        fields = json.loads(row["fields"]) if row["fields"] else {}
        result.append(
            {
                "id": row["id"],
                "identifiers": identifiers,
                "profile_url": profile_link(identifiers),
                "fields": fields,
                "events": json.loads(row["events"]) if row["events"] else {},
                "source": row["source"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    return result


# What the People view reads: each person's fields with who set them, and their timeline.

# Short names for the workflows that write records, shown beside a value as who set it.
WRITERS = {
    "social.linkedin_engagers": "engagers",
    "social.linkedin_requests": "requests",
    "product.visitor_journeys": "visitors",
}
VIEW_FIELDS = ("icp", "icp_fit", "linkedin_relationship", "customer", "web_tier", "web_summary")
# Values computed under a definition, such as the ICP file, go stale when it changes.
DEFINED_FIELDS = ("icp", "icp_fit")
POST = re.compile(r"urn:li:(?:activity|ugcPost|share):[0-9]{1,30}")
REPLAY = re.compile(r"https://(?:us|eu)\.posthog\.com/project/\d{1,12}/replay/[0-9A-Za-z-]{8,80}")


def writer(key: str) -> str:
    return WRITERS.get(key) or key.rsplit(".", 1)[-1].replace("_", " ")


async def _run_writers(conn, run_ids: set[str]) -> dict[str, dict[str, str | None]]:
    """Each run's workflow name and who started it."""

    ids = []
    for value in run_ids:
        try:
            ids.append(UUID(value))
        except ValueError:
            continue
    rows = await conn.fetch(
        """SELECT r.id, COALESCE(w.key, r.executor) AS key, r.started_by_clerk_user_id
           FROM workflow_runs r LEFT JOIN workflows w ON w.id = r.workflow_id
           WHERE r.id = ANY($1::uuid[])""",
        ids,
    )
    return {
        str(row["id"]): {"by": writer(row["key"]), "started_by": row["started_by_clerk_user_id"]}
        for row in rows
    }


def _when(row) -> datetime:
    """When an event happened: its own time, else the post's date, else when Tin saw it."""

    if row["occurred_at"]:
        return row["occurred_at"]
    try:
        posted = datetime.fromisoformat(row["posted_at"])
    except (TypeError, ValueError):
        return row["recorded_at"]
    return posted if posted.tzinfo else posted.replace(tzinfo=UTC)


def _member(set_by: str, viewer: str) -> str:
    return "you" if set_by == viewer else "a teammate"


def _fields(
    person: dict, runs: dict, *, viewer: str, definition: str, noted: datetime | None = None
) -> dict[str, dict]:
    """The view's fields. A run's score is stale under another definition, or when a note
    came after it (`noted`): the next run reads the note and scores again."""

    shown = {}
    for key in VIEW_FIELDS:
        value = person["fields"].get(key)
        if not value:
            continue
        member = value["set_by_kind"] == "member"
        by = _member(value["set_by"], viewer) if member else None
        if not member:
            by = (runs.get(value["set_by"]) or {}).get("by") or "Tin"
        stale = ""
        if key in DEFINED_FIELDS and not member:
            if definition and value["definition"] and value["definition"] != definition:
                stale = "definition"
            elif noted and noted > datetime.fromisoformat(value["set_at"]):
                stale = "note"
        previous = value.get("previous") if member else None
        shown[key] = {
            "value": value["number"] if key == "icp_fit" else value["text"],
            "reason": value["reason"],
            "by": by,
            "at": value["set_at"],
            "stale": bool(stale),
            "stale_reason": stale or None,
            "definition": value["definition"].split("@", 1)[0],
            # The run's value a member's value replaced: what "Use Tin's score" brings back.
            "previous": (
                {
                    "value": previous["number"] if key == "icp_fit" else previous["text"],
                    "reason": previous["reason"] or "",
                }
                if previous
                else None
            ),
        }
    return shown


async def _notes(conn, project_id: UUID, only: UUID | None) -> dict[UUID, list]:
    """Each person's notes, newest first."""

    found: dict[UUID, list] = {}
    for row in await conn.fetch(
        """SELECT n.id, COALESCE(r.merged_into, r.id) AS person, n.body, n.author,
               n.created_at, n.updated_at
           FROM record_notes n JOIN records r ON r.id = n.record_id
           WHERE n.project_id = $1 AND ($2::uuid IS NULL OR COALESCE(r.merged_into, r.id) = $2)
           ORDER BY n.created_at DESC, n.id""",
        project_id,
        only,
    ):
        found.setdefault(row["person"], []).append(row)
    return found


async def directory(
    conn,
    project_id: UUID,
    *,
    viewer: str,
    definition: str = "",
    limit: int = 5000,
    only: UUID | None = None,
) -> list[dict[str, Any]]:
    """Every current person as the People view lists them, newest first.

    `definition` is the current ICP definition; a score computed under another one is stale.
    """

    listed = await people(conn, project_id, limit=limit, only=only)
    touches: dict[UUID, dict] = {}
    invited: dict[UUID, datetime] = {}
    for row in await conn.fetch(
        """SELECT COALESCE(r.merged_into, r.id) AS person, e.kind, e.occurred_at,
               e.recorded_at, e.detail->>'posted_at' AS posted_at
           FROM record_events e JOIN records r ON r.id = e.record_id
           WHERE e.project_id = $1 AND ($2::uuid IS NULL OR COALESCE(r.merged_into, r.id) = $2)""",
        project_id,
        only,
    ):
        at = _when(row)
        if row["person"] not in touches or at > touches[row["person"]]["at"]:
            touches[row["person"]] = {"kind": row["kind"], "at": at}
        # When a connection request last went out, for "Invited oct 16".
        if row["kind"] == "invited" and at > invited.get(row["person"], at - timedelta(1)):
            invited[row["person"]] = at
    runs = await _run_writers(
        conn,
        {
            value["set_by"]
            for person in listed
            for value in person["fields"].values()
            if value["set_by_kind"] == "run"
        },
    )
    notes = await _notes(conn, project_id, only)
    result = []
    for person in listed:
        written = notes.get(person["id"], [])
        latest = written[0] if written else None
        result.append(
            {
                "id": person["id"],
                "name": (person["fields"].get("name") or {}).get("text") or "",
                "headline": (person["fields"].get("headline") or {}).get("text") or "",
                "profile_url": person["profile_url"],
                "email": person["identifiers"].get("email", ""),
                "fields": _fields(
                    person,
                    runs,
                    viewer=viewer,
                    definition=definition,
                    noted=max((n["updated_at"] for n in written), default=None),
                ),
                "last_touch": touches.get(person["id"]),
                "invited_at": invited.get(person["id"]),
                "added": {"at": person["created_at"], "by": writer(person["source"])},
                # The newest note for the grid, and every note's words for search.
                "note": (
                    {
                        "body": latest["body"],
                        "by": _member(latest["author"], viewer),
                        "at": latest["created_at"],
                        "count": len(written),
                    }
                    if latest
                    else None
                ),
                "notes_text": " \n".join(n["body"] for n in written)[: NOTE_LIMIT * 2],
            }
        )
    return result


async def person(
    conn, project_id: UUID, record_id: UUID, *, viewer: str, definition: str = ""
) -> dict[str, Any] | None:
    """One person with their timeline, newest first; None once they're gone."""

    target = await current(conn, project_id, record_id)
    if target is None:
        return None
    found = await directory(
        conn, project_id, viewer=viewer, definition=definition, limit=1, only=target
    )
    if not found:
        return None
    rows = await conn.fetch(
        """SELECT e.kind, e.occurred_at, e.recorded_at, e.detail, e.run_id,
               e.detail->>'posted_at' AS posted_at
           FROM record_events e JOIN records r ON r.id = e.record_id
           WHERE e.project_id = $1 AND (r.id = $2 OR r.merged_into = $2)""",
        project_id,
        target,
    )
    runs = await _run_writers(conn, {str(row["run_id"]) for row in rows if row["run_id"]})
    events = []
    for row in rows:
        detail = json.loads(row["detail"]) if isinstance(row["detail"], str) else row["detail"]
        post = detail.get("post") if isinstance(detail.get("post"), str) else ""
        reaction = detail.get("reaction")
        started_by = (runs.get(str(row["run_id"])) or {}).get("started_by")
        visit = {}
        if row["kind"] == "visited":
            # What a visitor journeys run read about their day on the site.
            replay = detail.get("replay_url")
            visit = {
                key: detail[key] if isinstance(detail.get(key), str) else ""
                for key in ("summary", "stopped_at", "action", "tier")
            }
            visit["replay_url"] = (
                replay if isinstance(replay, str) and REPLAY.fullmatch(replay) else ""
            )
        events.append(
            {
                **visit,
                "kind": row["kind"],
                "at": _when(row),
                "reaction": reaction if isinstance(reaction, str) else None,
                "post_url": (
                    f"https://www.linkedin.com/feed/update/{post}/" if POST.fullmatch(post) else ""
                ),
                # Whose post: a run reads the LinkedIn of the member who started it.
                "yours": started_by == viewer if started_by else None,
            }
        )
    events.sort(key=lambda event: event["at"], reverse=True)
    notes = [
        {
            "id": row["id"],
            "body": row["body"],
            "by": _member(row["author"], viewer),
            "mine": row["author"] == viewer,
            "at": row["created_at"],
            "edited": row["updated_at"] > row["created_at"],
        }
        for row in (await _notes(conn, project_id, target)).get(target, [])
    ]
    return {**found[0], "events": events, "notes": notes}
