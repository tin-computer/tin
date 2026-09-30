"""Site evidence Tin reads itself: robots.txt, sitemaps, static HTML facts and page selection.

Pure parsing and deterministic selection. No network, database or Temporal dependencies;
`organic_audit_fetch` performs the bounded reads and stores what these functions return.
"""

from __future__ import annotations

import html
import json
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
    "Perplexity-User",
    "ClaudeBot",
    "Claude-SearchBot",
    "Claude-User",
    "Google-Extended",
    "Applebot-Extended",
    "Bingbot",
    "CCBot",
)
# Crawlers that fetch pages to answer or cite in a person's question. Bingbot feeds Bing,
# which Copilot and several assistants search through.
AI_SEARCH_CRAWLERS = frozenset(
    {
        "OAI-SearchBot",
        "ChatGPT-User",
        "PerplexityBot",
        "Perplexity-User",
        "Claude-SearchBot",
        "Claude-User",
        "Bingbot",
    }
)
# User agents for the access comparison: the same page read as a browser and as each
# crawler. A CDN that refuses the crawler but serves the browser is likely blocking it.
BROWSER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
CRAWLER_AGENTS = {
    "GPTBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; "
    "+https://openai.com/gptbot)",
    "OAI-SearchBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
    "OAI-SearchBot/1.0; +https://openai.com/searchbot)",
    "ChatGPT-User": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
    "ChatGPT-User/1.0; +https://openai.com/bot)",
    "PerplexityBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
    "PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)",
    "ClaudeBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
    "ClaudeBot/1.0; +claudebot@anthropic.com)",
    "Claude-SearchBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
    "Claude-SearchBot/1.0; +https://www.anthropic.com)",
}
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


# Text inside these elements is not page content a reader sees.
_HIDDEN_TEXT = frozenset({"script", "style", "noscript", "template", "svg", "head", "title"})
# Elements an app shell mounts its client-rendered content into.
_MOUNT_IDS = frozenset({"root", "__next", "app", "__nuxt", "___gatsby", "svelte", "main-app"})
# Analytics tags recognizable from a script URL or inline snippet in the HTML.
ANALYTICS_SIGNATURES = (
    ("Google Analytics", re.compile(r"googletagmanager\.com/gtag/js|google-analytics\.com|gtag\(")),
    ("Google Tag Manager", re.compile(r"googletagmanager\.com/gtm\.js|GTM-[A-Z0-9]{4,}")),
    ("PostHog", re.compile(r"posthog", re.I)),
    ("Plausible", re.compile(r"plausible\.io", re.I)),
    ("Segment", re.compile(r"cdn\.segment\.com|segment\.io/analytics", re.I)),
    ("Fathom", re.compile(r"usefathom\.com", re.I)),
    ("Umami", re.compile(r"umami", re.I)),
    ("Microsoft Clarity", re.compile(r"clarity\.ms", re.I)),
    ("HubSpot", re.compile(r"hs-scripts\.com|js\.hs-analytics\.net", re.I)),
    ("Vercel Analytics", re.compile(r"/_vercel/insights|vercel-insights|va\.vercel-scripts", re.I)),
    ("Cloudflare Web Analytics", re.compile(r"static\.cloudflareinsights\.com", re.I)),
    ("Mixpanel", re.compile(r"mixpanel", re.I)),
    ("Amplitude", re.compile(r"cdn\.amplitude\.com|amplitude\.getInstance", re.I)),
)
NOT_FOUND_TEXT = re.compile(
    r"\b(?:404|not found|page (?:can(?:no|')?t|could not) be found|does(?:n'?t| not) exist|"
    r"no longer (?:exists|available))\b",
    re.I,
)
QUESTION_START = re.compile(
    r"^(?:how|what|why|which|when|where|who|can|does|do|is|are|should|will)\b", re.I
)
MAX_HEADINGS = 8
MAX_HEADING_CHARS = 100
MAX_LEAD_CHARS = 300


