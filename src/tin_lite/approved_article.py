"""Resolve one approved content.generate article at its exact project revision.

The source is usable by workflows other than repository delivery. A run's approved
state, review decision, publication receipt, and immutable code.storage bytes form
the authority; callers pin the returned source in their own admission receipt.
"""

import hashlib
import json
import re
from uuid import UUID

from tin_lite import content_draft, content_editorial_judgment
from tin_lite.content_delivery import DRAFT_WORKFLOW_ID, article_body
from tin_lite.domain import RunStatus
from tin_lite.workflow_review_store import digest
from tin_lite.writing_style import STYLE_PATH

MAX_ARTICLE_BYTES = 80_000
MAX_STYLE_BYTES = 24_000


async def discover(database, project_id):
    """List approved drafts by title without requiring a delivery integration."""
    rows = await database.pool.fetch(
        "SELECT r.id, COALESCE(p.result->'item'->>'title', 'Approved article') AS title "
        "FROM workflow_runs r LEFT JOIN effect_receipts p "
        "ON p.execution_key='content-draft:' || r.id::text || ':prepare' AND p.status='completed' "
        "WHERE r.project_id=$1 AND r.workflow_id=$2 "
        "AND r.status='succeeded' AND r.review_decision='approved' "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT 100",
        project_id,
        DRAFT_WORKFLOW_ID,
    )
    return [{"run_id": str(row["id"]), "title": row["title"]} for row in rows]


def _review_artifact(run, publication):
    checkpoint = publication.get("checkpoint") or {}
    sha256 = checkpoint.get("sha256")
    if sha256 is not None and (
        not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)
    ):
        raise ValueError("The approved article's publication digest is invalid.")
    return {
        "run_id": str(run.id),
        "path": run.artifact_path,
        "revision": run.canonical_commit_sha,
        "sha256": sha256,
        "assessment": content_editorial_judgment.no_draft(publication),
    }


def _verify_review(run, publication, command):
    artifact = _review_artifact(run, publication)
    if artifact["assessment"]:
        raise ValueError("This run saved an assessment, not an approved article.")
    # Older approved runs can lack the command row. When present, the command must
    # prove that this review version approved this exact published artifact.
    if command is not None:
        saved = command["artifact"]
        if isinstance(saved, str):
            saved = json.loads(saved)
        expected_token = digest(
            {"run": str(run.id), "version": run.review_version, "artifact": artifact}
        )
        if (
            command["action"] != "approve"
            or saved != artifact
            or command["review_token"] != expected_token
        ):
            raise ValueError("The approved article's review proof differs from its publication.")
    return artifact


async def select(*, database, storage, project_id, source_run_id, include_style=False):
    """Return bounded article bytes and provenance from one approved source run."""
    run = await database.get_run(UUID(str(source_run_id)))
    if not run or run.project_id != project_id or run.workflow_id != DRAFT_WORKFLOW_ID:
        raise ValueError("Choose an article draft from this project.")
    if run.status != RunStatus.SUCCEEDED or run.review_decision != "approved":
        raise ValueError("Read and approve this article in Tin before using it.")
    prepared = await database.get_effect(content_draft.receipt_key(run.id))
    published = await database.get_effect(f"{run.id}:procedure_canonical_commit")
    if (
        not prepared
        or prepared.status != "completed"
        or not prepared.result
        or not published
        or published.status != "completed"
        or not published.result
        or published.result.get("canonical_commit_sha") != run.canonical_commit_sha
        or published.result.get("artifact_path") != run.artifact_path
        or run.artifact_path != content_draft.PATH_TEMPLATE.format(run_id=run.id)
    ):
        raise ValueError("The approved article's publication proof is unavailable.")
    command = await database.pool.fetchrow(
        "SELECT action, artifact, review_token FROM workflow_review_commands "
        "WHERE source_run_id=$1",
        run.id,
    )
    artifact = _verify_review(run, published.result, command)
    project = await database.get_project(project_id)
    raw = await storage.read_canonical_artifact(
        repo_id=project.state_repo_id, commit_sha=run.canonical_commit_sha, path=run.artifact_path
    )
    if len(raw) > MAX_ARTICLE_BYTES:
        raise ValueError("The approved article exceeds its size limit.")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if artifact["sha256"] is not None and source_sha256 != artifact["sha256"]:
        raise ValueError("The approved article differs from its publication digest.")
    article, title = article_body(raw, prepared.result)
    source = {
        "source_run_id": str(run.id),
        "source_revision": run.canonical_commit_sha,
        "source_path": run.artifact_path,
        "source_sha256": source_sha256,
        "publication_sha256": artifact["sha256"],
        "article": article,
        "article_sha256": hashlib.sha256(article.encode()).hexdigest(),
        "title": title,
        "item": prepared.result["item"],
        "program_id": prepared.result["program_id"],
        "due_date": prepared.result["due_date"],
    }
    style = prepared.result.get("style")
    if include_style and style is not None and not isinstance(style, dict):
        raise ValueError("The approved article's writing style reference is invalid.")
    metadata = (
        {key: style.get(key) for key in ("path", "revision", "sha256")}
        if isinstance(style, dict)
        else {"path": None, "revision": None, "sha256": None}
    )
    if include_style and metadata["sha256"] is not None:
        if (
            metadata["path"] != STYLE_PATH
            or not isinstance(metadata["revision"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", metadata["revision"])
            or not isinstance(metadata["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
        ):
            raise ValueError("The approved article's writing style reference is invalid.")
        content = await storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=metadata["revision"],
            path=metadata["path"],
        )
        if len(content) > MAX_STYLE_BYTES:
            raise ValueError("The approved article's writing style exceeds its size limit.")
        if hashlib.sha256(content).hexdigest() != metadata["sha256"]:
            raise ValueError("The approved article's writing style differs from its digest.")
        metadata["content"] = content.decode("utf-8")
    source["style"] = metadata
    return source


async def guard(conn, *, project_id, source):
    """Recheck approval before run/budget/receipt commit under the project lock."""
    approved = await conn.fetchrow(
        "SELECT artifact_path, review_version FROM workflow_runs "
        "WHERE id=$1 AND project_id=$2 AND workflow_id=$3 "
        "AND status='succeeded' AND review_decision='approved' "
        "AND canonical_commit_sha=$4",
        UUID(source["source_run_id"]),
        project_id,
        DRAFT_WORKFLOW_ID,
        source["source_revision"],
    )
    if not approved or approved["artifact_path"] != source["source_path"]:
        raise ValueError("The selected article is not approved.")
    command = await conn.fetchrow(
        "SELECT action, artifact, review_token FROM workflow_review_commands "
        "WHERE source_run_id=$1",
        UUID(source["source_run_id"]),
    )
    if command is not None:
        artifact = command["artifact"]
        if isinstance(artifact, str):
            artifact = json.loads(artifact)
        expected = {
            "run_id": source["source_run_id"],
            "path": source["source_path"],
            "revision": source["source_revision"],
            "sha256": source["publication_sha256"],
            "assessment": False,
        }
        token = digest(
            {
                "run": source["source_run_id"],
                "version": approved["review_version"],
                "artifact": expected,
            }
        )
        if (
            command["action"] != "approve"
            or artifact != expected
            or command["review_token"] != token
        ):
            raise ValueError("The selected article's review proof changed.")
