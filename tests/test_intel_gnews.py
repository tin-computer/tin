"""Google News links: decoded to the publisher only where a reader sees them, cached for all."""

from __future__ import annotations

import asyncio
import json

from tin_lite.intel_fetch import gnews
from tin_lite.intel_fetch.http import Response

GID = "CBMiVkFVX3lxTE1hc2tRcG5zRVd6"
OTHER = "CBMiOTHERxxxxxxxxxxxxxxxxxxxx"
LINK = f"https://news.google.com/rss/articles/{GID}?oc=5"
BROKEN = f"https://news.google.com/rss/articles/{OTHER}?oc=5"
PUBLISHER = "https://www.reuters.com/markets/a-story-2026-11-24/"


class Cache:
    """intel_link_cache as a connection sees it."""

    def __init__(self):
        self.rows: dict[str, str] = {}

    async def fetchval(self, query, gid):
        return self.rows.get(gid)

    async def execute(self, query, gid, url):
        self.rows.setdefault(gid, url)


class Reader:
    def __init__(self):
        self.calls: list[str] = []

    async def get(self, url, *, data=None, headers=None, **kwargs):
        self.calls.append(url)
        if url.startswith(f"https://news.google.com/rss/articles/{GID}"):
            page = '<c-wiz data-n-a-sg="SIG123" data-n-a-ts="1763990000"></c-wiz>'
            return Response(200, page.encode(), url)
        if url.startswith("https://news.google.com/rss/articles/"):
            return Response(200, b"<html>no signature</html>", url)
        if url == gnews.BATCH:
            assert b"SIG123" in data and GID.encode() in data
            inner = json.dumps(["garturlres", PUBLISHER, 1])
            body = ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner]])
            return Response(200, body.encode(), url)
        return Response(404, b"", url, "HTTP 404")


async def test_decode_reads_the_publisher_url():
    assert await gnews.decode(Reader(), GID) == PUBLISHER
    assert await gnews.decode(Reader(), OTHER) is None


async def test_unwrap_replaces_decoded_links_caches_them_and_keeps_the_rest():
    cache, reader = Cache(), Reader()
    text = f"See [Reuters]({LINK}) and [another]({BROKEN}), and again {LINK}."
    unwrapped, count = await gnews.unwrap_text(cache, reader, text)
    assert (
        unwrapped == f"See [Reuters]({PUBLISHER}) and [another]({BROKEN}), and again {PUBLISHER}."
    )
    assert count == 2
    assert cache.rows == {GID: PUBLISHER}
    # The cache answers next time: no request.
    reader.calls.clear()
    again, _ = await gnews.unwrap_text(cache, reader, LINK)
    assert again == PUBLISHER and reader.calls == []


async def test_unwrap_is_bounded_and_survives_a_failing_reader():
    class Failing(Reader):
        async def get(self, url, **kwargs):
            raise RuntimeError("network down")

    links = " ".join(f"https://news.google.com/rss/articles/CBMi{n:08d}xxxxxx" for n in range(5))
    text, count = await gnews.unwrap_text(Cache(), Failing(), links, limit=2)
    assert text == links and count == 0


def test_article_ids():
    assert gnews.article_id(LINK) == GID
    assert gnews.article_id("https://example.com/") is None


async def test_resolve_many_is_the_one_unwrap_loop():
    """Only article links, each once, at most `limit`; a link that won't unwrap is left out."""
    reader = Reader()
    found = await gnews.resolve_many(
        Cache(),
        reader,
        [
            LINK,
            "https://news.google.com/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB",
            "https://www.reuters.com/a",
            LINK,
            BROKEN,
        ],
        limit=2,
    )
    assert found == {LINK: PUBLISHER}
    assert gnews.is_article(LINK) and not gnews.is_article("https://news.google.com/home")
    assert not any("topics" in call for call in reader.calls)


class PublisherReader(Reader):
    """The Reader above, plus the publisher's article page; `paywalled` returns a cookie wall."""

    def __init__(self, page: bytes):
        super().__init__()
        self.page = page

    async def get(self, url, *, data=None, headers=None, **kwargs):
        if url == PUBLISHER:
            self.calls.append(url)
            return Response(200, self.page, url)
        return await super().get(url, data=data, headers=headers, **kwargs)


ARTICLE_PAGE = (
    b"<html><head><meta name='description' content='Workday closes its $850M C1.ai deal.'>"
    b"</head><body><article><p>Workday said on Tuesday it had closed its acquisition of C1.ai "
    b"for about $850 million, adding agent tooling to its data cloud.</p></article></body></html>"
)


async def test_headline_only_items_get_the_publishers_text_and_the_link_is_cached():
    """Review 2026-11-27: Paloma's 81 Google News items were all headlines, and ZeroTB's lead
    went out "to verify" two days running for want of article text."""
    cache, reader = Cache(), PublisherReader(ARTICLE_PAGE)
    saved: dict[str, str] = {}

    async def save(canon_url, text):
        saved[canon_url] = text

    rows = [
        {"canon_url": "a", "url": LINK, "title": "Workday closes C1.ai deal"},
        {"canon_url": "b", "url": BROKEN, "title": "A link that won't unwrap"},
    ]
    counts = await gnews.add_publisher_text(cache, reader, rows, save)
    assert counts == {"wanted": 2, "unwrapped": 1, "with_text": 1}
    assert list(saved) == ["a"] and "$850 million" in saved["a"]
    assert cache.rows == {GID: PUBLISHER}


async def test_a_paywall_or_a_slow_pass_keeps_the_headline():
    """Plausible but unusable: the publisher answers 200 with a cookie wall shorter than the
    headline, so the item keeps its headline; a pass out of time saves nothing it hasn't read."""
    saved: dict[str, str] = {}

    async def save(canon_url, text):
        saved[canon_url] = text

    wall = b"<html><body><p>Subscribe</p></body></html>"
    row = {"canon_url": "a", "url": LINK, "title": "Workday closes C1.ai deal for agents"}
    counts = await gnews.add_publisher_text(Cache(), PublisherReader(wall), [row], save)
    assert counts["with_text"] == 0 and saved == {}

    class Slow(PublisherReader):
        async def get(self, url, **kwargs):
            await asyncio.sleep(5)
            return await super().get(url, **kwargs)

    counts = await gnews.add_publisher_text(Cache(), Slow(ARTICLE_PAGE), [row], save, seconds=0.05)
    assert counts["with_text"] == 0 and saved == {}
