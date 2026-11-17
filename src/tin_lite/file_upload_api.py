"""HTTP for files a member's coding agent uploads through a link prepare_file_upload gave it
(file_uploads.py). The link is the credential: no session, bound to one folder, short-lived."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tin_lite.file_uploads import MAX_UPLOAD_BYTES, FileUploadError, FileUploads

router = APIRouter()


@router.put("/api/file-uploads/{token}/{name:path}")
async def upload_file(token: str, name: str, request: Request):
    """One file into the link's folder, as `name`. The answer says where Tin kept it, or what
    to fix."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_UPLOAD_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Upload at most {MAX_UPLOAD_BYTES // 1_000_000} MB a file."},
            )
    runtime = request.app.state.runtime
    uploads = FileUploads(
        database=runtime.database, storage=runtime.storage, settings=request.app.state.settings
    )
    try:
        return await uploads.receive(token, name, bytes(body))
    except FileUploadError as error:
        return JSONResponse(status_code=error.status, content={"detail": str(error)})
