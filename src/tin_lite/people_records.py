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
import html
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
EVENT_KINDS = (
    "reacted",
    "commented",
    "viewed",
    "invited",
    "accepted",
    "withdrawn",
    "visited",
    "connected",
    "starred",
    "new_connection",
)
# What a person did that shows interest in a member: reacting to or commenting on their posts,
# viewing their profile. Tin proposes connection requests to people with any of these.
SIGNALS = ("reacted", "commented", "viewed")
# What a person did on a social network the project is on: the LinkedIn signals, connecting with
# a member after Tin first read their connections, and starring one of the project's GitHub
# repositories. People's Social segment, and worth a rating.
SOCIAL = (*SIGNALS, "new_connection", "starred")
# A repository as `starred` events name it.
REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
RELATIONSHIPS = ("connected", "pending", "none")
# What a person was on the project's site or product when a run last read them.
WEB_TIERS = ("lead", "new_user", "user")
# How much interest a person shows now, as the ICP rating judged it from what they did.
INTENTS = ("hot", "warm", "cold")
# Intent cools with time since the person last did something (a visit, reaction, comment,
# profile view or star): hot reads as warm after HOT_DAYS, anything as cold after WARM_DAYS.
# The rated value stays as the model set it; a new signal gets them rated again.
HOT_DAYS = 7
WARM_DAYS = 30
ACTIVE = (*SOCIAL, "visited")
# Who added people through Tin's MCP (`add_people`): a list a member brought, such as people to
# ask to connect. Their values are written as this writer, never as a member's own.
ADD_SOURCE = "people.added"
IMPORT_SOURCE = "people.import"
IMPORT_WRITER = "people.import"
# Tags: a member's short labels on people, such as the outreach group they were added in.
TAG_LENGTH = 60
TAGS_PER_PERSON = 20
# Sources that bring in a member's LinkedIn connections in bulk: the connections export workflow
# and a CSV import (`add_people` with `connections`). Someone known only from one, who hasn't
# done anything Tin saw (ACTIVE), is a connection, not a lead: People lists them under
# Connections, and Tin buys nothing for them.
BULK_SOURCES = ("social.linkedin_connections", "people.import")


def cooled_intent(intent: str | None, active_at: datetime | None, now: datetime) -> str | None:
    """The intent as it reads now: the rated one cooled by days since their last activity."""

    if intent not in INTENTS or active_at is None:
        return intent
    age = now - active_at
    if age > timedelta(days=WARM_DAYS):
        return "cold"
    if age > timedelta(days=HOT_DAYS) and intent == "hot":
        return "warm"
    return intent


