"""A bounded editorial decision, bound to the ordinary two-file publication proof.

Validation proves the declared decision and source accounting, not editorial truth.
No new model service, progress table, or workflow engine is needed.
"""

import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from tin_lite import content_draft

SCHEMA = "content-editorial-check.v1"
NO_DRAFT = frozenset({"already_covered", "needs_replanning", "insufficient_evidence"})
LABELS = {
    "already_covered": "Already covered",
    "needs_replanning": "Brief needs revision",
    "insufficient_evidence": "Coverage could not be established",
}


class ComparedPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=10, max_length=2000)
    status: Literal["inspected", "unavailable"]
    coverage: str = Field(min_length=20, max_length=1200)


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: Literal["draft", "already_covered", "needs_replanning", "insufficient_evidence"]
    rationale: str = Field(min_length=30, max_length=1600)
    reader_gain: str = Field(max_length=1600)
    change_scope: str = Field(max_length=1600)
    compared_pages: list[ComparedPage] = Field(max_length=12)


def page_identity(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Compare public HTTP(S) pages, not local paths or authenticated URLs.")
    return (parsed.hostname.removeprefix("www.").lower(), parsed.path.rstrip("/"), parsed.query)


def parse(notes, context):
    if context.get("output_validator") != content_draft.EDITORIAL_VALIDATOR:
        return None
    content_draft.validate_notes(notes, context)
    blocks = re.findall(r"(?m)^## Editorial judgment\n+```json\n(.*?)\n```", notes.decode(), re.S)
    if len(blocks) != 1:
        raise ValueError("Generation notes need one structured editorial judgment.")

    # Reject duplicate keys rather than letting a conflicting verdict be silently replaced.
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Editorial judgment contains duplicate fields.")
            value[key] = item
        return value

    judgment = Judgment.model_validate(json.loads(blocks[0], object_pairs_hook=object_pairs))
    identities = [page_identity(page.url) for page in judgment.compared_pages]
    if len(set(identities)) != len(identities):
        raise ValueError("List each compared page once, not duplicate anchors.")
    inspected = [page for page in judgment.compared_pages if page.status == "inspected"]
    host = context.get("host", "").removeprefix("www.").lower()
    if judgment.outcome == "already_covered" and not _supports_covered(inspected, host, context):
        # A page that couldn't be read is no evidence either way. "Already covered" needs the
        # site's own coverage read (the destination, for an update); without it the honest
        # result is that coverage couldn't be established, not a failed run.
        judgment = judgment.model_copy(update={"outcome": "insufficient_evidence"})
    if judgment.outcome in {"draft", "already_covered"} and not inspected:
        raise ValueError(
            "Inspect current coverage before declaring a gap or an already-covered topic."
        )
    if (
        host
        and judgment.outcome in {"draft", "already_covered"}
        and not any(page_identity(page.url)[0] == host for page in inspected)
    ):
        raise ValueError(
            "Inspect the planned site's own coverage before deciding to draft or skip."
        )
    if judgment.outcome == "draft":
        if min(len(judgment.reader_gain.strip()), len(judgment.change_scope.strip())) < 30:
            raise ValueError("Explain the concrete reader benefit and bounded change scope.")
        if context["item"]["action"] == "update_page" and page_identity(
            context["item"]["destination"]
        ) not in [page_identity(page.url) for page in inspected]:
            raise ValueError(
                "Inspect the exact update destination before drafting its replacement."
            )
    elif judgment.reader_gain or judgment.change_scope:
        raise ValueError("A no-draft assessment must not also declare proposed article copy.")
    return {"schema": SCHEMA, **judgment.model_dump()}


def _supports_covered(inspected, host, context):
    """Whether the read pages can show the brief is already covered on the planned site."""
    read = [page_identity(page.url) for page in inspected]
    if host and not any(identity[0] == host for identity in read):
        return False
    item = context.get("item") or {}
    if item.get("action") == "update_page" and item.get("destination"):
        return page_identity(item["destination"]) in read
    return bool(read)


def assessment_document(judgment):
    return f"# Content assessment\n\n{judgment['rationale']}\n".encode()


def validate_pair(article, notes, context):
    judgment = parse(notes, context)
    if judgment is None:
        return None
    content_draft.validate_artifact(article, context)
    if judgment["outcome"] in NO_DRAFT:
        if article.strip() != assessment_document(judgment).strip():
            raise ValueError("A no-draft result contains only its assessment, not article copy.")
    elif article.startswith(b"# Content assessment\n"):
        raise ValueError("A draft judgment requires article copy, not an assessment.")
    return judgment


def no_draft(publication):
    judgment = (publication or {}).get("content_editorial") or {}
    return judgment.get("schema") == SCHEMA and judgment.get("outcome") in NO_DRAFT


def covering_page(judgment, host=None):
    """The page that already covers an `already_covered` brief, or None.

    The judgment lists the pages it compared, closest first, and validation requires every one
    to be inspected and at least one to be on the planned site; the first on the site (else the
    first listed) is the covering page.
    """
    if not isinstance(judgment, dict) or judgment.get("outcome") != "already_covered":
        return None
    pages = [
        page["url"]
        for page in judgment.get("compared_pages") or []
        if isinstance(page, dict) and page.get("status") == "inspected" and page.get("url")
    ]
    own = (host or "").removeprefix("www.").lower()
    for url in pages:
        try:
            if own and page_identity(url)[0] == own:
                return url
        except ValueError:
            continue
    return pages[0] if pages else None


async def saved(database, run):
    if str(run.workflow_id) != "00000000-0000-4000-8000-000000000031":
        return None
    receipt = await database.get_effect(f"{run.id}:procedure_canonical_commit")
    if (
        receipt
        and receipt.status == "completed"
        and receipt.result
        and receipt.result.get("canonical_commit_sha") == run.canonical_commit_sha
        and receipt.result.get("artifact_path") == run.artifact_path
    ):
        return receipt.result.get("content_editorial")
    return None
