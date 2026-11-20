"""HTTP for the image library (image_library.py): the Brand > Images page reads and discards
images with the member's session, and an agent fetches a file through a short-lived link that
get_image gave it. MCP shares the same service calls."""

from pathlib import PurePosixPath
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from tin_lite.auth import AuthContext, require_user
from tin_lite.image_library import DownloadLinks, ImageLibrary, ImageLibraryError

router = APIRouter()
USER = Depends(require_user)


class ImageDiscard(BaseModel):
    """One image to discard (within 48 hours) or retire (after)."""

    model_config = ConfigDict(extra="forbid")
    image_id: str = Field(min_length=3, max_length=120)


def _library(request: Request) -> ImageLibrary:
    runtime = request.app.state.runtime
    return ImageLibrary(
        database=runtime.database, storage=runtime.storage, settings=request.app.state.settings
    )


async def _authorized(request: Request, project_id: UUID, user: AuthContext) -> None:
    if not await request.app.state.runtime.database.has_project_access(
        project_id=project_id, clerk_user_id=user.clerk_user_id
    ):
        raise HTTPException(status_code=404, detail="project not found")


def _refused(error: ImageLibraryError) -> JSONResponse:
    return JSONResponse(status_code=error.status, content={"detail": str(error)})


@router.get("/api/projects/{project_id}/images")
async def list_images(
    project_id: UUID,
    request: Request,
    q: str = Query(default="", max_length=300),
    kind: str | None = Query(default=None, max_length=20),
    retired: bool = False,
    user: AuthContext = USER,
):
    """The library for the Brand > Images grid: the newest or best-matching images, with the
    revision their paths belong to."""
    await _authorized(request, project_id, user)
    try:
        return await _library(request).search(
            project_id, q, kind=kind or None, include_retired=retired, limit=50
        )
    except ImageLibraryError as error:
        return _refused(error)


@router.post("/api/projects/{project_id}/images/discard")
async def discard_image(
    project_id: UUID, payload: ImageDiscard, request: Request, user: AuthContext = USER
):
    await _authorized(request, project_id, user)
    try:
        return await _library(request).discard(
            project_id, user.clerk_user_id, payload.image_id, client_id="browser"
        )
    except ImageLibraryError as error:
        return _refused(error)


@router.get("/api/file-downloads/{token}")
async def download_file(token: str, request: Request):
    """One file through a link get_image gave an agent. The link is the credential: no
    session, one path at one revision, 15 minutes. The bytes always arrive as an attachment
    with the raw-file sandbox headers, so an SVG never runs as a page."""
    from tin_lite.api import _file_media_type, _project_content_headers

    runtime = request.app.state.runtime
    links = DownloadLinks(database=runtime.database, settings=request.app.state.settings)
    try:
        path, content = await links.open(token, runtime.storage)
    except ImageLibraryError as error:
        return _refused(error)
    filename = PurePosixPath(path).name
    media_type = _file_media_type(filename)
    return Response(
        content=content,
        media_type=media_type,
        headers=_project_content_headers(filename, media_type, download=True),
    )
