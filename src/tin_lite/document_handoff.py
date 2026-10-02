"""What a founder's agent needs to revise a document a run produced.

Pure facts from the run projection: where the document is, the founder's link to it, and the
one existing, member-authorized route that changes it safely. Reading these fields never
reads storage, writes a file, approves a review or grants new authority.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlencode

from tin_lite.domain import EMAIL_CAMPAIGN_WORKFLOW_NAME, RunStatus
from tin_lite.product_urls import dashboard_url
from tin_lite.workflow_reviews import SUPPORTED_IDS

# MCP reads and commits UTF-8 text only; other outputs (video, images) have no edit route.
TEXT_SUFFIXES = frozenset(
    {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".mmd", ".yaml", ".yml", ".html"}
)
ONBOARDING_EXECUTORS = frozenset({"growth.onboarding", "growth.onboarding_plan"})
CURRENT = "<revision from list_project_files>"
NEW_REQUEST = "<new UUID; reuse it only to retry this same call>"


def review_pending(run: Any) -> bool:
    status = getattr(run.status, "value", run.status)
    return status == RunStatus.NEEDS_INPUT.value and bool(getattr(run, "review_required", False))


def document_url(settings: Any, run: Any) -> str:
    if (getattr(run, "artifact_path", "") or "").startswith(
        "social/x-drafts/"
    ) and run.artifact_path.endswith(".json"):
        query = urlencode({"project": str(run.project_id), "x_draft": run.artifact_path})
        return f"{dashboard_url(settings)}/files?{query}"
    return f"{dashboard_url(settings)}/document/{run.id}?project={run.project_id}"


def file_url_template(settings: Any, run: Any) -> str:
    """The founder's link to a revised copy; {revision} is the revision a commit returns."""
    query = urlencode({"project": str(run.project_id), "path": run.artifact_path})
    return f"{dashboard_url(settings)}/file?{query}&revision={{revision}}"


def _edit_file(settings: Any, run: Any) -> dict[str, Any]:
    project, path = str(run.project_id), run.artifact_path
    return {
        "direct_edit": True,
        "before": [
            {
                "tool": "list_project_files",
                "arguments": {"project_id": project},
                "use": "Its `revision` is the current project revision.",
            },
            {
                "tool": "read_project_file",
                "arguments": {"project_id": project, "path": path, "revision": CURRENT},
                "use": (
                    "Start from the current text. If it differs from this run's revision, "
                    "someone edited it since; keep their changes."
                ),
            },
        ],
        "tool": "commit_project_changes",
        "arguments": {
            "project_id": project,
            "expected_revision": CURRENT,
            "request_id": NEW_REQUEST,
            "message": "<what changed and why, under 240 bytes>",
            "changes": [
                {"operation": "upsert", "path": path, "content": "<the complete revised text>"}
            ],
        },
        "after": (
            "commit_project_changes returns the new `revision`. Give the founder "
            "`revised_url` with that revision filled in; `review_url` keeps showing this "
            "run's copy. A stale expected_revision fails with conflict: list, read and "
            "commit again with a new request_id."
        ),
        "revised_url": file_url_template(settings, run),
    }


