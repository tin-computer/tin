"""The intel fetchers' public reader: no private addresses, vetted redirects, pacing, 429s."""

from __future__ import annotations

import gzip
import time

import httpx
import pytest

from tin_lite.intel_fetch import http as intel_http

PUBLIC = "93.184.215.14"


def answer(status, text=None, *, content=None, headers=None):
    """A streamed answer, as a real server's is (the reader reads raw bytes itself)."""
    body = content if content is not None else (text or "").encode()
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


ADDRESSES = {"news.example": PUBLIC, "other.example": "151.101.1.67", "inside.example": "10.0.0.5"}


async def resolver(host, port, type=None):
    return [(2, 1, 6, "", (ADDRESSES.get(host, "192.168.1.1"), port))]


def reader(handler, *, sleeps=None, **kwargs):
    async def sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return intel_http.PublicReader(
        user_agent="test-agent", client=client, resolver=resolver, sleep=sleep, **kwargs
    )


async def test_a_private_address_is_never_contacted():
    contacted = []

    def handler(request):
        contacted.append(request)
        return answer(200, text="ok")

    async with reader(handler) as r:
        refused = await r.get("https://inside.example/feed")
        literal = await r.get("http://127.0.0.1:8080/admin")
        scheme = await r.get("file:///etc/passwd")
        userinfo = await r.get("https://user:pw@news.example/")
    assert refused.status == 0 and "not a public address" in refused.error
    assert literal.status == 0 and "not a public address" in literal.error
    assert scheme.status == 0 and userinfo.status == 0
    assert contacted == []


async def test_requests_go_to_the_vetted_address_with_the_name_for_tls():
    seen = []

    def handler(request):
        seen.append(request)
        return answer(200, text="ok", headers={"set-cookie": "a=b"})

    async with reader(handler) as r:
        await r.get("https://news.example/one")
        await r.get("https://news.example/two")
    assert seen[0].url.host == PUBLIC
    assert seen[0].headers["host"] == "news.example"
    assert seen[0].extensions["sni_hostname"] == "news.example"
    assert seen[0].headers["user-agent"] == "test-agent"
    assert "cookie" not in seen[1].headers


async def test_each_redirect_is_vetted_again():
    def handler(request):
        if request.headers["host"] == "news.example" and request.url.path == "/go-inside":
            return answer(302, headers={"location": "http://inside.example/secret"})
        if request.headers["host"] == "news.example":
            return answer(301, headers={"location": "https://other.example/final"})
        if request.headers["host"] == "other.example":
            return answer(200, text="final")
        raise AssertionError("a private address was contacted")

    async with reader(handler) as r:
        followed = await r.get("https://news.example/start")
        refused = await r.get("https://news.example/go-inside")
    assert followed.status == 200 and followed.body == b"final"
    assert followed.url == "https://other.example/final"
    assert refused.status == 0 and "not a public address" in refused.error


async def test_redirect_loops_stop():
    def handler(request):
        return answer(302, headers={"location": "https://news.example/again"})

    async with reader(handler) as r:
        looped = await r.get("https://news.example/again")
    assert looped.status == 0 and "too many redirects" in looped.error


async def test_bodies_are_capped_and_decompressed():
    def handler(request):
        if request.url.path == "/big":
            return answer(200, content=b"x" * 2048)
        return answer(200, content=gzip.compress(b"<rss/>"), headers={"content-encoding": "gzip"})

    async with reader(handler) as r:
        big = await r.get("https://news.example/big", max_bytes=1024)
        zipped = await r.get("https://news.example/zip")
    assert big.status == 0 and "larger than" in big.error
    assert zipped.body == b"<rss/>"
    bomb = gzip.compress(b"\0" * 5000)
    with pytest.raises(intel_http.FetchError):
        intel_http.decode_body(bomb, "gzip", 1000)


async def test_a_429_waits_for_retry_after_and_pauses_the_host(monkeypatch):
    monkeypatch.setitem(intel_http.HOST_GAP, "news.example", 0.0)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return answer(429, headers={"retry-after": "7"}, text="slow down")
        return answer(200, text="ok")

    sleeps: list[float] = []
    async with reader(handler, sleeps=sleeps) as r:
        got = await r.get("https://news.example/feed")
    assert got.status == 200 and len(calls) == 2
    assert 7 in sleeps
    # The host is paused for every other reader too.
    assert intel_http._host_next["news.example"] >= time.monotonic() + 5


async def test_refused_reads_keep_a_short_body_and_rate_limit_headers():
    def handler(request):
        return answer(
            403, text="Too many requests " + "x" * 1000, headers={"x-ratelimit-reset": "42"}
        )

    async with reader(handler) as r:
        refused = await r.get("https://news.example/feed")
        with pytest.raises(intel_http.FetchError) as raised:
            await r.fetch("https://news.example/feed")
    assert refused.status == 403 and len(refused.body) == 300
    assert refused.error.startswith("HTTP 403: Too many requests")
    assert refused.headers["x-ratelimit-reset"] == "42"
    assert raised.value.code == 403


async def test_paced_hosts_take_turns(monkeypatch):
    monkeypatch.setitem(intel_http.HOST_GAP, "other.example", 0.15)
    intel_http._host_next.pop("other.example", None)

    def handler(request):
        return answer(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    started = time.monotonic()
    async with intel_http.PublicReader(user_agent="t", client=client, resolver=resolver) as r:
        import asyncio

        await asyncio.gather(*(r.get(f"https://other.example/{n}") for n in range(4)))
    assert time.monotonic() - started >= 0.44
