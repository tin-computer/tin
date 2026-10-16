"""HTTP routes for connection requests (#450): the member's sending, their waiting batch, and
the people they choose in People.

They share `linkedin_requests_service.ConnectionRequests` with MCP. Only project members reach
them, and each member sees and decides only what their own LinkedIn would send.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from tin_lite.auth import AuthContext, require_user
from tin_lite.linkedin_requests_service import ConnectionRequests, RequestsRefused

router = APIRouter(prefix="/api/projects/{project_id}/linkedin-requests")
USER = Depends(require_user)


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_cap: int | None = Field(default=None, ge=1, le=25)
    weekly_cap: int | None = Field(default=None, ge=1, le=100)
    start_minute: int | None = Field(default=None, ge=0, le=1439)
    end_minute: int | None = Field(default=None, ge=1, le=1440)


class SendingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    consent_version: int | None = None
    limits: Limits | None = None


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    people: list[UUID] = Field(default_factory=list, max_length=500)


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    people: list[UUID] = Field(min_length=1, max_length=100)


def _service(request: Request) -> ConnectionRequests:
    return ConnectionRequests(request.app.state.runtime, request.app.state.settings)


async def _answer(call):
    try:
        return await call
    except RequestsRefused as error:
        raise HTTPException(error.status, error.message) from None


@router.get("")
async def read_requests(project_id: UUID, request: Request, user: AuthContext = USER):
    return await _answer(_service(request).state(project_id, user.clerk_user_id))


@router.put("/sending")
async def change_sending(
    project_id: UUID, change: SendingChange, request: Request, user: AuthContext = USER
):
    limits = change.limits.model_dump(exclude_none=True) if change.limits else None
    return await _answer(
        _service(request).set_sending(
            project_id,
            user.clerk_user_id,
            enabled_=change.enabled,
            consent_version=change.consent_version,
            limits=limits,
        )
    )


@router.post("/sending/resume")
async def resume_sending(project_id: UUID, request: Request, user: AuthContext = USER):
    return await _answer(_service(request).resume(project_id, user.clerk_user_id))


@router.post("/batches/{batch_id}/approve")
async def approve(
    project_id: UUID, batch_id: UUID, decision: Decision, request: Request, user: AuthContext = USER
):
    return await _answer(
        _service(request).decide(
            project_id,
            batch_id,
            user.clerk_user_id,
            request_id=decision.request_id,
            approve=True,
            people=decision.people,
        )
    )


@router.post("/batches/{batch_id}/discard")
async def discard(
    project_id: UUID, batch_id: UUID, decision: Decision, request: Request, user: AuthContext = USER
):
    return await _answer(
        _service(request).decide(
            project_id,
            batch_id,
            user.clerk_user_id,
            request_id=decision.request_id,
            approve=False,
        )
    )


@router.post("/send")
async def send(project_id: UUID, choice: Choice, request: Request, user: AuthContext = USER):
    return await _answer(
        _service(request).send(
            project_id, user.clerk_user_id, request_id=choice.request_id, people=choice.people
        )
    )
