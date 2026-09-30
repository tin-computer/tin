"""Site repairs for `site-fix-v4`: which audit findings Tin can fix, and how a change is checked.

Pure functions over text, shared by preparation, delivery and the live re-check after merge.

A static fix edits the exact file the site serves (robots.txt, a sitemap, a plain HTML page).
The worker checks it from the diff alone: before and after may differ only in the one change
the finding calls for. A framework fix edits source code that renders the file (Next.js
`app/robots.ts`, a layout's metadata). Tin can't build the site to check that, so the change
is bounded by the files Tin named and by size, the pull request says so, and Tin checks the
live page after the founder merges and deploys.
"""

from __future__ import annotations

import difflib
import html as htmllib
import json
import posixpath
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from tin_lite.organic_audit_site import (
    AI_SEARCH_CRAWLERS,
    _example_path,
    crawler_stances,
    html_facts,
    is_noindex,
    parse_robots,
    parse_sitemap,
    path_allowed,
    robots_group,
    url_key,
)
from tin_lite.technical_metadata_rules import (
    DESCRIPTION_CHECK,
    TITLE_CHECK,
    has_metadata,
    verify_metadata_change,
)

# The one table from audit check ID to fix kind. A renamed audit check is a one-line change.
SITE_FIXES = {
    TITLE_CHECK: "html_title",
    DESCRIPTION_CHECK: "html_description",
    "robots.sitemap_reference_missing": "robots_sitemap_line",
    "robots.ai_search_crawlers_blocked": "robots_allow_ai_search",
    "sitemap.non_indexable_urls": "sitemap_remove_urls",
    "sitemap.ad_landing_urls": "sitemap_remove_urls",
    "sitemap.missing_search_pages": "sitemap_add_urls",
    "indexation.utility_pages_indexable": "html_noindex",
    "indexation.ad_landing_pages_indexable": "html_noindex",
    "indexation.canonical_elsewhere": "html_self_canonical",
    "indexation.multiple_canonicals": "html_one_canonical",
    "onpage.lang_missing": "html_lang",
    "onpage.h1_missing": "html_h1",
}
KINDS = frozenset(SITE_FIXES.values())

# What each kind changes, in the words the report and the PR use.
CHANGES = {
    "robots_sitemap_line": "adds a Sitemap line to robots.txt",
    "robots_allow_ai_search": "lets AI search crawlers read the site in robots.txt",
    "sitemap_remove_urls": "removes pages that shouldn't be indexed from the sitemap",
    "sitemap_add_urls": "adds pages that get search traffic to the sitemap",
    "html_title": "gives each page a title",
    "html_description": "gives each page a meta description",
    "html_noindex": "marks sign-in, account or ad pages noindex",
    "html_self_canonical": "points each page's canonical at the page itself",
    "html_one_canonical": "keeps one canonical tag per page",
    "html_lang": "declares the page language on <html>",
    "html_h1": "gives each page one H1",
}

MAX_HTML_BYTES = 250_000
MAX_TEXT_BYTES = 500_000
MAX_HTML_PAGES = 3  # The repair contract allows at most three changed files.
MAX_SITEMAP_URLS = 10
MAX_FRAMEWORK_FILES = 8
MAX_FRAMEWORK_FILE_BYTES = 30_000
MAX_FRAMEWORK_ORIGINAL_BYTES = 45_000
MAX_FRAMEWORK_NEW_FILE_BYTES = 4_000
MAX_FRAMEWORK_CHANGED_LINES = 60
LANG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

# The PR for a framework fix must carry this sentence verbatim; the worker checks it.
FRAMEWORK_NOTE = (
    "Tin couldn't build your site to check this change. After you merge and deploy it, "
    "Tin checks the live page and records whether the problem is gone."
)


def target(kind: str) -> str:
    """robots, sitemap or html."""
    if kind not in KINDS:
        raise ValueError("Unsupported site repair.")
    return kind.split("_", 1)[0]


def page_limit(kind: str) -> int:
    return MAX_SITEMAP_URLS if target(kind) == "sitemap" else MAX_HTML_PAGES


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
        canonical = facts.get("canonical")
        return bool(canonical) and url_key(canonical) != url_key(url)
    if kind == "html_one_canonical":
        return facts.get("canonical_count", 0) > 1
    if kind == "html_lang":
        return not facts.get("lang")
    if kind == "html_h1":
        return facts.get("h1_count") == 0
    raise ValueError("Not an HTML repair.")


def still_needed(kind: str, text: str, url: str, expected: dict, *, status_code: int = 200) -> bool:
    """Whether one freshly read file or page still shows the finding."""
    kind_target = target(kind)
    if kind_target == "robots":
        state = "observed" if status_code == 200 else "missing"
        return robots_needs(kind, {"status": state, "text": text})["needed"]
    if kind_target == "sitemap":
        keys = {url_key(loc) for loc in sitemap_locs(text)}
        if kind == "sitemap_remove_urls":
            return any(url_key(loc) in keys for loc in expected["remove"])
        return any(url_key(loc) not in keys for loc in expected["add"])
    return page_needs(kind, text, url)


# --- robots.txt ------------------------------------------------------------------------------


