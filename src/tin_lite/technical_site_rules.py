"""Checks for the files a site serves: what a finding still needs, and whether a change did
only that.

Pure functions over text, shared by site-fix-v5's preparation, its diff checks and the live
re-check after merge. A file the site serves byte for byte (robots.txt, a sitemap, a plain
HTML page) is checked from the diff alone: before and after may differ only in the change its
findings call for. Source that renders a file (a framework's `app/robots.ts`, a layout's
metadata) can't be checked that way, so the pull request carries FRAMEWORK_NOTE and Tin checks
the live page after the founder merges and deploys.
"""

from __future__ import annotations

import html as htmllib
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from tin_lite.organic_audit_site import (
    AI_SEARCH_CRAWLERS,
    _example_path,
    canonical_elsewhere,
    crawler_stances,
    html_facts,
    is_noindex,
    parse_robots,
    parse_sitemap,
    url_key,
)
from tin_lite.technical_metadata_rules import (
    DESCRIPTION_CHECK,
    TITLE_CHECK,
    has_metadata,
    verify_metadata_change,
)

MAX_HTML_BYTES = 250_000
MAX_TEXT_BYTES = 500_000
LANG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

# The PR for a framework fix must carry this sentence verbatim; the worker checks it.
FRAMEWORK_NOTE = (
    "Tin couldn't build your site to check this change. After you merge and deploy it, "
    "Tin checks the live page and records whether the problem is gone."
)


# --- Is the problem still there? ------------------------------------------------------------


def robots_needs(kind: str, robots: dict) -> dict:
    """What a robots.txt fix still has to do, from a fresh read.

    `robots` is {"status": "observed"|"missing", "text": str}. Returns {"needed": bool, ...}.
    """
    parsed = parse_robots(robots.get("text", "")) if robots["status"] == "observed" else None
    if kind == "robots_sitemap_line":
        return {"needed": parsed is None or not parsed["sitemaps"]}
    if kind == "robots_allow_ai_search":
        if parsed is None:
            return {"needed": False, "agents": []}
        blocked = sorted(
            row["agent"]
            for row in crawler_stances({"status": "observed", **parsed})
            if row["agent"] in AI_SEARCH_CRAWLERS and row["stance"] == "blocked"
        )
        return {"needed": bool(blocked), "agents": blocked}
    raise ValueError("Not a robots.txt repair.")


def sitemap_locs(text: str) -> list[str]:
    parsed = parse_sitemap(text, max_urls=100_000)
    if parsed["kind"] != "urlset":
        return []
    return [row["loc"] for row in parsed["entries"]]


def page_needs(kind: str, html: str, url: str) -> bool:
    if kind == "html_title":
        return not has_metadata(html, TITLE_CHECK)
    if kind == "html_description":
        return not has_metadata(html, DESCRIPTION_CHECK)
    facts = html_facts(html.encode(), url=url, charset="utf-8", truncated=False)
    if kind == "html_noindex":
        return not is_noindex(facts)
    if kind == "html_self_canonical":
        return bool(canonical_elsewhere(facts.get("canonical"), url, {urlsplit(url).hostname}))
    if kind == "html_one_canonical":
        return facts.get("canonical_count", 0) > 1
    if kind == "html_lang":
        return not facts.get("lang")
    if kind == "html_h1":
        return facts.get("h1_count") == 0
    raise ValueError("Not an HTML repair.")


# --- robots.txt ------------------------------------------------------------------------------


def _probe_paths(*parsed: dict) -> set[str]:
    paths = {"/"}
    for robots in parsed:
        for group in robots["groups"]:
            for _kind, pattern in group["rules"]:
                paths.add(_example_path(pattern))
                paths.add(pattern.rstrip("$*") or "/")
    return paths


def _agents(*parsed: dict) -> set[str]:
    names = {"*", "googlebot", "bingbot", "gptbot", "claudebot", "google-extended", "ccbot"}
    for robots in parsed:
        for group in robots["groups"]:
            names.update(agent.split("/", 1)[0].strip().lower() for agent in group["agents"])
    return names


# --- sitemaps -------------------------------------------------------------------------------

_BLOCK = re.compile(r"<url\b[^>]*>.*?</url\s*>", re.I | re.S)
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.I | re.S)
_NEW_BLOCK = re.compile(
    r"<url>\s*<loc>([^<]{1,2000})</loc>\s*(?:<lastmod>[0-9T:+\-.Z]{10,40}</lastmod>\s*)?</url>",
    re.S,
)


def _blocks(text: str) -> tuple[str, list[tuple[str, str]], str]:
    matches = list(_BLOCK.finditer(text))
    if not matches:
        return text.strip(), [], ""
    blocks = []
    for match in matches:
        loc = _LOC.search(match.group(0))
        blocks.append((htmllib.unescape(loc.group(1)).strip() if loc else "", match.group(0)))
    return text[: matches[0].start()].strip(), blocks, text[matches[-1].end() :].strip()


# --- HTML ------------------------------------------------------------------------------------


