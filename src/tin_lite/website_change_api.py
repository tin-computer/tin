"""HTTP reads and decisions for website change rows; MCP shares the same service calls."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from tin_lite import website_change
from tin_lite.auth import AuthContext, require_user

router = APIRouter(prefix="/api/projects/{project_id}/website-changes")
USER = Depends(require_user)


class ChangeDecision(BaseModel):
    """The founder's decision on one change row, bound to the content they read."""

    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


async def _authorized(request, project_id, user):
    database = request.app.state.runtime.database
    if not await database.has_project_access(
        project_id=project_id, clerk_user_id=user.clerk_user_id
    ):
        raise HTTPException(status_code=404, detail="project not found")
    return database


@router.get("")
async def list_changes(
    project_id: UUID,
    request: Request,
    status: Literal["pending", "approved", "declined"] | None = None,
    user: AuthContext = USER,
):
    database = await _authorized(request, project_id, user)
    return await website_change.list_changes(database, project_id=project_id, status=status)


@router.get("/{change_id}")
async def get_change(project_id: UUID, change_id: str, request: Request, user: AuthContext = USER):
    database = await _authorized(request, project_id, user)
    change = await website_change.get_change(database, project_id=project_id, change_id=change_id)
    if change is None:
        raise HTTPException(status_code=404, detail="change not found")
    return change


async def _decide(project_id, change_id, action, payload, request, user):
    database = await _authorized(request, project_id, user)
    try:
        return await website_change.decide(
            database,
            project_id=project_id,
            change_id=change_id,
            action=action,
            actor=user.clerk_user_id,
            request_id=payload.request_id,
            content_sha256=payload.content_sha256,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="change not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{change_id}/approve")
async def approve_change(
    project_id: UUID,
    change_id: str,
    payload: ChangeDecision,
    request: Request,
    user: AuthContext = USER,
):
    """Approve one change row. website.change then publishes it, unless it is protected."""
    return await _decide(project_id, change_id, "approve", payload, request, user)


@router.post("/{change_id}/decline")
async def decline_change(
    project_id: UUID,
    change_id: str,
    payload: ChangeDecision,
    request: Request,
    user: AuthContext = USER,
):
    """Decline one change row. Tin never makes it, and later runs do not propose it again."""
    return await _decide(project_id, change_id, "decline", payload, request, user)
