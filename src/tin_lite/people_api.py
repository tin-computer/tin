"""The People view's HTTP routes (#435): read people, correct them, write notes, forget them.

Project members read and change a project's people through `people_service`, the same service
MCP uses; Tin's own workflows write the rest. A score computed under an older ICP file, or
before a newer note, comes back marked stale.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from tin_lite.auth import AuthContext, require_user
from tin_lite.people_service import UNCHANGED, People, PeopleError

router = APIRouter(prefix="/api/projects/{project_id}/people")
USER = Depends(require_user)


class Fit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A profile from the ICP file, or null for not a fit.
    icp: str | None = Field(default=None, max_length=200)


class PersonChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A member's fit, or null to use Tin's score again.
    fit: Fit | None = None
    customer: bool | None = None


class NoteText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(max_length=4000)


def _people(request: Request) -> People:
    runtime = request.app.state.runtime
    return People(runtime.database, runtime.storage, getattr(request.app.state, "auth", None))


async def _answer(call):
    try:
        return await call
    except PeopleError as error:
        raise HTTPException(error.status, error.message) from None


@router.get("")
async def list_people(project_id: UUID, request: Request, user: AuthContext = USER):
    return await _answer(_people(request).list(project_id, user.clerk_user_id))


@router.get("/{person_id}")
async def get_person(project_id: UUID, person_id: UUID, request: Request, user: AuthContext = USER):
    return await _answer(_people(request).get(project_id, person_id, user.clerk_user_id))


@router.patch("/{person_id}")
async def change_person(
    project_id: UUID,
    person_id: UUID,
    change: PersonChange,
    request: Request,
    user: AuthContext = USER,
):
    fit = UNCHANGED
    if "fit" in change.model_fields_set:
        fit = None if change.fit is None else change.fit.model_dump()
    return await _answer(
        _people(request).update(
            project_id, person_id, user.clerk_user_id, fit=fit, customer=change.customer
        )
    )


@router.post("/{person_id}/notes", status_code=201)
async def add_note(
    project_id: UUID, person_id: UUID, note: NoteText, request: Request, user: AuthContext = USER
):
    return await _answer(
        _people(request).add_note(project_id, person_id, user.clerk_user_id, note.body)
    )


@router.patch("/{person_id}/notes/{note_id}")
async def edit_note(
    project_id: UUID,
    person_id: UUID,
    note_id: UUID,
    note: NoteText,
    request: Request,
    user: AuthContext = USER,
):
    return await _answer(
        _people(request).edit_note(project_id, person_id, note_id, user.clerk_user_id, note.body)
    )


@router.delete("/{person_id}/notes/{note_id}")
async def delete_note(
    project_id: UUID, person_id: UUID, note_id: UUID, request: Request, user: AuthContext = USER
):
    return await _answer(
        _people(request).delete_note(project_id, person_id, note_id, user.clerk_user_id)
    )


@router.delete("/{person_id}", status_code=204)
async def forget_person(
    project_id: UUID, person_id: UUID, request: Request, user: AuthContext = USER
):
    await _answer(_people(request).forget(project_id, person_id, user.clerk_user_id))
    return Response(status_code=204)
