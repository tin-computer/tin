from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from pierre_storage import GitStorage
from pierre_storage.errors import ApiError
from pierre_storage.types import GitFileMode, Repo
from pierre_storage.version import get_user_agent

from tin_lite.publication import (
    OutputCheckpoint,
    OutputConflictError,
    PublicationPendingError,
    StaleOutputComparisonError,
)

if TYPE_CHECKING:
    from tin_lite.project_files import ProjectFileMutation

_FILE_READ_ATTEMPTS = 5
_TASK_DIFF_MAX_FILES = 100
_TASK_DIFF_MAX_BYTES = 1_000_000
# Text outputs stay under the comparison limit; a declared binary output (the demo video) is
# only ever read whole, never diffed.
_PUBLICATION_TEXT_MAX_BYTES = 1_000_000
_PUBLICATION_BINARY_MAX_BYTES = 16_000_000
_BINARY_MEDIA_TYPES = frozenset({"video/mp4"})


def _publication_limit(checkpoint: OutputCheckpoint) -> int:
    if checkpoint.media_type in _BINARY_MEDIA_TYPES:
        return _PUBLICATION_BINARY_MAX_BYTES
    return _PUBLICATION_TEXT_MAX_BYTES


def reviewed_task_diff(task_diff: dict[str, Any]) -> str:
    """Rebuild and verify the exact patch presented for project-task approval."""
    files = task_diff.get("files")
    expected_sha = task_diff.get("sha256")
    if not isinstance(files, list) or not isinstance(expected_sha, str):
        raise RuntimeError("task review has no exact diff")
    if not files or len(files) > _TASK_DIFF_MAX_FILES:
        raise RuntimeError("task review exact diff is outside the approved bounds")
    raw_parts: list[str] = []
    total_bytes = 0
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("patch"), str):
            raise RuntimeError("task review has an invalid exact diff")
        path = item.get("path")
        old_path = item.get("old_path")
        if (
            not isinstance(path, str)
            or not _safe_repo_path(path)
            or (
                old_path is not None
                and (not isinstance(old_path, str) or not _safe_repo_path(old_path))
            )
        ):
            raise RuntimeError("task review exact diff contains an unsafe path")
        total_bytes += int(item.get("bytes", 0))
        raw = str(item["patch"])
        if raw:
            raw_parts.append(raw)
    if total_bytes > _TASK_DIFF_MAX_BYTES:
        raise RuntimeError("task review exact diff is outside the approved bounds")
    raw_diff = "\n".join(part.rstrip("\n") for part in raw_parts).strip()
    if raw_diff:
        raw_diff += "\n"
    if not raw_diff or hashlib.sha256(raw_diff.encode()).hexdigest() != expected_sha:
        raise RuntimeError("task review exact diff no longer matches its approval hash")
    return raw_diff


@dataclass(frozen=True)
class SandboxRemotes:
    canonical_url: str
    canonical_auth_header: str
    ephemeral_url: str
    ephemeral_auth_header: str


