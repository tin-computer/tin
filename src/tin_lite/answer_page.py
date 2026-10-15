"""Page checks for content.generate's answer pages: the search listing frontmatter and the
structure a search-structured answer needs."""

from __future__ import annotations

import json
import re

MAX_ANSWER_PAGE_BYTES = 150_000
MAX_META_TITLE = 60
META_DESCRIPTION_RANGE = (70, 160)
ANSWER_WORDS_RANGE = (25, 90)  # The skill asks for 40-60 words; this catches a missing answer.
MIN_CITED_SOURCES = 3
MIN_INLINE_CITATIONS = 2
MAX_PARAGRAPH_WORDS = 150


def answer_page_problems(content: bytes, *, structured: bool = False) -> list[str]:
    """Every page check the content misses, in the order validation reports them."""
    if not content or len(content) > MAX_ANSWER_PAGE_BYTES:
        return [f"answer page must contain between 1 and {MAX_ANSWER_PAGE_BYTES} bytes"]
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ["answer page must be UTF-8 text"]
    problems: list[str] = []
    if structured:
        try:
            text = _validate_search_metadata(text)
        except ValueError as exc:
            problems.append(str(exc))
            match = re.match(r"\A\s*---[ \t]*\n.*?\n---[ \t]*\n+", text, re.S)
            text = text[match.end() :] if match else text
    if not text.startswith("# "):
        problems.append("answer page must begin with a Markdown title")
    if "\n## Sources\n" not in text:
        problems.append("answer page must contain a Sources section")
    if len(re.findall(r"^## ", text, flags=re.MULTILINE)) < 3:
        problems.append("answer page must contain at least three sections")
    if structured:
        problems.extend(_search_structure_problems(text))
    return problems


def _validate_search_metadata(text: str) -> str:
    """Check the search listing frontmatter; return the page after it."""
    match = re.match(r'\A---\nmeta_title: (".*")\nmeta_description: (".*")\n---\n\n', text)
    if match is None:
        raise ValueError("answer page must begin with meta_title and meta_description frontmatter")
    title, description = json.loads(match.group(1)), json.loads(match.group(2))
    if not 0 < len(title) <= MAX_META_TITLE:
        raise ValueError(f"answer page meta_title must be 1-{MAX_META_TITLE} characters")
    low, high = META_DESCRIPTION_RANGE
    if not low <= len(description) <= high:
        raise ValueError(f"answer page meta_description must be {low}-{high} characters")
    return text[match.end() :]


def search_structure_problems(text: str) -> list[str]:
    """The search-structure checks on a page without frontmatter; content.generate answer
    drafts (content_draft.validate_answer) reuse them."""
    return _search_structure_problems(text)


def _search_structure_problems(text: str) -> list[str]:
    """Catch the missing pieces of a search-structured page, not matters of taste."""
    problems: list[str] = []
    sections = re.split(r"(?m)^## ", text)
    lead, body = sections[0], sections[1:]
    if not re.search(r"(?m)^Last updated: \d{4}-\d{2}-\d{2}$", lead):
        problems.append("answer page must show a 'Last updated: YYYY-MM-DD' line under its title")
    lead_paragraphs = [
        block
        for block in _prose_blocks(lead)
        if not block.startswith("# ") and not block.startswith("Last updated:")
    ]
    words = len(lead_paragraphs[0].split()) if lead_paragraphs else 0
    low, high = ANSWER_WORDS_RANGE
    if not low <= words <= high:
        problems.append(
            f"answer page must open with a direct answer of about 40-60 words (found {words})"
        )
    headings = [section.split("\n", 1)[0].strip() for section in body]
    if not headings or headings[-1] != "Sources":
        problems.append("answer page must end with its Sources section")
    faq = [
        section
        for section in body
        if re.match(r"(FAQ|Frequently asked questions)\s*$", section.split("\n", 1)[0].strip())
    ]
    if not faq or len(re.findall(r"(?m)^### .+\?\s*$", faq[0])) < 2:
        problems.append("answer page must include an FAQ section with at least two questions")
    questions = [heading for heading in headings if heading.endswith("?")]
    if len(questions) < 2:
        problems.append("answer page must phrase at least two section headings as questions")
    cited = set(re.findall(r"\]\((https?://[^)\s]+)\)", body[-1])) if body else set()
    if len(cited) < MIN_CITED_SOURCES:
        problems.append(f"answer page must list at least {MIN_CITED_SOURCES} sources")
    inline = re.findall(r"\]\((https?://[^)\s]+)\)", "## ".join([lead, *body[:-1]]))
    if len(inline) < MIN_INLINE_CITATIONS:
        problems.append("answer page must cite its sources inline, next to the claims")
    if any(len(block.split()) > MAX_PARAGRAPH_WORDS for block in _prose_blocks(text)):
        problems.append(
            f"answer page paragraphs must stay under {MAX_PARAGRAPH_WORDS} words; split them"
        )
    return problems


def _prose_blocks(text: str) -> list[str]:
    """Paragraphs of running prose: not headings, lists, tables, quotes or code."""
    text = re.sub(r"(?ms)^```.*?^```\s*$", "", text)
    blocks = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if block and not re.match(r"(#{1,6} |[-*+] |\d+[.)] |\||>|<!--)", block):
            blocks.append(block)
    return blocks
