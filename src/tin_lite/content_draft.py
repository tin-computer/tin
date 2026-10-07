"""One selected plan item, one separately addressable review draft; no execution engine."""

from __future__ import annotations

import json
import re
from uuid import UUID

import yaml

from tin_lite.answer_page import (
    MAX_META_TITLE,
    META_DESCRIPTION_RANGE,
    answer_page_problems,
    search_structure_problems,
)
from tin_lite.content_plan import ANSWER, ARTICLE, KINDS, REFRESH
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


# content.generate 1.9.0 drafts every kind of plan item. Its definition lists the kinds; an
# older pinned definition has no list and drafts articles only.
KINDS_FIELD = "content_kinds"
# The review line each kind's draft gets in Decisions and Activity.
KIND_REVIEW = {
    ARTICLE: "The planned article is ready for your review.",
    ANSWER: "The answer page is ready for your review. After you approve it, website.change "
    "puts it on your site at the route you chose for answer pages.",
    REFRESH: "A refresh of one of your pages is ready. Compare each current line with the "
    "proposed one; after you approve, Tin changes exactly those lines in your site's source.",
}


def supported_kinds(definition: dict | None) -> tuple[str, ...]:
    """The plan-item kinds a pinned content.generate definition drafts."""
    kinds = (definition or {}).get(KINDS_FIELD)
    if not isinstance(kinds, list) or not kinds:
        return (ARTICLE,)
    return tuple(kind for kind in KINDS if kind in kinds)


def context_kind(context: dict | None) -> str:
    """The kind a prepared draft context writes. Preparation records a kind only for an answer
    or a refresh, so an article's context is exactly what content.generate 1.8.0 prepared."""
    return (context or {}).get("kind") or ARTICLE


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
    kind = context_kind(context)
    if kind == REFRESH:
        # The exact old to new replacements content.refresh proposes, checked the same way.
        from tin_lite import content_refresh

        content_refresh.validate_document(content, context["refresh"])
        return
    if kind == ANSWER:
        validate_answer(content)
        return
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


def answer_metadata(text: str) -> tuple[dict[str, str], str]:
    """An answer draft's search listing (meta_title, meta_description) and the page after it."""
    match = re.match(r"\A---\n(.*?)\n---\n+", text, re.S)
    if match is None:
        raise ValueError("An answer page starts with its meta_title and meta_description.")
    try:
        fields = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(
            "The answer page's meta_title and meta_description are unreadable."
        ) from exc
    if not isinstance(fields, dict) or set(fields) != {"meta_title", "meta_description"}:
        raise ValueError("An answer page's frontmatter holds meta_title and meta_description only.")
    if not all(isinstance(value, str) for value in fields.values()):
        raise ValueError("The answer page's meta_title and meta_description are plain text.")
    return {key: " ".join(value.split()) for key, value in fields.items()}, text[match.end() :]


def validate_answer(content: bytes) -> None:
    """content.answer_page's quality rules for an answer draft: a meta title and description,
    a 40 to 60 word direct answer, question headings, an FAQ and at least three cited sources."""
    text = content.decode("utf-8")
    metadata, page = answer_metadata(text)
    problems = []
    if not 0 < len(metadata["meta_title"]) <= MAX_META_TITLE:
        problems.append(f"The answer page's meta_title is 1 to {MAX_META_TITLE} characters.")
    low, high = META_DESCRIPTION_RANGE
    if not low <= len(metadata["meta_description"]) <= high:
        problems.append(f"The answer page's meta_description is {low} to {high} characters.")
    if re.search(r"(?mi)^## (?:Verification notes|Generation notes)\s*$", page):
        problems.append("Generation notes belong in the companion document, not the page.")
    problems += answer_page_problems(page.encode())
    if not problems:
        problems += search_structure_problems(page)
    if problems:
        raise ValueError(problems[0])


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
