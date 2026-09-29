"""Site evidence Tin reads itself: robots.txt, sitemaps, static HTML facts and page selection.

Pure parsing and deterministic selection. No network, database or Temporal dependencies;
`organic_audit_fetch` performs the bounded reads and stores what these functions return.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

FETCH_AGENT = "Tin-Organic-Audit"
GOOGLE_AGENT = "googlebot"
# The AI crawlers the report states a stance for. Search crawlers fetch pages to answer
# a person's question; the others collect training data.
AI_CRAWLERS = (
    "GPTBot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "PerplexityBot",
    "ClaudeBot",
    "Google-Extended",
    "CCBot",
)
AI_SEARCH_CRAWLERS = frozenset({"OAI-SearchBot", "ChatGPT-User", "PerplexityBot"})
MAX_ROBOTS_LINES = 5000
MAX_ROBOTS_RULES = 2000
# Path segments of pages that exist for signed-in use, not for search.
UTILITY_SEGMENTS = frozenset(
    {
        "sign-in",
        "signin",
        "sign_in",
        "login",
        "log-in",
        "logon",
        "sign-up",
        "signup",
        "sign_up",
        "register",
        "auth",
        "sso",
        "account",
        "reset-password",
        "forgot-password",
        "logout",
        "log-out",
        "sign-out",
        "signout",
    }
)
# First path segments that usually hold paid-ad landing pages.
AD_LANDING_SEGMENTS = frozenset(
    {"offer", "offers", "lp", "landing", "landing-page", "landing-pages", "ads", "promo", "promos"}
)
# Two-letter language path prefixes (ISO 639-1) plus common region forms such as pt-br.
LANGUAGE_PREFIX = re.compile(
    r"^(?:ar|bg|cs|da|de|el|en|es|et|fi|fr|he|hi|hr|hu|id|it|ja|ko|lt|lv|ms|nb|nl|no|pl|pt|ro|"
    r"ru|sk|sl|sr|sv|th|tr|uk|vi|zh)(?:[-_][a-z]{2,4})?$",
    re.I,
)


# --- robots.txt (RFC 9309) -------------------------------------------------------------


def parse_robots(text: str) -> dict:
    """Groups of user agents with their rules, plus sitemap references.

    Consecutive user-agent lines share one group; a user-agent line after a rule starts
    a new group. Unknown records are ignored, as major crawlers do.
    """
    groups: list[dict] = []
    sitemaps: list[str] = []
    current: dict | None = None
    collecting_agents = False
    rules = 0
    for raw in text.splitlines()[:MAX_ROBOTS_LINES]:
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or not collecting_agents:
                current = {"agents": [], "rules": []}
                groups.append(current)
            current["agents"].append(value[:100])
            collecting_agents = True
        elif key in {"allow", "disallow"}:
            collecting_agents = False
            if current is not None and value and rules < MAX_ROBOTS_RULES:
                current["rules"].append([key, value[:500]])
                rules += 1
        elif key == "sitemap" and value and len(sitemaps) < 50:
            sitemaps.append(value[:2000])
    return {"groups": groups, "sitemaps": sitemaps}


def _agent_token(value: str) -> str:
    return value.split("/", 1)[0].strip().lower()


def robots_group(robots: dict, agent: str) -> tuple[str, list[list[str]]]:
    """The rules one crawler obeys: its own named groups, else the wildcard groups.

    Named groups do not inherit wildcard rules. Returns ("named"|"wildcard"|"none", rules).
    """
    token = agent.lower()
    named = [g for g in robots["groups"] if any(_agent_token(a) == token for a in g["agents"])]
    if named:
        return "named", [rule for g in named for rule in g["rules"]]
    wildcard = [g for g in robots["groups"] if any(_agent_token(a) == "*" for a in g["agents"])]
    if wildcard:
        return "wildcard", [rule for g in wildcard for rule in g["rules"]]
    return "none", []


@lru_cache(maxsize=4096)
def _rule_pattern(path: str) -> re.Pattern:
    anchored = path.endswith("$")
    body = re.escape(path[:-1] if anchored else path).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""), re.S)


def path_allowed(rules: list[list[str]], path: str) -> bool:
    """Longest matching rule wins; on a tie, allow wins. No match means allowed."""
    best: tuple[int, bool] | None = None
    for kind, pattern in rules:
        if _rule_pattern(pattern).match(path):
            candidate = (len(pattern), kind == "allow")
            if best is None or candidate > best:
                best = candidate
    return True if best is None else best[1]


def url_path(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def robots_allows(robots: dict | None, agent: str, url: str) -> bool | None:
    """None when robots.txt could not be read; a 4xx robots.txt allows everything."""
    if robots is None:
        return None
    if robots.get("status") == "missing":
        return True
    if robots.get("status") != "observed":
        return None
    _, rules = robots_group(robots, agent)
    return path_allowed(rules, url_path(url))


def _example_path(pattern: str) -> str:
    return pattern.rstrip("$").replace("*", "x") or "/"


def crawler_stances(robots: dict) -> list[dict]:
    """How each AI crawler is treated at the site root and whether any path is closed."""
    rows = []
    for agent in AI_CRAWLERS:
        source, rules = robots_group(robots, agent)
        root = path_allowed(rules, "/")
        closed = [
            pattern
            for kind, pattern in rules
            if kind == "disallow" and not path_allowed(rules, _example_path(pattern))
        ]
        rows.append(
            {
                "agent": agent,
                "kind": "search" if agent in AI_SEARCH_CRAWLERS else "training",
                "group": source,
                "stance": "blocked" if not root else "partly_blocked" if closed else "allowed",
                "closed_paths": sorted(set(closed))[:5],
            }
        )
    return rows


def named_groups_missing_wildcard_rules(robots: dict) -> list[dict]:
    """Named groups that reopen paths the wildcard group closes (RFC 9309 has no inheritance)."""
    wildcard = [g for g in robots["groups"] if any(_agent_token(a) == "*" for a in g["agents"])]
    closed = [pattern for g in wildcard for kind, pattern in g["rules"] if kind == "disallow"]
    if not closed:
        return []
    result = []
    seen: set[str] = set()
    for group in robots["groups"]:
        for agent in group["agents"]:
            name = _agent_token(agent)
            if name == "*" or name in seen:
                continue
            seen.add(name)
            _, rules = robots_group(robots, name)
            if not path_allowed(rules, "/"):
                continue
            reopened = sorted({p for p in closed if path_allowed(rules, _example_path(p))})
            if reopened:
                result.append({"agent": agent[:100], "reopened": reopened[:20]})
    return result[:30]


# --- sitemaps -----------------------------------------------------------------------------

_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.I | re.S)
_LASTMOD = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.I | re.S)
_BLOCK = re.compile(r"<(url|sitemap)\b[^>]*>(.*?)</\1\s*>", re.I | re.S)


def parse_sitemap(text: str, *, max_urls: int) -> dict:
    """A urlset or a sitemap index, read with bounded regular expressions (no XML entities)."""
    kind = "index" if re.search(r"<sitemapindex\b", text[:5000], re.I) else "urlset"
    if kind == "urlset" and not re.search(r"<urlset\b", text[:5000], re.I):
        return {"kind": "invalid", "entries": [], "total": 0}
    entries = []
    total = 0
    for match in _BLOCK.finditer(text):
        loc = _LOC.search(match.group(2))
        if not loc:
            continue
        total += 1
        if len(entries) >= max_urls:
            continue
        lastmod = _LASTMOD.search(match.group(2))
        entries.append(
            {
                "loc": html.unescape(loc.group(1)).strip()[:2000],
                "lastmod": html.unescape(lastmod.group(1)).strip()[:40] if lastmod else None,
            }
        )
    return {"kind": kind, "entries": entries, "total": total}


# --- static HTML -----------------------------------------------------------------------------


class _FactsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang: str | None = None
        self.seen_html = False
        self.robots: list[str] = []
        self.canonicals: list[str] = []
        self.hreflang: list[dict] = []
        self.h1_count = 0
        self.title: str | None = None
        self._in_title = False
        self._title_parts: list[str] = []
        self._in_json_ld = False
        self._json_ld_text: list[str] = []
        self.json_ld_blocks = 0
        self.json_ld_types: list[str] = []
        self.microdata = False

    def handle_starttag(self, tag, attrs):
        values = {name.lower(): (value or "") for name, value in attrs}
        if tag == "html" and not self.seen_html:
            self.seen_html = True
            lang = values.get("lang", "").strip()
            self.lang = lang[:35] if lang else None
        elif tag == "meta":
            name = values.get("name", "").strip().lower()
            if name in {"robots", "googlebot"}:
                self.robots.extend(
                    token.strip().lower()
                    for token in values.get("content", "").split(",")
                    if token.strip()
                )
        elif tag == "link":
            rel = {item.lower() for item in values.get("rel", "").split()}
            href = values.get("href", "").strip()
            if "canonical" in rel and href:
                self.canonicals.append(href[:2000])
            if "alternate" in rel and values.get("hreflang") and href and len(self.hreflang) < 50:
                self.hreflang.append({"lang": values["hreflang"].strip()[:35], "href": href[:2000]})
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "title" and self.title is None:
            self._in_title = True
        elif tag == "script" and values.get("type", "").strip().lower() == "application/ld+json":
            self._in_json_ld = True
            self._json_ld_text = []
            self.json_ld_blocks += 1
        if "itemscope" in values:
            self.microdata = True

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = " ".join("".join(self._title_parts).split())[:200]
        elif tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            for found in re.findall(r'"@type"\s*:\s*"([^"]{1,60})"', "".join(self._json_ld_text)):
                if found not in self.json_ld_types and len(self.json_ld_types) < 10:
                    self.json_ld_types.append(found)

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_json_ld and sum(map(len, self._json_ld_text)) < 200_000:
            self._json_ld_text.append(data)


_VALUED_DIRECTIVES = frozenset(
    {"unavailable_after", "max-snippet", "max-image-preview", "max-video-preview"}
)


def x_robots_directives(values: list[str]) -> list[str]:
    """Directives from X-Robots-Tag headers that apply to every crawler or to Googlebot."""
    result = []
    for value in values:
        for part in value.split(","):
            token = part.strip().lower()
            if ":" in token:
                name, _, rest = token.partition(":")
                if name.strip() not in _VALUED_DIRECTIVES:
                    if name.strip() != GOOGLE_AGENT:
                        continue  # A directive for another crawler.
                    token = rest.strip()
            if token:
                result.append(token[:40])
    return result[:20]


def html_facts(body: bytes, *, url: str, charset: str | None, truncated: bool) -> dict:
    """What a crawler that does not run JavaScript sees in the page's HTML."""
    text = body.decode(charset or "utf-8", "replace").replace("\x00", "")
    parser = _FactsParser()
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError):
        pass
    canonical = None
    if parser.canonicals:
        canonical = urljoin(url, html.unescape(parser.canonicals[0]))[:2000]
    return {
        "lang": parser.lang,
        "robots": sorted(set(parser.robots))[:20],
        "canonical": canonical,
        "canonical_count": len(parser.canonicals),
        "hreflang": [
            {"lang": item["lang"], "href": urljoin(url, item["href"])[:2000]}
            for item in parser.hreflang
        ],
        "h1_count": parser.h1_count,
        "title": parser.title,
        "json_ld_blocks": parser.json_ld_blocks,
        "json_ld_types": parser.json_ld_types,
        "microdata": parser.microdata,
        "html_bytes": len(body),
        "truncated": truncated,
    }


