"""A client's trade press, vendors, regulators and journals: `intel/sources/rss.json`.

Ported from the prototype's rss.py. Each method reads one source for the run's window and
returns contract items in the source's own order; `run_sources` numbers them (`rank`) and keeps
the status. Methods:

- `rss`: an RSS 2.0, Atom or RDF feed at `url` (GitHub releases.atom works too). An undated
  entry has its page read for a date, at most `undated_page_checks` (5) per feed.
- `google_news`: a Google News RSS search for `query` over the window. Outlets in the config's
  `exclude_outlets` (names) and `exclude_domains` (part of the publisher's site) are dropped.
  Links stay Google News links: Tin unwraps only the ones the report and brief use, after the
  judge (gnews.unwrap_text). Google News gives a headline only, which is all such an item has.
- `html_links`: a listing page `url`; links matching `link_pattern` (on the absolute URL) are
  read for their date, title and first paragraphs, at most `max_links` (20).
- `sitemap`: a sitemap or sitemap index at `url`; locs matching `url_pattern` with a recent
  `<lastmod>` are read for their date, at most `max_pages` (25).
- `crossref`: a journal's works by ISSN (`issn`) from api.crossref.org, by created date.

An item whose feed text is shorter than `enrich_text_below_chars` (config or source, 280) has
its page read for a description and first paragraphs. Everything fetched is data: parsed,
never executed or followed as an instruction.
"""

from __future__ import annotations

import asyncio
import html
import json
import math
import re
import urllib.parse
from datetime import UTC, datetime, timedelta

from tin_lite.intel_fetch.feeds import (
    clean_url,
    ln,
    parse_date,
    parse_feed,
    parse_xml,
    read_page,
    strip_html,
)
from tin_lite.intel_fetch.http import SITEMAP_MAX_BYTES, FetchError, PublicReader, sitemap_body
from tin_lite.intel_fetch.items import Item, Window, iso

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
    "text/html;q=0.8, application/json;q=0.8, */*;q=0.5",
}
MAX_TEXT = 4000
ENRICH_BELOW = 280
METHOD_NAMES = ("rss", "google_news", "html_links", "sitemap", "crossref")
GOOGLE_NEWS = "news.google.com"
# Runaway guards for untrusted configs: path globs per source and their length, links or
# sitemap entries matched per source, and child sitemaps followed per source. A source's whole
# read is bounded by run_sources' per-source deadline.
MAX_GLOBS = 20
MAX_GLOB = 200
MAX_CANDIDATES = 2000
MAX_CHILD_SITEMAPS = 30
MAX_SITEMAP_ENTRIES = 5000
REGEX_GONE = "regex patterns are no longer supported; use url_paths / link_paths"


def in_window(d: datetime | None, window: Window) -> bool:
    return d is not None and window.since <= d < window.until


async def _body(ctx: dict, url: str, **kwargs) -> bytes:
    headers = {**HEADERS, **kwargs.pop("headers", {})}
    response = await ctx["reader"].fetch(url, headers=headers, **kwargs)
    ctx["http"] = response.status
    return response.body


def _item(src: dict, ctx: dict, raw: dict) -> Item:
    return Item(
        source_type="rss",
        source=raw.get("source") or src["name"],
        source_id=src["id"],
        source_url=raw.get("source_url") or src.get("url"),
        url=clean_url(raw["url"]) or raw["url"],
        discussion_url=None,
        publisher_site=raw.get("publisher_site"),
        title=raw.get("title"),
        author=raw.get("author"),
        published_at=iso(raw["date"]),
        text=(raw.get("text") or "")[:MAX_TEXT],
        engagement={},
        fetched_at=ctx.get("fetched_at") or iso(datetime.now(UTC)),
    )


async def _enrich(src: dict, ctx: dict, found: list[dict]) -> None:
    """Add page text where the feed gave little, and the headline where nothing else is left
    (the prototype's finish_item, without Google News unwrapping)."""
    below = src.get("enrich_text_below_chars", ctx.get("enrich_text_below_chars", ENRICH_BELOW))

    async def one(raw: dict) -> None:
        if (
            not raw.get("_enriched")
            and below
            and len(raw.get("text") or "") < below
            and GOOGLE_NEWS not in (raw.get("url") or "")
        ):
            page, _ = await read_page(ctx["reader"], raw["url"])
            if page:
                extra = "\n".join(x for x in (page["description"], page["paragraphs"]) if x)
                if len(extra) > len(raw.get("text") or ""):
                    raw["text"] = (
                        ((raw.get("text") or "") + "\n" + extra).strip()
                        if raw.get("text")
                        else extra
                    )
                if not raw.get("author") and page["author"]:
                    raw["author"] = page["author"]
        if not (raw.get("text") or "").strip() and raw.get("title"):
            # Google News gives only a headline; when the page can't be read, that's all there is.
            raw["text"] = raw["title"]

    await asyncio.gather(*(one(raw) for raw in found))