class _FactsParser(HTMLParser):
    def __init__(self, url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.url = url
        self.host = (urlsplit(url).hostname or "").lower()
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
        self.json_ld_documents: list[str] = []
        self.microdata = False
        self.description: str | None = None
        self.viewport = False
        self.open_graph: set[str] = set()
        self.meta_author = False
        self.meta_dated = False
        self.time_elements = 0
        self.images = 0
        self.images_without_alt = 0
        self.scripts = 0
        self.mount_point = False
        self.words = 0
        self.lists = 0
        self.tables = 0
        self.external_links = 0
        self.headings: list[str] = []
        self.question_headings = 0
        self.h1_texts: list[str] = []
        self.lead: str | None = None
        self.analytics: set[str] = set()
        self._stack: list[str] = []
        self._hidden = 0
        self._heading: list[str] | None = None
        self._heading_tag: str | None = None
        self._paragraph: list[str] | None = None
        self._paragraph_after_h1 = False
        self._lead_before_h1: str | None = None
        self._script_text: list[str] = []
        self._script_chars = 0
        # Accessible names, as site health used to check them: a link or button needs text,
        # an aria-label, aria-labelledby, a title, or an image with alt text inside it. A form
        # field needs a <label> (wrapping it or naming its id), an aria-label or a title.
        self.unnamed_controls = 0
        self._controls: list[dict] = []
        self._label_depth = 0
        self._label_for: set[str] = set()
        self._fields: list[str | None] = []

    def _scan_analytics(self, text: str) -> None:
        for name, pattern in ANALYTICS_SIGNATURES:
            if name not in self.analytics and pattern.search(text):
                self.analytics.add(name)

    def _accessibility_start(self, tag, values):
        named = any(
            values.get(key, "").strip() for key in ("aria-label", "aria-labelledby", "title")
        )
        if (tag == "a" and values.get("href")) or tag == "button":
            if values.get("aria-hidden", "").lower() != "true":
                self._controls.append({"tag": tag, "named": named})
        elif tag == "img" and self._controls and values.get("alt", "").strip():
            self._controls[-1]["named"] = True
        elif tag == "label":
            self._label_depth += 1
            if values.get("for", "").strip():
                self._label_for.add(values["for"].strip())
        elif tag in {"input", "select", "textarea"}:
            kind = values.get("type", "text").strip().lower()
            if tag == "input" and kind in {"hidden", "submit", "button", "reset", "image"}:
                return
            if named or self._label_depth:
                return
            # Resolved at the end, since <label for> may come after the field.
            self._fields.append(values.get("id", "").strip() or None)

    def _accessibility_end(self, tag):
        if tag == "label" and self._label_depth:
            self._label_depth -= 1
        elif tag in {"a", "button"} and self._controls and self._controls[-1]["tag"] == tag:
            if not self._controls.pop()["named"]:
                self.unnamed_controls += 1

    @property
    def unlabeled_fields(self) -> int:
        return sum(1 for field in self._fields if field is None or field not in self._label_for)

    def handle_starttag(self, tag, attrs):
        values = {name.lower(): (value or "") for name, value in attrs}
        self._accessibility_start(tag, values)
        if tag in _HIDDEN_TEXT:
            self._hidden += 1
        if tag == "html" and not self.seen_html:
            self.seen_html = True
            lang = values.get("lang", "").strip()
            self.lang = lang[:35] if lang else None
        elif tag == "meta":
            name = values.get("name", "").strip().lower()
            prop = values.get("property", "").strip().lower()
            content = values.get("content", "")
            if name in {"robots", "googlebot"}:
                self.robots.extend(
                    token.strip().lower() for token in content.split(",") if token.strip()
                )
            elif name == "description" and self.description is None:
                self.description = " ".join(content.split())[:400]
            elif name == "viewport" and content.strip():
                self.viewport = True
            elif name == "author" and content.strip():
                self.meta_author = True
            if prop in {"og:title", "og:image", "og:description"} and content.strip():
                self.open_graph.add(prop.removeprefix("og:"))
            if (
                prop in {"article:published_time", "article:modified_time"}
                or name in {"date", "last-modified", "article:published_time"}
            ) and content.strip():
                self.meta_dated = True
        elif tag == "link":
            rel = {item.lower() for item in values.get("rel", "").split()}
            href = values.get("href", "").strip()
            if "canonical" in rel and href:
                self.canonicals.append(href[:2000])
            if "alternate" in rel and values.get("hreflang") and href and len(self.hreflang) < 50:
                self.hreflang.append({"lang": values["hreflang"].strip()[:35], "href": href[:2000]})
            if "author" in rel:
                self.meta_author = True
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "title" and self.title is None:
            self._in_title = True
        elif tag == "script":
            kind = values.get("type", "").strip().lower()
            if kind == "application/ld+json":
                self._in_json_ld = True
                self._json_ld_text = []
                self.json_ld_blocks += 1
            else:
                self.scripts += 1
                if values.get("src"):
                    self._scan_analytics(values["src"][:2000])
        elif tag == "img":
            self.images += 1
            if "alt" not in values:
                self.images_without_alt += 1
        elif tag == "time" and values.get("datetime"):
            self.time_elements += 1
        elif tag in {"ul", "ol"}:
            self.lists += 1
        elif tag == "table":
            self.tables += 1
        elif tag == "a":
            href = values.get("href", "").strip()
            target = urlsplit(urljoin(self.url, href)) if href else None
            if target and target.scheme in {"http", "https"} and target.hostname:
                host = target.hostname.lower()
                if host != self.host and host.removeprefix("www.") != self.host.removeprefix(
                    "www."
                ):
                    self.external_links += 1
        elif tag == "div" and values.get("id", "").strip().lower() in _MOUNT_IDS:
            self.mount_point = True
        if tag in {"h1", "h2", "h3"} and self._heading is None:
            self._heading, self._heading_tag = [], tag
        elif tag == "p" and self.lead is None and self._paragraph is None:
            self._paragraph = []
            self._paragraph_after_h1 = bool(self.h1_texts)
        if "itemscope" in values:
            self.microdata = True
        if tag not in _VOID_ELEMENTS:
            self._stack.append(tag)

    def handle_endtag(self, tag):
        self._accessibility_end(tag)
        if tag in self._stack:
            while self._stack:
                if self._stack.pop() == tag:
                    break
        if tag in _HIDDEN_TEXT and self._hidden:
            self._hidden -= 1
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = " ".join("".join(self._title_parts).split())[:200]
        elif tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            text = "".join(self._json_ld_text)
            for found in re.findall(r'"@type"\s*:\s*"([^"]{1,60})"', text):
                if found not in self.json_ld_types and len(self.json_ld_types) < 10:
                    self.json_ld_types.append(found)
            if len(self.json_ld_documents) < 10:
                self.json_ld_documents.append(text)
        elif tag == "script" and self._script_text:
            self._scan_analytics("".join(self._script_text))
            self._script_text = []
        elif tag == self._heading_tag and self._heading is not None:
            text = " ".join("".join(self._heading).split())[:MAX_HEADING_CHARS]
            if text:
                if self._heading_tag == "h1" and len(self.h1_texts) < 3:
                    self.h1_texts.append(text)
                elif len(self.headings) < MAX_HEADINGS:
                    self.headings.append(text)
                if self._heading_tag != "h1" and (text.endswith("?") or QUESTION_START.match(text)):
                    self.question_headings += 1
            self._heading, self._heading_tag = None, None
        elif tag == "p" and self._paragraph is not None:
            text = " ".join("".join(self._paragraph).split())
            self._paragraph = None
            if len(text.split()) >= 5:
                if self._paragraph_after_h1:
                    self.lead = text[:MAX_LEAD_CHARS]
                elif self._lead_before_h1 is None:
                    self._lead_before_h1 = text[:MAX_LEAD_CHARS]

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_json_ld and sum(map(len, self._json_ld_text)) < 200_000:
            self._json_ld_text.append(data)
        elif self._stack and self._stack[-1] == "script":
            if self._script_chars < 200_000:
                self._script_text.append(data[:20_000])
                self._script_chars += min(len(data), 20_000)
        elif not self._hidden:
            if self._controls and data.strip():
                self._controls[-1]["named"] = True
            self.words += len(data.split())
            if self._heading is not None:
                self._heading.append(data)
            if self._paragraph is not None:
                self._paragraph.append(data)


_VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)