def is_noindex(facts: dict) -> bool:
    directives = set(facts.get("robots", [])) | set(facts.get("x_robots_tag", []))
    return bool(directives & {"noindex", "none"})


# --- URLs, sections and page selection ------------------------------------------------------


def url_key(url: str) -> str:
    """Host-independent identity for matching sitemap, crawl, fetch and Search Console URLs.

    Only the audit's verified hosts reach this function, so the host is dropped; a trailing
    slash is not a different page for matching purposes.
    """
    parts = urlsplit(url)
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return path + (f"?{parts.query}" if parts.query else "")


def segments(url: str) -> list[str]:
    return [part for part in urlsplit(url).path.split("/") if part]


def section(url: str) -> str:
    parts = segments(url)
    return "/" if not parts else f"/{parts[0]}/"


def language_prefix(url: str) -> str | None:
    parts = segments(url)
    if parts and LANGUAGE_PREFIX.match(parts[0]) and parts[0].lower() != "en":
        return parts[0].lower()
    return None


def is_utility_url(url: str) -> bool:
    return any(part.lower() in UTILITY_SEGMENTS for part in segments(url)[:2])


def is_ad_landing_url(url: str) -> bool:
    parts = segments(url)
    query = urlsplit(url).query.lower()
    return bool(
        (parts and parts[0].lower() in AD_LANDING_SEGMENTS)
        or re.search(r"(?:^|&)(?:utm_[a-z]+|gclid|fbclid)=", query)
    )


