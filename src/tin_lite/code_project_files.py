"""Pinned, read-only project files for isolated code workflows."""

from __future__ import annotations

import base64
import fnmatch
import json
import re
from functools import lru_cache
from uuid import UUID

from tin_lite.domain import RunStatus
from tin_lite.project_files import safe_project_file_path

OPERATION = "code_project_files_v1"
MAX_FILE_BYTES = 64_000
MAX_GLOB_RESULTS = 100
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def source_key(run_id):
    return f"code-project-files:{UUID(str(run_id))}:source"


def _valid(source):
    return (
        isinstance(source, dict)
        and source.get("version") == 1
        and isinstance(source.get("project_id"), str)
        and isinstance(source.get("repo_id"), str)
        and bool(source["repo_id"])
        and isinstance(source.get("revision"), str)
        and bool(_SHA.fullmatch(source["revision"]))
    )


def _glob_matches(path: str, pattern: str) -> bool:
    """Match POSIX path components; only a whole `**` spans directories."""
    parts, tests = path.split("/"), pattern.split("/")

    @lru_cache(None)
    def match(path_index, test_index):
        if test_index == len(tests):
            return path_index == len(parts)
        if tests[test_index] == "**":
            return match(path_index, test_index + 1) or (
                path_index < len(parts) and match(path_index + 1, test_index)
            )
        return (
            path_index < len(parts)
            and fnmatch.fnmatchcase(parts[path_index], tests[test_index])
            and match(path_index + 1, test_index + 1)
        )

    return match(0, 0)


async def select(*, database, storage, project_id):
    """Sample the current canonical HEAD once before a new run is admitted."""
    project = await database.get_project(project_id)
    if project is None or project.id != project_id:
        raise LookupError("project files are unavailable")
    revision = await storage.head_sha(
        await storage.get_repo(project.state_repo_id), project.canonical_branch
    )
    if not isinstance(revision, str) or not _SHA.fullmatch(revision):
        raise ValueError("project files have no canonical revision")
    return {
        "version": 1,
        "project_id": str(project_id),
        "repo_id": project.state_repo_id,
        "revision": revision,
    }


async def guard(conn, *, project_id, source):
    """Bind the sampled repository to the locked project before run/budget creation."""
    if source is None:
        # Runs admitted before this capability have no receipt and keep their old context.
        return
    if not _valid(source) or source["project_id"] != str(project_id):
        raise ValueError("the selected project file revision is invalid")
    repo_id = await conn.fetchval(
        "SELECT state_repo_id FROM projects WHERE id=$1 AND deleted_at IS NULL", project_id
    )
    if repo_id != source["repo_id"]:
        raise ValueError("the project file repository changed before admission")


async def saved_source(database, run, project):
    receipt = await database.get_effect(source_key(run.id))
    if receipt is None:
        return None  # Historical runs keep the original no-project-files behavior.
    source = receipt.result
    if (
        receipt.status != "completed"
        or receipt.operation != OPERATION
        or not _valid(source)
        or source["project_id"] != str(run.project_id)
        or source["repo_id"] != project.state_repo_id
    ):
        raise ValueError("the run's pinned project file revision is unavailable")
    return source


class CodeProjectFileError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class CodeProjectFiles:
    def __init__(self, *, database, storage):
        self.db, self.storage = database, storage

    async def call(self, *, conn, run, project, source, payload):
        """Authorize one read under the current run lease; return JSON-safe content."""
        if not isinstance(payload, dict) or set(payload) != {"kind", "operation", "path"}:
            raise CodeProjectFileError("invalid_file_request")
        if payload["kind"] != "file":
            raise CodeProjectFileError("invalid_file_request")
        fresh = await self.db.get_run(run.id, conn=conn)
        if (
            fresh is None
            or fresh.status not in {RunStatus.PENDING, RunStatus.RUNNING}
            or fresh.project_id != project.id
            or fresh.executor != "workflow.code"
            or source is None
            or source["project_id"] != str(project.id)
            or source["repo_id"] != project.state_repo_id
            or not run.lease_owner
            or not run.sandbox_id
            or not await self.db.validate_lease(
                project_id=run.project_id,
                thread_id=run.thread_id,
                generation=run.generation,
                lease_owner=run.lease_owner,
                fencing_token=run.fencing_token,
                sandbox_id=run.sandbox_id,
                conn=conn,
            )
        ):
            raise CodeProjectFileError("file_access_revoked")
        operation, path = payload["operation"], payload["path"]
        if not isinstance(path, str) or not safe_project_file_path(path):
            raise CodeProjectFileError("invalid_file_path")
        if operation == "glob":
            paths = await self.storage.list_canonical_files_at(
                repo_id=project.state_repo_id, revision=source["revision"]
            )
            matched = sorted(
                item for item in paths if safe_project_file_path(item) and _glob_matches(item, path)
            )
            if len(matched) > MAX_GLOB_RESULTS:
                raise CodeProjectFileError("file_glob_too_broad")
            if len(json.dumps(matched, ensure_ascii=False).encode()) > 120_000:
                raise CodeProjectFileError("file_glob_too_broad")
            return matched
        if operation not in {"read_text", "read_bytes"}:
            raise CodeProjectFileError("invalid_file_request")
        try:
            raw = await self.storage.read_bounded_project_file(
                repo_id=project.state_repo_id,
                commit_sha=source["revision"],
                path=path,
                max_bytes=MAX_FILE_BYTES,
            )
        except ValueError as exc:
            raise CodeProjectFileError("file_too_large_or_not_regular") from exc
        if raw is None:
            raise CodeProjectFileError("file_not_found")
        if operation == "read_text":
            try:
                raw.decode("utf-8")
            except UnicodeError as exc:
                raise CodeProjectFileError("file_not_utf8") from exc
        # Base64 keeps even quote/control-heavy UTF-8 below the 128 KiB IPC frame.
        return base64.b64encode(raw).decode("ascii")
