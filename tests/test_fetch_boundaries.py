"""Switchboard page reads: pinned public addresses, bounded bodies, no NUL in results."""

from __future__ import annotations

import socket

import httpx
import pytest

from tin_lite import character_design, organic_audit_fetch, site_health
from tin_lite.character_design import CharacterDesignError, fetch_product_page
from tin_lite.site_health import SiteHealthProtocolError, fetch_live_page_evidence

PUBLIC = "93.184.216.34"
PAGE = (
    "<html lang='en'><head><title>Acme\x00 tools</title>"
    "<meta name='description' content='Build\x00 faster'>"
    "<link rel=stylesheet href='/site.css'></head>"
    "<body><h1>Ship\x00 it</h1><button>Start</button>" + "word " * 60 + "</body></html>"
)


def resolver_for(*answers: str):
    """A DNS server that gives each lookup the next answer: public first, then rebinding."""
    calls: list[str] = []

    async def resolve(host, port, *args, **kwargs):
        answer = answers[min(len(calls), len(answers) - 1)]
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer, port))]

    resolve.calls = calls
    return resolve


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def html_response(text: str = PAGE) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=text)


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch", ["character", "site_health"])
async def test_page_reads_connect_to_the_address_that_was_checked(fetch) -> None:
    seen: list[tuple[str, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.url.host, request.headers.get("host"), request.extensions.get("sni_hostname"))
        )
        if request.url.path.endswith(".css"):
            return httpx.Response(200, headers={"content-type": "text/css"}, text="a{}")
        return html_response()

    # The second lookup would rebind to loopback; the fetch must never make it.
    resolver = resolver_for(PUBLIC, "127.0.0.1")
    async with client_for(handler) as client:
        if fetch == "character":
            await fetch_product_page("https://example.com/", client=client, resolver=resolver)
        else:
            await fetch_live_page_evidence("https://example.com/", client=client, resolver=resolver)
    assert seen and all(host == PUBLIC for host, _, _ in seen)
    assert all(header == "example.com" and sni == "example.com" for _, header, sni in seen)
    assert "127.0.0.1" not in {host for host, _, _ in seen}


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "100.64.0.1", "169.254.169.254"])
async def test_character_page_refuses_every_non_public_address(address) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("a non-public address was contacted")

    async with client_for(handler) as client:
        with pytest.raises(CharacterDesignError, match="private address"):
            await fetch_product_page(
                "https://example.com/", client=client, resolver=resolver_for(address)
            )


@pytest.mark.asyncio
async def test_redirects_to_a_non_public_host_are_refused_before_contact() -> None:
    contacted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.append(request.headers["host"])
        return httpx.Response(302, headers={"location": "https://internal.example/"})

    async def resolve(host, port, *args, **kwargs):
        address = PUBLIC if host == "example.com" else "10.1.2.3"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    async with client_for(handler) as client:
        with pytest.raises(SiteHealthProtocolError, match="non-public"):
            await fetch_live_page_evidence("https://example.com/", client=client, resolver=resolve)
        with pytest.raises(CharacterDesignError, match="private address"):
            await fetch_product_page("https://example.com/", client=client, resolver=resolve)
    assert contacted == ["example.com", "example.com"]


@pytest.mark.asyncio
async def test_character_stylesheet_read_stops_at_its_byte_bound() -> None:
    streamed = {"bytes": 0}
    chunk = b"a{color:#ff0000}" * 4096  # 64 KiB

    async def endless_sheet():
        for _ in range(640):  # 40 MiB, 100 times the stylesheet bound
            streamed["bytes"] += len(chunk)
            yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".css"):
            return httpx.Response(
                200, headers={"content-type": "text/css"}, content=endless_sheet()
            )
        return html_response()

    async with client_for(handler) as client:
        page = await fetch_product_page(
            "https://example.com/", client=client, resolver=resolver_for(PUBLIC)
        )
    assert streamed["bytes"] <= character_design.MAX_STYLESHEET_BYTES + 2 * len(chunk)
    # An oversized stylesheet is skipped, as before; the page itself still reads.
    assert page.title == "Acme tools"


@pytest.mark.asyncio
async def test_character_page_facts_carry_no_nul_characters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".css"):
            return httpx.Response(
                200, headers={"content-type": "text/css"}, content=b".btn{color:#ff0000}\x00"
            )
        return html_response()

    async with client_for(handler) as client:
        page = await fetch_product_page(
            "https://example.com/", client=client, resolver=resolver_for(PUBLIC)
        )
    assert "\x00" not in repr(page.definition())
    assert page.title == "Acme tools" and page.description == "Build faster"


@pytest.mark.asyncio
async def test_site_health_evidence_carries_no_nul_characters() -> None:
    async with client_for(lambda request: html_response()) as client:
        evidence = await fetch_live_page_evidence(
            "https://example.com/", client=client, resolver=resolver_for(PUBLIC)
        )
    assert evidence.title == "Acme tools" and evidence.description == "Build faster"
    assert evidence.h1s == ("Ship it",)


@pytest.mark.asyncio
async def test_site_health_refuses_an_oversized_page() -> None:
    body = "<html><title>x</title>" + "a" * (site_health.MAX_SITE_RESPONSE_BYTES + 10)
    async with client_for(lambda request: html_response(body)) as client:
        with pytest.raises(SiteHealthProtocolError, match="exceeds"):
            await fetch_live_page_evidence(
                "https://example.com/", client=client, resolver=resolver_for(PUBLIC)
            )


@pytest.mark.asyncio
async def test_robots_and_sitemaps_carry_no_nul_characters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                content=b"User-agent: *\x00\nDisallow: /a\x00b\nSitemap: https://example.com/s.xml\n",
            )
        return httpx.Response(
            200,
            content=b"<urlset><url><loc>https://example.com/p\x00q</loc></url></urlset>",
        )

    policy = {"max_sitemap_files": 3, "max_sitemap_urls": 10}
    async with (
        client_for(handler) as client,
        organic_audit_fetch.SiteReader(
            ("example.com",), client=client, resolver=resolver_for(PUBLIC)
        ) as reader,
    ):
        result = await organic_audit_fetch.read_site_files(reader, "https://example.com", policy)
    assert result["robots"]["status"] == "observed"
    assert result["sitemaps"]["urls"] == [{"loc": "https://example.com/pq", "lastmod": None}]
    assert "\x00" not in repr(result)