def select_pages(
    *,
    home: str,
    sitemap_urls: list[str],
    search_pages: list[dict],
    cap: int,
) -> dict:
    """Choose which pages to inspect when the site has more than the cap.

    Order: the homepage, pages with Search Console impressions (most first, up to half the
    cap), one page from every URL section, then the remaining pages taken in turn from
    each section. Deterministic for the same inputs.
    """
    impressions: dict[str, float] = {}
    clicks: dict[str, float] = {}
    form: dict[str, str] = {}
    for url in sitemap_urls:
        form.setdefault(url_key(url), url)
    for row in search_pages:
        key = url_key(row["url"])
        form.setdefault(key, row["url"])
        impressions[key] = impressions.get(key, 0) + row.get("impressions", 0)
        clicks[key] = clicks.get(key, 0) + row.get("clicks", 0)
    form.setdefault(url_key(home), home)

    def rank(key):
        return (-impressions.get(key, 0), -clicks.get(key, 0), key)

    sections: dict[str, list[str]] = {}
    for key in sorted(form, key=rank):
        sections.setdefault(section(form[key]), []).append(key)
    section_order = sorted(
        sections,
        key=lambda name: (
            -sum(impressions.get(k, 0) for k in sections[name]),
            -len(sections[name]),
            name,
        ),
    )
    chosen: list[tuple[str, str]] = []
    taken: set[str] = set()

    def take(key: str, reason: str) -> None:
        if key not in taken and len(chosen) < cap:
            taken.add(key)
            chosen.append((key, reason))

    take(url_key(home), "homepage")
    ranked = [key for key in sorted(impressions, key=rank) if impressions[key] > 0]
    for key in ranked[: max(1, cap // 2)]:
        take(key, "search_impressions")
    for name in section_order:
        take(sections[name][0], "section")
    queues = {name: [k for k in sections[name] if k not in taken] for name in section_order}
    while len(chosen) < cap and any(queues.values()):
        for name in section_order:
            if queues[name]:
                take(queues[name].pop(0), "section_fill")
    return {
        "cap": cap,
        "candidates": len(form),
        "selected": [
            {"url": form[key], "reason": reason, "section": section(form[key])}
            for key, reason in chosen
        ],
        "sections": [
            {
                "section": name,
                "pages": len(sections[name]),
                "selected": sum(1 for k in sections[name] if k in taken),
                "impressions": sum(impressions.get(k, 0) for k in sections[name]),
            }
            for name in section_order
        ][:200],
    }
