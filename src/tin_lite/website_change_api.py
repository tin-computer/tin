"""HTTP reads and decisions for website change rows, and the project's protected pages;
MCP shares the same service calls."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from tin_lite import website_change
from tin_lite.auth import AuthContext, require_user

router = APIRouter(prefix="/api/projects/{project_id}/website-changes")
settings_router = APIRouter(prefix="/api/projects/{project_id}/protected-paths")
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


class ProtectedPaths(BaseModel):
    """The project's protected pages, saved over the revision the caller read."""

    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: int = Field(ge=0)
    paths: list[str] = Field(max_length=website_change.MAX_PROTECTED_PATHS * 2)


@settings_router.get("")
async def read_protected_paths(project_id: UUID, request: Request, user: AuthContext = USER):
    """The pages whose changes always wait for the founder's merge, and who changed them."""
    try:
        return await website_change.read_protected_paths(
            request.app.state.runtime.database, project_id=project_id, actor=user.clerk_user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc


@settings_router.put("")
async def save_protected_paths(
    project_id: UUID, payload: ProtectedPaths, request: Request, user: AuthContext = USER
):
    """Replace the project's protected pages. /sign-in, /sign-up and /auth-complete stay."""
    try:
        return await website_change.save_protected_paths(
            request.app.state.runtime.database,
            project_id=project_id,
            actor=user.clerk_user_id,
            paths=payload.paths,
            expected_revision=payload.expected_revision,
            request_id=payload.request_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
    except website_change.WebsiteChangeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