class _Tags(HTMLParser):
    """Byte spans of the tags a repair may touch, plus head/body boundaries."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        # HTMLParser counts lines by "\n" alone, so the offsets must too.
        self.offsets = [0]
        for line in text.split("\n"):
            self.offsets.append(self.offsets[-1] + len(line) + 1)
        self.html: list[tuple[int, int, list]] = []
        self.robots: list[tuple[int, int, list, bool]] = []
        self.canonicals: list[tuple[int, int, list, bool]] = []
        self.h1: list[tuple[int, bool]] = []
        self.h1_ends: list[int] = []
        self.in_head = self.in_body = False
        self.feed(text)
        self.close()

    def _span(self):
        line, column = self.getpos()
        start = self.offsets[line - 1] + column
        return start, start + len(self.get_starttag_text() or "")

    def handle_starttag(self, tag, attrs):
        values = {key: (value or "") for key, value in attrs}
        if tag == "html":
            self.html.append((*self._span(), attrs))
        elif tag == "head":
            self.in_head = True
        elif tag == "body":
            self.in_head, self.in_body = False, True
        elif tag == "meta" and values.get("name", "").lower() == "robots":
            self.robots.append((*self._span(), attrs, self.in_head))
        elif tag == "link" and "canonical" in values.get("rel", "").lower().split():
            self.canonicals.append((*self._span(), attrs, self.in_head))
        elif tag == "h1":
            self.h1.append((self._span()[0], self.in_body))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False
        elif tag == "h1":
            line, column = self.getpos()
            self.h1_ends.append(self.offsets[line - 1] + column)


def _cut(text: str, spans) -> str:
    """The text without the given spans, and without blank lines, so a tag that sat on its
    own line leaves no trace. Blank lines don't change how a page renders outside <pre>."""
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return re.sub(r"[ \t]*\n(?:[ \t]*\n)+", "\n", text)


def _same_page(before: str, after: str) -> None:
    if not isinstance(after, str) or len(after.encode()) > MAX_HTML_BYTES:
        raise ValueError("The page is too large to verify.")
    if before == after:
        raise ValueError("The page did not change.")


def verify_html_change(before: str, after: str, kind: str, page_url: str) -> None:
    """The page may change only in the one tag the fix calls for."""
    _same_page(before, after)
    if kind in {"html_title", "html_description"}:
        check = TITLE_CHECK if kind == "html_title" else DESCRIPTION_CHECK
        return verify_metadata_change(before, after, check)
    old, new = _Tags(before), _Tags(after)
    if kind == "html_lang":
        if len(old.html) != 1 or len(new.html) != 1:
            raise ValueError("The page needs exactly one <html> tag.")
        old_attrs, new_attrs = dict(old.html[0][2]), dict(new.html[0][2])
        lang = new_attrs.pop("lang", None)
        if old_attrs.get("lang") or not lang or not LANG.fullmatch(lang) or new_attrs != old_attrs:
            raise ValueError("Only a valid lang attribute may be added to <html>.")
        if _cut(before, [old.html[0][:2]]) != _cut(after, [new.html[0][:2]]):
            raise ValueError("Only the <html> tag may change.")
        return
    if kind == "html_noindex":
        if len(new.robots) != 1 or not new.robots[0][3]:
            raise ValueError("The page needs one robots meta tag inside <head>.")
        values = {key: (value or "") for key, value in new.robots[0][2]}
        directives = {part.strip().lower() for part in values.get("content", "").split(",")}
        if (
            set(values) != {"name", "content"}
            or "noindex" not in directives
            or not (directives <= {"noindex", "follow", "nofollow"})
        ):
            raise ValueError('Use <meta name="robots" content="noindex">.')
        if len(old.robots) > 1 or _cut(before, [r[:2] for r in old.robots]) != _cut(
            after, [new.robots[0][:2]]
        ):
            raise ValueError("Only the robots meta tag may change.")
        return
    if kind in {"html_self_canonical", "html_one_canonical"}:
        if len(new.canonicals) != 1 or not new.canonicals[0][3]:
            raise ValueError("The page needs exactly one canonical link inside <head>.")
        href = dict(new.canonicals[0][2]).get("href") or ""
        if kind == "html_self_canonical":
            if (
                url_key(urljoin(page_url, href)) != url_key(page_url)
                or urlsplit(urljoin(page_url, href)).hostname != urlsplit(page_url).hostname
            ):
                raise ValueError("The canonical must point at the page itself.")
        else:
            kept = after[new.canonicals[0][0] : new.canonicals[0][1]]
            if len(old.canonicals) < 2 or kept not in {
                before[start:end] for start, end, *_ in old.canonicals
            }:
                raise ValueError("Keep one of the page's existing canonical tags as it was.")
        if _cut(before, [c[:2] for c in old.canonicals]) != _cut(after, [new.canonicals[0][:2]]):
            raise ValueError("Only the canonical link may change.")
        return
    if kind == "html_h1":
        if old.h1 or len(new.h1) != 1 or not new.h1[0][1] or len(new.h1_ends) != 1:
            raise ValueError("The page must gain exactly one <h1> inside <body>.")
        start = new.h1[0][0]
        end = after.find(">", new.h1_ends[0]) + 1
        element = after[start:end]
        text = re.sub(r"^<h1\b[^>]*>|</h1\s*>$", "", element, flags=re.I)
        if not text.strip() or len(text) > 160 or "<" in text or ">" in text:
            raise ValueError("The H1 holds one short line of plain text.")
        if _cut(after, [(start, end)]) != _cut(before, []):
            raise ValueError("Only the new H1 may be added.")
        return
    raise ValueError("Not an HTML repair.")


# --- Framework sources (Next.js) -------------------------------------------------------------

BLOCKED_NAMES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lockb",
        "tsconfig.json",
        "vercel.json",
        "netlify.toml",
    }
)
