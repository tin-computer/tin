"""Refresh one existing page a week from the latest audit's search findings.

Tin picks the page, reads what it shows today, and pins the brand, positioning and writing
style files. A bounded writing procedure proposes exact replacements for the page's title,
meta description, H1 and opening answer (and, where the audit flags decay or weak answer
structure, a few body paragraphs). The founder reviews them in Decisions. On approval Tin
itself finds each approved old text in the site's source and replaces it, and nothing else,
then follows the delivery setting: a pull request, or a commit to main.

A page waits six weeks after a refresh goes live before it can be refreshed again, so each
change has time to show whether it worked. Once 28 days of Search Console data exist after
a change went live, the next run's report compares them with the 28 days before it.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from tin_lite.content_delivery import REFRESH_WORKFLOW_ID
from tin_lite.writing_style import STYLE_PATH as WRITING_STYLE_PATH

KEY = "content.refresh"
WORKFLOW_ID = REFRESH_WORKFLOW_ID
STYLE_PATH = WRITING_STYLE_PATH
VALIDATOR = "content-refresh.v1"
PATH_TEMPLATE = "content/refreshes/{run_folder}.md"
PREPARATION = "content_refresh_prepare"
DOCUMENT_SCHEMA = "tin-refresh.v1"
# Search findings a refresh answers. The last two appear only in audits that measure them.
TITLE_CHECKS = frozenset({"search.low_ctr", "search.near_page_one"})
BODY_CHECKS = frozenset({"search.decay", "aeo.answer_structure"})
REFRESH_CHECKS = TITLE_CHECKS | BODY_CHECKS
WAIT = timedelta(weeks=6)
RESULT_DAYS = 28
# Search Console publishes a day's data about three days later.
SEARCH_CONSOLE_DELAY = timedelta(days=3)
FIELDS = ("title", "description", "h1", "lead", "paragraph")
LIMITS = {"title": 70, "description": 170, "h1": 120, "lead": 700, "paragraph": 1500}
SINGLE_LINE = frozenset({"title", "description", "h1"})
MAX_PARAGRAPHS = 3
MAX_PAGE_PARAGRAPHS = 12
MAX_PAGE_BYTES = 2_000_000
MAX_SEARCHES = 6
MAX_RESULTS_SHOWN = 5
# Every earlier refresh with an unmerged pull request is checked (at most 50 are read).
MAX_PR_CHECKS = 50
# Where a project's positioning lives: the brand guide's direction, founder notes, project
# memory and the Start here plan. The procedure reads them from its pinned checkout.
POSITIONING_PATHS = ("brand/BRAND.md", "wiki/INDEX.md", "reports/GROWTH_ONBOARDING_PLAN.md")
CONTEXT_PREFIX = "context/"
MAX_CONTEXT_FILES = 5
# Source files Tin searches for the page's current text. Build output and dependencies are
# skipped; they are not where a founder edits copy.
SOURCE_SUFFIXES = (
    ".astro",
    ".erb",
    ".hbs",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".liquid",
    ".md",
    ".mdx",
    ".njk",
    ".php",
    ".py",
    ".svelte",
    ".ts",
    ".tsx",
    ".vue",
    ".yaml",
    ".yml",
)
SKIPPED_PARTS = frozenset(
    {"node_modules", ".git", ".next", "dist", "build", "out", ".vercel", "vendor", "coverage"}
)
MAX_SOURCE_BYTES = 512_000
MAX_CHANGED_FILES = 5
ACTIVE_STATES = frozenset({"pending", "running", "needs_input", "paused"})
_WS = re.compile(r"\s+")


def run_key(run_id: Any, stage: str) -> str:
    return f"content-refresh:{UUID(str(run_id))}:{stage}"


def normalize(text: str | None) -> str:
    return _WS.sub(" ", html.unescape(text or "")).strip()


def url_key(url: str) -> str:
    """One page, whatever its host alias or trailing slash."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return path.rstrip("/") or "/"


def page_path(url: str) -> str:
    return url_key(url)


# Selection.


