"""What project members read and change about a project's people, for HTTP and MCP alike.

Members read people, correct an ICP pick, mark a customer, write notes and forget a person.
Tin's own workflows write the rest (#435). Anyone outside the project gets "project not found".
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from tin_lite import linkedin_engagers as engagers
from tin_lite import linkedin_requests as requests
from tin_lite import people_records as records

logger = logging.getLogger(__name__)
LIMIT = 5000
GONE = "This person is no longer in this project."
# Fields a member's fit replaces, and what "Use Tin's score" gives back.
FIT_FIELDS = ("icp", "icp_fit")
NOT_A_FIT = "none"
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


class People:
    def __init__(self, database, storage):
        self.db, self.storage = database, storage

    async def _project(self, project_id: UUID, viewer: str):
        if not await self.db.has_project_access(project_id=project_id, clerk_user_id=viewer):
            raise PeopleError(404, "project not found")
        project = await self.db.get_project(project_id)
        if project is None:
            raise PeopleError(404, "project not found")
        return project

    async def _icp(self, project) -> dict:
        try:
            return await engagers.read_icp(self.storage, project)
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
        return {
            "people": people[:LIMIT],
            "truncated": len(people) > LIMIT,
            "fit_shown": engagers.FIT_SHOWN,
            # The profiles a member can pick for someone, from the ICP file.
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
        if found is None:
            raise PeopleError(404, GONE)
        return found

    async def update(
        self,
        project_id: UUID,
        person_id: UUID,
        viewer: str,
        *,
        fit: Any = UNCHANGED,
        customer: bool | None = None,
    ) -> dict[str, Any]:
        """Correct a person. `fit` is {"icp": a profile from the ICP file, or None for not a
        fit}, or None to use Tin's score again; `customer` marks them a customer or not."""

        project = await self._project(project_id, viewer)
        icp = await self._icp(project)
        values: list[records.Value] = []
        if isinstance(fit, dict):
            picked = fit.get("icp")
            if picked is None or picked == NOT_A_FIT:
                values += [records.Value("icp", NOT_A_FIT), records.Value("icp_fit", 0.0)]
            elif not icp.get("profiles"):
                raise PeopleError(422, "This project's ICP file names no profiles yet.")
            elif picked not in icp["profiles"]:
                raise PeopleError(422, f"Pick one of the profiles in {icp['path']}.")
            else:
                values += [records.Value("icp", picked), records.Value("icp_fit", 1.0)]
        elif fit is not None and fit is not UNCHANGED:
            raise PeopleError(422, "Pick a profile, not a fit, or Tin's score.")
        if customer is not None:
            values.append(records.Value("customer", "yes" if customer else "no"))
        try:
            async with self.db.pool.acquire() as conn:
                if values:
                    await records.set_by_member(conn, project.id, person_id, values, member=viewer)
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