# How their last visit arrived: an ad click, a search engine, an AI assistant, a social site,
# another site, or no referrer. The value's reason names it ("Google Ads, keyword …").
WEB_SOURCES = ("ad", "organic", "ai", "social", "referral", "direct")
# Built-in fields: key → (type, label, choices).
FIELDS: dict[str, tuple[str, str, tuple[str, ...] | None]] = {
    "name": ("text", "Name", None),
    "headline": ("text", "Headline", None),
    "company": ("text", "Company", None),
    "position": ("text", "Position", None),
    "location": ("text", "Location", None),
    "linkedin_relationship": ("choice", "On LinkedIn", RELATIONSHIPS),
    "icp": ("text", "ICP", None),
    "icp_fit": ("number", "ICP fit", None),
    "intent": ("choice", "Intent", INTENTS),
    "customer": ("choice", "Customer", ("yes", "no")),
    "web_tier": ("choice", "On the site", WEB_TIERS),
    "web_summary": ("text", "Last visit", None),
    "web_source": ("choice", "Came from", WEB_SOURCES),
    # Numbers on file for a good or strong lead (lead_phones), comma-separated.
    "phone": ("text", "Phone", None),
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
    # Likewise each member's relationship: the survivor's own, else the newest.
    await conn.execute(
        """WITH moving AS (
               SELECT DISTINCT ON (member) record_id, member FROM record_relationships
               WHERE record_id = ANY($1::uuid[])
                 AND member NOT IN (SELECT member FROM record_relationships WHERE record_id = $2)
               ORDER BY member, set_at DESC)
           UPDATE record_relationships x SET record_id = $2 FROM moving m
           WHERE x.record_id = m.record_id AND x.member = m.member""",
        others,
        survivor,
    )
    await conn.execute("DELETE FROM record_relationships WHERE record_id = ANY($1::uuid[])", others)
    await conn.execute(
        "UPDATE record_notes SET record_id = $2 WHERE record_id = ANY($1::uuid[])", others, survivor
    )
    # Tags join the survivor's.
    await conn.execute(
        """UPDATE record_tags t SET record_id = $2 WHERE t.record_id = ANY($1::uuid[])
             AND NOT EXISTS (SELECT 1 FROM record_tags s WHERE s.record_id = $2
                             AND s.tag = t.tag)""",
        others,
        survivor,
    )
    await conn.execute("DELETE FROM record_tags WHERE record_id = ANY($1::uuid[])", others)


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


def _whole(number):
    """A rating reads as 4, not 4.0; a number that isn't whole stays as it is."""

    return int(number) if isinstance(number, float) and number.is_integer() else number


# Company and position Tin reads from what it already has, when no source gave one: a
# headline's "Title at Company" (or "@ Company"), else a work email's domain. Any run's write
# of someone without a company fills it (`fill_company`); a source that names the company
# later replaces it.
COMPANY_WRITER = "people.company"
_TITLE_AT = re.compile(r"^(?P<title>.{2,120}?)\s+(?:at\s+|@\s*)(?P<company>.{1,120})$", re.I)
_HEADLINE_PARTS = re.compile(r"\s*[|·•]\s*")
# Where a company's name ends inside a headline: "Acme - building X", "Acme, ex-Google".
_COMPANY_END = re.compile(r"\s+[-–—]\s+|,\s+|\s+\(|\s+/\s+")
NOT_COMPANIES = (
    "retired", "self employed", "self-employed", "freelanc", "unemployed", "seeking",
    "looking for", "open to work", "between roles", "various", "none",
)  # fmt: skip
FREE_MAIL = frozenset(
    """gmail.com googlemail.com yahoo.com yahoo.co.uk ymail.com hotmail.com hotmail.co.uk
    outlook.com live.com msn.com icloud.com me.com mac.com aol.com proton.me protonmail.com
    pm.me gmx.com gmx.de gmx.net web.de mail.com yandex.com yandex.ru mail.ru qq.com 163.com
    126.com zoho.com fastmail.com hey.com tutanota.com comcast.net att.net verizon.net
    sbcglobal.net cox.net bellsouth.net charter.net naver.com hanmail.net seznam.cz
    libero.it orange.fr free.fr laposte.net t-online.de btinternet.com sky.com""".split()
)
# Values whose text arrives HTML-escaped from some sources ("Co-Owner &amp; Operator").
UNESCAPED = frozenset({"name", "headline", "company", "position", "location"})


def company_from_headline(headline: str | None) -> tuple[str, str] | None:
    """(position, company) from a headline like "VP Sales at Acme | ex-Google", or None when
    it names no company: a lowercase phrase ("building at the intersection of…"), a long
    sentence, or a placeholder ("Retired", "Self-employed")."""

    first = _HEADLINE_PARTS.split(html.unescape(headline or "").strip())[0]
    found = _TITLE_AT.match(first)
    if not found:
        return None
    title = found["title"].strip(" ,;:-")
    company = _COMPANY_END.split(found["company"])[0].strip(" ,;:.")
    if not title or not company or not (company[0].isupper() or company[0].isdigit()):
        return None
    if len(title.split()) > 8 or len(company.split()) > 5:
        return None
    if any(word in company.lower() for word in NOT_COMPANIES):
        return None
    return title, company


def company_from_email(email: str | None) -> str | None:
    """A work email's domain; None for a personal address."""

    domain = (email or "").rsplit("@", 1)[-1].strip().lower() if "@" in (email or "") else ""
    if not domain or domain in FREE_MAIL or "." not in domain:
        return None
    return domain


async def fill_company(conn, project_id: UUID, record_id: UUID, *, set_by: str) -> bool:
    """Fill a person's company (and position) from their headline or work email when no
    source gave one; True when it did. One Tin read before follows a changed headline."""

    row = await conn.fetchrow(
        """SELECT
               (SELECT value_text FROM record_values WHERE record_id = $1 AND field = 'company')
                   AS company,
               (SELECT set_by FROM record_values WHERE record_id = $1 AND field = 'company')
                   AS company_by,
               (SELECT value_text FROM record_values WHERE record_id = $1 AND field = 'position')
                   AS position,
               (SELECT set_by FROM record_values WHERE record_id = $1 AND field = 'position')
                   AS position_by,
               (SELECT value_text FROM record_values WHERE record_id = $1 AND field = 'headline')
                   AS headline,
               (SELECT value FROM record_identifiers WHERE record_id = $1 AND kind = 'email'
                ORDER BY added_at LIMIT 1) AS email""",
        record_id,
    )
    if row is None or (row["company"] and row["company_by"] != COMPANY_WRITER):
        return False
    values = []
    found = company_from_headline(row["headline"])
    if found:
        title, company = found
        values.append(Value("company", company, "From their headline"))
        if not row["position"] or row["position_by"] == COMPANY_WRITER:
            values.append(Value("position", title, "From their headline"))
    elif domain := company_from_email(row["email"]):
        values.append(Value("company", domain, "From their email address"))
    elif row["company_by"] == COMPANY_WRITER:
        # What Tin read before no longer holds (a new headline, a stricter reading): cleared.
        values.append(Value("company", None, ""))
        if row["position_by"] == COMPANY_WRITER:
            values.append(Value("position", None, ""))
    if not values:
        return False
    await set_values(conn, project_id, record_id, values, set_by_kind="run", set_by=set_by)
    return True


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
    A run's write of someone with no company fills it from what Tin has (`fill_company`).
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
        value = item.value
        if item.field in UNESCAPED and isinstance(value, str):
            value = html.unescape(value)
        text, number = _typed(fields[item.field], value)
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
    if (
        changed
        and set_by_kind == "run"
        and not any(item.field == "company" for item in values)
        and "company" in fields
    ):
        await fill_company(conn, project_id, target, set_by=COMPANY_WRITER)
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


# Each member's LinkedIn relationship with a person.


async def set_relationship(
    conn,
    project_id: UUID,
    record_id: UUID,
    member: str,
    state: str,
    *,
    set_by: str,
    reason: str = "",
    keep_pending: bool = False,
) -> bool:
    """Record what a run read, or what a request made, of a member's relationship with a person;
    returns whether it changed. A reaction's degree can't show an invitation, so a read with
    `keep_pending` leaves a pending request as it is."""

    if state not in RELATIONSHIPS:
        raise RecordError("invalid_value")
    if not member or not set_by:
        raise RecordError("invalid_author")
    target = await current(conn, project_id, record_id)
    if target is None:
        raise RecordError("record_gone")
    return bool(
        await conn.fetchval(
            """INSERT INTO record_relationships (project_id, record_id, member, state, reason,
                   set_by)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (record_id, member) DO UPDATE SET
                   state = EXCLUDED.state, reason = EXCLUDED.reason, set_by = EXCLUDED.set_by,
                   set_at = now()
               WHERE (record_relationships.state, record_relationships.reason)
                     IS DISTINCT FROM (EXCLUDED.state, EXCLUDED.reason)
                 AND NOT ($7 AND record_relationships.state = 'pending'
                          AND EXCLUDED.state = 'none')
               RETURNING true""",
            project_id,
            target,
            member[:200],
            state,
            " ".join((reason or "").split())[:REASON_LIMIT],
            set_by[:200],
            keep_pending,
        )
    )


async def relationships(
    conn, project_id: UUID, member: str, *, only: UUID | None = None
) -> dict[UUID, dict[str, Any]]:
    """The member's relationship with each person Tin has read or asked for them."""

    rows = await conn.fetch(
        """SELECT record_id, state, reason, set_by, set_at FROM record_relationships
           WHERE project_id = $1 AND member = $2 AND ($3::uuid IS NULL OR record_id = $3)""",
        project_id,
        member,
        only,
    )
    return {row["record_id"]: dict(row) for row in rows}


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
        await conn.execute("DELETE FROM record_tags WHERE record_id = ANY($1::uuid[])", ids)
        await conn.execute(
            "DELETE FROM record_relationships WHERE record_id = ANY($1::uuid[])", ids
        )
        await conn.execute("DELETE FROM record_values WHERE record_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM record_identifiers WHERE record_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM records WHERE id = ANY($1::uuid[])", ids)
        return True


# Values that make someone more than a name in a member's network: a score, a customer mark or
# a visit. People with one, a note or an event besides `connected` come first, so a long list
# of imported connections never pushes them past a limit.
ENGAGED_FIELDS = ("icp", "icp_fit", "intent", "customer", "web_tier", "web_source")


async def people(
    conn, project_id: UUID, *, limit: int = 5000, only: UUID | None = None
) -> list[dict[str, Any]]:
    """Current people with identifiers, values and event counts: people with a score, a visit,
    a note or an event besides `connected` first, then the rest, newest first in each; `only`
    reads one. Each part is read once for the whole list, so ten thousand people stay quick."""

    rows = await conn.fetch(
        """WITH chosen AS (
               SELECT r.id, r.source, r.created_at, r.updated_at,
                   (EXISTS (SELECT 1 FROM record_events e
                            WHERE e.record_id = r.id AND e.kind <> 'connected')
                    OR EXISTS (SELECT 1 FROM record_values v
                               WHERE v.record_id = r.id AND v.field = ANY($5::text[]))
                    OR EXISTS (SELECT 1 FROM record_notes n WHERE n.record_id = r.id)
                    OR EXISTS (SELECT 1 FROM record_tags t WHERE t.record_id = r.id)) AS engaged
               FROM records r
               WHERE r.project_id = $1 AND r.type = $2 AND r.merged_into IS NULL
                 AND ($4::uuid IS NULL OR r.id = $4)
               ORDER BY engaged DESC, r.created_at DESC, r.id
               LIMIT $3
           ),
           -- The chosen records and the records merged into them, whose events count too.
           written AS (
               SELECT id, id AS person FROM chosen
               UNION ALL
               SELECT m.id, m.merged_into FROM records m
               WHERE m.project_id = $1 AND m.merged_into IN (SELECT id FROM chosen)
           )
           SELECT c.id, c.source, c.created_at, c.updated_at, i.identifiers, v.fields, k.events,
               (SELECT array_agg(t.tag ORDER BY t.tag) FROM record_tags t
                WHERE t.record_id = c.id) AS tags
           FROM chosen c
           LEFT JOIN (
               SELECT record_id, jsonb_object_agg(kind, value) AS identifiers
               FROM record_identifiers WHERE record_id IN (SELECT id FROM chosen)
               GROUP BY record_id
           ) i ON i.record_id = c.id
           LEFT JOIN (
               SELECT record_id, jsonb_object_agg(field, jsonb_build_object(
                    'text', value_text, 'number', value_number, 'reason', reason,
                    'definition', definition, 'set_by_kind', set_by_kind,
                    'set_by', set_by, 'set_at', set_at, 'previous', previous)) AS fields
               FROM record_values WHERE record_id IN (SELECT id FROM chosen)
               GROUP BY record_id
           ) v ON v.record_id = c.id
           LEFT JOIN (
               SELECT person, jsonb_object_agg(kind, n) AS events FROM (
                   SELECT w.person, e.kind, count(*) AS n
                   FROM record_events e JOIN written w ON w.id = e.record_id
                   GROUP BY w.person, e.kind) counted
               GROUP BY person
           ) k ON k.person = c.id
           ORDER BY c.engaged DESC, c.created_at DESC, c.id""",
        project_id,
        PERSON,
        limit,
        only,
        list(ENGAGED_FIELDS),
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
                "tags": list(row["tags"] or []),
                "source": row["source"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    return result


# What the People view reads: each person's fields with who set them, and their timeline.

# The `visited` event of someone found at a company whose visit RB2B matched only to the company.
COMPANY_VISIT = "rb2b_company"

# Short names for the workflows that write records, shown beside a value as who set it.
WRITERS = {
    COMPANY_WRITER: "Tin",
    IMPORT_WRITER: "import",
    ADD_SOURCE: "added",
    "github_star": "GitHub stars",
    "social.linkedin_engagers": "engagers",
    "social.linkedin_profile_viewers": "profile viewers",
    "social.linkedin_connections": "connections",
    "social.linkedin_requests": "requests",
    "product.visitor_journeys": "visitors",
}
# LinkedIn shows each member their own relationship, from `record_relationships`.
VIEW_FIELDS = ("icp", "icp_fit", "intent", "customer", "web_tier", "web_summary", "web_source")


def rubric(definition: str | None) -> str:
    """What a rating's definition says about how it was rated: the ICP file's path and the
    scorer's version ("strategy/ICP.md:v3"), without the digest of the file's words. ICP files
    are edited all the time with small changes; a rating goes stale only when the scorer
    changes or the ICP moves to another file, never on an edit."""

    return re.sub(r"@[^:]*", "", definition or "")


# Values computed under a definition, such as the ICP file, go stale when it changes.
DEFINED_FIELDS = ("icp", "icp_fit", "intent")
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


def _event_member(member: str | None, run_id, runs: dict) -> str | None:
    """The member behind an event: the one whose request it was, else the one who started the
    run that saw it, whose LinkedIn that run read (so whose post was reacted to)."""

    if member:
        return member
    return (runs.get(str(run_id)) or {}).get("started_by") if run_id else None


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
            by = (
                (runs.get(value["set_by"]) or {}).get("by") or WRITERS.get(value["set_by"]) or "Tin"
            )
        stale = ""
        if key in DEFINED_FIELDS and not member:
            if (
                definition
                and value["definition"]
                and rubric(value["definition"]) != rubric(definition)
            ):
                stale = "definition"
            elif noted and noted > datetime.fromisoformat(value["set_at"]):
                stale = "note"
        previous = value.get("previous") if member else None
        shown[key] = {
            "value": _whole(value["number"]) if key == "icp_fit" else value["text"],
            "reason": value["reason"],
            "by": by,
            "at": value["set_at"],
            "stale": bool(stale),
            "stale_reason": stale or None,
            "definition": value["definition"].split("@", 1)[0],
            # The run's value a member's value replaced: what "Use Tin's score" brings back.
            "previous": (
                {
                    "value": _whole(previous["number"]) if key == "icp_fit" else previous["text"],
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
    # Who visited, and whose only visits were their company's (RB2B named the company, and Tin
    # found them there): the Visitors segment, and the panel's mark.
    visits: dict[UUID, set[bool]] = {}
    # Who did something on a social network: the Social segment.
    social: set[UUID] = set()
    # When each person last did something, for how warm their intent still is.
    active: dict[UUID, datetime] = {}
    for row in await conn.fetch(
        """SELECT COALESCE(r.merged_into, r.id) AS person, e.kind, e.occurred_at,
               e.recorded_at, e.detail->>'posted_at' AS posted_at, e.detail->>'member' AS member,
               e.detail->>'source' AS source, e.run_id
           FROM record_events e JOIN records r ON r.id = e.record_id
           WHERE e.project_id = $1 AND ($2::uuid IS NULL OR COALESCE(r.merged_into, r.id) = $2)""",
        project_id,
        only,
    ):
        at = _when(row)
        # How warm intent still is counts from when they did it, as the rater sees it: a
        # reaction without its own time dates from when Tin saw it, never the post's date.
        did = row["occurred_at"] or row["recorded_at"]
        if row["kind"] in ACTIVE and did > active.get(row["person"], did - timedelta(1)):
            active[row["person"]] = did
        company_visit = row["kind"] == "visited" and row["source"] == COMPANY_VISIT
        if row["kind"] == "visited":
            visits.setdefault(row["person"], set()).add(company_visit)
        if row["kind"] in SOCIAL:
            social.add(row["person"])
        if row["person"] not in touches or at > touches[row["person"]]["at"]:
            touches[row["person"]] = {"kind": row["kind"], "at": at, "row": row}
            if company_visit:
                touches[row["person"]]["company_visit"] = True
        # When the viewer's connection request last went out, for "Invited oct 16".
        if (
            row["kind"] == "invited"
            and row["member"] == viewer
            and at > invited.get(row["person"], at - timedelta(1))
        ):
            invited[row["person"]] = at
    mine = await relationships(conn, project_id, viewer, only=only)
    runs = await _run_writers(
        conn,
        {
            value["set_by"]
            for person in listed
            for value in person["fields"].values()
            if value["set_by_kind"] == "run"
        }
        | {row["set_by"] for row in mine.values()}
        | {str(touch["row"]["run_id"]) for touch in touches.values() if touch["row"]["run_id"]},
    )
    for touch in touches.values():
        row = touch.pop("row")
        touch["member"] = _event_member(row["member"], row["run_id"], runs)
    notes = await _notes(conn, project_id, only)
    now = datetime.now(UTC)
    result = []
    for person in listed:
        written = notes.get(person["id"], [])
        latest = written[0] if written else None
        fields = _fields(
            person,
            runs,
            viewer=viewer,
            definition=definition,
            noted=max((n["updated_at"] for n in written), default=None),
        )
        intent = fields.get("intent")
        if intent and intent.get("value"):
            at = active.get(person["id"])
            now_reads = cooled_intent(intent["value"], at, now)
            if now_reads != intent["value"]:
                # Shown as it reads now; the panel says what Tin rated and when they went quiet.
                fields["intent"] = {**intent, "value": now_reads, "rated": intent["value"],
                                    "active_at": at}  # fmt: skip
        relationship = mine.get(person["id"])
        if relationship:
            fields["linkedin_relationship"] = {
                "value": relationship["state"],
                "reason": relationship["reason"],
                "by": (runs.get(relationship["set_by"]) or {}).get("by") or "Tin",
                "at": relationship["set_at"],
                "stale": False,
                "stale_reason": None,
                "definition": "",
                "previous": None,
            }
        visited = visits.get(person["id"], set())
        # Only a bulk import knows them, and they've done nothing Tin saw: the Connections tab.
        imported = person["source"] in BULK_SOURCES and person["id"] not in active
        result.append(
            {
                "id": person["id"],
                "name": (person["fields"].get("name") or {}).get("text") or "",
                "headline": (person["fields"].get("headline") or {}).get("text") or "",
                "company": (person["fields"].get("company") or {}).get("text") or "",
                "profile_url": person["profile_url"],
                "email": person["identifiers"].get("email", ""),
                # A visit Tin read, from RB2B or a visitor journeys run.
                "visited": bool(visited),
                # Found for their company's visit, never matched to a visit of their own.
                "company_visit": visited == {True},
                # Reacted, commented, viewed a profile, newly connected or starred a repository.
                "social": person["id"] in social,
                "fields": fields,
                "last_touch": touches.get(person["id"]),
                "invited_at": invited.get(person["id"]),
                "added": {"at": person["created_at"], "by": writer(person["source"])},
                "imported": imported,
                # A member's labels on them, such as the outreach group they were added in.
                "tags": person.get("tags", []),
                # The newest note for the grid, and every note's words for search.
                "note": (
                    {
                        "body": latest["body"],
                        "by": _member(latest["author"], viewer),
                        "member": latest["author"],
                        "at": latest["created_at"],
                        "count": len(written),
                    }
                    if latest
                    else None
                ),
                "notes_text": " \n".join(n["body"] for n in written)[: NOTE_LIMIT * 2],
            }
        )
        if only is not None:
            # One person, for the panel: where they work and live, beside the headline.
            for key in ("position", "location"):
                result[-1][key] = (person["fields"].get(key) or {}).get("text") or ""
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
        member = _event_member(detail.get("member"), row["run_id"], runs)
        events.append(
            {
                **visit,
                "kind": row["kind"],
                "at": _when(row),
                "reaction": reaction if isinstance(reaction, str) else None,
                # The repository a `starred` event names.
                "repository": (
                    repository
                    if row["kind"] == "starred"
                    and isinstance(repository := detail.get("repository"), str)
                    and REPOSITORY.fullmatch(repository)
                    else ""
                ),
                "post_url": (
                    f"https://www.linkedin.com/feed/update/{post}/" if POST.fullmatch(post) else ""
                ),
                # Whose post: a run reads the LinkedIn of the member who started it.
                "yours": started_by == viewer if started_by else None,
                # Named for the viewer by the service; never sent as a member ID.
                "member": member if isinstance(member, str) else None,
            }
        )
    events.sort(key=lambda event: event["at"], reverse=True)
    notes = [
        {
            "id": row["id"],
            "body": row["body"],
            "by": _member(row["author"], viewer),
            "member": row["author"],
            "mine": row["author"] == viewer,
            "at": row["created_at"],
            "edited": row["updated_at"] > row["created_at"],
        }
        for row in (await _notes(conn, project_id, target)).get(target, [])
    ]
    return {**found[0], "events": events, "notes": notes}


# Adding people and tagging them, as a member does through Tin's MCP.


def clean_tag(value) -> str:
    """A tag as kept: its words single-spaced, at most TAG_LENGTH characters."""

    if not isinstance(value, str):
        raise RecordError("invalid_tag")
    tag = " ".join(value.split())
    if not 0 < len(tag) <= TAG_LENGTH or any(ord(c) < 32 for c in tag):
        raise RecordError("invalid_tag")
    return tag


def linkedin_link(value: str) -> dict[str, str]:
    """A profile link as people paste it: without https://, over http, on a country subdomain
    (tr.linkedin.com), with a query or a trailing slash. A profile ID or URN also works."""

    text = (value or "").strip()
    if text.startswith("urn:li:") or MEMBER_ID.fullmatch(text):
        return linkedin_identifiers(
            text if text.startswith("urn:") else f"urn:li:fsd_profile:{text}"
        )
    if "://" not in text:
        text = "https://" + text
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not (
        host == "linkedin.com" or host.endswith(".linkedin.com")
    ):
        raise RecordError("invalid_identifier")
    return linkedin_identifiers(f"https://www.linkedin.com{parsed.path}")


ADDED_FIELDS = ("name", "headline", "company", "position", "location")


async def add_person(
    conn,
    project_id: UUID,
    member: str,
    person: dict[str, Any],
    *,
    connection: bool = False,
    tags: list[str] = (),
) -> dict[str, Any]:
    """Keep one person a member brought, by their LinkedIn profile link, their email or both,
    with what the member knows of them. A field Tin already has stays as it is: a list is
    older than what Tin read. `connection` says they're the member's own LinkedIn connection
    (People lists them under Connections until they do something). Returns their ID, whether
    they're new, or that they were forgotten."""

    tags = list(dict.fromkeys(clean_tag(tag) for tag in tags))
    if len(tags) > TAGS_PER_PERSON:
        raise RecordError("too_many_tags")
    identifiers: dict[str, str] = {}
    link = person.get("linkedin_url") or person.get("linkedin")
    if link:
        identifiers.update(linkedin_link(str(link)))
    email = str(person.get("email") or "").strip().lower()
    if email:
        if not EMAIL.fullmatch(email):
            raise RecordError("invalid_identifier")
        identifiers["email"] = email
    if not identifiers:
        raise RecordError("invalid_identifier")
    pairs = _pairs(identifiers)
    known = await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM record_identifiers WHERE project_id = $1
               AND (kind, value) IN (SELECT * FROM unnest($2::text[], $3::text[])))""",
        project_id,
        [k for k, _ in pairs],
        [v for _, v in pairs],
    )
    record_id = await upsert_person(
        conn, project_id, identifiers, source=IMPORT_SOURCE if connection else ADD_SOURCE
    )
    if record_id is None:
        return {"id": None, "new": False, "forgotten": True}
    target = await current(conn, project_id, record_id)
    have = {
        row["field"]
        for row in await conn.fetch(
            "SELECT field FROM record_values WHERE record_id = $1 AND field = ANY($2::text[])",
            target,
            list(ADDED_FIELDS),
        )
    }
    values = []
    for field in ADDED_FIELDS:
        text = person.get(field)
        if field not in have and isinstance(text, str) and text.strip():
            values.append(Value(field, text, "From the list a member added"))
    if values:
        await set_values(conn, project_id, target, values, set_by_kind="run", set_by=IMPORT_WRITER)
    if connection:
        await set_relationship(
            conn,
            project_id,
            target,
            member,
            "connected",
            set_by=IMPORT_WRITER,
            reason="In your LinkedIn connections, from your list",
        )
    if tags:
        await add_tags(conn, project_id, [target], tags, member=member)
    return {"id": target, "new": not known, "forgotten": False}


async def add_tags(
    conn, project_id: UUID, record_ids: list[UUID], tags: list[str], *, member: str
) -> int:
    """Tag each current person; returns how many tags were new. A person keeps at most
    TAGS_PER_PERSON."""

    cleaned = list(dict.fromkeys(clean_tag(tag) for tag in tags))
    if len(cleaned) > TAGS_PER_PERSON:
        raise RecordError("too_many_tags")
    if not cleaned:
        return 0
    added = 0
    for record_id in record_ids:
        target = await current(conn, project_id, record_id)
        if target is None:
            continue
        # One statement a person: the new tags, as many as still fit under TAGS_PER_PERSON.
        added += await conn.fetchval(
            """WITH have AS (SELECT tag FROM record_tags WHERE record_id = $2),
                    fresh AS (SELECT t, n FROM unnest($3::text[]) WITH ORDINALITY AS u(t, n)
                              WHERE t NOT IN (SELECT tag FROM have)),
                    put AS (
                        INSERT INTO record_tags (project_id, record_id, tag, added_by)
                        SELECT $1, $2, f.t, $4 FROM fresh f
                        WHERE (SELECT count(*) FROM fresh g WHERE g.n <= f.n)
                              <= $5 - (SELECT count(*) FROM have)
                        ON CONFLICT (record_id, tag) DO NOTHING
                        RETURNING 1)
               SELECT count(*) FROM put""",
            project_id,
            target,
            cleaned,
            member,
            TAGS_PER_PERSON,
        )
    return added


async def remove_tags(conn, project_id: UUID, record_ids: list[UUID], tags: list[str]) -> int:
    """Take tags off people; returns how many came off."""

    cleaned = list(dict.fromkeys(clean_tag(tag) for tag in tags))
    if len(cleaned) > TAGS_PER_PERSON:
        raise RecordError("too_many_tags")
    targets = [t for t in [await current(conn, project_id, r) for r in record_ids] if t]
    result = await conn.execute(
        """DELETE FROM record_tags WHERE project_id = $1 AND record_id = ANY($2::uuid[])
             AND tag = ANY($3::text[])""",
        project_id,
        targets,
        cleaned,
    )
    return int(result.split()[-1])