def candidates(findings_document: dict) -> dict[str, dict[str, Any]]:
    """Pages the audit's search findings name, with the checks that name each one."""
    pages: dict[str, dict[str, Any]] = {}
    for finding in findings_document.get("findings") or []:
        check = finding.get("check_id")
        if check not in REFRESH_CHECKS:
            continue
        for url in finding.get("urls") or []:
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            entry = pages.setdefault(url_key(url), {"url": url, "checks": set()})
            entry["checks"].add(check)
    return pages


def page_rows(evidence: dict) -> dict[str, dict[str, float]]:
    value = ((evidence.get("search_console") or {}).get("value")) or {}
    rows = {}
    for row in value.get("pages") or []:
        if isinstance(row, dict) and isinstance(row.get("url"), str):
            rows[url_key(row["url"])] = {
                "clicks": float(row.get("clicks") or 0),
                "impressions": float(row.get("impressions") or 0),
                "position": float(row.get("position") or 0),
            }
    return rows


def top_searches(evidence: dict, key: str) -> list[dict[str, Any]]:
    value = ((evidence.get("search_console_queries") or {}).get("value")) or {}
    found = []
    for row in value.get("queries") or []:
        if not isinstance(row, list) or len(row) != 5 or url_key(str(row[1])) != key:
            continue
        query, _, clicks, impressions, position = row
        found.append(
            {
                "query": str(query)[:200],
                "clicks": float(clicks or 0),
                "impressions": float(impressions or 0),
                "position": round(float(position or 0), 1),
            }
        )
    found.sort(key=lambda item: (-item["impressions"], item["query"]))
    return found[:MAX_SEARCHES]


def choose(findings_document: dict, evidence: dict, blocked: set[str]) -> dict | None:
    """The eligible page with the most search impressions at stake, or None."""
    rows = page_rows(evidence)
    ranked = []
    for key, entry in candidates(findings_document).items():
        if key in blocked:
            continue
        row = rows.get(key, {"clicks": 0.0, "impressions": 0.0, "position": 0.0})
        ranked.append((-row["impressions"], key, entry, row))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    _, key, entry, row = ranked[0]
    ctr = row["clicks"] / row["impressions"] if row["impressions"] else 0.0
    return {
        "url": entry["url"],
        "path": key,
        "checks": sorted(entry["checks"]),
        "metrics": {**row, "ctr": round(ctr, 4)},
        "searches": top_searches(evidence, key),
        "body_allowed": bool(entry["checks"] & BODY_CHECKS),
    }


# Reading the page as it is today.


