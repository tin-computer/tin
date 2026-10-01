"""One selected plan item, one separately addressable review draft; no execution engine."""

from __future__ import annotations

import json
import re
from uuid import UUID

from tin_lite.organic_audit import digest

KEY = "content.generate"
VALIDATOR = "content-draft.v1"
CLEAN_VALIDATOR = "content-draft.v2"
EDITORIAL_VALIDATOR = "content-draft.v3"
CLEAN_VALIDATORS = frozenset({CLEAN_VALIDATOR, EDITORIAL_VALIDATOR})
VALIDATORS = frozenset({VALIDATOR, *CLEAN_VALIDATORS})
PATH_TEMPLATE = "content/drafts/{run_id}.md"
# A pinned prompt that names this context field gets the project's positioning files listed in
# its draft context. Older pins keep their original context.
POSITIONING_MARKER = "content_draft.positioning"
NOTES_MAX_BYTES = 24_000


def notes_path(article_path):
    return article_path.removesuffix(".md") + ".generation.md"


# Leave space for the pinned procedure package and inputs in the runner's base64
# environment transport (Linux limits a single environment string to 128 KiB).
MAX_CONTEXT_BYTES = 64_000
SELECTION_OPERATION = "content_draft_selection_v1"


def selection_key(run_id):
    return f"content-draft:{UUID(str(run_id))}:selection"


def receipt_key(run_id):
    return f"content-draft:{UUID(str(run_id))}:prepare"


def provenance(context):
    return {
        "schema": context.get("output_validator", VALIDATOR),
        "program_id": context["program_id"],
        "item_id": context["item"]["id"],
        "plan_revision": context["plan_revision"],
        "project_revision": context["project_revision"],
        "style_sha256": context["style"]["sha256"] or "absent",
        "brief_sha256": digest(context["item"]),
    }


def validate_artifact(content, context):
    if not context:
        raise ValueError("Draft source binding is missing.")
    text = content.decode("utf-8")
    if context.get("output_validator") == EDITORIAL_VALIDATOR and text.startswith(
        "# Content assessment\n\n"
    ):
        if len(text.strip()) < 40:
            raise ValueError("Explain why no article was drafted.")
        return  # The exact assessment is checked against its companion before publication.
    if context.get("output_validator") in CLEAN_VALIDATORS:
        if not text.startswith("# ") or len(text.strip()) < 200:
            raise ValueError("Draft must start with its article title and contain the article.")
        if re.search(r"(?mi)^## (?:Verification notes|Generation notes)\s*$", text):
            raise ValueError("Generation notes belong in the companion document, not the article.")
        return
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("Draft must carry its source frontmatter.")
    header, body = text[4:].split("\n---\n", 1)
    fields = {}
    for line in header.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key in fields:
            raise ValueError("Draft source frontmatter is malformed.")
        fields[key] = value.strip().strip("\"'")
    if fields != provenance(context):
        raise ValueError("Draft provenance differs from its selected brief or writing guide.")
    marker = "\n## Verification notes\n"
    if body.count(marker) != 1:
        raise ValueError("Draft needs one separate verification-notes section.")
    article, notes = body.split(marker)
    if not re.search(r"(?m)^# .+", article) or len(article.strip()) < 200:
        raise ValueError("Draft article is missing.")
    # This proves each requested check is accounted for, not that an LLM's verdict is true.
    headings = re.findall(r"(?m)^### (v[1-8]) — (checked|unresolved|omitted)\s*$", notes)
    expected = [f"v{i}" for i in range(1, len(context["item"]["verification"]) + 1)]
    if [key for key, _ in headings] != expected:
        raise ValueError("Account for every verification requirement once and in order.")
    sections = re.split(r"(?m)^### v[1-8] — (?:checked|unresolved|omitted)\s*$", notes)[1:]
    if any(len(section.strip()) < 20 for section in sections):
        raise ValueError("Explain each verification result and remaining gap.")


def validate_notes(content, context):
    """Account for desk checks and optional QA; no LLM verdict proves live behavior."""
    if not context or context.get("output_validator") not in CLEAN_VALIDATORS:
        raise ValueError("Generation notes require the clean-draft contract.")
    if not 1 <= len(content) <= NOTES_MAX_BYTES:
        raise ValueError("Generation notes exceed their bounded document size.")
    # Reuse the historical provenance/check accounting parser without changing its
    # meaning for old runs. A follow-up is an accounted-for gap, never a failed run.
    text = content.decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("Generation notes must carry their source frontmatter.")
    header, body = text[4:].split("\n---\n", 1)
    fields = {}
    for line in header.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key in fields:
            raise ValueError("Generation notes frontmatter is malformed.")
        fields[key] = value.strip().strip("\"'")
    if fields != provenance(context):
        raise ValueError("Generation notes differ from their pinned source provenance.")
    if not body.lstrip().startswith("# Generation notes\n"):
        raise ValueError("Generation notes need their document title.")
    statuses = "checked|unresolved|omitted|follow-up"
    headings = re.findall(rf"(?m)^### (v[1-8]) — ({statuses})\s*$", body)
    expected = [f"v{i}" for i in range(1, len(context["item"]["verification"]) + 1)]
    if [key for key, _ in headings] != expected:
        raise ValueError("Account for every brief requirement once and in order.")
    sections = re.split(rf"(?m)^### v[1-8] — (?:{statuses})\s*$", body)[1:]
    if any(len(section.strip()) < 20 for section in sections):
        raise ValueError("Explain each check or optional follow-up.")


def bounded(context):
    if len(json.dumps(context, separators=(",", ":")).encode()) > MAX_CONTEXT_BYTES:
        raise ValueError("The selected brief's evidence is too large; select shorter context.")
    return context