def _without_sitemap_lines(text: str) -> list[str]:
    return [
        line.rstrip()
        for line in text.splitlines()
        if not line.split("#", 1)[0].strip().lower().startswith("sitemap:")
    ]


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


def verify_robots_change(before: str | None, after: str, kind: str, expected: dict) -> None:
    """robots.txt may change only as the fix needs. `before` is None for a new file."""
    if not isinstance(after, str) or len(after.encode()) > MAX_TEXT_BYTES:
        raise ValueError("robots.txt is too large.")
    new = parse_robots(after)
    if kind == "robots_sitemap_line":
        wanted = expected["sitemaps"]
        if before is None:
            # A new robots.txt allows everything and names the sitemap; nothing else.
            if any(rule == "disallow" for group in new["groups"] for rule, _ in group["rules"]):
                raise ValueError("A new robots.txt may not disallow anything.")
            if new["sitemaps"] != wanted:
                raise ValueError("The new robots.txt must name exactly the site's sitemap.")
            return
        old = parse_robots(before)
        if old["sitemaps"]:
            raise ValueError("robots.txt already names a sitemap.")
        if new["sitemaps"] != wanted:
            raise ValueError("robots.txt must gain exactly the site's Sitemap line.")
        # Blank lines don't end a group (RFC 9309), so only the other lines must match.
        if [line for line in _without_sitemap_lines(before) if line.strip()] != [
            line for line in _without_sitemap_lines(after) if line.strip()
        ]:
            raise ValueError("Only Sitemap lines may be added to robots.txt.")
        return
    if kind == "robots_allow_ai_search":
        if before is None:
            raise ValueError("Allowing AI search crawlers edits an existing robots.txt.")
        old = parse_robots(before)
        if old["sitemaps"] != new["sitemaps"]:
            raise ValueError("The Sitemap lines must not change.")
        allowed = {agent.lower() for agent in expected["agents"]}
        for agent in expected["agents"]:
            _, rules = robots_group(new, agent)
            if not path_allowed(rules, "/"):
                raise ValueError(f"{agent} is still blocked at the site root.")
        paths = _probe_paths(old, new)
        for agent in _agents(old, new) - allowed:
            _, before_rules = robots_group(old, agent)
            _, after_rules = robots_group(new, agent)
            for path in paths:
                if path_allowed(before_rules, path) != path_allowed(after_rules, path):
                    raise ValueError(f"The change also alters what {agent} may crawl.")
        return
    raise ValueError("Not a robots.txt repair.")


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


def verify_sitemap_change(before: str, after: str, kind: str, expected: dict) -> None:
    """Only whole <url> entries for the finding's pages may leave or join the sitemap."""
    if not isinstance(after, str) or len(after.encode()) > MAX_TEXT_BYTES:
        raise ValueError("The sitemap is too large.")
    if parse_sitemap(before, max_urls=1)["kind"] != "urlset":
        raise ValueError("Only a urlset sitemap can be edited.")
    head, old, tail = _blocks(before)
    new_head, new, new_tail = _blocks(after)
    if (head, tail) != (new_head, new_tail):
        raise ValueError("Only <url> entries may change in the sitemap.")
    if kind == "sitemap_remove_urls":
        remove = {url_key(url) for url in expected["remove"]}
        kept = [row for row in old if url_key(row[0]) not in remove]
        if new != kept or len(kept) == len(old):
            raise ValueError("The sitemap must lose exactly the listed pages' entries.")
        return
    if kind == "sitemap_add_urls":
        add = {url_key(url): url for url in expected["add"]}
        existing = [row for row in new if row in old]
        if existing != old:
            raise ValueError("Existing sitemap entries must not change.")
        added = [row for row in new if row not in old]
        if len(added) != len(add) or {url_key(loc) for loc, _ in added} != set(add):
            raise ValueError("The sitemap must gain exactly the listed pages.")
        for loc, block in added:
            if not _NEW_BLOCK.fullmatch(block.strip()) or loc != add[url_key(loc)]:
                raise ValueError("A new sitemap entry holds one <loc> and an optional <lastmod>.")
        return
    raise ValueError("Not a sitemap repair.")


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


def verify_static_change(kind: str, before: str | None, after: str, expected: dict) -> None:
    kind_target = target(kind)
    if kind_target == "robots":
        return verify_robots_change(before, after, kind, expected)
    if before is None:
        raise ValueError("Only robots.txt may be created by a static repair.")
    if kind_target == "sitemap":
        return verify_sitemap_change(before, after, kind, expected)
    return verify_html_change(before, after, kind, expected["page_url"])


# --- Framework sources (Next.js) -------------------------------------------------------------

CODE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mdx")
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
APP_ROOTS = ("app", "src/app")
PAGES_ROOTS = ("pages", "src/pages")
SITEMAP_CONFIGS = ("next-sitemap.config.js", "next-sitemap.config.cjs", "next-sitemap.config.mjs")


def is_nextjs(package_json: str | None) -> bool:
    if not package_json:
        return False
    try:
        manifest = json.loads(package_json)
    except ValueError:
        return False
    if not isinstance(manifest, dict):
        return False
    return any(
        isinstance(manifest.get(section), dict) and "next" in manifest[section]
        for section in ("dependencies", "devDependencies")
    )


