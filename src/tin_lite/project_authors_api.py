"""Thin HTTP adapter for the same author operations used by MCP."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from tin_lite.auth import AuthContext, require_user
from tin_lite.project_authors import ProjectAuthors

router = APIRouter()
USER = Depends(require_user)


class AuthorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=0)
    selected_guide: str | None = Field(default=None, max_length=512)
    link_to_me: bool | None = None


def service(request):
    runtime = request.app.state.runtime
    return ProjectAuthors(runtime.database, runtime.storage)


@router.get("/api/projects/{project_id}/authors")
async def list_authors(project_id: UUID, request: Request, user: AuthContext = USER):
    try:
        result = await service(request).list(project_id, user.clerk_user_id)
    except LookupError:
        raise HTTPException(404, "project not found") from None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.put("/api/projects/{project_id}/authors/{author_id}")
async def save_author(
    project_id: UUID,
    author_id: UUID,
    payload: AuthorInput,
    request: Request,
    user: AuthContext = USER,
):
    try:
        result = await service(request).save(
            project_id=project_id,
            author_id=author_id,
            actor=user.clerk_user_id,
            **payload.model_dump(),
        )
    except LookupError:
        raise HTTPException(404, "project not found") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