class PageText(HTMLParser):
    """Title, meta description, first H1 and body paragraphs, as a reader sees them."""

    SKIP = frozenset({"script", "style", "noscript", "template", "svg", "nav", "footer"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.h1 = ""
        self.paragraphs: list[str] = []
        self.after_h1: int | None = None
        self._in_title = self._in_h1 = False
        self._h1_done = False
        self._paragraph: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title" and not self.title:
            self._in_title = True
        elif tag == "meta" and values.get("name", "").casefold() == "description":
            self.description = self.description or values.get("content", "")
        elif tag == "h1" and not self._h1_done and not self._skip:
            self._in_h1 = True
        elif tag == "p" and not self._skip and len(self.paragraphs) < MAX_PAGE_PARAGRAPHS:
            self._paragraph = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "h1" and self._in_h1:
            self._in_h1, self._h1_done = False, True
            self.after_h1 = len(self.paragraphs)
        elif tag == "p" and self._paragraph is not None:
            text = normalize("".join(self._paragraph))
            if text:
                self.paragraphs.append(text)
            self._paragraph = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_h1:
            self.h1 += data
        if self._paragraph is not None and not self._skip:
            self._paragraph.append(data)


def page_text(markup: str) -> dict[str, Any]:
    parser = PageText()
    parser.feed(markup)
    paragraphs = parser.paragraphs
    start = parser.after_h1 if parser.after_h1 is not None else 0
    lead = next((p for p in paragraphs[start:] if len(p) >= 40), "") or next(
        (p for p in paragraphs if len(p) >= 40), ""
    )
    return {
        "title": normalize(parser.title),
        "description": normalize(parser.description),
        "h1": normalize(parser.h1),
        "lead": lead,
        "paragraphs": paragraphs,
    }


# History: what earlier refreshes changed, whether they went live, and what happened.


def result_windows(live_at: datetime) -> dict[str, dict[str, str]]:
    day = live_at.date()
    return {
        "before": {
            "start": (day - timedelta(days=RESULT_DAYS)).isoformat(),
            "end": (day - timedelta(days=1)).isoformat(),
        },
        "after": {
            "start": day.isoformat(),
            "end": (day + timedelta(days=RESULT_DAYS - 1)).isoformat(),
        },
    }


def measurable(live_at: datetime, now: datetime) -> bool:
    return now >= live_at + timedelta(days=RESULT_DAYS) + SEARCH_CONSOLE_DELAY


def totals(raw: dict) -> dict[str, float]:
    clicks = impressions = weighted = 0.0
    for row in raw.get("rows") or []:
        if not isinstance(row, dict):
            continue
        rows_clicks = float(row.get("clicks") or 0)
        rows_impressions = float(row.get("impressions") or 0)
        clicks += rows_clicks
        impressions += rows_impressions
        weighted += float(row.get("position") or 0) * rows_impressions
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": round(clicks / impressions, 4) if impressions else 0.0,
        "position": round(weighted / impressions, 1) if impressions else 0.0,
    }


def _number(value: float) -> str:
    return f"{value:,.0f}"


def results_markdown(results: list[dict]) -> str:
    """The report table of earlier refreshes, newest first; Tin writes it, not the model."""
    if not results:
        return "No earlier refresh has 28 days of results yet."
    lines = [
        "| Page | Live since | Clicks | Impressions | CTR | Position |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in results[:MAX_RESULTS_SHOWN]:
        before, after = item["before"], item["after"]
        lines.append(
            f"| {item['path']} | {item['live_at'][:10]} "
            f"| {_number(before['clicks'])} → {_number(after['clicks'])} "
            f"| {_number(before['impressions'])} → {_number(after['impressions'])} "
            f"| {before['ctr']:.1%} → {after['ctr']:.1%} "
            f"| {before['position']:g} → {after['position']:g} |"
        )
    lines.append("")
    lines.append(
        f"Each row compares the {RESULT_DAYS} days before a refresh went live with the "
        f"{RESULT_DAYS} days after, from Search Console."
    )
    return "\n".join(lines)


# The proposal document the procedure writes and the founder reviews.

_JSON_BLOCK = re.compile(r"^```json\s*\n(.*?)\n```[ \t]*$", re.M | re.S)


def _block(text: str) -> tuple[dict, int, int]:
    found = []
    for match in _JSON_BLOCK.finditer(text):
        try:
            value = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and value.get("schema") == DOCUMENT_SCHEMA:
            found.append((value, match.start(), match.end()))
    if len(found) != 1:
        raise ValueError(f"The refresh needs exactly one {DOCUMENT_SCHEMA} block.")
    return found[0]


def replacements(text: str) -> list[dict[str, str]]:
    value, _, _ = _block(text)
    items = value.get("replacements")
    if not isinstance(items, list):
        raise ValueError("The refresh lists no replacements.")
    return [
        {key: str(item.get(key, "")) for key in ("field", "old", "new", "reason")}
        for item in items
        if isinstance(item, dict)
    ]


def validate_document(content: bytes, context: dict) -> list[dict[str, str]]:
    """Every replacement starts from the page's exact current text and stays plain copy."""
    if not context or not context.get("page"):
        raise ValueError("This refresh has no pinned page.")
    text = content.decode("utf-8")
    if not text.startswith("# "):
        raise ValueError("Start the refresh with a # heading.")
    value, start, end = _block(text)
    page, current = context["page"], context["current"]
    if value.get("page") != page["url"]:
        raise ValueError("The refresh names a different page than Tin selected.")
    items = replacements(text)
    if not 1 <= len(items) <= 4 + MAX_PARAGRAPHS:
        raise ValueError("Propose between one and seven replacements.")
    seen: set[tuple[str, str]] = set()
    paragraphs = 0
    for item in items:
        field, old, new = item["field"], item["old"], item["new"]
        if field not in FIELDS:
            raise ValueError(f"{field or 'A replacement'} is not a field a refresh changes.")
        if field == "paragraph":
            if not page.get("body_allowed"):
                raise ValueError(
                    "Body paragraphs change only when the audit flags decay or weak "
                    "answer structure."
                )
            paragraphs += 1
            if old not in current.get("paragraphs", []):
                raise ValueError("A replaced paragraph must quote the page's current text.")
        elif old != current.get(field):
            raise ValueError(f"The old {field} must be the page's current {field}, exactly.")
        if (field, old) in seen:
            raise ValueError(f"The {field} is replaced twice.")
        seen.add((field, old))
        if not old:
            raise ValueError(f"The page shows no {field} to replace.")
        if not new.strip() or new == old or len(new) > LIMITS[field]:
            raise ValueError(f"The new {field} must differ and stay under {LIMITS[field]} chars.")
        if field in SINGLE_LINE and ("\n" in new or "\r" in new):
            raise ValueError(f"The new {field} must be one line.")
        if re.search(r"[<>{}`]", new):
            raise ValueError(f"The new {field} must be plain text, without markup or braces.")
        if not item["reason"].strip():
            raise ValueError(f"Say why the {field} changes.")
        # The reviewer sees each change outside the machine-readable block.
        prose = text[:start] + text[end:]
        if old not in prose or new not in prose:
            raise ValueError(f"Show the current and proposed {field} in the review table.")
    if paragraphs > MAX_PARAGRAPHS:
        raise ValueError(f"Replace at most {MAX_PARAGRAPHS} body paragraphs.")
    if context.get("results_markdown") and context["results_markdown"] not in text:
        raise ValueError("Copy Tin's table of earlier refresh results unchanged.")
    return items


# Applying approved replacements to the site's source.


def _variants(value: str) -> list[tuple[str, Any]]:
    """Ways a page's text appears in source: as-is, HTML-escaped or inside a JSON string."""
    forms = [
        ("plain", lambda s: s),
        ("html", lambda s: html.escape(s, quote=False)),
        ("html_quoted", lambda s: html.escape(s, quote=True)),
        ("json", lambda s: json.dumps(s, ensure_ascii=False)[1:-1]),
    ]
    seen, found = set(), []
    for name, encode in forms:
        encoded = encode(value)
        if encoded not in seen:
            seen.add(encoded)
            found.append((name, encode))
    return found


def _pattern(text: str) -> re.Pattern:
    """Exact words; any run of source whitespace may separate them (wrapped JSX, HTML).

    A text that starts or ends with a letter or digit must start or end a word in the
    source too, so "Pricing" never matches inside "PricingTable".
    """
    body = r"\s+".join(re.escape(word) for word in text.split())
    before = r"(?<!\w)" if re.match(r"\w", text) else ""
    after = r"(?!\w)" if re.search(r"\w$", text) else ""
    return re.compile(before + body + after)


def matches(source: str, old: str) -> list[tuple[int, int, str, Any]]:
    """Non-overlapping (start, end, encoding name, encoder) spans of `old` in the source."""
    spans: list[tuple[int, int, str, Any]] = []
    for name, encode in _variants(old):
        for match in _pattern(encode(old)).finditer(source):
            if not any(start < match.end() and match.start() < end for start, end, *_ in spans):
                spans.append((match.start(), match.end(), name, encode))
    return sorted(spans, key=lambda span: span[:2])


# Where the new text lands decides how it must be written: a quoted string in code, an HTML
# attribute, element text, or a JSON value. A text Tin cannot place safely stops delivery.
SCRIPT_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py")
JSX_SUFFIXES = (".js", ".jsx", ".tsx")
MARKUP_SUFFIXES = (".astro", ".erb", ".hbs", ".htm", ".html", ".liquid", ".njk", ".php")
YAML_SUFFIXES = (".yaml", ".yml")
QUOTES = "'\"`"
# Characters that can end or break a string in code. validate_document already refuses <, >,
# braces and backticks in new text.
SENSITIVE = frozenset("'\"\\")


def _neighbours(source: str, start: int, end: int) -> tuple[str, str]:
    left = source[:start].rstrip()
    right = source[end:].lstrip()
    return (left[-1] if left else "", right[0] if right else "")


def placed(path: str, source: str, start: int, end: int, new: str, name: str, encode) -> str:
    """`new` written so the file stays valid where the old text sat, or ValueError."""
    encoded = encode(new)
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    attribute_name = re.search(r"[A-Za-z_:@][\w:.@-]*=$", source[max(0, start - 80) : start - 1])
    unsafe = ValueError(
        f"Tin cannot tell how to write the new text safely where it sits in {path}. "
        "Change it by hand or ask your coding agent."
    )
    if before and before == after and before in QUOTES:
        quote = before
        if path.endswith(".json") or name == "json":
            if quote != '"':
                raise unsafe
            return json.dumps(new, ensure_ascii=False)[1:-1]
        if attribute_name and path.endswith(JSX_SUFFIXES):
            # A JSX attribute string takes no backslash escapes.
            if quote in new or "\\" in new:
                raise unsafe
            return encoded
        if attribute_name and path.endswith(MARKUP_SUFFIXES + (".vue", ".svelte")):
            return html.escape(new, quote=True)
        if path.endswith(YAML_SUFFIXES):
            return (
                json.dumps(new, ensure_ascii=False)[1:-1]
                if quote == '"'
                else (encoded.replace("'", "''"))
            )
        if path.endswith(SCRIPT_SUFFIXES + MARKUP_SUFFIXES + (".vue", ".svelte", ".mdx")):
            if "\n" in new and quote != "`":
                raise unsafe
            written = encoded.replace("\\", "\\\\").replace(quote, "\\" + quote)
            return written.replace("${", "\\${") if quote == "`" else written
        if quote in new:
            raise unsafe
        return encoded
    left, right = _neighbours(source, start, end)
    if left == ">" and right == "<":
        if path.endswith(MARKUP_SUFFIXES + (".vue", ".svelte")):
            return html.escape(new, quote=False)
        if path.endswith(JSX_SUFFIXES):
            # JSX text: keep a quote the source already writes raw, otherwise use its entity.
            original = source[start:end]
            for char, entity in (("'", "&apos;"), ('"', "&quot;")):
                if char in encoded and char not in original:
                    encoded = encoded.replace(char, entity)
            return encoded
        return encoded
    if path.endswith((".md", ".mdx")):
        return encoded
    # Tin cannot see where this text begins or ends (part of a longer string or paragraph):
    # it writes a quote or backslash only where the replaced source already writes one raw.
    if name == "plain" and any(char not in source[start:end] for char in SENSITIVE & set(new)):
        raise unsafe
    return encoded


def _source_path(path: str) -> bool:
    parts = path.split("/")
    return path.endswith(SOURCE_SUFFIXES) and not SKIPPED_PARTS.intersection(parts)


def searched(path: str, size: int) -> bool:
    """Whether plan_patch searches a repository file for the page's text."""
    return _source_path(path) and size <= MAX_SOURCE_BYTES


def plan_patch(files: dict[str, bytes], items: list[dict[str, str]]) -> dict[str, str]:
    """The changed source files for approved replacements, or ValueError saying why not.

    The page's own file usually holds several of its texts at once (title, description,
    H1), so Tin edits the files that hold the most of them; a text found only elsewhere is
    edited where it is unique. Anything ambiguous or missing stops the delivery.
    """
    texts = {}
    for path, raw in files.items():
        if not _source_path(path) or len(raw) > MAX_SOURCE_BYTES:
            continue
        try:
            texts[path] = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
    holders = {}
    for index, item in enumerate(items):
        holders[index] = {path for path, text in texts.items() if matches(text, item["old"])}
        if not holders[index]:
            raise ValueError(
                f"Tin could not find the page's current {item['field']} in the repository: "
                f'"{item["old"][:120]}". The site may build it from pieces or load it from '
                "elsewhere. Change it by hand or ask your coding agent."
            )
    counts: dict[str, int] = {}
    for paths in holders.values():
        for path in paths:
            counts[path] = counts.get(path, 0) + 1
    best = max(counts.values())
    leaders = sorted(path for path, count in counts.items() if count == best)
    # The page's own file holds more of its texts than any other; a tie is ambiguous.
    page_file = leaders[0] if len(leaders) == 1 else None
    chosen: dict[int, set[str]] = {}
    for index, paths in holders.items():
        if page_file in paths:
            chosen[index] = {page_file}
        elif len(paths) == 1:
            chosen[index] = paths
        else:
            raise ValueError(
                f"The page's current {items[index]['field']} appears in several files "
                f"({', '.join(sorted(paths)[:4])}), so Tin cannot tell which one this page "
                "uses. Change it by hand or ask your coding agent."
            )
    changed_paths = set().union(*chosen.values())
    if len(changed_paths) > MAX_CHANGED_FILES:
        raise ValueError(
            f"The refresh would change {len(changed_paths)} files; Tin changes at most "
            f"{MAX_CHANGED_FILES}."
        )
    changed = {}
    for path in sorted(changed_paths):
        text = texts[path]
        spans = []
        for index, paths in chosen.items():
            if path not in paths:
                continue
            item = items[index]
            found = matches(text, item["old"])
            # One place per file: a second copy could be a different element or component.
            if len(found) != 1:
                raise ValueError(
                    f"The page's current {item['field']} appears {len(found)} times in {path}, "
                    "so Tin cannot tell which one the page shows. Change it by hand or ask "
                    "your coding agent."
                )
            [(start, end, name, encode)] = found
            try:
                replacement = placed(path, text, start, end, item["new"], name, encode)
            except ValueError as exc:
                raise ValueError(f"The new {item['field']}: {exc}") from exc
            if not any(s < end and start < e for s, e, _ in spans):
                spans.append((start, end, replacement))
        out, cursor = [], 0
        for start, end, replacement in sorted(spans):
            out.append(text[cursor:start])
            out.append(replacement)
            cursor = end
        out.append(text[cursor:])
        changed[path] = "".join(out)
    verify_patch({path: texts[path] for path in changed}, changed, items)
    return changed


def verify_patch(
    originals: dict[str, str], changed: dict[str, str], items: list[dict[str, str]]
) -> None:
    """Each changed file is its original with approved old texts swapped for their new text.

    Nothing else may differ: no other edit, no new or deleted file. Every approved
    replacement must land at least once.
    """
    if set(changed) - set(originals):
        raise ValueError("The refresh may only edit files that already exist.")
    landed = set()
    for path, new_text in changed.items():
        original = originals[path]
        spans = []
        for index, item in enumerate(items):
            for start, end, name, encode in matches(original, item["old"]):
                try:
                    replacement = placed(path, original, start, end, item["new"], name, encode)
                except ValueError:
                    # Text Tin could not place safely must stay exactly as it was.
                    replacement = original[start:end]
                spans.append((start, end, replacement, index))
        spans.sort()
        parts, cursor = [], 0
        choices: list[tuple[str, str, int]] = []
        for start, end, replacement, index in spans:
            if start < cursor:
                continue
            parts.append(re.escape(original[cursor:start]))
            choices.append((original[start:end], replacement, index))
            parts.append(
                f"(?P<g{len(choices) - 1}>{re.escape(original[start:end])}|"
                f"{re.escape(replacement)})"
            )
            cursor = end
        parts.append(re.escape(original[cursor:]))
        match = re.fullmatch("".join(parts), new_text, flags=re.S)
        if match is None:
            raise ValueError(f"{path} changes more than the approved text.")
        for number, (_, replacement, index) in enumerate(choices):
            if match.group(f"g{number}") == replacement:
                landed.add(index)
    missing = [items[index]["field"] for index in range(len(items)) if index not in landed]
    if missing:
        raise ValueError(f"The approved {', '.join(missing)} did not reach the source.")


def patch_body(context: dict, items: list[dict[str, str]]) -> str:
    lines = [
        f"Refreshes {context['page']['url']} after Tin's audit found it "
        + (
            "ranking just below the top results"
            if "search.near_page_one" in context["page"]["checks"]
            else "shown near the top of results but rarely clicked"
        )
        + ". The founder approved each change in Tin.",
        "",
        "| Field | Before | After |",
        "| --- | --- | --- |",
    ]
    for item in items:
        before = item["old"].replace("|", "\\|")[:300]
        after = item["new"].replace("|", "\\|")[:300]
        lines.append(f"| {item['field']} | {before} | {after} |")
    lines.extend(
        [
            "",
            "Tin changed only the approved text, wherever the page's source holds it, and "
            "checked that nothing else in these files changed. Tin did not build the site; "
            "check the preview before merging.",
        ]
    )
    return "\n".join(lines)


def document_digest(items: list[dict[str, str]]) -> str:
    return hashlib.sha256(
        json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).date()
