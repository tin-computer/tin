"""Audit network bounds and credential handling, using synthetic HTTP responses only."""

import asyncio
import logging

import httpx

from tin_lite.organic_audit_fetch import SiteReader, read_pagespeed


async def public_address(host, port, type=None):
    return [(2, 1, 6, "", ("93.184.215.14", port))]


async def test_pagespeed_key_does_not_reach_request_urls_or_logs(caplog):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={})

    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await read_pagespeed("https://example.com/", "synthetic-key", client=client)
    assert result["status"] == "observed"
    assert "synthetic-key" not in caplog.text
    assert "synthetic-key" not in str(requests[0].url)
    assert requests[0].headers["x-goog-api-key"] == "synthetic-key"


async def test_a_site_cannot_force_unbounded_http_decompression():
    class UnreadBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("An unsolicited encoded body must never be decoded")
            yield b""  # pragma: no cover

    def respond(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=UnreadBody())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        async with SiteReader(("example.com",), client=client, resolver=public_address) as reader:
            result = await reader.get("https://example.com/", max_bytes=100)
    assert result["status"] == "unsupported_encoding"


async def test_dns_resolution_is_inside_the_request_deadline(monkeypatch):
    async def stalled(*args, **kwargs):
        await asyncio.sleep(1)
        raise AssertionError("The DNS lookup should have timed out")

    timeout = asyncio.timeout
    monkeypatch.setattr("tin_lite.organic_audit_fetch.asyncio.timeout", lambda _: timeout(0.01))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)) as client:
        async with SiteReader(("example.com",), client=client, resolver=stalled) as reader:
            result = await reader.get("https://example.com/", max_bytes=100)
    assert result == {"status": "unreachable"}