# Required and recommended properties Tin checks for common structured data types, from
# Google's structured data documentation. "any" means at least one of the listed fields.
SCHEMA_RULES = {
    "Organization": {"required": ("name",), "recommended": ("url", "logo")},
    "Article": {"required": ("headline",), "recommended": ("author", "datePublished")},
    "BlogPosting": {"required": ("headline",), "recommended": ("author", "datePublished")},
    "NewsArticle": {"required": ("headline",), "recommended": ("author", "datePublished")},
    "TechArticle": {"required": ("headline",), "recommended": ("author", "datePublished")},
    "FAQPage": {"required": ("mainEntity",), "recommended": ()},
    "BreadcrumbList": {"required": ("itemListElement",), "recommended": ()},
    "Product": {"required": ("name",), "any": ("offers", "review", "aggregateRating")},
    "SoftwareApplication": {"required": ("name",), "any": ("offers", "aggregateRating")},
}
SCHEMA_ALIASES = {"Corporation": "Organization", "LocalBusiness": "Organization"}


def _schema_items(value, depth: int = 0):
    if depth > 6:
        return
    if isinstance(value, list):
        for item in value[:50]:
            yield from _schema_items(item, depth + 1)
    elif isinstance(value, dict):
        yield value
        if "@graph" in value:
            yield from _schema_items(value["@graph"], depth + 1)


