from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tin_lite.code_storage import CodeStorage
from tin_lite.db import Database
from tin_lite.domain import Project, SideEffectConflictError

MAX_PROJECT_FILE_CHANGES = 50
MAX_PROJECT_FILE_BYTES = 1_000_000
MAX_PROJECT_FILE_PATH_BYTES = 512
MAX_PROJECT_FILE_MESSAGE_BYTES = 240
_PROTECTED_NAMES = frozenset(
    {
        ".git",
        ".gitmodules",
        "auth.json",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
)
_PROTECTED_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
# Credential shapes precise enough to refuse on sight. A project file is read by every run
# and by the founder's agent, so a token in one is a leak, not a note. Kept to formats with
# a fixed prefix or a key/value assignment with a long opaque value; prose never matches.
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "a GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    ),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a Stripe or Clerk secret key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("an OpenAI key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "a secret assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|"
            r"auth[_-]?token|password|passwd)\b['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_.\-/+=]{16,}"
        ),
    ),
)


def credential_findings(content: str) -> list[str]:
    """What kind of credential a text appears to hold; empty when it looks clean."""
    return [kind for kind, pattern in _CREDENTIAL_PATTERNS if pattern.search(content)]


class ProjectFileError(RuntimeError):
    pass


class StaleProjectRevisionError(ProjectFileError):
    pass


@dataclass(frozen=True)
class ProjectFileMutation:
    operation: str
    path: str
    content: str | None = None
    new_path: str | None = None


