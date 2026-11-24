"""Google News article links, unwrapped to the publisher's URL only where a reader sees them.

Fetches keep the `news.google.com/rss/articles/<id>` links: unwrapping takes two paced
requests per link, and a day's fetch holds hundreds. After the judge, code unwraps only the
links its report and brief use (`unwrap_text`), caching every answer in `intel_link_cache`
for all runs. A link that won't unwrap stays a Google News link, which still works.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from tin_lite.intel_fetch.http import PublicReader

ARTICLE = re.compile(
    r"https://news\.google\.com/rss/articles/([A-Za-z0-9_\-]{10,600})(\?[^\s)\]>|\"']*)?"
)
_ID = re.compile(r"/articles/([^?/]+)")
BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def article_id(link: str) -> str | None:
    match = _ID.search(link or "")
    return match.group(1) if match and len(match.group(1)) <= 600 else None


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


async def unwrap_text(conn, reader: PublicReader, text: str, *, limit: int = 60) -> tuple[str, int]:
    """Replace the Google News article links in `text` with publisher URLs, at most `limit`
    distinct links. Returns the text and how many links were replaced; a link that won't
    unwrap is left as it is."""
    # Sentence punctuation after a link is not part of it.
    links = list(
        dict.fromkeys(match.group(0).rstrip(".,;:!?") for match in ARTICLE.finditer(text or ""))
    )
    replaced = 0
    found: dict[str, str] = {}
    for link in links[:limit]:
        try:
            url = await resolve(conn, reader, link)
        except Exception:  # noqa: BLE001 — a link that won't unwrap keeps working as it is
            url = None
        if url:
            found[link] = url
    if not found:
        return text, 0
    pattern = re.compile("|".join(re.escape(link) for link in sorted(found, key=len, reverse=True)))

    def swap(match: re.Match) -> str:
        nonlocal replaced
        replaced += 1
        return found[match.group(0)]

    return pattern.sub(swap, text), replaced
