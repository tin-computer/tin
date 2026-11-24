"""What project members read and change about a project's people, for HTTP and MCP alike.

Members read people, correct a rating, mark a customer, write notes and forget a person.
Tin's own workflows write the rest (#435). Anyone outside the project gets "project not found".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from tin_lite import linkedin_requests as requests
from tin_lite import people_icp, warm_paths
from tin_lite import people_records as records

logger = logging.getLogger(__name__)
# A runaway guard well above a big LinkedIn network: a member's ten thousand imported
# connections list in full. Past it, the people with a score, a visit or a note come first.
LIMIT = 30000
GONE = "This person is no longer in this project."
# Fields a member's rating replaces, and what "Use Tin's score" gives back. Earlier members
# picked a profile too, so giving Tin's score back restores both.
FIT_FIELDS = ("icp", "icp_fit")
UNCHANGED = object()

SAYS = {
    "record_gone": (404, GONE),
    "note_gone": (404, "This note is no longer here."),
    "not_author": (403, "Only the note's author can change it."),
    "empty_note": (422, "Write something first."),
    "note_too_long": (422, f"A note can be up to {records.NOTE_LIMIT:,} characters."),
}


class PeopleError(Exception):
    """A request Tin refuses, with the status and the sentence a member reads."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _refused(error: records.RecordError) -> PeopleError:
    status, message = SAYS.get(error.code, (422, "Tin couldn't save that."))
    return PeopleError(status, message)


async def _requests(conn, project_id: UUID, viewer: str, people: list[dict], only=None) -> None:
    """Whether the viewer can ask each person to connect from People, and whether a request of
    theirs waits to go out."""

    open_ = await requests.pickable(conn, project_id, viewer, only=only)
    queued = await requests.waiting(conn, project_id, viewer)
    for person in people:
        person["requestable"] = person["id"] in open_
        person["request"] = "queued" if person["id"] in queued else None


def _first_name(profile: dict) -> str | None:
    """A teammate's first name, from their Tin account; none for an email or placeholder."""

    name = str(profile.get("display_name") or "").strip()
    if not name or "@" in name or name == "Project member":
        return None
    return name.split()[0][:40]


# Teammates' first names, kept briefly: Activity asks on every refresh.
NAME_SECONDS = 600
_names: dict[str, tuple[str | None, float]] = {}


async def first_names(profiles, members: set[str]) -> dict[str, str]:
    """Teammates' first names from their Tin accounts, kept for ten minutes. A member without
    one, or whose profile can't be read, is left out; callers say "a teammate"."""

    now = time.monotonic()
    found: dict[str, str] = {}

    async def name(member):
        cached = _names.get(member)
        if cached and cached[1] > now:
            value = cached[0]
        else:
            try:
                value = _first_name(await profiles.member_profile(member))
            except Exception as error:  # noqa: BLE001
                logger.info("Tin couldn't read a teammate's name: %s", type(error))
                return
            _names[member] = (value, now + NAME_SECONDS)
        if value:
            found[member] = value

    if profiles is not None and members:
        await asyncio.gather(*(name(member) for member in sorted(members)))
    return found


async def _second_degree(conn, project_id: UUID, record_id: UUID) -> dict | None:
    """The newest second-degree path an RB2B run of this project found for the person, from the
    run's warm-path receipt; only a path with mutual connections."""

    row = await conn.fetchrow(
        """SELECT er.result->'second' AS second FROM effect_receipts er
           WHERE er.status = 'completed' AND er.execution_key IN (
               SELECT e.run_id::text || ':rb2b:warm'
               FROM record_events e
               JOIN records r ON r.id = e.record_id
               JOIN workflow_runs w ON w.id = e.run_id AND w.project_id = e.project_id
               WHERE e.project_id = $1 AND (r.id = $2 OR r.merged_into = $2)
                 AND e.kind = 'visited')
             AND jsonb_typeof(er.result->'second') = 'object'
           ORDER BY er.updated_at DESC
           LIMIT 1""",
        project_id,
        record_id,
    )
    if row is None:
        return None
    second = row["second"]
    second = json.loads(second) if isinstance(second, str) else second
    if not isinstance(second, dict) or not second.get("member"):
        return None
    try:
        count = int(second.get("mutual_count") or 0)
    except (TypeError, ValueError):
        return None
    names = [n for n in second.get("mutual_names") or [] if isinstance(n, str) and n]
    return {"member": str(second["member"]), "mutual_count": count, "mutual_names": names[:3]}