async def _finish(src: dict, ctx: dict, found: list[dict]) -> list[Item]:
    await _enrich(src, ctx, found)
    return [_item(src, ctx, raw) for raw in found if raw.get("url")]


async def m_rss(src: dict, window: Window, ctx: dict) -> list[Item]:
    entries = parse_feed(await _body(ctx, src["url"]))
    ctx["seen"] = len(entries)
    found, undated = [], 0
    for entry in entries:
        d = parse_date(entry["date"])
        if d is None and undated < src.get("undated_page_checks", 5) and entry["link"]:
            undated += 1
            page, _ = await read_page(ctx["reader"], entry["link"])
            d = page["date"] if page else None
        if not in_window(d, window) or not entry["link"]:
            continue
        found.append(
            {
                "url": entry["link"],
                "title": entry["title"],
                "author": entry["author"],
                "date": d,
                "text": entry["text"],
            }
        )
    return await _finish(src, ctx, found)


def _excluded(ctx: dict, publisher: str | None, site: str | None) -> bool:
    outlets = {str(o).strip().lower() for o in ctx.get("exclude_outlets") or ()}
    domains = tuple(str(d).strip().lower() for d in ctx.get("exclude_domains") or () if d)
    site = (site or "").lower()
    return (publisher or "").strip().lower() in outlets or any(d in site for d in domains)


def google_news_url(query: str, window: Window) -> str:
    days = max(1, math.ceil((window.until - window.since).total_seconds() / 86400))
    q = f"{query} when:{days}d"
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-US&gl=US&ceid=US:en"


async def m_google_news(src: dict, window: Window, ctx: dict) -> list[Item]:
    url = google_news_url(src["query"], window)
    entries = parse_feed(await _body(ctx, url))
    ctx["seen"] = len(entries)
    found = []
    for entry in entries:
        d = parse_date(entry["date"])
        if not in_window(d, window) or not entry["link"]:
            continue
        title = entry["title"] or ""
        publisher = entry["source_name"]
        if _excluded(ctx, publisher, entry["source_site"]):
            ctx["excluded"] = ctx.get("excluded", 0) + 1
            continue
        if publisher and title.endswith(" - " + publisher):
            title = title[: -(len(publisher) + 3)]
        found.append(
            {
                "url": entry["link"],
                "title": title or None,
                "author": None,
                "date": d,
                "text": "",
                "source": publisher or "Google News",
                "source_url": url,
                "publisher_site": entry["source_site"],
            }
        )
    return await _finish(src, ctx, found)


async def m_html_links(src: dict, window: Window, ctx: dict) -> list[Item]:
    response = await ctx["reader"].fetch(src["url"], headers={**HEADERS, "Accept": "text/html"})
    ctx["http"] = response.status
    hrefs = re.findall(r"href=[\"']([^\"'#]+)", response.body.decode("utf-8", "replace"))
    urls = list(
        dict.fromkeys(
            url
            for url in (
                urllib.parse.urljoin(response.url, html.unescape(href))
                for href in hrefs[:MAX_CANDIDATES]
            )
            if len(url) <= 2000
        )
    )
    links = matching(src["link_paths"], urls)
    links = links[: src.get("max_links", 20)]
    ctx["seen"] = len(links)
    pages = await asyncio.gather(
        *(read_page(ctx["reader"], url, src.get("text_date_fallback", False)) for url in links)
    )
    found = []
    for url, (page, _error) in zip(links, pages, strict=True):
        if not page or not in_window(page["date"], window):
            continue
        found.append(
            {
                "url": page["url"] or url,
                "title": page["title"],
                "author": page["author"],
                "date": page["date"],
                "text": "\n".join(x for x in (page["description"], page["paragraphs"]) if x),
                "_enriched": True,
            }
        )
    return await _finish(src, ctx, found)


