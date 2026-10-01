"""Create-only, atomic audit publication with lost-response reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime

from tin_lite.code_storage import CodeStorage
from tin_lite.organic_audit import AUDIT_POLICY, audit_policy, publication_contract
from tin_lite.publication import OutputConflictError, PublicationPendingError


def _sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{40}", value) is not None


async def publish_audit(
    *,
    storage: CodeStorage,
    repo_id: str,
    branch: str,
    run_id: str,
    documents: dict[str, bytes],
    intent: dict | None,
    save_intent: Callable[[dict], Awaitable[None]],
    validate_active: Callable[[], Awaitable[None]],
    policy_version: str = AUDIT_POLICY["version"],
    completion: bool = False,
) -> str:
    """The run's files per its pinned policy; from v12 that includes LATEST.json.

    LATEST.json points at the latest-started audit, so this run replaces it only when the one
    there didn't start later: a slow audit that finishes after a newer one leaves it alone. It
    is Tin's pointer, not a founder's file: an edit to it is replaced like any older summary,
    an exception to keeping later edits that applies to this path only. An answer completion
    never writes it (`publication_contract`).
    """
    paths, limits, replaceable = publication_contract(
        run_id, audit_policy(policy_version), completion=completion
    )
    return await publish_artifacts(
        storage=storage,
        repo_id=repo_id,
        branch=branch,
        documents=documents,
        paths=paths,
        limits=limits,
        message=f"organic.audit {run_id} [organic:{run_id}:publish]",
        intent=intent,
        save_intent=save_intent,
        validate_active=validate_active,
        replaceable=replaceable,
        supersedes=_not_older,
    )


def _not_older(existing: bytes, new: bytes) -> bool:
    """Whether this summary replaces LATEST.json: unless the one there started later.

    A file Tin can't read as a summary with a start time is replaced.
    """
    try:
        then = datetime.fromisoformat(json.loads(existing)["audited_at"])
        return not then > datetime.fromisoformat(json.loads(new)["audited_at"])
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return True


async def publish_artifacts(
    *,
    storage: CodeStorage,
    repo_id: str,
    branch: str,
    documents: dict[str, bytes],
    paths: dict[str, str],
    limits: dict[str, int],
    message: str,
    intent: dict | None,
    save_intent: Callable[[dict], Awaitable[None]],
    validate_active: Callable[[], Awaitable[None]],
    replaceable: frozenset[str] = frozenset(),
    supersedes: Callable[[bytes, bytes], bool] | None = None,
) -> str:
    """Shared create-only bundle writer; each caller owns its exact path/size contract.

    `replaceable` names stable pointer paths the bundle overwrites, such as the organic
    audit's LATEST.json; every other path must not exist yet. With `supersedes`, a pointer is
    written only when it supersedes the file at the destination head, or there is none; the
    rest of the bundle publishes either way. A retry repeats its saved intent's choice until
    reconciliation shows that attempt never landed.
    """
    if set(documents) != set(paths.values()):
        raise ValueError("Publication must contain exactly its declared run-scoped artifacts.")
    for name, path in paths.items():
        content = documents[path]
        content.decode("utf-8")
        if not 0 < len(content) <= limits[name]:
            raise ValueError("Audit artifact exceeds its safe read contract.")
    repo = await storage.get_repo(repo_id)
    async with asyncio.timeout(120):
        head = await storage.head_sha(repo, branch)
        if not _sha(head):
            raise PublicationPendingError("Audit destination has no canonical revision.")
        if intent is not None:
            saved = intent.get("manifest") if isinstance(intent.get("manifest"), dict) else {}
            attempted = {
                path: content
                for path, content in documents.items()
                if path not in replaceable or path in saved
            }
            if intent.get("manifest") != _manifest(attempted) or not _sha(intent.get("parent")):
                raise PublicationPendingError("Audit publication intent changed.")
            original = await _reconcile(
                storage,
                repo,
                head=head,
                parent=intent["parent"],
                message=message,
                documents=attempted,
            )
            if original is not None:
                return original
        planned = {}
        for path, content in documents.items():
            if path in replaceable:
                if supersedes is None or await _supersedes(
                    storage, repo, head=head, path=path, content=content, supersedes=supersedes
                ):
                    planned[path] = content
                continue
            if await storage._publication_file(repo, ref=head, path=path) is not None:
                raise OutputConflictError(
                    "An audit output path already exists; it was left unchanged."
                )
            planned[path] = content
        documents, manifest = planned, _manifest(planned)
        await validate_active()
        await save_intent({"version": 1, "manifest": manifest, "parent": head})
        await validate_active()
        try:
            builder = repo.create_commit(
                target_branch=branch,
                expected_head_sha=head,
                commit_message=message,
                author={"name": "Tin Switchboard", "email": "switchboard@tin.local"},
                ttl=300,
            )
            for path, content in documents.items():
                builder.add_file(path, content)
            result = await builder.send()
            if not _sha(result.get("commit_sha")):
                raise PublicationPendingError("Audit publication returned no commit identity.")
            return result["commit_sha"]
        except Exception as exc:
            raise PublicationPendingError("Audit publication must reconcile before retry.") from exc


def _manifest(documents: dict[str, bytes]) -> dict[str, str]:
    return {path: hashlib.sha256(content).hexdigest() for path, content in documents.items()}


async def _supersedes(storage, repo, *, head, path, content, supersedes) -> bool:
    try:
        existing = await storage._publication_file(repo, ref=head, path=path)
    except OutputConflictError:
        return False  # Not a file Tin can compare, such as a folder; it stays as it is.
    return existing is None or supersedes(existing[1], content)


async def _reconcile(
    storage, repo, *, head: str, parent: str, message: str, documents: dict[str, bytes]
) -> str | None:
    cursor = None
    next_sha = head
    nodes: dict[str, dict] = {}
    visited: set[str] = set()
    cursors: set[str] = set()
    for _ in range(20):
        if next_sha == parent:
            return None
        params = {"ref": head, "limit": "100"}
        if cursor is not None:
            params["cursor"] = cursor
        result = await storage._publication_json(repo, "commits", **params)
        for commit in result.get("commits", []):
            if not _sha(commit.get("sha")):
                raise PublicationPendingError("Audit publication history is invalid.")
            nodes[commit["sha"]] = commit
        while next_sha in nodes and next_sha != parent:
            if next_sha in visited:
                raise PublicationPendingError("Audit publication history is cyclic.")
            visited.add(next_sha)
            commit = nodes[next_sha]
            parents = commit.get("parent_shas")
            if not isinstance(parents, list) or len(parents) != 1 or not _sha(parents[0]):
                raise PublicationPendingError(
                    "Audit publication history is not a proven parent chain."
                )
            if commit.get("message") == message:
                if parents != [parent]:
                    raise PublicationPendingError(
                        "Audit publication marker has a different parent."
                    )
                for path, content in documents.items():
                    entry = await storage._publication_file(
                        repo, ref=next_sha, path=path, max_bytes=max(1_000_000, len(content))
                    )
                    if entry != ("100644", content):
                        raise PublicationPendingError(
                            "Audit publication marker has different output."
                        )
                diff = await repo.get_commit_diff(sha=next_sha, ttl=300)
                files = diff.get("files", [])
                if (
                    diff.get("filtered_files")
                    or {f.get("path") for f in files} != set(documents)
                    or any(f.get("old_path") not in {None, f.get("path")} for f in files)
                ):
                    raise PublicationPendingError("Audit publication changes unexpected paths.")
                return next_sha
            next_sha = parents[0]
        if next_sha == parent:
            return None
        cursor = result.get("next_cursor")
        if not result.get("has_more") or not isinstance(cursor, str) or cursor in cursors:
            raise PublicationPendingError("Audit publication history is incomplete.")
        cursors.add(cursor)
    raise PublicationPendingError("Audit publication reconciliation reached its history limit.")
