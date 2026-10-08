"""qa.feedback_to_fix's plan: PLAN.md and the copy fix website.change applies after approval.

qa.feedback_to_fix reads public threads about the product and the connected repository
read-only, and opens no pull request. It writes `reports/feedback-to-fix/{run_id}/PLAN.md`: the
plan in prose, for a `patch` outcome a fenced JSON patch between the markers below, and a
closing `feedback-to-fix` summary block. website.change (`source: feedback`) applies the patch
once the founder approves it in Decisions, against `base_sha`.

The patch, `feedback-fix-patch/1`, is a contract shared with website.change:

    {"schema": "feedback-fix-patch/1", "repository": "owner/repo", "base_ref": "<branch>",
     "base_sha": "<sha the plan read>", "route": "/", "summary": "...",
     "kind": "unclear_what_it_is", "people": 2, "quotes": ["https://..."],
     "edits": [{"path": "...", "before": "<old wording>", "after": "<new wording>"}],
     "files": [{"path": "...", "action": "update", "content": "<full file text>"}],
     "caps": {"max_files": 3}}

A copy fix changes wording that already exists: at most three files, each an update, never
package.json, a lockfile, or CI and deploy settings. `edits` are what the founder reads in
Decisions; each `after` must appear in its file. This module only checks the shape;
website.change checks the files against the repository at `base_sha` before it writes anything.
"""

from __future__ import annotations

import hashlib
import json
import re

from tin_lite.blog_index_plan import REPOSITORY, SHA, _block, _fenced, check_path