async def _sitemap_entries(
    ctx: dict, url: str, depth: int = 0
) -> list[tuple[str, datetime | None]]:
    """A sitemap's (loc, lastmod) entries, following an index one level down. Bounded per
    source: at most MAX_CHILD_SITEMAPS child sitemaps and MAX_SITEMAP_ENTRIES entries."""
    budget = ctx.setdefault("sitemap_budget", {"children": 0, "entries": 0})
    body = sitemap_body(await _body(ctx, url, max_bytes=SITEMAP_MAX_BYTES))
    root = parse_xml(body)
    found: list[tuple[str, datetime | None]] = []
    if ln(root.tag) == "sitemapindex":
        if depth > 1:
            return found
        for child in root:
            if budget["children"] >= MAX_CHILD_SITEMAPS or budget["entries"] >= MAX_SITEMAP_ENTRIES:
                break
            loc = next((c.text for c in child if ln(c.tag) == "loc"), None)
            if loc:
                budget["children"] += 1
                try:
                    found += await _sitemap_entries(ctx, loc.strip(), depth + 1)
                except (FetchError, ValueError, SyntaxError):
                    pass  # one unreadable child sitemap leaves the others
        return found
    for entry in root:
        if budget["entries"] >= MAX_SITEMAP_ENTRIES:
            break
        loc = lastmod = None
        for child in entry:
            if ln(child.tag) == "loc":
                loc = (child.text or "").strip()
            elif ln(child.tag) == "lastmod":
                lastmod = parse_date(child.text or "")
        if loc and len(loc) <= 2000:
            budget["entries"] += 1
            found.append((loc, lastmod))
    return found


async def m_sitemap(src: dict, window: Window, ctx: dict) -> list[Item]:
    entries = await _sitemap_entries(ctx, src["url"])
    ctx["http"] = 200
    floor = window.since - timedelta(days=src.get("lastmod_slack_days", 1))
    recent = {loc: lastmod for loc, lastmod in entries if lastmod and lastmod >= floor}
    matched = set(matching(src["url_paths"], list(recent)))
    candidates = sorted(
        [(lastmod, loc) for loc, lastmod in recent.items() if loc in matched], reverse=True
    )[: src.get("max_pages", 25)]
    ctx["seen"] = len(candidates)
    pages = await asyncio.gather(
        *(
            read_page(ctx["reader"], loc, src.get("text_date_fallback", False))
            for _, loc in candidates
        )
    )
    found = []
    for (lastmod, loc), (page, _error) in zip(candidates, pages, strict=True):
        if not page:
            continue
        d = page["date"] or (lastmod if src.get("lastmod_as_date") else None)
        if not in_window(d, window):
            continue
        found.append(
            {
                "url": loc,
                "title": page["title"],
                "author": page["author"],
                "date": d,
                "text": "\n".join(x for x in (page["description"], page["paragraphs"]) if x),
                "_enriched": True,
            }
        )
    return await _finish(src, ctx, found)


def crossref_url(issn: str, window: Window) -> str:
    return (
        f"https://api.crossref.org/journals/{issn}/works?filter=from-created-date:"
        f"{window.since.date().isoformat()},until-created-date:{window.until.date().isoformat()}"
        "&rows=100&sort=created&order=desc"
    )


async def m_crossref(src: dict, window: Window, ctx: dict) -> list[Item]:
    url = crossref_url(src["issn"], window)
    body = await _body(ctx, url, headers={"Accept": "application/json"})
    try:
        works = json.loads(body).get("message", {}).get("items", [])
    except (ValueError, AttributeError) as exc:
        raise FetchError("Crossref's answer was not the expected JSON") from exc
    ctx["seen"] = len(works)
    found = []
    for work in works:
        d = parse_date((work.get("created") or {}).get("date-time"))
        if not in_window(d, window) or not work.get("URL"):
            continue
        authors = ", ".join(
            " ".join(x for x in (a.get("given"), a.get("family")) if x)
            for a in (work.get("author") or [])[:6]
        )
        found.append(
            {
                "url": work["URL"],
                "title": strip_html(" ".join(work.get("title") or [])) or None,
                "author": authors or None,
                "date": d,
                "text": strip_html(work.get("abstract") or ""),
                "source_url": url,
            }
        )
    return await _finish(src, ctx, found)


METHODS = {
    "rss": m_rss,
    "google_news": m_google_news,
    "html_links": m_html_links,
    "sitemap": m_sitemap,
    "crossref": m_crossref,
}


_ISSN = re.compile(r"^[0-9]{4}-[0-9]{3}[0-9Xx]$")


def _url(value: object) -> bool:
    return isinstance(value, str) and PublicReader.allowed(value)