class ProjectFileMutationInput(BaseModel):
    """One project file change as a client sends it over HTTP or MCP.

    upsert replaces the whole file at path with content; delete takes only path; rename moves
    path to new_path. Unknown keys are rejected so a wrong shape fails with the field named.
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal["upsert", "delete", "rename"]
    path: str = Field(min_length=1, max_length=MAX_PROJECT_FILE_PATH_BYTES)
    content: str | None = None
    new_path: str | None = Field(default=None, min_length=1, max_length=MAX_PROJECT_FILE_PATH_BYTES)


@dataclass(frozen=True)
class ProjectFileCommitResult:
    project_id: UUID
    request_id: UUID
    revision: str
    changed_paths: tuple[str, ...]
    operation: str
    replayed: bool = False


def safe_project_file_path(path: str) -> bool:
    if (
        not path
        or len(path.encode()) > MAX_PROJECT_FILE_PATH_BYTES
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
    ):
        return False
    parts = PurePosixPath(path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return False
    for part in parts:
        lowered = part.casefold()
        if (
            lowered in _PROTECTED_NAMES
            or lowered == ".env"
            or lowered.startswith(".env.")
            or PurePosixPath(lowered).suffix in _PROTECTED_SUFFIXES
        ):
            return False
    return True


def normalize_project_file_mutations(
    changes: list[dict[str, Any]] | tuple[ProjectFileMutation, ...],
) -> tuple[ProjectFileMutation, ...]:
    if not changes or len(changes) > MAX_PROJECT_FILE_CHANGES:
        raise ValueError(f"provide 1-{MAX_PROJECT_FILE_CHANGES} project file changes")
    normalized: list[ProjectFileMutation] = []
    touched: set[str] = set()
    total_bytes = 0
    for raw in changes:
        item = raw if isinstance(raw, ProjectFileMutation) else ProjectFileMutation(**raw)
        if item.operation not in {"upsert", "delete", "rename"}:
            raise ValueError("project file operation must be upsert, delete, or rename")
        if not safe_project_file_path(item.path):
            raise ValueError(f"unsafe or protected project path: {item.path}")
        paths = [item.path]
        if item.operation == "upsert":
            if not isinstance(item.content, str) or item.new_path is not None:
                raise ValueError("upsert requires UTF-8 text content and no new_path")
            found = credential_findings(item.content)
            if found:
                raise ValueError(
                    f"project file {item.path} appears to contain {found[0]}; remove the "
                    "credential and commit the rest"
                )
            total_bytes += len(item.content.encode())
        elif item.operation == "delete":
            if item.content is not None or item.new_path is not None:
                raise ValueError("delete accepts only path")
        else:
            if item.content is not None or not isinstance(item.new_path, str):
                raise ValueError("rename requires path and new_path")
            if item.new_path == item.path or not safe_project_file_path(item.new_path):
                raise ValueError("rename destination is unsafe or unchanged")
            paths.append(item.new_path)
        if any(path in touched for path in paths):
            raise ValueError("a project path may appear in only one change")
        touched.update(paths)
        normalized.append(item)
    if total_bytes > MAX_PROJECT_FILE_BYTES:
        raise ValueError(f"project file changes exceed {MAX_PROJECT_FILE_BYTES} UTF-8 bytes")
    return tuple(normalized)


def project_file_request_fingerprint(
    *, operation: str, expected_revision: str, payload: Any
) -> str:
    canonical = json.dumps(
        {
            "operation": operation,
            "expected_revision": expected_revision,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ProjectFileService:
    def __init__(self, *, database: Database, storage: CodeStorage) -> None:
        self._database = database
        self._storage = storage

    async def commit(
        self,
        *,
        project: Project,
        actor_clerk_user_id: str,
        client_id: str | None,
        request_id: UUID,
        expected_revision: str,
        message: str,
        changes: list[dict[str, Any]] | tuple[ProjectFileMutation, ...],
    ) -> ProjectFileCommitResult:
        if len(expected_revision) != 40:
            raise ValueError("expected_revision must be a 40-character commit SHA")
        normalized_message = " ".join(message.split()).strip()
        if (
            not normalized_message
            or len(normalized_message.encode()) > MAX_PROJECT_FILE_MESSAGE_BYTES
        ):
            raise ValueError(f"message must contain 1-{MAX_PROJECT_FILE_MESSAGE_BYTES} UTF-8 bytes")
        normalized = normalize_project_file_mutations(changes)
        payload = [item.__dict__ for item in normalized]
        fingerprint = project_file_request_fingerprint(
            operation="commit", expected_revision=expected_revision, payload=payload
        )
        async with self._database.project_file_change_lock(
            project_id=project.id, request_id=request_id
        ):
            existing = await self._database.get_project_file_change(
                project_id=project.id, request_id=request_id
            )
            replay = self._replay(existing, fingerprint=fingerprint)
            if replay is not None:
                return replay
            await self._database.start_project_file_change(
                project_id=project.id,
                request_id=request_id,
                actor_clerk_user_id=actor_clerk_user_id,
                client_id=client_id,
                operation="commit",
                request_fingerprint=fingerprint,
                expected_head_sha=expected_revision,
            )
            try:
                revision, changed_paths = await self._storage.commit_project_changes(
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    expected_head_sha=expected_revision,
                    request_id=str(request_id),
                    message=normalized_message,
                    changes=normalized,
                )
            except RuntimeError as exc:
                await self._database.fail_project_file_change(
                    project_id=project.id,
                    request_id=request_id,
                    error_code="stale_revision" if "changed" in str(exc) else "storage_failed",
                )
                if "changed" in str(exc):
                    raise StaleProjectRevisionError(str(exc)) from exc
                raise
            await self._database.complete_project_file_change(
                project_id=project.id,
                request_id=request_id,
                commit_sha=revision,
                changed_paths=list(changed_paths),
                actor_clerk_user_id=actor_clerk_user_id,
                client_id=client_id,
                message=normalized_message,
                operation="commit",
            )
            return ProjectFileCommitResult(
                project_id=project.id,
                request_id=request_id,
                revision=revision,
                changed_paths=changed_paths,
                operation="commit",
            )

    async def revert_latest(
        self,
        *,
        project: Project,
        actor_clerk_user_id: str,
        client_id: str | None,
        request_id: UUID,
        commit_sha: str,
        expected_revision: str,
    ) -> ProjectFileCommitResult:
        if len(commit_sha) != 40 or len(expected_revision) != 40:
            raise ValueError("commit revisions must be 40-character SHAs")
        fingerprint = project_file_request_fingerprint(
            operation="revert",
            expected_revision=expected_revision,
            payload={"commit_sha": commit_sha},
        )
        async with self._database.project_file_change_lock(
            project_id=project.id, request_id=request_id
        ):
            existing = await self._database.get_project_file_change(
                project_id=project.id, request_id=request_id
            )
            replay = self._replay(existing, fingerprint=fingerprint)
            if replay is not None:
                return replay
            await self._database.start_project_file_change(
                project_id=project.id,
                request_id=request_id,
                actor_clerk_user_id=actor_clerk_user_id,
                client_id=client_id,
                operation="revert",
                request_fingerprint=fingerprint,
                expected_head_sha=expected_revision,
            )
            try:
                revision, changed_paths = await self._storage.revert_latest_project_commit(
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    commit_sha=commit_sha,
                    expected_head_sha=expected_revision,
                    request_id=str(request_id),
                )
            except RuntimeError as exc:
                await self._database.fail_project_file_change(
                    project_id=project.id,
                    request_id=request_id,
                    error_code="stale_revision" if "current" in str(exc) else "storage_failed",
                )
                if "current" in str(exc):
                    raise StaleProjectRevisionError(str(exc)) from exc
                raise
            await self._database.complete_project_file_change(
                project_id=project.id,
                request_id=request_id,
                commit_sha=revision,
                changed_paths=list(changed_paths),
                actor_clerk_user_id=actor_clerk_user_id,
                client_id=client_id,
                message=f"Undo {commit_sha[:8]}",
                operation="revert",
            )
            return ProjectFileCommitResult(
                project_id=project.id,
                request_id=request_id,
                revision=revision,
                changed_paths=changed_paths,
                operation="revert",
            )

    @staticmethod
    def _replay(
        existing: dict[str, Any] | None, *, fingerprint: str
    ) -> ProjectFileCommitResult | None:
        if existing is None:
            return None
        if existing["request_fingerprint"] != fingerprint:
            raise SideEffectConflictError("project file request ID belongs to different changes")
        if existing["status"] != "completed" or not existing.get("commit_sha"):
            return None
        summary = existing.get("summary") or {}
        return ProjectFileCommitResult(
            project_id=existing["project_id"],
            request_id=existing["request_id"],
            revision=existing["commit_sha"],
            changed_paths=tuple(summary.get("changed_paths", [])),
            operation=existing["operation"],
            replayed=True,
        )
