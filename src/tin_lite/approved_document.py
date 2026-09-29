"""Resolve one approved answer page or public article at its exact project revision.

The authority matches an approved planned article (approved_article): the run's approved
state, its publication receipt, the review command where the workflow has one, and the
immutable code.storage bytes. Callers pin the returned source in their own admission
receipt. The page's own search listing (its frontmatter) travels beside the copy, never
inside it, so a delivery can keep the reviewed copy byte-for-byte.
"""

import hashlib
import json
import re
from uuid import UUID

from tin_lite import approved_article
from tin_lite.content_delivery import (
    ANSWER_PAGE_WORKFLOW_ID,
    PUBLIC_ARTICLE_WORKFLOW_ID,
    document_body,
    page_frontmatter,
)
from tin_lite.domain import ANSWER_PAGE_DIR, RunStatus
from tin_lite.project_files import safe_project_file_path
from tin_lite.workflow_review_store import digest

KINDS = {ANSWER_PAGE_WORKFLOW_ID: "answer_page", PUBLIC_ARTICLE_WORKFLOW_ID: "public_article"}
# The largest page each workflow saves: answer pages 150 KB, public articles 300 KB.
MAX_BYTES = {"answer_page": 150_000, "public_article": 300_000}
FOLDERS = {"answer_page": f"{ANSWER_PAGE_DIR}/", "public_article": "content/articles/"}
RECEIPTS = {"answer_page": "answer_page_commit", "public_article": "procedure_canonical_commit"}
METADATA_KEYS = ("meta_title", "meta_description")


def kind_for(run):
    return KINDS.get(getattr(run, "workflow_id", None))


def _page_path(path, kind):
    folder = FOLDERS[kind]
    return (
        isinstance(path, str)
        and safe_project_file_path(path)
        and path.startswith(folder)
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.md", path[len(folder) :]))
    )


def _metadata(values):
    """The search listing a site can reuse: plain one-line strings only."""
    return {
        key: " ".join(values[key].split())[:300]
        for key in METADATA_KEYS
        if isinstance(values.get(key), str) and values[key].strip()
    }


async def _review_command(executor, run_id):
    return await executor.fetchrow(
        "SELECT action, artifact, review_token FROM workflow_review_commands "
        "WHERE source_run_id=$1",
        run_id,
    )


async def select(*, database, storage, project_id, source_run_id):
    """Return the approved page's exact copy, listing and provenance, or refuse plainly."""
    run = await database.get_run(UUID(str(source_run_id)))
    kind = kind_for(run)
    if not run or run.project_id != project_id or kind is None:
        raise ValueError("Choose an approved page from this project.")
    if run.status != RunStatus.SUCCEEDED or run.review_decision != "approved":
        raise ValueError("Read and approve this page in Tin before using it.")
    published = await database.get_effect(f"{run.id}:{RECEIPTS[kind]}")
    if (
        not published
        or published.status != "completed"
        or not published.result
        or not run.canonical_commit_sha
        or published.result.get("canonical_commit_sha") != run.canonical_commit_sha
        or published.result.get("artifact_path") != run.artifact_path
        or not _page_path(run.artifact_path, kind)
    ):
        raise ValueError("The approved page's publication proof is unavailable.")
    publication_sha256 = None
    if kind == "public_article":
        # Public articles are approved through the review command; its token binds the
        # exact version that was read. An approval without one proves nothing.
        command = await _review_command(database.pool, run.id)
        if command is None:
            raise ValueError("The approved article's review proof is unavailable.")
        publication_sha256 = approved_article._verify_review(run, published.result, command)[
            "sha256"
        ]
    project = await database.get_project(project_id)
    raw = await storage.read_canonical_artifact(
        repo_id=project.state_repo_id, commit_sha=run.canonical_commit_sha, path=run.artifact_path
    )
    if len(raw) > MAX_BYTES[kind]:
        raise ValueError(
            f"The approved page is larger than {MAX_BYTES[kind] // 1000} KB, "
            "the most Tin adapts to a site."
        )
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if publication_sha256 is not None and source_sha256 != publication_sha256:
        raise ValueError("The approved page differs from its publication digest.")
    page, title = document_body(raw)
    listing, article = page_frontmatter(page)
    return {
        "source_kind": kind,
        "source_run_id": str(run.id),
        "source_revision": run.canonical_commit_sha,
        "source_path": run.artifact_path,
        "source_sha256": source_sha256,
        "publication_sha256": publication_sha256,
        "article": article,
        "article_sha256": hashlib.sha256(article.encode()).hexdigest(),
        "title": title,
        "page_metadata": _metadata(listing),
    }


async def guard(conn, *, project_id, source):
    """Recheck approval before run/budget/receipt commit under the project lock."""
    kind = source.get("source_kind")
    workflow_id = next((key for key, value in KINDS.items() if value == kind), None)
    approved = await conn.fetchrow(
        "SELECT artifact_path, review_version FROM workflow_runs "
        "WHERE id=$1 AND project_id=$2 AND workflow_id=$3 "
        "AND status='succeeded' AND review_decision='approved' "
        "AND canonical_commit_sha=$4",
        UUID(source["source_run_id"]),
        project_id,
        workflow_id,
        source["source_revision"],
    )
    if workflow_id is None or not approved or approved["artifact_path"] != source["source_path"]:
        raise ValueError("The selected page is not approved.")
    if kind != "public_article":
        return
    command = await _review_command(conn, UUID(source["source_run_id"]))
    artifact = command["artifact"] if command else None
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
        command is None
        or command["action"] != "approve"
        or artifact != expected
        or command["review_token"] != token
    ):
        raise ValueError("The selected article's review proof changed.")
