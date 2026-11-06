"""HTTP for films a project's coding agent makes (films.py): the versions waiting in Decisions,
the founder's decisions on them, the agent's uploads, and the film kit itself. MCP shares the
same service calls."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from tin_lite import film_kit
from tin_lite.auth import AuthContext, require_user
from tin_lite.films import MAX_DECISION_NOTE, MAX_UPLOAD_BYTES, FilmError, Films, check_slug

router = APIRouter()
USER = Depends(require_user)


class FilmDecision(BaseModel):
    """The founder's decision on one waiting version of a film."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["pick", "others", "approve", "changes", "discard"]
    direction: int | None = Field(default=None, ge=1, le=8)
    note: str = Field(default="", max_length=MAX_DECISION_NOTE)


def _films(request: Request) -> Films:
    runtime = request.app.state.runtime
    return Films(
        database=runtime.database, storage=runtime.storage, settings=request.app.state.settings
    )


async def _authorized(request: Request, project_id: UUID, user: AuthContext) -> None:
    if not await request.app.state.runtime.database.has_project_access(
        project_id=project_id, clerk_user_id=user.clerk_user_id
    ):
        raise HTTPException(status_code=404, detail="project not found")


def _refused(error: FilmError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={"detail": str(error), "problems": list(error.problems)},
    )


@router.get("/api/projects/{project_id}/films")
async def waiting_films(project_id: UUID, request: Request, user: AuthContext = USER):
    """The versions waiting for the founder, for Decisions."""
    await _authorized(request, project_id, user)
    return {"films": await _films(request).open(project_id)}


@router.get("/api/projects/{project_id}/films/{slug}")
async def film_versions(project_id: UUID, slug: str, request: Request, user: AuthContext = USER):
    await _authorized(request, project_id, user)
    try:
        versions = await _films(request).versions(project_id, check_slug(slug))
    except FilmError as error:
        return _refused(error)
    if not versions:
        raise HTTPException(status_code=404, detail="film not found")
    return {"slug": slug, "versions": versions}


@router.post("/api/projects/{project_id}/films/{slug}/versions/{version}/decision")
async def decide_film(
    project_id: UUID,
    slug: str,
    version: int,
    payload: FilmDecision,
    request: Request,
    user: AuthContext = USER,
):
    await _authorized(request, project_id, user)
    try:
        return await _films(request).decide(
            project_id,
            slug,
            user.clerk_user_id,
            version=version,
            decision=payload.decision,
            direction=payload.direction,
            note=payload.note,
        )
    except FilmError as error:
        return _refused(error)


@router.put("/api/film-uploads/{token}")
async def upload_film(token: str, request: Request):
    """A version from the agent, through the link get_film gave it: a tar.gz of the note and its
    folder. The answer says what Tin kept, or exactly what to fix."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_UPLOAD_BYTES:
            return _refused(
                FilmError(f"Upload at most {MAX_UPLOAD_BYTES // 1_000_000} MB.", status=413)
            )
    try:
        return await _films(request).receive(token, bytes(body))
    except FilmError as error:
        return _refused(error)


@router.get("/kit/tin-film.tgz", include_in_schema=False)
async def current_kit():
    """This release's kit, by its lasting name."""
    try:
        return RedirectResponse(f"/kit/{film_kit.file_name()}", status_code=302)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.get("/kit/{name}", include_in_schema=False)
async def kit(name: str):
    try:
        if name != film_kit.file_name():
            raise HTTPException(status_code=404, detail="Fetch the kit get_film names.")
        content, digest = film_kit.package()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return Response(
        content=content,
        media_type="application/gzip",
        headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{digest}"'},
    )