class CodeStorage:
    def __init__(self, *, organization: str, private_key: str) -> None:
        self._organization = organization
        self._private_key = private_key
        self._client = GitStorage({"name": organization, "key": private_key})

    async def ensure_repo(
        self,
        repo_id: str,
        *,
        initial_readme: str = "# Project state\n\nManaged by the Tin Lite switchboard.\n",
    ) -> Repo:
        try:
            repo = await self._client.find_one(id=repo_id)
        except ApiError as exc:
            # An empty organization currently answers 403 for a repo-scoped lookup before create.
            if exc.status_code != 403:
                raise
            repo = None
        if repo is None:
            try:
                repo = await self._client.create_repo(id=repo_id, default_branch="main", ttl=300)
            except ApiError as exc:
                if exc.status_code != 409:
                    raise
                repo = await self._client.find_one(id=repo_id)
                if repo is None:
                    raise
        if await self.head_sha(repo, "main") is None:
            await (
                repo.create_commit(
                    target_branch="main",
                    commit_message="Initialize Tin Lite repository",
                    author={"name": "Tin Switchboard", "email": "switchboard@tin.local"},
                    ttl=300,
                )
                .add_file_from_string(
                    "README.md",
                    initial_readme,
                )
                .send()
            )
        return repo

    async def delete_repo(self, repo_id: str) -> bool:
        """True when the repository is gone after this call; upstream cleanup is asynchronous."""
        try:
            await self._client.delete_repo(id=repo_id, ttl=300)
        except ApiError as exc:
            # 404: never created or already removed; 409: a deletion is already in progress.
            if exc.status_code not in {404, 409}:
                raise
        return True

    async def get_repo(self, repo_id: str) -> Repo:
        repo = await self._client.find_one(id=repo_id)
        if repo is None:
            raise LookupError(f"code.storage repository {repo_id!r} does not exist")
        return repo

    async def head_sha(self, repo: Repo, branch: str) -> str | None:
        result = await repo.list_commits(branch=branch, limit=1, ttl=300)
        commits = result.get("commits", [])
        return commits[0]["sha"] if commits else None

    def sandbox_remotes(self, *, repo_id: str, branch: str, subject: str) -> SandboxRemotes:
        canonical_token = self._generate_token(
            repo_id=repo_id,
            scopes=["git:read"],
            subject=f"{subject}:read",
            ttl=900,
        )
        ephemeral_ref = f"refs/namespaces/ephemeral/refs/heads/{branch}"
        ephemeral_token = self._generate_token(
            repo_id=repo_id,
            scopes=["git:read", "git:write"],
            subject=f"{subject}:write",
            ttl=900,
            refs=[
                [ephemeral_ref, ["no-force-push"]],
                ["*", ["no-push"]],
            ],
        )
        base_url = f"https://{self._organization}.code.storage/{repo_id}"
        return SandboxRemotes(
            canonical_url=f"{base_url}.git",
            canonical_auth_header=_basic_auth_header(canonical_token),
            ephemeral_url=f"{base_url}+ephemeral.git",
            ephemeral_auth_header=_basic_auth_header(ephemeral_token),
        )

    async def read_ephemeral_artifact(self, *, repo_id: str, branch: str, path: str) -> bytes:
        repo = await self.get_repo(repo_id)
        return await _read_file_with_retry(
            repo,
            path=path,
            ref=branch,
            ephemeral=True,
        )

    async def read_ephemeral_artifact_if_exists(
        self, *, repo_id: str, branch: str, path: str
    ) -> bytes | None:
        """Return durable branch output when recovering after an ambiguous activity exit."""
        try:
            return await self.read_ephemeral_artifact(repo_id=repo_id, branch=branch, path=path)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def read_canonical_artifact(self, *, repo_id: str, commit_sha: str, path: str) -> bytes:
        repo = await self.get_repo(repo_id)
        return await _read_file_with_retry(repo, path=path, ref=commit_sha)

    async def read_workflow_resource(self, *, repo_id: str, commit_sha: str, path: str) -> bytes:
        """New-format packages require bounded regular files at every path component."""
        from tin_lite.workflow_packages import MAX_DEFINITION_BYTES, relative_path

        relative_path(path)
        if not _is_commit_sha(commit_sha):
            raise ValueError("workflow resource must pin a commit")
        repo = await self.get_repo(repo_id)
        try:
            entry = await self._publication_file(
                repo, ref=commit_sha, path=path, max_bytes=MAX_DEFINITION_BYTES
            )
        except OutputConflictError as exc:
            raise ValueError("workflow resource must be a bounded regular file") from exc
        if entry is None:
            raise ValueError("declared workflow resource is missing")
        return entry[1]

    async def read_canonical_artifact_if_exists(
        self, *, repo_id: str, commit_sha: str, path: str
    ) -> bytes | None:
        """Read one canonical file, or None when that revision does not contain it."""
        try:
            return await self.read_canonical_artifact(
                repo_id=repo_id, commit_sha=commit_sha, path=path
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def _publication_json(self, repo: Repo, endpoint: str, **params: str) -> dict:
        """Bounded reads missing from the pinned SDK; auth stays on the switchboard."""
        token = repo.generate_jwt(repo.id, {"permissions": ["git:read"], "ttl": 300})
        url = f"{repo.api_base_url}/api/repos/{quote(repo.id, safe='')}/{endpoint}"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Code-Storage-Agent": get_user_agent(),
                },
            )
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise PublicationPendingError("publication metadata exceeds its read budget")
            result = response.json()
        if not isinstance(result, dict):
            raise PublicationPendingError("publication metadata is unavailable")
        return result

    async def procedure_checkpoint_revision(self, *, repo_id: str, branch: str) -> str | None:
        repo = await self.get_repo(repo_id)
        try:
            result = await self._publication_json(
                repo, "commits", ref=branch, ephemeral="true", limit="1"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        commits = result.get("commits")
        if not isinstance(commits, list):
            raise PublicationPendingError("checkpoint revision is unavailable")
        if not commits:
            return None
        sha = commits[0].get("sha")
        if not _is_commit_sha(sha):
            raise PublicationPendingError("checkpoint revision is invalid")
        return sha

    async def _publication_file(
        self, repo: Repo, *, ref: str, path: str, max_bytes: int = _PUBLICATION_TEXT_MAX_BYTES
    ) -> tuple[str, bytes] | None:
        """Compare real regular-file entries, not a 404 or a followed symlink."""
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            result = await self._publication_json(
                repo, "files/metadata", ref=ref, path=prefix, limit="2"
            )
            if result.get("ref") != ref or not isinstance(result.get("files"), list):
                raise PublicationPendingError("file metadata did not resolve the pinned revision")
            files = result["files"]
            entry = next((item for item in files if item.get("path") == prefix), None)
            if index < len(parts):
                if entry is not None and entry.get("type") != "tree":
                    raise OutputConflictError("the output's parent is no longer a directory")
                continue
            if entry is None:
                if files or result.get("has_more"):
                    raise OutputConflictError("the output path is a directory")
                return None
            if entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}:
                raise OutputConflictError("the output path is not a regular file")
            size = entry.get("size")
            if type(size) is not int or not 0 <= size <= max_bytes:
                raise OutputConflictError("the output file exceeds the safe comparison limit")
            content = await _read_file_with_retry(repo, path=path, ref=ref)
            if len(content) != size:
                raise PublicationPendingError("file read did not match its pinned metadata")
            return entry["mode"], content
        raise ValueError("output has no path")

    async def read_procedure_checkpoint(
        self, *, repo_id: str, revision: str, path: str, binary: bool = False
    ) -> bytes:
        if not _is_commit_sha(revision) or not _safe_repo_path(path):
            raise ValueError("procedure checkpoint requires an immutable revision and safe path")
        repo = await self.get_repo(repo_id)
        result = await self._publication_file(
            repo,
            ref=revision,
            path=path,
            max_bytes=_PUBLICATION_BINARY_MAX_BYTES if binary else _PUBLICATION_TEXT_MAX_BYTES,
        )
        if result is None:
            raise ValueError("procedure checkpoint has no declared output")
        return result[1]

    async def stage_native_output(
        self,
        *,
        repo_id: str,
        branch: str,
        run_id: str,
        generation: int,
        path: str,
        content: bytes,
        executor: str = "style.capture",
    ) -> str:
        """Trusted native output uses a run-owned branch, never a sandbox credential."""
        from uuid import UUID

        from tin_lite.domain import (
            GROWTH_ONBOARDING_PLAN_PATH,
            GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME,
        )
        from tin_lite.project_files import safe_project_file_path
        from tin_lite.writing_style import STYLE_PATH

        code = executor == "workflow.code"
        # Each trusted executor may stage only its own declared output, within its own bound.
        if code:
            valid_path = (
                safe_project_file_path(path)
                and path != "wiki/INDEX.md"
                and path.split("/")[0]
                not in {".tin-lite", "procedures", "registry", "workflow_packages"}
            )
            limit, target = 64_000, f"procedures/{run_id}/{generation}"
        elif executor == GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME:
            valid_path = path == GROWTH_ONBOARDING_PLAN_PATH
            limit, target = 40_000, f"native-plan/{run_id}/{generation}"
        else:
            valid_path = path == STYLE_PATH
            limit, target = 24_000, f"native-style/{run_id}/{generation}"
        if (
            executor not in {"style.capture", "workflow.code", GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME}
            or str(UUID(run_id)) != run_id
            or not valid_path
            or not 0 < len(content) <= limit
        ):
            raise ValueError("invalid native output checkpoint")
        content.decode("utf-8")
        revision = await self.procedure_checkpoint_revision(repo_id=repo_id, branch=target)
        if revision:
            saved = await self.read_procedure_checkpoint(
                repo_id=repo_id, revision=revision, path=path
            )
            if saved != content:
                raise PublicationPendingError("native output checkpoint has different bytes")
            return revision
        repo = await self.get_repo(repo_id)
        if code:
            # A recurring code report may be identical to the current durable file.
            # code.storage rejects an empty commit; the immutable canonical revision
            # is already a valid checkpoint. Publication still checks the run's base,
            # lease and destination, and records its normal no-change receipt.
            head = await self.head_sha(repo, branch)
            if _is_commit_sha(head):
                current = await self._publication_file(repo, ref=head, path=path)
                if current is not None and current[1] == content:
                    return head
        try:
            result = (
                await repo.create_commit(
                    target_branch=target,
                    base_branch=branch,
                    ephemeral=True,
                    ephemeral_base=False,
                    commit_message=f"{executor} {run_id} checkpoint",
                    author={"name": "Tin Switchboard", "email": "switchboard@tin.local"},
                    ttl=300,
                )
                .add_file(path, content)
                .send()
            )
            if not _is_commit_sha(result.get("commit_sha")):
                raise ValueError("missing checkpoint revision")
            return result["commit_sha"]
        except Exception:
            raise PublicationPendingError("native checkpoint must reconcile before retry") from None

    async def _checkpoint_contents(self, repo_id, checkpoint, content):
        checkpoint.validate_content(content)
        files = {checkpoint.artifact_path: content}
        for item in checkpoint.companions:
            raw = await self.read_procedure_checkpoint(
                repo_id=repo_id, revision=item.ephemeral_commit_sha, path=item.artifact_path
            )
            item.validate_content(raw)
            files[item.artifact_path] = raw
        return files

    async def _verify_published_checkpoint(self, repo, revision, checkpoint):
        for item in checkpoint.files:
            entry = await self._publication_file(
                repo, ref=revision, path=item.artifact_path, max_bytes=_publication_limit(item)
            )
            if entry is None:
                raise PublicationPendingError("saved publication is missing an output file")
            item.validate_content(entry[1])

    async def _reconcile_procedure_publication(
        self,
        repo: Repo,
        *,
        head: str,
        parent: str,
        checkpoint: OutputCheckpoint,
        message: str,
    ) -> str | None:
        """Walk a pinned first-parent chain; an incomplete scan never proves absence.

        Listings are date-ordered, not necessarily parent-ordered. Assemble pages but
        only inspect nodes on the actual chain from the captured HEAD to the intent.
        """
        cursor: str | None = None
        next_sha = head
        nodes: dict[str, dict] = {}
        visited: set[str] = set()
        cursors: set[str] = set()
        for _page in range(20):
            if next_sha == parent:
                return None
            params = {"ref": head, "limit": "100"}
            if cursor is not None:
                params["cursor"] = cursor
            result = await self._publication_json(repo, "commits", **params)
            for commit in result.get("commits", []):
                if not _is_commit_sha(commit.get("sha")):
                    raise PublicationPendingError("publication history is invalid")
                nodes[commit["sha"]] = commit
            while next_sha in nodes and next_sha != parent:
                if next_sha in visited:
                    raise PublicationPendingError("publication history is cyclic")
                visited.add(next_sha)
                commit = nodes[next_sha]
                parents = commit.get("parent_shas")
                if (
                    not isinstance(parents, list)
                    or len(parents) != 1
                    or not _is_commit_sha(parents[0])
                ):
                    raise PublicationPendingError(
                        "publication history does not reach the saved parent"
                    )
                if commit.get("message") == message:
                    if parents != [parent]:
                        raise PublicationPendingError("publication marker has a different parent")
                    await self._verify_published_checkpoint(repo, next_sha, checkpoint)
                    diff = await repo.get_commit_diff(sha=next_sha, ttl=300)
                    paths = {item.artifact_path for item in checkpoint.files}
                    if diff.get("filtered_files") or any(
                        item.get("path") not in paths
                        or item.get("old_path") not in {None, item.get("path")}
                        for item in diff.get("files", [])
                    ):
                        raise PublicationPendingError("publication marker changes unexpected paths")
                    return next_sha
                next_sha = parents[0]
            if next_sha == parent:
                return None
            cursor = result.get("next_cursor")
            if not result.get("has_more") or not isinstance(cursor, str) or cursor in cursors:
                raise PublicationPendingError("publication history is incomplete")
            cursors.add(cursor)
        raise PublicationPendingError("publication reconciliation reached its history budget")

    async def publish_procedure_output(
        self,
        *,
        repo_id: str,
        branch: str,
        checkpoint: OutputCheckpoint,
        content: bytes,
        execution_key: str,
        workflow_key: str,
        intent: dict | None,
        legacy_attempt: bool,
        save_intent: Callable[[dict], Awaitable[None]],
        validate_lease: Callable[[], Awaitable[None]],
        companion_contents: dict[str, bytes] | None = None,
    ) -> tuple[str, bool]:
        """Atomically publish a validated primary output and its bounded companion."""
        checkpoint.validate_content(content)
        repo = await self.get_repo(repo_id)
        message = f"{workflow_key} {checkpoint.run_id} [{execution_key}]"
        try:
            async with asyncio.timeout(120):
                if companion_contents is None:
                    files = await self._checkpoint_contents(repo_id, checkpoint, content)
                else:
                    if set(companion_contents) != {c.artifact_path for c in checkpoint.companions}:
                        raise ValueError("reviewed companion contents do not match the checkpoint")
                    for item in checkpoint.companions:
                        item.validate_content(companion_contents[item.artifact_path])
                    files = {checkpoint.artifact_path: content, **companion_contents}
                head = await self.head_sha(repo, branch)
                if not _is_commit_sha(head):
                    raise PublicationPendingError("project has no canonical revision")
                if intent is not None:
                    if intent.get("checkpoint") != checkpoint.to_dict():
                        raise PublicationPendingError(
                            "publication intent identifies a different output"
                        )
                    parent = intent.get("attempted_parent_sha")
                    if not _is_commit_sha(parent):
                        raise PublicationPendingError("publication intent has no valid parent")
                    if intent.get("no_change") is True:
                        await self._verify_published_checkpoint(repo, parent, checkpoint)
                        return parent, False
                else:
                    parent = checkpoint.source_base_sha
                if intent is not None or legacy_attempt:
                    reconciled = await self._reconcile_procedure_publication(
                        repo, head=head, parent=parent, checkpoint=checkpoint, message=message
                    )
                    if reconciled is not None:
                        return reconciled, True
                no_change = True
                for item in checkpoint.files:
                    original = await self._publication_file(
                        repo,
                        ref=checkpoint.source_base_sha,
                        path=item.artifact_path,
                        max_bytes=_publication_limit(item),
                    )
                    current = (
                        original
                        if head == checkpoint.source_base_sha
                        else (
                            await self._publication_file(
                                repo,
                                ref=head,
                                path=item.artifact_path,
                                max_bytes=_publication_limit(item),
                            )
                        )
                    )
                    if original != current:
                        raise OutputConflictError(
                            "an output file changed while this run was working"
                        )
                    no_change = (
                        no_change
                        and current is not None
                        and current[1] == files[item.artifact_path]
                    )
                await validate_lease()
                await save_intent(
                    {
                        "version": 1,
                        "checkpoint": checkpoint.to_dict(),
                        "attempted_parent_sha": head,
                        "no_change": no_change,
                    }
                )
                if no_change:
                    return head, False
                # Recheck after the durable intent write and immediately before the effect.
                await validate_lease()
                try:
                    commit = repo.create_commit(
                        target_branch=branch,
                        expected_head_sha=head,
                        commit_message=message,
                        author={"name": "Tin Switchboard", "email": "switchboard@tin.local"},
                        ttl=300,
                    )
                    for path, raw in files.items():
                        commit = commit.add_file(path, raw)
                    result = await commit.send()
                    sha = result.get("commit_sha")
                    if not _is_commit_sha(sha):
                        raise PublicationPendingError("publication returned no commit identity")
                    return sha, True
                except Exception as exc:
                    # Temporal retries this activity, reconciling the intent before any write.
                    raise PublicationPendingError(
                        "publication needs reconciliation before retry"
                    ) from exc
        except TimeoutError as exc:
            raise PublicationPendingError("publication reconciliation timed out") from exc

    async def read_output_destination(
        self, *, repo_id: str, revision: str, path: str
    ) -> tuple[str, bytes] | None:
        if not _is_commit_sha(revision) or not _safe_repo_path(path):
            raise ValueError("comparison requires an immutable revision and safe path")
        return await self._publication_file(
            await self.get_repo(repo_id),
            ref=revision,
            path=path,
            max_bytes=_PUBLICATION_BINARY_MAX_BYTES
            if path.lower().endswith(".mp4")
            else _PUBLICATION_TEXT_MAX_BYTES,
        )

    async def apply_reviewed_documents(
        self,
        *,
        repo_id,
        branch,
        checkpoint,
        proposal_revision,
        destinations,
        execution_key,
        intent,
        save_intent,
        validate_authority,
    ):
        if len(checkpoint.files) != 2 or len(destinations) != 2:
            raise ValueError("reviewed documents require exactly two files")
        files = {}
        for item in checkpoint.files:
            raw = await self.read_canonical_artifact(
                repo_id=repo_id, commit_sha=proposal_revision, path=item.artifact_path
            )
            item.validate_content(raw)
            files[item.artifact_path] = raw
        content = files[checkpoint.artifact_path]
        primary, companion = checkpoint.files
        mapped_companion = replace(companion, artifact_path=destinations[1])
        mapped = replace(primary, artifact_path=destinations[0], companions=(mapped_companion,))
        return await self.publish_procedure_output(
            repo_id=repo_id,
            branch=branch,
            checkpoint=mapped,
            content=content,
            execution_key=execution_key,
            workflow_key="Apply reviewed documents",
            intent=intent,
            legacy_attempt=False,
            save_intent=save_intent,
            validate_lease=validate_authority,
            companion_contents={destinations[1]: files[companion.artifact_path]},
        )

    async def apply_saved_output(
        self,
        *,
        repo_id: str,
        branch: str,
        checkpoint: OutputCheckpoint,
        content: bytes,
        expected_revision: str,
        execution_key: str,
        intent: dict | None,
        save_intent: Callable[[dict], Awaitable[None]],
    ) -> tuple[str, bool]:
        """A member-authorized replacement, never a continuation of the sandbox lease.

        Reuse the bounded publication reconciler, but never rebase a reviewed choice.
        Callers serialize the run's resolution and persist intent before this write.
        """
        checkpoint.validate_content(content)
        if not _is_commit_sha(expected_revision):
            raise ValueError("comparison requires an immutable project revision")
        message = f"Apply saved output {checkpoint.run_id} [{execution_key}]"
        async with asyncio.timeout(120):
            files = await self._checkpoint_contents(repo_id, checkpoint, content)
            repo = await self.get_repo(repo_id)
            head = await self.head_sha(repo, branch)
            if not _is_commit_sha(head):
                raise PublicationPendingError("project revision is unavailable")
            if intent is not None:
                if (
                    intent.get("checkpoint") != checkpoint.to_dict()
                    or intent.get("attempted_parent_sha") != expected_revision
                ):
                    raise PublicationPendingError("saved application intent does not match")
                if intent.get("no_change") is True:
                    await self._verify_published_checkpoint(repo, expected_revision, checkpoint)
                    return expected_revision, False
                found = await self._reconcile_procedure_publication(
                    repo,
                    head=head,
                    parent=expected_revision,
                    checkpoint=checkpoint,
                    message=message,
                )
                if found is not None:
                    return found, True
            if head != expected_revision:
                raise StaleOutputComparisonError("the project changed; compare again")
            current = await self._publication_file(
                repo,
                ref=head,
                path=checkpoint.artifact_path,
                max_bytes=_publication_limit(checkpoint),
            )
            no_change = current is not None and current[1] == content
            # The member compared only the primary file. Never replace edited notes
            # silently as a side effect of that choice.
            for item in checkpoint.companions:
                original = await self._publication_file(
                    repo,
                    ref=checkpoint.source_base_sha,
                    path=item.artifact_path,
                    max_bytes=_publication_limit(item),
                )
                companion = await self._publication_file(
                    repo, ref=head, path=item.artifact_path, max_bytes=_publication_limit(item)
                )
                if companion != original:
                    raise StaleOutputComparisonError(
                        "The companion notes changed; keep current files."
                    )
                no_change = (
                    no_change
                    and companion is not None
                    and companion[1] == files[item.artifact_path]
                )
            await save_intent(
                {
                    "version": 1,
                    "checkpoint": checkpoint.to_dict(),
                    "attempted_parent_sha": head,
                    "no_change": no_change,
                }
            )
            if no_change:
                return head, False
            try:
                commit = repo.create_commit(
                    target_branch=branch,
                    expected_head_sha=head,
                    commit_message=message,
                    author={"name": "Tin Switchboard", "email": "switchboard@tin.local"},
                    ttl=300,
                ).add_file(
                    checkpoint.artifact_path,
                    content,
                    mode=GitFileMode(current[0]) if current is not None else GitFileMode.REGULAR,
                )
                for item in checkpoint.companions:
                    commit = commit.add_file(item.artifact_path, files[item.artifact_path])
                result = await commit.send()
                sha = result.get("commit_sha")
                if not _is_commit_sha(sha):
                    raise PublicationPendingError("application returned no commit identity")
                return sha, True
            except Exception as exc:
                raise PublicationPendingError(
                    "application must be reconciled before retry"
                ) from exc

    async def list_canonical_files(
        self,
        *,
        repo_id: str,
        branch: str,
    ) -> tuple[list[str], str]:
        """Resolve and list one immutable canonical project snapshot."""
        repo = await self.get_repo(repo_id)
        commit_sha = await self.head_sha(repo, branch)
        if commit_sha is None:
            raise RuntimeError("project state repository has no canonical head")
        result = await repo.list_files(ref=commit_sha, ttl=300)
        paths = result.get("paths", [])
        if not isinstance(paths, list) or any(
            not isinstance(path, str) or not _safe_repo_path(path) for path in paths
        ):
            raise RuntimeError("project state repository returned an unsafe file path")
        return sorted(paths), commit_sha

    async def list_canonical_files_at(
        self,
        *,
        repo_id: str,
        revision: str,
    ) -> list[str]:
        repo = await self.get_repo(repo_id)
        result = await repo.list_files(ref=revision, ttl=300)
        paths = result.get("paths", [])
        if not isinstance(paths, list) or any(
            not isinstance(path, str) or not _safe_repo_path(path) for path in paths
        ):
            raise RuntimeError("project state repository returned an unsafe file path")
        return sorted(paths)

    async def search_canonical_files(
        self,
        *,
        repo_id: str,
        revision: str,
        query: str,
        paths: list[str] | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], bool]:
        repo = await self.get_repo(repo_id)
        result = await repo.grep(
            pattern=query,
            ref=revision,
            paths=paths,
            case_sensitive=False,
            limits={"max_files": limit, "max_matches": limit, "max_line_length": 1000},
            ttl=300,
        )
        matches: list[dict[str, Any]] = []
        for item in result.get("matches", []):
            path = item.get("path")
            if not isinstance(path, str) or not _safe_repo_path(path):
                raise RuntimeError("project search returned an unsafe file path")
            lines = []
            for line in item.get("lines", []):
                number = line.get("line_number")
                value = line.get("text")
                if isinstance(number, int) and isinstance(value, str):
                    lines.append({"line_number": number, "text": value[:1000]})
            matches.append({"path": path, "lines": lines})
        return matches[:limit], bool(result.get("has_more"))

    async def canonical_file_history(
        self,
        *,
        repo_id: str,
        branch: str,
        path: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        repo = await self.get_repo(repo_id)
        result = await repo.list_commits(branch=branch, limit=min(limit * 4, 100), ttl=300)
        history: list[dict[str, Any]] = []
        for commit in result.get("commits", []):
            if len(history) >= limit:
                break
            diff = await repo.get_commit_diff(sha=commit["sha"], paths=[path], ttl=300)
            files = diff.get("files", [])
            if not files:
                continue
            history.append(
                {
                    "revision": commit["sha"],
                    "message": commit["message"],
                    "author_name": commit["author_name"],
                    "date": commit["date"],
                    "state": str(files[0].get("state", "modified")),
                }
            )
        return history

    async def commit_project_changes(
        self,
        *,
        repo_id: str,
        branch: str,
        expected_head_sha: str,
        request_id: str,
        message: str,
        changes: tuple[ProjectFileMutation, ...],
    ) -> tuple[str, tuple[str, ...]]:
        """Make one bounded member-requested canonical project-state commit."""
        repo = await self.get_repo(repo_id)
        marker = f"[project-file:{request_id}]"
        commit_message = f"{message} {marker}"
        current_head = await self.head_sha(repo, branch)
        if current_head != expected_head_sha:
            recent = await repo.list_commits(branch=branch, limit=1, ttl=300)
            commits = recent.get("commits", [])
            if commits and commits[0].get("message") == commit_message:
                return commits[0]["sha"], tuple(
                    sorted(
                        {
                            path
                            for item in changes
                            for path in (item.path, item.new_path)
                            if path is not None
                        }
                    )
                )
            raise RuntimeError("canonical project state changed before file commit")
        listing = await repo.list_files(ref=expected_head_sha, ttl=300)
        existing_paths = set(listing.get("paths", []))
        builder = repo.create_commit(
            target_branch=branch,
            expected_head_sha=expected_head_sha,
            commit_message=commit_message,
            author={"name": "Tin project member", "email": "member@tin.local"},
            ttl=300,
        )
        changed_paths: set[str] = set()
        for change in changes:
            if change.operation == "upsert":
                assert change.content is not None
                builder = builder.add_file_from_string(change.path, change.content)
                changed_paths.add(change.path)
            elif change.operation == "delete":
                if change.path not in existing_paths:
                    raise ValueError(f"project file does not exist: {change.path}")
                builder = builder.delete_path(change.path)
                changed_paths.add(change.path)
            else:
                assert change.new_path is not None
                if change.path not in existing_paths:
                    raise ValueError(f"project file does not exist: {change.path}")
                if change.new_path in existing_paths:
                    raise ValueError(f"project file already exists: {change.new_path}")
                content = await _read_file_with_retry(repo, path=change.path, ref=expected_head_sha)
                content.decode("utf-8")
                builder = builder.add_file(change.new_path, content).delete_path(change.path)
                changed_paths.update((change.path, change.new_path))
        try:
            result = await builder.send()
            return result["commit_sha"], tuple(sorted(changed_paths))
        except Exception:
            recent = await repo.list_commits(branch=branch, limit=1, ttl=300)
            commits = recent.get("commits", [])
            if commits and commits[0].get("message") == commit_message:
                return commits[0]["sha"], tuple(sorted(changed_paths))
            raise

    async def revert_latest_project_commit(
        self,
        *,
        repo_id: str,
        branch: str,
        commit_sha: str,
        expected_head_sha: str,
        request_id: str,
    ) -> tuple[str, tuple[str, ...]]:
        """Undo only the current head as a new commit, preserving an auditable history."""
        repo = await self.get_repo(repo_id)
        current_head = await self.head_sha(repo, branch)
        if current_head != expected_head_sha or commit_sha != expected_head_sha:
            raise RuntimeError("only the current project revision can be undone")
        recent = await repo.list_commits(branch=branch, limit=2, ttl=300)
        commits = recent.get("commits", [])
        if len(commits) < 2 or commits[0].get("sha") != commit_sha:
            raise RuntimeError("current project revision has no restorable parent")
        diff = await repo.get_commit_diff(sha=commit_sha, ttl=300)
        if diff.get("filtered_files"):
            raise RuntimeError("current project revision is too large to undo safely")
        changed_paths = tuple(
            sorted(
                {
                    str(path)
                    for item in diff.get("files", [])
                    for path in (item.get("path"), item.get("old_path"))
                    if isinstance(path, str) and _safe_repo_path(path)
                }
            )
        )
        commit_message = f"Undo {commit_sha[:8]} [project-file:{request_id}]"
        try:
            result = await repo.restore_commit(
                target_branch=branch,
                target_commit_sha=commits[1]["sha"],
                expected_head_sha=expected_head_sha,
                commit_message=commit_message,
                author={"name": "Tin project member", "email": "member@tin.local"},
                ttl=300,
            )
            return result["commit_sha"], changed_paths
        except Exception:
            latest = await repo.list_commits(branch=branch, limit=1, ttl=300)
            values = latest.get("commits", [])
            if values and values[0].get("message") == commit_message:
                return values[0]["sha"], changed_paths
            raise

    async def publish_workflow_definition(
        self,
        *,
        repo_id: str,
        branch: str,
        path: str,
        content: bytes,
        commit_message: str,
        known_commit_sha: str | None = None,
    ) -> str:
        """Publish a built-in definition once and return its immutable commit."""
        return await self.publish_workflow_files(
            repo_id=repo_id,
            branch=branch,
            files={path: content},
            commit_message=commit_message,
            known_commit_sha=known_commit_sha,
        )

    async def publish_workflow_files(
        self,
        *,
        repo_id: str,
        branch: str,
        files: dict[str, bytes],
        commit_message: str,
        known_commit_sha: str | None = None,
    ) -> str:
        """Publish one immutable workflow definition and its pinned resources atomically."""
        if not files:
            raise ValueError("at least one workflow registry file is required")
        repo = await self.ensure_repo(
            repo_id,
            initial_readme="# Workflow registry\n\nManaged by the Tin Lite switchboard.\n",
        )
        if known_commit_sha and await self._documents_equal(
            repo, ref=known_commit_sha, documents=files
        ):
            return known_commit_sha
        head_sha = await self.head_sha(repo, branch)
        if head_sha is None:
            raise RuntimeError("code.storage repository has no canonical head")
        if await self._documents_equal(repo, ref=head_sha, documents=files):
            return head_sha
        try:
            commit = repo.create_commit(
                target_branch=branch,
                expected_head_sha=head_sha,
                commit_message=commit_message,
                author={"name": "Tin Switchboard", "email": "switchboard@tin.local"},
                ttl=300,
            )
            for file_path, file_content in sorted(files.items()):
                commit = commit.add_file(file_path, file_content)
            result = await commit.send()
            return result["commit_sha"]
        except Exception:
            latest_sha = await self.head_sha(repo, branch)
            if latest_sha and await self._documents_equal(repo, ref=latest_sha, documents=files):
                return latest_sha
            raise

    async def publish_system_wiki_document(
        self,
        *,
        repo_id: str,
        branch: str,
        path: str,
        content: bytes,
        commit_message: str,
    ) -> str:
        """Publish a system-wiki document without granting a project writer access."""
        return await self._publish_document_once(
            repo_id=repo_id,
            branch=branch,
            path=path,
            content=content,
            commit_message=commit_message,
            known_commit_sha=None,
            initial_readme="# System wiki\n\nRead-only knowledge managed by Tin.\n",
        )

    async def _publish_document_once(
        self,
        *,
        repo_id: str,
        branch: str,
        path: str,
        content: bytes,
        commit_message: str,
        known_commit_sha: str | None,
        initial_readme: str,
    ) -> str:
        repo = await self.ensure_repo(repo_id, initial_readme=initial_readme)
        if known_commit_sha and await self._file_equals(
            repo,
            ref=known_commit_sha,
            path=path,
            expected=content,
        ):
            return known_commit_sha
        head_sha = await self.head_sha(repo, branch)
        if head_sha is None:
            raise RuntimeError("code.storage repository has no canonical head")
        if await self._file_equals(repo, ref=head_sha, path=path, expected=content):
            return head_sha
        try:
            result = await (
                repo.create_commit(
                    target_branch=branch,
                    expected_head_sha=head_sha,
                    commit_message=commit_message,
                    author={"name": "Tin Switchboard", "email": "switchboard@tin.local"},
                    ttl=300,
                )
                .add_file(path, content)
                .send()
            )
            return result["commit_sha"]
        except Exception:
            latest_sha = await self.head_sha(repo, branch)
            if latest_sha and await self._file_equals(
                repo,
                ref=latest_sha,
                path=path,
                expected=content,
            ):
                return latest_sha
            raise

    async def create_canonical_commit(
        self,
        *,
        repo_id: str,
        branch: str,
        expected_head_sha: str,
        artifact_path: str,
        artifact: bytes,
        execution_key: str,
        run_id: str,
        workflow_key: str = "content.design_md",
    ) -> str:
        repo = await self.get_repo(repo_id)
        commit_message = f"{workflow_key} {run_id} [{execution_key}]"
        current_head_sha = await self.head_sha(repo, branch)
        if current_head_sha != expected_head_sha:
            recent = await repo.list_commits(branch=branch, limit=1, ttl=300)
            commits = recent.get("commits", [])
            if commits and commits[0].get("message") == commit_message:
                return commits[0]["sha"]
            raise RuntimeError("canonical project state changed before artifact commit")
        if await self._file_equals(
            repo,
            ref=expected_head_sha,
            path=artifact_path,
            expected=artifact,
        ):
            return expected_head_sha
        try:
            result = await (
                repo.create_commit(
                    target_branch=branch,
                    expected_head_sha=expected_head_sha,
                    commit_message=commit_message,
                    author={"name": "Tin Switchboard", "email": "switchboard@tin.local"},
                    ttl=300,
                )
                .add_file(artifact_path, artifact)
                .send()
            )
            return result["commit_sha"]
        except Exception:
            # Reconcile a crash after the remote commit succeeded but before its result was saved.
            recent = await repo.list_commits(branch=branch, limit=1, ttl=300)
            commits = recent.get("commits", [])
            if commits and commits[0].get("message") == commit_message:
                return commits[0]["sha"]
            raise

    async def get_task_branch_diff(
        self,
        *,
        repo_id: str,
        branch: str,
        base_branch: str,
        expected_base_sha: str,
    ) -> tuple[dict[str, Any], str]:
        """Return a bounded product projection and the exact unified diff to approve."""
        repo = await self.get_repo(repo_id)
        current_base_sha = await self.head_sha(repo, base_branch)
        if current_base_sha != expected_base_sha:
            raise RuntimeError("canonical project state changed before task review")
        result = await repo.get_branch_diff(
            branch=branch,
            base=base_branch,
            ephemeral=True,
            ephemeral_base=False,
            ttl=300,
        )
        files = result.get("files", [])
        filtered = result.get("filtered_files", [])
        if filtered or len(files) > _TASK_DIFF_MAX_FILES:
            raise RuntimeError("task diff is too large to review safely")
        total_bytes = sum(int(item.get("bytes", 0)) for item in files)
        if total_bytes > _TASK_DIFF_MAX_BYTES:
            raise RuntimeError("task diff is too large to review safely")
        projected_files: list[dict[str, Any]] = []
        raw_parts: list[str] = []
        for item in files:
            path = str(item.get("path", ""))
            old_path = item.get("old_path")
            if not _safe_repo_path(path) or (
                old_path is not None and not _safe_repo_path(str(old_path))
            ):
                raise RuntimeError("task diff contains an unsafe path")
            raw = str(item.get("raw", ""))
            raw_parts.append(raw)
            projected_files.append(
                {
                    "path": path,
                    "old_path": old_path,
                    "state": item.get("state"),
                    "bytes": int(item.get("bytes", 0)),
                    "patch": raw,
                }
            )
        raw_diff = "\n".join(part.rstrip("\n") for part in raw_parts if part).strip()
        if raw_diff:
            raw_diff += "\n"
        projection = {
            "stats": dict(result.get("stats", {})),
            "files": projected_files,
            "sha256": hashlib.sha256(raw_diff.encode()).hexdigest(),
        }
        return projection, raw_diff

    async def apply_task_diff(
        self,
        *,
        repo_id: str,
        branch: str,
        expected_head_sha: str,
        raw_diff: str,
        expected_diff_sha256: str,
        execution_key: str,
        run_id: str,
    ) -> str:
        if hashlib.sha256(raw_diff.encode()).hexdigest() != expected_diff_sha256:
            raise RuntimeError("task diff changed after review was requested")
        repo = await self.get_repo(repo_id)
        commit_message = f"project.task {run_id} [{execution_key}]"
        try:
            result = await repo.create_commit_from_diff(
                target_branch=branch,
                expected_head_sha=expected_head_sha,
                commit_message=commit_message,
                diff=raw_diff,
                author={"name": "Tin Switchboard", "email": "switchboard@tin.local"},
                ttl=300,
            )
            return result["commit_sha"]
        except Exception:
            recent = await repo.list_commits(branch=branch, limit=1, ttl=300)
            commits = recent.get("commits", [])
            if commits and commits[0].get("message") == commit_message:
                return commits[0]["sha"]
            raise

    async def publish_state_document(
        self,
        *,
        repo_id: str,
        branch: str,
        path: str,
        content: bytes,
        workflow_key: str,
        execution_key: str,
        run_id: str,
    ) -> tuple[str, bool]:
        """Publish one managed state document with optimistic concurrency and reconciliation."""
        return await self.publish_state_documents(
            repo_id=repo_id,
            branch=branch,
            documents={path: content},
            workflow_key=workflow_key,
            execution_key=execution_key,
            run_id=run_id,
        )

    async def publish_state_documents(
        self,
        *,
        repo_id: str,
        branch: str,
        documents: dict[str, bytes],
        workflow_key: str,
        execution_key: str,
        run_id: str,
    ) -> tuple[str, bool]:
        """Atomically publish managed state documents with optimistic reconciliation."""
        if not documents:
            raise ValueError("at least one state document is required")
        repo = await self.ensure_repo(repo_id)
        head_sha = await self.head_sha(repo, branch)
        if head_sha is None:
            raise RuntimeError("project state repository has no canonical head")
        if await self._documents_equal(repo, ref=head_sha, documents=documents):
            return head_sha, False
        commit_message = f"{workflow_key} {run_id} [{execution_key}]"
        try:
            commit = repo.create_commit(
                target_branch=branch,
                expected_head_sha=head_sha,
                commit_message=commit_message,
                author={"name": "Tin Switchboard", "email": "switchboard@tin.local"},
                ttl=300,
            )
            for path, content in sorted(documents.items()):
                commit = commit.add_file(path, content)
            result = await commit.send()
            return result["commit_sha"], True
        except Exception:
            latest = await repo.list_commits(branch=branch, limit=1, ttl=300)
            commits = latest.get("commits", [])
            if commits and commits[0].get("message") == commit_message:
                return commits[0]["sha"], True
            if commits and await self._documents_equal(
                repo, ref=commits[0]["sha"], documents=documents
            ):
                return commits[0]["sha"], False
            raise

    async def _documents_equal(self, repo: Repo, *, ref: str, documents: dict[str, bytes]) -> bool:
        for path, expected in documents.items():
            if not await self._file_equals(repo, ref=ref, path=path, expected=expected):
                return False
        return True

    def _generate_token(
        self,
        *,
        repo_id: str,
        scopes: list[str],
        subject: str,
        ttl: int,
        refs: list[list[Any]] | None = None,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": self._organization,
            "sub": subject,
            "repo": repo_id,
            "scopes": scopes,
            "iat": now,
            "exp": now + ttl,
        }
        if refs is not None:
            payload["refs"] = refs
        private_key = serialization.load_pem_private_key(self._private_key.encode(), password=None)
        key_type = type(private_key).__name__
        algorithm = "RS256" if "RSA" in key_type else "ES256"
        return jwt.encode(
            payload,
            private_key,
            algorithm=algorithm,
            headers={"alg": algorithm, "typ": "JWT"},
        )

    @staticmethod
    async def _file_equals(repo: Repo, *, ref: str, path: str, expected: bytes) -> bool:
        try:
            return await _read_file_with_retry(repo, path=path, ref=ref) == expected
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return False
            raise
        except ApiError as exc:
            if exc.status_code == 404:
                return False
            raise


async def _read_file_with_retry(
    repo: Repo,
    *,
    path: str,
    ref: str,
    ephemeral: bool = False,
) -> bytes:
    values: dict[str, Any] = {"path": path, "ref": ref, "ttl": 300}
    if ephemeral:
        values["ephemeral"] = True
    for attempt in range(_FILE_READ_ATTEMPTS):
        try:
            if _supports_eager_file_read(repo):
                return await _read_file_eager(repo, values=values)
            response = await repo.get_file_stream(**values)
            async with response:
                return await response.aread()
        except httpx.TransportError:
            if attempt == _FILE_READ_ATTEMPTS - 1:
                raise
            await asyncio.sleep(0.1 * (2**attempt))
    raise AssertionError("unreachable")


def _supports_eager_file_read(repo: Repo) -> bool:
    return all(
        hasattr(repo, attribute) for attribute in ("api_base_url", "api_version", "generate_jwt")
    )


async def _read_file_eager(repo: Repo, *, values: dict[str, Any]) -> bytes:
    """Buffer a bounded file without the SDK's prematurely closed stream wrapper."""
    token = repo.generate_jwt(  # type: ignore[attr-defined]
        repo.id, {"permissions": ["git:read"], "ttl": values["ttl"]}
    )
    params = {"path": values["path"], "ref": values["ref"]}
    if values.get("ephemeral"):
        params["ephemeral"] = "true"
    url = f"{repo.api_base_url}/api/v{repo.api_version}/repos/file"  # type: ignore[attr-defined]
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Code-Storage-Agent": get_user_agent(),
            },
        )
        response.raise_for_status()
        return response.content


def _basic_auth_header(token: str) -> str:
    encoded = base64.b64encode(f"t:{token}".encode()).decode()
    return f"Authorization: Basic {encoded}"


def _is_commit_sha(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value)
    )


def _safe_repo_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))