def _route_pattern(path: str, root: str) -> list[str] | None:
    """URL segments for an app-router page file, or None when it isn't one."""
    relative = path[len(root) + 1 :]
    directory, name = posixpath.split(relative)
    if posixpath.splitext(name)[0] != "page" or not name.endswith(CODE_EXTENSIONS):
        return None
    parts = [part for part in directory.split("/") if part]
    return [
        part
        for part in parts
        if not (part.startswith("(") and part.endswith(")")) and not part.startswith("@")
    ]


def _matches(pattern: list[str], segments: list[str]) -> int | None:
    """0 for an exact match, 1 for a match through a dynamic segment, None otherwise."""
    dynamic = 0
    for index, part in enumerate(pattern):
        if part.startswith("[[...") or part.startswith("[..."):
            return 1 if len(segments) >= index + (0 if part.startswith("[[") else 1) else None
        if index >= len(segments):
            return None
        if part.startswith("[") and part.endswith("]"):
            dynamic = 1
        elif part != segments[index]:
            return None
    return dynamic if len(pattern) == len(segments) else None


def framework_candidates(kind: str, names: list[str], urls: list[str]) -> dict:
    """Files a framework fix may edit or create, chosen from the repository's own layout.

    Returns {"existing": [paths], "new": [paths]}; both empty when Tin doesn't recognize
    where the change belongs. Only Next.js layouts are recognized in this version.
    """
    present = set(names)
    code = [name for name in names if name.endswith(CODE_EXTENSIONS)]
    typed = "tsconfig.json" in present
    app_roots = [root for root in APP_ROOTS if any(n.startswith(root + "/") for n in code)]
    pages_roots = [root for root in PAGES_ROOTS if any(n.startswith(root + "/") for n in code)]

    def named(root, stem):
        return [n for n in code if posixpath.splitext(n)[0] == f"{root}/{stem}"]

    existing: list[str] = []
    new: list[str] = []
    kind_target = target(kind)
    if kind_target in {"robots", "sitemap"}:
        stem = "robots" if kind_target == "robots" else "sitemap"
        for root in app_roots:
            existing += named(root, stem)
        existing += [name for name in SITEMAP_CONFIGS if name in present]
        if not existing and kind == "robots_sitemap_line" and app_roots:
            new.append(f"{app_roots[0]}/robots.{'ts' if typed else 'js'}")
    elif kind == "html_lang":
        for root in app_roots:
            existing += named(root, "layout")
        for root in pages_roots:
            existing += named(root, "_document")
    else:
        for url in urls:
            segments = [part for part in urlsplit(url).path.split("/") if part]
            best: list[tuple[int, str]] = []
            for root in app_roots:
                for name in code:
                    if not name.startswith(root + "/"):
                        continue
                    pattern = _route_pattern(name, root)
                    score = None if pattern is None else _matches(pattern, segments)
                    if score is not None:
                        best.append((score, name))
            for root in pages_roots:
                stem = "/".join(segments) or "index"
                for candidate in (f"{root}/{stem}", f"{root}/{stem}/index"):
                    best += [(0, n) for n in code if posixpath.splitext(n)[0] == candidate]
            if best:
                page = min(best)[1]
                existing.append(page)
                # Metadata often lives in the nearest layout above the page.
                directory = posixpath.dirname(page)
                while directory and "/" in directory or directory in APP_ROOTS:
                    existing += [
                        n for n in code if posixpath.splitext(n)[0] == f"{directory}/layout"
                    ]
                    if directory in APP_ROOTS:
                        break
                    directory = posixpath.dirname(directory)
    existing = [
        name
        for name in dict.fromkeys(existing)
        if posixpath.basename(name) not in BLOCKED_NAMES and not name.startswith(".github/")
    ][:MAX_FRAMEWORK_FILES]
    return {"existing": existing, "new": new}


def _changed_lines(before: str, after: str) -> int:
    return sum(
        1
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    )


def verify_framework_change(files: list[dict], originals: dict, new_paths: list[str]) -> None:
    """Bounded, not built: only the named files, a small change, and plain text."""
    if not 1 <= len(files) <= 3:
        raise ValueError("A framework repair changes one to three files.")
    changed = 0
    for item in files:
        path, content = item["path"], item["content"]
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("A framework repair changes text files only.")
        if path in originals:
            if len(content.encode()) > MAX_FRAMEWORK_FILE_BYTES + 2_000:
                raise ValueError("The changed file is too large.")
            if content == originals[path]:
                raise ValueError(f"{path} did not change.")
            changed += _changed_lines(originals[path], content)
        elif path in new_paths:
            if len(content.encode()) > MAX_FRAMEWORK_NEW_FILE_BYTES:
                raise ValueError("A new file for this repair stays small.")
            changed += len(content.splitlines())
        else:
            raise ValueError(f"{path} is not one of the files Tin named for this repair.")
    if changed > MAX_FRAMEWORK_CHANGED_LINES:
        raise ValueError("The framework change is larger than this repair allows.")