def _types(item: dict) -> list[str]:
    value = item.get("@type")
    values = value if isinstance(value, list) else [value]
    return [v.split("/")[-1] for v in values if isinstance(v, str)][:5]


def _present(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None


def _faq_problems(item: dict) -> list[str]:
    entities = item.get("mainEntity")
    entities = entities if isinstance(entities, list) else [entities] if entities else []
    for entity in entities[:50]:
        if not isinstance(entity, dict) or not _present(entity.get("name")):
            return ["mainEntity[].name"]
        answer = entity.get("acceptedAnswer")
        answer = answer[0] if isinstance(answer, list) and answer else answer
        if not isinstance(answer, dict) or not _present(answer.get("text")):
            return ["mainEntity[].acceptedAnswer.text"]
    return []


def _breadcrumb_problems(item: dict) -> list[str]:
    elements = item.get("itemListElement")
    elements = elements if isinstance(elements, list) else []
    for element in elements[:50]:
        if not isinstance(element, dict) or "position" not in element:
            return ["itemListElement[].position"]
        if not _present(element.get("name")) and not _present(element.get("item")):
            return ["itemListElement[].name"]
    return []


def structured_data(documents: list[str]) -> dict:
    """Parse JSON-LD blocks and check the common types' required fields.

    Only what the static HTML contains; structured data added by JavaScript is not seen.
    """
    problems: list[dict] = []
    invalid = 0
    dated = author = False
    for text in documents:
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            invalid += 1
            continue
        for item in _schema_items(value):
            if _present(item.get("datePublished")) or _present(item.get("dateModified")):
                dated = True
            if _present(item.get("author")):
                author = True
            for name in _types(item):
                rule = SCHEMA_RULES.get(SCHEMA_ALIASES.get(name, name))
                if rule is None:
                    continue
                missing = [field for field in rule["required"] if not _present(item.get(field))]
                if rule.get("any") and not any(_present(item.get(f)) for f in rule["any"]):
                    missing.append(" or ".join(rule["any"]))
                if name == "FAQPage" and not missing:
                    missing.extend(_faq_problems(item))
                if name == "BreadcrumbList" and not missing:
                    missing.extend(_breadcrumb_problems(item))
                recommended = [
                    field for field in rule.get("recommended", ()) if not _present(item.get(field))
                ]
                if (missing or recommended) and len(problems) < 10:
                    problems.append(
                        {"type": name, "missing": missing, "recommended_missing": recommended}
                    )
    return {"problems": problems, "invalid_blocks": invalid, "dated": dated, "author": author}


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
    parser = _FactsParser(url)
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError):
        pass
    canonical = None
    if parser.canonicals:
        canonical = urljoin(url, html.unescape(parser.canonicals[0]))[:2000]
    schema = structured_data(parser.json_ld_documents)
    heading_text = " ".join([parser.title or "", *parser.h1_texts])
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
        "description_length": len(parser.description) if parser.description is not None else None,
        "viewport": parser.viewport,
        "open_graph": sorted(parser.open_graph),
        "images": parser.images,
        "images_without_alt": parser.images_without_alt,
        "text_words": parser.words,
        "scripts": parser.scripts,
        "mount_point": parser.mount_point,
        "h1_texts": parser.h1_texts,
        "headings": parser.headings,
        "question_headings": parser.question_headings,
        "lead": parser.lead or parser._lead_before_h1,
        "lists": parser.lists,
        "tables": parser.tables,
        "external_links": parser.external_links,
        "dated": parser.meta_dated or parser.time_elements > 0 or schema["dated"],
        "author": parser.meta_author or schema["author"],
        "schema_problems": schema["problems"],
        "schema_invalid_blocks": schema["invalid_blocks"],
        "analytics": sorted(parser.analytics),
        "not_found_text": bool(NOT_FOUND_TEXT.search(heading_text)),
        "unnamed_controls": parser.unnamed_controls,
        "unlabeled_fields": parser.unlabeled_fields,
    }


def client_rendered(facts: dict) -> bool:
    """The HTML is an app shell: scripts, almost no text and no heading before JavaScript runs."""
    return (
        facts.get("fetch") in {None, "observed"}
        and facts.get("text_words") is not None
        and facts["text_words"] < 50
        and facts.get("h1_count", 0) == 0
        and (facts.get("scripts", 0) > 0 or facts.get("mount_point", False))
    )


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