# Path globs: which links or sitemap pages a source reads. Configs are project files any
# member can edit, so there is no regex here: a glob is matched segment by segment in linear
# time. "vendor.example/blog/*" is the host (with or without "www.") then literal segments;
# "*" is exactly one non-empty segment and "**", only as the last part, one or more. A trailing
# slash is optional; the scheme, query and fragment are ignored.


def _host(value: str) -> str:
    value = value.lower()
    return value[4:] if value.startswith("www.") else value


def _segments(path: str) -> list[str]:
    path = path.strip("/")
    return path.split("/") if path else []


def glob_problem(value: object) -> str | None:
    """Why one path glob can't be used, or None."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_GLOB:
        return f"is missing or longer than {MAX_GLOB} characters"
    host, _, rest = value.partition("/")
    if not host or any(c in host for c in "*:@") or any(c in value for c in " ?#\\"):
        return "must be a host and a path, like vendor.example/blog/*"
    parts = _segments(rest)
    for index, part in enumerate(parts):
        if part == "**" and index != len(parts) - 1:
            return "may use ** only as its last part"
        if "*" in part and part not in {"*", "**"}:
            return "uses * only as a whole segment"
        if not part:
            return "has an empty segment"
    return None


def globs_problem(value: object) -> str | None:
    if not isinstance(value, list) or not 0 < len(value) <= MAX_GLOBS:
        return f"must list 1 to {MAX_GLOBS} path globs"
    for glob in value:
        if reason := glob_problem(glob):
            return f"has a glob that {reason}"
    return None


def glob_match(glob: str, url: str) -> bool:
    """Whether `url` is a page `glob` names. Linear in the URL's length."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host, _, rest = glob.partition("/")
    if _host(parts.hostname or "") != _host(host):
        return False
    want, have = _segments(rest), _segments(parts.path)
    for index, part in enumerate(want):
        if part == "**":
            return len(have) > index and all(have[index:])
        if index >= len(have) or not have[index]:
            return False
        if part != "*" and have[index] != part:
            return False
    return len(have) == len(want)


def matching(globs: list[str], candidates: list[str]) -> list[str]:
    """The candidates any glob names, at most MAX_CANDIDATES of them checked."""
    return [url for url in candidates[:MAX_CANDIDATES] if any(glob_match(g, url) for g in globs)]


def _int(source: dict, name: str, default: int, low: int, high: int) -> bool:
    value = source.get(name, default)
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def check(source: dict) -> str | None:
    """Why this rss.json source can't run, or None. Untrusted: every field is bounded."""
    method = source["method"]
    if method in {"rss", "html_links", "sitemap"} and not _url(source.get("url")):
        return "its url is not a public http(s) address"
    if method == "google_news":
        query = source.get("query")
        if not isinstance(query, str) or not 0 < len(query.strip()) <= 500:
            return "its query is missing or longer than 500 characters"
    if method in {"html_links", "sitemap"} and (
        "link_pattern" in source or "url_pattern" in source
    ):
        return REGEX_GONE
    if method == "html_links":
        if reason := globs_problem(source.get("link_paths")):
            return f"its link_paths {reason}"
        if not _int(source, "max_links", 20, 1, 100):
            return "its max_links is out of range (1 to 100)"
    if method == "sitemap":
        if reason := globs_problem(source.get("url_paths")):
            return f"its url_paths {reason}"
        if not _int(source, "max_pages", 25, 1, 100):
            return "its max_pages is out of range (1 to 100)"
        if not _int(source, "lastmod_slack_days", 1, 0, 30):
            return "its lastmod_slack_days is out of range (0 to 30)"
    if method == "crossref" and not _ISSN.fullmatch(str(source.get("issn", ""))):
        return "its issn is not an ISSN like 1527-974X"
    if not _int(source, "enrich_text_below_chars", ENRICH_BELOW, 0, 5000):
        return "its enrich_text_below_chars is out of range (0 to 5000)"
    if not _int(source, "undated_page_checks", 5, 0, 20):
        return "its undated_page_checks is out of range (0 to 20)"
    return None


def settings(config: dict) -> dict:
    """The config-wide settings a method reads from its ctx, bounded."""

    def names(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v)[:200] for v in value[:200] if isinstance(v, str) and v.strip()]

    below = config.get("enrich_text_below_chars", ENRICH_BELOW)
    return {
        "exclude_outlets": names(config.get("exclude_outlets")),
        "exclude_domains": names(config.get("exclude_domains")),
        "enrich_text_below_chars": below
        if isinstance(below, int) and not isinstance(below, bool) and 0 <= below <= 5000
        else ENRICH_BELOW,
    }