VALIDATOR = "feedback-fix-plan.v1"
WORKFLOW_KEY = "qa.feedback_to_fix"
PATH_TEMPLATE = "reports/feedback-to-fix/{run_id}/PLAN.md"
SCHEMA = "feedback-fix-patch/1"
START = "<!-- feedback-fix-patch.json:start -->"
END = "<!-- feedback-fix-patch.json:end -->"
MAX_FILES = 3
MAX_EDITS = 6
MAX_EDIT_CHARS = 500
MAX_QUOTES = 30
MAX_CONTENT_BYTES = 150_000
MAX_PLAN_BYTES = 200_000
KEYS = {
    "schema",
    "repository",
    "base_ref",
    "base_sha",
    "route",
    "summary",
    "kind",
    "people",
    "quotes",
    "edits",
    "files",
    "caps",
}
KINDS = {"unclear_what_it_is", "objection", "unanswered_question"}
OUTCOMES = {"patch", "report_only", "insufficient_data"}
ROUTE = re.compile(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*|/")
QUOTE_URL = re.compile(r"https://[A-Za-z0-9.-]*[A-Za-z0-9][A-Za-z0-9.-]*(?:[/?#]\S*)?")


def _text(value, *, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def check_patch(patch: object) -> dict:
    """The parsed feedback-fix-patch/1 object, or ValueError naming the broken rule."""
    if not isinstance(patch, dict) or set(patch) != KEYS:
        raise ValueError("The patch must have exactly the feedback-fix-patch/1 keys.")
    if patch["schema"] != SCHEMA:
        raise ValueError(f"The patch schema must be {SCHEMA}.")
    if not isinstance(patch["repository"], str) or not REPOSITORY.fullmatch(patch["repository"]):
        raise ValueError("The patch repository must be owner/repo.")
    ref = patch["base_ref"]
    if (
        not isinstance(ref, str)
        or not 1 <= len(ref) <= 255
        or ref.startswith("-")
        or any(c.isspace() or ord(c) < 32 for c in ref)
    ):
        raise ValueError("The patch base_ref must be a branch name.")
    if not isinstance(patch["base_sha"], str) or not SHA.fullmatch(patch["base_sha"]):
        raise ValueError("The patch base_sha must be the commit the plan read.")
    if not isinstance(patch["route"], str) or not ROUTE.fullmatch(patch["route"]):
        raise ValueError("The patch route must be the site path of the changed page, such as /.")
    if not _text(patch["summary"], limit=500):
        raise ValueError("The patch summary is one sentence of at most 500 characters.")
    if patch["kind"] not in KINDS:
        raise ValueError("A copy fix answers an unclear product, an objection or a question.")
    if type(patch["people"]) is not int or patch["people"] < 2:
        raise ValueError("A copy fix needs at least 2 different people.")
    quotes = patch["quotes"]
    if (
        not isinstance(quotes, list)
        or not 2 <= len(quotes) <= MAX_QUOTES
        or not all(isinstance(url, str) and QUOTE_URL.fullmatch(url) for url in quotes)
    ):
        raise ValueError(f"The patch links 2 to {MAX_QUOTES} quotes, each an https link.")
    if patch["caps"] != {"max_files": MAX_FILES}:
        raise ValueError(f"The patch caps must be {{'max_files': {MAX_FILES}}}.")
    files = patch["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError(f"A copy fix changes one to {MAX_FILES} files.")
    contents, size = {}, 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "action", "content"}:
            raise ValueError("Each planned file has exactly path, action and content.")
        path = check_path(item["path"])
        if path in contents:
            raise ValueError("A planned file appears twice.")
        if item["action"] != "update":
            raise ValueError("A copy fix updates existing files; it never creates one.")
        if not isinstance(item["content"], str) or not item["content"]:
            raise ValueError("A planned file carries its full text.")
        contents[path] = item["content"]
        size += len(item["content"].encode())
    if size > MAX_CONTENT_BYTES:
        raise ValueError(f"The planned files exceed {MAX_CONTENT_BYTES} bytes.")
    edits = patch["edits"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
        raise ValueError(f"A copy fix shows one to {MAX_EDITS} edits.")
    for edit in edits:
        if (
            not isinstance(edit, dict)
            or set(edit) != {"path", "before", "after"}
            or not _text(edit["before"], limit=MAX_EDIT_CHARS)
            or not _text(edit["after"], limit=MAX_EDIT_CHARS)
        ):
            raise ValueError(
                f"Each edit is a path with its before and after wording, at most "
                f"{MAX_EDIT_CHARS} characters each."
            )
        if edit["path"] not in contents:
            raise ValueError("Each edit names one of the planned files.")
        if edit["before"] == edit["after"]:
            raise ValueError("An edit changes the wording.")
        if edit["after"] not in contents[edit["path"]]:
            raise ValueError(f"An edit's new wording is not in {edit['path']}.")
    return patch


def parse(content: bytes | str) -> dict:
    """{"patch": ... or None, "summary": ...} from a PLAN.md, or ValueError naming the rule."""
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    text = text.replace("\r\n", "\n")
    if len(text.encode()) > MAX_PLAN_BYTES:
        raise ValueError(f"The plan exceeds {MAX_PLAN_BYTES} bytes.")
    if not re.search(r"^## Outcome$", text, re.M):
        raise ValueError("The plan starts its report with an Outcome section.")
    blocks = _fenced(text, "feedback-to-fix")
    if not blocks:
        raise ValueError("The plan ends with a feedback-to-fix summary block.")
    try:
        summary = json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        raise ValueError("The feedback-to-fix summary block is not JSON.") from exc
    if not isinstance(summary, dict) or summary.get("outcome") not in OUTCOMES:
        raise ValueError("The summary's outcome is patch, report_only or insufficient_data.")
    if START not in text and END not in text:
        if summary["outcome"] == "patch":
            raise ValueError("A patch outcome holds exactly one feedback-fix-patch.json block.")
        if not _text(summary.get("reason"), limit=500):
            raise ValueError("A plan without a patch names its reason.")
        return {"patch": None, "summary": summary}
    if summary["outcome"] != "patch":
        raise ValueError("Only a patch outcome carries a feedback-fix-patch.json block.")
    raw = _block(text, START, END)
    if raw is None:
        raise ValueError("The plan holds exactly one feedback-fix-patch.json block.")
    try:
        patch = check_patch(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise ValueError("The feedback-fix-patch.json block is not JSON.") from exc
    return {"patch": patch, "summary": summary}


def validate(content: bytes | str) -> None:
    parse(content)


def quotes_digest(patch: dict) -> str:
    """What identifies a copy fix across runs: the quotes behind it and the files it changes.

    The same people on the same copy is the same change, whatever wording a later run picks,
    so a change the founder declined is not proposed again until new voices join it.
    """
    basis = json.dumps(
        {
            "quotes": sorted(set(patch["quotes"])),
            "files": sorted(item["path"] for item in patch["files"]),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(basis.encode()).hexdigest()