def revise(settings: Any, run: Any, proposal: Any = None) -> dict[str, Any]:
    """The route that changes this run's document, or why none exists yet.

    `proposal` is the run's capture revision contract (capture_revisions.contract), when the
    caller has read it: a 1.2.0 brand or style proposal waiting in Decisions.
    """
    project, path = str(run.project_id), run.artifact_path
    executor = getattr(run, "executor", None)
    pending = review_pending(run)
    if pending and proposal is not None:
        return {
            "direct_edit": False,
            "reason": (
                "Approval applies the exact version the founder reads in Decisions, so the "
                "proposal changes only through Tin's checked revision route."
            ),
            "before": [
                {
                    "tool": "get_workflow_review",
                    "arguments": {"run_id": str(run.id)},
                    "use": "Read `review_token` and the proposal files in `documents`.",
                }
            ],
            "tool": "revise_capture_proposal",
            "arguments": {
                "run_id": str(run.id),
                "review_token": "<review_token from get_workflow_review>",
                "request_id": NEW_REQUEST,
                "files": [
                    {"path": item, "content": "<the complete revised text>"}
                    for item in proposal.paths
                ],
            },
            "keeps_review": True,
            "metered": False,
            "after": (
                "The run keeps waiting in Decisions with the revised text. The founder approves "
                "or discards it there; give them `review_url`."
            ),
        }
    if pending and executor in ONBOARDING_EXECUTORS:
        return {
            "direct_edit": False,
            "tool": "record_onboarding_picks",
            "reason": "The plan changes through the founder's picks, not through file edits.",
        }
    if pending and getattr(run, "workflow_id", None) in SUPPORTED_IDS:
        return {
            "direct_edit": False,
            "reason": (
                "Approval and delivery use the saved copy at this revision, so an edit to the "
                "file would not reach them. Ask Tin for a new version of the same piece."
            ),
            "before": [
                {
                    "tool": "get_workflow_review",
                    "arguments": {"run_id": str(run.id)},
                    "use": "Read `review_token` and check `can_request_changes`.",
                }
            ],
            "tool": "request_workflow_changes",
            "arguments": {
                "run_id": str(run.id),
                "feedback": "<the founder's requested changes, in one pass>",
                "review_token": "<review_token from get_workflow_review>",
                "request_id": NEW_REQUEST,
            },
            "keeps_review": True,
            "metered": True,
            "after": (
                "Tin drafts a new version of the same piece and the review moves to it. "
                "Follow the returned run_id with get_run for its review link."
            ),
        }
    if pending:
        return {
            "direct_edit": False,
            "tool": None,
            "reason": (
                "No revision route exists while this review is open. Approval and delivery "
                "use the saved copy at this revision, so an edit to the file would not reach "
                "them. Once the founder decides in Decisions, the file can be edited here; "
                "the decided copy stays as it was."
            ),
        }
    if executor == "content.plan" and getattr(run, "project_workflow_id", None):
        program = str(run.project_workflow_id)
        return {
            "direct_edit": False,
            "reason": (
                "This file is a snapshot. Later batches come from the content program in "
                "My system, so revise the program instead."
            ),
            "before": [
                {
                    "tool": "read_content_plan",
                    "arguments": {"project_id": project, "project_workflow_id": program},
                    "use": "Read the complete roadmap and its `revision`.",
                }
            ],
            "tool": "edit_content_plan",
            "arguments": {
                "project_id": project,
                "project_workflow_id": program,
                "expected_revision": "<revision from read_content_plan>",
                "request_id": NEW_REQUEST,
                "plan": "<the complete roadmap with only future batches changed>",
            },
        }
    if executor == EMAIL_CAMPAIGN_WORKFLOW_NAME:
        return {
            "direct_edit": False,
            "reason": "Sent emails stay as sent; only follow-ups not yet sent can change.",
            "tool": "revise_email_campaign",
            "arguments": {
                "run_id": str(run.id),
                "request_id": NEW_REQUEST,
                "follow_up_body": "<the replacement follow-up copy>",
            },
            "keeps_review": True,
        }
    if PurePosixPath(path).suffix.casefold() not in TEXT_SUFFIXES:
        return {
            "direct_edit": False,
            "tool": None,
            "reason": (
                "MCP edits UTF-8 text files only. Start the workflow again with new "
                "direction to change this file."
            ),
        }
    route = _edit_file(settings, run)
    if getattr(run, "review_decision", None):
        route["note"] = (
            "The founder already decided on this copy. An edit changes this file in Tin only; "
            "the decided copy, and any pull request or applied document made from it, stay "
            "as they were. get_run's content_delivery names a pull request if one exists."
        )
    return route


def document_handoff(settings: Any, run: Any, proposal: Any = None) -> dict[str, Any]:
    """Fields added to a run's result link: the review link, the review state and the route."""
    return {
        "review_url": document_url(settings, run),
        "review_pending": review_pending(run),
        "review_decision": getattr(run, "review_decision", None),
        "revise": revise(settings, run, proposal),
    }