class People:
    def __init__(self, database, storage, profiles=None):
        # Clerk's member profiles, for teammates' first names; without them, "a teammate".
        self.db, self.storage, self.profiles = database, storage, profiles

    async def _named(self, viewer: str, people: list[dict]) -> None:
        """Say who was behind each person's last touch, timeline events and notes: "you", a
        teammate's first name, or "a teammate". Member IDs never leave Tin."""

        items = [person["last_touch"] for person in people if person.get("last_touch")]
        items += [person["note"] for person in people if person.get("note")]
        items += [item for person in people for item in person.get("events", [])]
        items += [item for person in people for item in person.get("notes", [])]
        teammates = {item["member"] for item in items if item.get("member")} - {viewer}
        names: dict[str, str] = {}
        if self.profiles is not None and teammates:

            async def name(member):
                try:
                    found = _first_name(await self.profiles.member_profile(member))
                except Exception as error:  # noqa: BLE001
                    logger.info("People couldn't read a teammate's name: %s", type(error))
                    return
                if found:
                    names[member] = found

            await asyncio.gather(*(name(member) for member in sorted(teammates)))
        for item in items:
            member = item.pop("member", None)
            item["who"] = (
                None
                if not member
                else "you"
                if member == viewer
                else names.get(member, "a teammate")
            )

    async def _project(self, project_id: UUID, viewer: str):
        if not await self.db.has_project_access(project_id=project_id, clerk_user_id=viewer):
            raise PeopleError(404, "project not found")
        project = await self.db.get_project(project_id)
        if project is None:
            raise PeopleError(404, "project not found")
        return project

    async def _icp(self, project) -> dict:
        try:
            return await people_icp.read_icp(self.storage, project)
        except Exception as error:  # noqa: BLE001
            # Without the ICP file nothing is marked stale; the people and scores still show.
            logger.warning("People couldn't read the ICP of %s: %s", project.id, type(error))
            return {}

    async def list(self, project_id: UUID, viewer: str) -> dict[str, Any]:
        project = await self._project(project_id, viewer)
        icp = await self._icp(project)
        async with self.db.pool.acquire() as conn:
            people = await records.directory(
                conn,
                project.id,
                viewer=viewer,
                definition=icp.get("definition", ""),
                limit=LIMIT + 1,
            )
            await _requests(conn, project.id, viewer, people)
        await self._named(viewer, people[:LIMIT])
        return {
            "people": people[:LIMIT],
            "truncated": len(people) > LIMIT,
            "fit_shown": people_icp.FIT_SHOWN,
            "ratings": {str(n): label for n, label in people_icp.RATINGS.items()},
            # The ICP file's profiles, which Tin names for each person it rates.
            "profiles": icp.get("profiles", []),
            "icp_path": icp.get("path"),
        }

    async def get(self, project_id: UUID, person_id: UUID, viewer: str) -> dict[str, Any]:
        project = await self._project(project_id, viewer)
        return await self._person(project, person_id, viewer)

    async def _person(self, project, person_id: UUID, viewer: str, icp: dict | None = None):
        icp = icp if icp is not None else await self._icp(project)
        async with self.db.pool.acquire() as conn:
            found = await records.person(
                conn, project.id, person_id, viewer=viewer, definition=icp.get("definition", "")
            )
            if found is not None:
                await _requests(conn, project.id, viewer, [found], only=[found["id"]])
                if found["request"]:
                    found["queued"] = await requests.queued(
                        conn, project.id, viewer, found["id"], datetime.now(UTC)
                    )
            if found is not None:
                found["way_in"] = await self._way_in(conn, project.id, found, viewer)
        if found is None:
            raise PeopleError(404, GONE)
        await self._named(viewer, [found])
        return found

    async def _way_in(self, conn, project_id: UUID, person: dict, viewer: str) -> str | None:
        """The 🤝 line Slack posts for a visitor, read for the viewer from People alone: who on
        the team is connected to them or knows someone at their company, or a second-degree
        path an RB2B run of this project already found. Never a LinkedIn read."""

        found = await warm_paths.known_many(conn, project_id, [(person["id"], person["company"])])
        known = found[person["id"]]
        second = await _second_degree(conn, project_id, person["id"])
        return await self._line(known, second, person["company"], viewer)

    async def _line(self, known, second, company, viewer: str) -> str | None:
        members = set(known.connected) | {p["member"] for p in known.at_company}
        if second:
            members.add(second["member"])
        names = await first_names(self.profiles, members - {viewer})
        return warm_paths.line(
            known=known.as_dict(), second=second, names=names, company=company, you=viewer
        )

    async def ways_in(self, project_id: UUID, viewer: str, people: list[dict]) -> dict[str, Any]:
        """Each listed person's 🤝 line, for MCP's list: two reads for the whole list."""

        project = await self._project(project_id, viewer)
        async with self.db.pool.acquire() as conn:
            known = await warm_paths.known_many(
                conn, project.id, [(UUID(str(p["id"])), p.get("company")) for p in people]
            )
        ways = {}
        for person in people:
            found = known[UUID(str(person["id"]))]
            ways[str(person["id"])] = await self._line(found, None, person.get("company"), viewer)
        return ways

    async def update(
        self,
        project_id: UUID,
        person_id: UUID,
        viewer: str,
        *,
        fit: Any = UNCHANGED,
        customer: bool | None = None,
    ) -> dict[str, Any]:
        """Correct a person. `fit` is {"rating": 1 to 4}, or None to use Tin's score again;
        `customer` marks them a customer or not."""

        project = await self._project(project_id, viewer)
        icp = await self._icp(project)
        values: list[records.Value] = []
        if isinstance(fit, dict):
            rating = fit.get("rating")
            if type(rating) is not int or rating not in people_icp.RATINGS:
                raise PeopleError(422, "Pick a fit from 1 to 4.")
            values.append(records.Value("icp_fit", rating))
        elif fit is not None and fit is not UNCHANGED:
            raise PeopleError(422, "Pick a fit from 1 to 4, or Tin's score.")
        if customer is not None:
            values.append(records.Value("customer", "yes" if customer else "no"))
        try:
            async with self.db.pool.acquire() as conn:
                if values:
                    await records.set_by_member(conn, project.id, person_id, values, member=viewer)
                if customer:
                    # A customer isn't asked to connect, even if a request already waits.
                    await requests.drop_customer(conn, project.id, person_id)
                if fit is None:
                    await records.restore_run_values(conn, project.id, person_id, FIT_FIELDS)
        except records.RecordError as error:
            raise _refused(error) from None
        return await self._person(project, person_id, viewer, icp)

    async def add_note(self, project_id: UUID, person_id: UUID, viewer: str, body: Any):
        project = await self._project(project_id, viewer)
        try:
            async with self.db.pool.acquire() as conn:
                await records.add_note(conn, project.id, person_id, body, author=viewer)
        except records.RecordError as error:
            raise _refused(error) from None
        return await self._person(project, person_id, viewer)

    async def edit_note(
        self, project_id: UUID, person_id: UUID, note_id: UUID, viewer: str, body: Any
    ):
        project = await self._project(project_id, viewer)
        try:
            async with self.db.pool.acquire() as conn:
                await records.edit_note(conn, project.id, person_id, note_id, body, author=viewer)
        except records.RecordError as error:
            raise _refused(error) from None
        return await self._person(project, person_id, viewer)

    async def delete_note(self, project_id: UUID, person_id: UUID, note_id: UUID, viewer: str):
        project = await self._project(project_id, viewer)
        try:
            async with self.db.pool.acquire() as conn:
                await records.delete_note(conn, project.id, person_id, note_id, author=viewer)
        except records.RecordError as error:
            raise _refused(error) from None
        return await self._person(project, person_id, viewer)

    async def forget(self, project_id: UUID, person_id: UUID, viewer: str) -> None:
        project = await self._project(project_id, viewer)
        async with self.db.pool.acquire() as conn:
            if not await records.forget(conn, project.id, person_id):
                raise PeopleError(404, GONE)
