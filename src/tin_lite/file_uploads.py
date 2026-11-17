"""Files a member's coding agent made on their machine (card images, a video, an SVG) put into
project Files. MCP carries no file bytes, so prepare_file_upload gives the agent a link bound to
one folder in the project's Files for 45 minutes, and the agent sends each file to it with curl.
Each file is checked as a dashboard upload is (PNG, JPEG, MP4) or as a committed text file is
(UTF-8, no credentials), then committed on its own, as the member who asked for the link."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from tin_lite.domain import Project
from tin_lite.project_files import (
    MAX_PROJECT_FILE_BYTES,
    ProjectFileService,
    StaleProjectRevisionError,
    safe_project_file_path,
)
from tin_lite.project_media import MAX_VIDEO_BYTES, MEDIA_TYPES

LINK_LIFETIME = timedelta(minutes=45)
MAX_FOLDER_BYTES = 400
# A runaway guard, well above a batch of cards or a film's frames.
MAX_FILES_PER_LINK = 200
MAX_UPLOAD_BYTES = MAX_VIDEO_BYTES
# Another write can land between reading the head and committing on it; take the new head.
COMMIT_TRIES = 3


class FileUploadError(Exception):
    """Why Tin didn't keep a file, in words the agent can act on, with the HTTP status."""

    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = status


def check_folder(folder: str) -> str:
    """A folder in project Files, written without leading or trailing slashes."""
    cleaned = folder.strip().strip("/") if isinstance(folder, str) else ""
    if (
        not cleaned
        or len(cleaned.encode()) > MAX_FOLDER_BYTES
        or not safe_project_file_path(f"{cleaned}/file.png")
    ):
        raise FileUploadError(
            "Choose an ordinary folder in project Files, like social/linkedin/media/2026-11-17."
        )
    return cleaned


class FileUploads:
    def __init__(self, *, database: Any, storage: Any, settings: Any) -> None:
        self.db = database
        self.storage = storage
        self.settings = settings

    async def link(self, project_id: UUID, folder: str, clerk_user_id: str) -> dict[str, Any]:
        """A link the member's agent uploads files into `folder` through, for 45 minutes."""
        folder = check_folder(folder)
        token = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + LINK_LIFETIME
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """DELETE FROM file_upload_links
                   WHERE project_id = $1 AND expires_at < now() - interval '1 day'""",
                project_id,
            )
            await conn.execute(
                """INSERT INTO file_upload_links
                       (token_sha256, id, project_id, folder, created_by_clerk_user_id,
                        expires_at)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                hashlib.sha256(token.encode()).hexdigest(),
                uuid4(),
                project_id,
                folder,
                clerk_user_id,
                expires,
            )
        # curl -T appends the local file's name to a URL that ends in a slash.
        url = f"{self.settings.switchboard_public_url.rstrip('/')}/api/file-uploads/{token}/"
        return {
            "folder": f"{folder}/",
            "url": url,
            "expires_at": expires.isoformat(),
            "command": f"curl --fail-with-body -T <file> '{url}'",
            "several": f"curl --fail-with-body -T '{{p1.png,p2.png}}' '{url}'",
        }

    async def receive(self, token: str, name: str, body: bytes) -> dict[str, Any]:
        """Keep one file at the link's folder/`name`, replacing a file already there."""
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            raise FileUploadError("This upload link isn't Tin's.", status=404)
        digest = hashlib.sha256(token.encode()).hexdigest()
        link = await self.db.pool.fetchrow(
            "SELECT * FROM file_upload_links WHERE token_sha256 = $1", digest
        )
        if link is None:
            raise FileUploadError("This upload link isn't Tin's.", status=404)
        if link["expires_at"] <= datetime.now(UTC):
            raise FileUploadError(
                "This upload link expired: call prepare_file_upload for a new one.", status=410
            )
        project = await self.db.get_project(link["project_id"])
        if project is None or project.deleted_at is not None:
            raise FileUploadError("This upload link's project is gone.", status=410)
        if not await self.db.has_project_access(
            project_id=project.id, clerk_user_id=link["created_by_clerk_user_id"]
        ):
            raise FileUploadError(
                "The member who asked for this link is no longer on the project.", status=403
            )
        path = f"{link['folder']}/{name}"
        if not name or not safe_project_file_path(path):
            raise FileUploadError(
                f"{name or 'The file'} can't be kept at {link['folder']}/: send it with an "
                "ordinary name, like p1.png."
            )
        change = _change(path, body)
        counted = await self.db.pool.fetchval(
            """UPDATE file_upload_links SET files = files + 1
               WHERE token_sha256 = $1 AND files < $2 RETURNING files""",
            digest,
            MAX_FILES_PER_LINK,
        )
        if counted is None:
            raise FileUploadError(
                f"This link has taken {MAX_FILES_PER_LINK} files: call prepare_file_upload for "
                "another.",
                status=429,
            )
        return await self._commit(project, link["created_by_clerk_user_id"], path, change)

    async def _commit(
        self, project: Project, clerk_user_id: str, path: str, change: dict[str, Any]
    ) -> dict[str, Any]:
        files = ProjectFileService(database=self.db, storage=self.storage)
        for attempt in range(COMMIT_TRIES):
            _paths, head = await self.storage.list_canonical_files(
                repo_id=project.state_repo_id, branch=project.canonical_branch
            )
            try:
                if "media_type" in change:
                    kept = await files.upload(
                        project=project,
                        actor_clerk_user_id=clerk_user_id,
                        client_id="upload-link",
                        request_id=uuid4(),
                        expected_revision=head,
                        path=path,
                        content=change["content"],
                        media_type=change["media_type"],
                    )
                    return {**kept, "bytes": len(change["content"])}
                result = await files.commit(
                    project=project,
                    actor_clerk_user_id=clerk_user_id,
                    client_id="upload-link",
                    request_id=uuid4(),
                    expected_revision=head,
                    message=f"Upload {path}"[:240],
                    changes=[{"operation": "upsert", "path": path, "content": change["text"]}],
                )
                return {
                    "path": path,
                    "revision": result.revision,
                    "bytes": len(change["text"].encode()),
                }
            except StaleProjectRevisionError:
                if attempt + 1 == COMMIT_TRIES:
                    raise FileUploadError(
                        "Files kept changing while this file was saved; send it again.",
                        status=409,
                    ) from None
            except ValueError as error:
                raise FileUploadError(str(error)) from None
        raise AssertionError("unreachable")


def _change(path: str, body: bytes) -> dict[str, Any]:
    """What the file's name and bytes say it is: an image or video Tin checks, or text."""
    media_type = MEDIA_TYPES.get(PurePosixPath(path).suffix.lower())
    if media_type:
        return {"content": body, "media_type": media_type}
    if not body:
        raise FileUploadError("The file is empty.")
    if len(body) > MAX_PROJECT_FILE_BYTES:
        raise FileUploadError(
            f"A text file can be at most {MAX_PROJECT_FILE_BYTES // 1_000_000} MB; images and "
            "videos have their own limits."
        )
    try:
        return {"text": body.decode("utf-8")}
    except UnicodeDecodeError:
        raise FileUploadError(
            "Tin keeps PNG, JPEG and MP4 files and UTF-8 text files; this file is neither."
        ) from None
