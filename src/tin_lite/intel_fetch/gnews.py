"""Google News article links, unwrapped to the publisher's URL only where it pays.

Items keep their `news.google.com/rss/articles/<id>` links: unwrapping takes two paced requests
per link, and a day's fetch holds hundreds. Two places unwrap, caching every answer in
`intel_link_cache` for all runs:

- a client's fetch reads the publisher's page for the top few headline-only items of each
  Google News source (`add_publisher_text`), so the analyst has article text, not a headline;
- after the judge, code unwraps the links its report and brief use (`unwrap_text`).

A link that won't unwrap stays a Google News link, which still works, and an item whose page
won't read keeps its headline. `is_article` is the one test of what counts as an article link,
and `resolve_many` the one loop that unwraps a list of them (the world summary, a client's
brief and report).
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from collections.abc import Awaitable, Callable

from tin_lite.intel_fetch.feeds import read_page
from tin_lite.intel_fetch.http import PublicReader

ARTICLE = re.compile(
    r"https://news\.google\.com/rss/articles/([A-Za-z0-9_\-]{10,600})(\?[^\s)\]>|\"']*)?"
)
_ID = re.compile(r"/articles/([^?/]+)")
# Distinct links unwrapped per call: each is two paced requests to Google News.
UNWRAP_LIMIT = 60
BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
# Publisher text during a client's fetch: the top items of each Google News source, at most
# TEXT_MAX_ITEMS a run, within TEXT_SECONDS. Google's own requests are paced process-wide
# (http.HOST_GAP), so an uncached item costs about 3 s; publisher pages read alongside.
TEXT_PER_SOURCE = 3
TEXT_MAX_ITEMS = 90
TEXT_SECONDS = 360
TEXT_CONCURRENCY = 4


def article_id(link: str) -> str | None:
    match = _ID.search(link or "")
    return match.group(1) if match and len(match.group(1)) <= 600 else None


def is_article(link: object) -> bool:
    """A Google News article link Tin can unwrap."""
    return isinstance(link, str) and ARTICLE.fullmatch(link) is not None


async def link_cache_get(conn, gid: str) -> str | None:
    return await conn.fetchval("SELECT url FROM intel_link_cache WHERE gid = $1", gid)


async def link_cache_put(conn, gid: str, url: str) -> None:
    if not re.match(r"^https?://", url) or len(url) > 2000 or len(gid) > 600:
        return
    await conn.execute(
        "INSERT INTO intel_link_cache (gid, url) VALUES ($1, $2) ON CONFLICT (gid) DO NOTHING",
        gid,
        url,
    )


async def decode(reader: PublicReader, gid: str) -> str | None:
    """The publisher URL behind one article id (two requests), or None."""
    page = await reader.get(
        f"https://news.google.com/rss/articles/{gid}?hl=en-US&gl=US&ceid=US:en",
        headers={"Accept": "text/html"},
    )
    if page.status != 200:
        return None
    text = page.body.decode("utf-8", "replace")
    sg = re.search(r'data-n-a-sg="([^"]+)"', text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', text)
    if not sg or not ts or not ts.group(1).isdigit():
        return None
    inner = json.dumps(
        [
            "garturlreq",
            [
                ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1]
                + [None, None, None, None, None, 0, 1],
                "X",
                "X",
                1,
                [1, 1, 1],
                1,
                1,
                None,
                0,
                0,
                None,
                0,
            ],
            gid,
            int(ts.group(1)),
            sg.group(1),
        ],
        separators=(",", ":"),
    )
    freq = json.dumps([[["Fbv4je", inner, None, "generic"]]], separators=(",", ":"))
    answer = await reader.get(
        BATCH,
        data=urllib.parse.urlencode({"f.req": freq}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    if answer.status != 200:
        return None
    try:
        arr = json.loads(answer.body.decode("utf-8", "replace").split("\n\n", 1)[1])
        url = json.loads(arr[0][2])[1]
    except (ValueError, IndexError, KeyError, TypeError):
        return None
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


async def resolve(conn, reader: PublicReader, link: str) -> str | None:
    gid = article_id(link)
    if gid is None:
        return None
    cached = await link_cache_get(conn, gid)
    if cached:
        return cached
    url = await decode(reader, gid)
    if url:
        await link_cache_put(conn, gid, url)
    return url


async def resolve_many(
    conn, reader: PublicReader, links, *, limit: int = UNWRAP_LIMIT, heartbeat=None
) -> dict[str, str]:
    """Publisher URLs for the Google News article links among `links`, at most `limit`
    distinct ones, in order. A link that won't unwrap is left out and keeps working as it is.
    `heartbeat(found)` is called after each link."""
    wanted = [link for link in dict.fromkeys(links) if is_article(link)][:limit]
    found: dict[str, str] = {}
    for link in wanted:
        try:
            url = await resolve(conn, reader, link)
        except Exception:  # noqa: BLE001 — a link that won't unwrap keeps working as it is
            url = None
        if url:
            found[link] = url
        if heartbeat is not None:
            heartbeat(len(found))
    return found


async def unwrap_text(
    conn, reader: PublicReader, text: str, *, limit: int = UNWRAP_LIMIT
) -> tuple[str, int]:
    """Replace the Google News article links in `text` with publisher URLs, at most `limit`
    distinct links. Returns the text and how many links were replaced; a link that won't
    unwrap is left as it is."""
    # Sentence punctuation after a link is not part of it.
    links = [match.group(0).rstrip(".,;:!?") for match in ARTICLE.finditer(text or "")]
    found = await resolve_many(conn, reader, links, limit=limit)
    if not found:
        return text, 0
    replaced = 0
    pattern = re.compile("|".join(re.escape(link) for link in sorted(found, key=len, reverse=True)))

    def swap(match: re.Match) -> str:
        nonlocal replaced
        replaced += 1
        return found[match.group(0)]

    return pattern.sub(swap, text), replaced


def page_text(page: dict | None) -> str:
    """A publisher page's description and first paragraphs, as an item's text."""
    if not page:
        return ""
    return "\n".join(part for part in (page.get("description"), page.get("paragraphs")) if part)


async def add_publisher_text(
    conn,
    reader: PublicReader,
    rows: list[dict],
    save: Callable[[str, str], Awaitable[None]],
    *,
    seconds: float = TEXT_SECONDS,
    concurrency: int = TEXT_CONCURRENCY,
) -> dict[str, int]:
    """For each headline-only item (`url`, `canon_url`, `title`), unwrap its link (the cache
    first), read the publisher's page and `save(canon_url, text)`. Each item is saved as it
    finishes, so running out of `seconds` keeps what was read; an item whose link won't unwrap
    or whose page won't read (a paywall, a 403) keeps its headline. Never raises for a read."""
    counts = {"wanted": len(rows), "unwrapped": 0, "with_text": 0}
    gate = asyncio.Semaphore(concurrency)

    async def one(row: dict) -> None:
        async with gate:
            try:
                publisher = await resolve(conn, reader, row["url"])
            except Exception:  # noqa: BLE001 — a link that won't unwrap keeps its headline
                return
            if not publisher:
                return
            counts["unwrapped"] += 1
            try:
                page, _error = await read_page(reader, publisher)
            except Exception:  # noqa: BLE001 — a page that won't read keeps the headline
                return
            text = page_text(page).strip()
            # Less than the headline says nothing: a cookie wall or an empty page.
            if len(text) <= len(row.get("title") or "") + 20:
                return
            await save(row["canon_url"], text)
            counts["with_text"] += 1

    tasks = [asyncio.create_task(one(row)) for row in rows]
    if tasks:
        _done, pending = await asyncio.wait(tasks, timeout=seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return counts
