"""website.change's fresh reads of the audited site: pinned public sockets, one host."""

import runpy
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from tin_lite import technical_fix

BEFORE = "<!doctype html><html><head></head><body><h1>Useful page</h1></body></html>"
AFTER = BEFORE.replace("<head>", "<head><title>Useful page</title>")


def test_sandbox_templates_construct_without_the_retired_verifiers():
    module = runpy.run_path(str(Path(__file__).parents[1] / "sandbox/template.py"))
    for name in ("task_template", "browser_template", "studio_template"):
        assert type(module[name]()).__name__ == "TemplateBuilder"


def resolver(ip="93.184.215.14"):
    return AsyncMock(return_value=[(2, 1, 6, "", (ip, 443))])


async def test_fresh_check_pins_public_socket_but_preserves_tls_and_http_hostname():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "93.184.215.14"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        return httpx.Response(200, content=BEFORE, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        value = await technical_fix.fetch_page(
            "https://example.com/guide", host="example.com", resolver=resolver(), client=client
        )
    assert value["url"] == "https://example.com/guide"
    assert value["has_title"] is False
    assert len(requests) == 1


@pytest.mark.parametrize(
    "addresses",
    [
        ["93.184.215.14", "2606:4700:4700::1111"],
        ["2606:4700:4700::1111", "93.184.215.14"],
    ],
)
async def test_fresh_check_keeps_resolver_preference_and_checks_every_address(addresses):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=BEFORE, headers={"content-type": "text/html"})

    rows = AsyncMock(return_value=[(2, 1, 6, "", (ip, 443)) for ip in addresses])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await technical_fix.fetch_page(
            "https://example.com/", host="example.com", resolver=rows, client=client
        )
        assert [request.url.host for request in requests] == [addresses[0]]

        rows.return_value.append((2, 1, 6, "", ("127.0.0.1", 443)))
        with pytest.raises(ValueError, match="public network"):
            await technical_fix.fetch_page(
                "https://example.com/", host="example.com", resolver=rows, client=client
            )
    assert len(requests) == 1


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
async def test_private_dns_resolution_never_opens_a_socket(ip):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("network"))
    ) as client:
        with pytest.raises(ValueError, match="public network"):
            await technical_fix.fetch_page(
                "https://example.com/", host="example.com", resolver=resolver(ip), client=client
            )


@pytest.mark.parametrize(
    "location", ["https://other.example/", "http://example.com/", "https://example.com:8443/"]
)
async def test_redirects_cannot_expand_the_audited_scope(location):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await technical_fix.fetch_page(
                "https://example.com/", host="example.com", resolver=resolver(), client=client
            )
    assert len(requests) == 1


@pytest.mark.parametrize(
    "status,body,mime",
    [
        (404, AFTER, "text/html"),
        (200, AFTER, "application/json"),
        (200, "x" * 250_001, "text/html"),
    ],
)
async def test_failed_fetch_is_not_already_resolved(status, body, mime):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, content=body, headers={"content-type": mime})
        )
    ) as client:
        with pytest.raises(ValueError):
            await technical_fix.fetch_page(
                "https://example.com/", host="example.com", resolver=resolver(), client=client
            )


async def test_site_file_pins_the_socket_and_reads_a_missing_robots_as_empty():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "93.184.215.14"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        value = await technical_fix.fetch_site_file(
            "https://example.com/robots.txt",
            host="example.com",
            kind="robots",
            resolver=resolver(),
            client=client,
        )
    assert value["status_code"] == 404
    assert value["text"] == ""
    assert len(requests) == 1


async def test_site_file_reads_a_sitemap_and_refuses_a_missing_one_or_html():
    xml = '<?xml version="1.0"?><urlset></urlset>'

    def handler(request):
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, content=xml, headers={"content-type": "application/xml"})
        if request.url.path == "/page.xml":
            return httpx.Response(200, content=BEFORE, headers={"content-type": "text/html"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        value = await technical_fix.fetch_site_file(
            "https://example.com/sitemap.xml",
            host="example.com",
            kind="sitemap",
            resolver=resolver(),
            client=client,
        )
        assert value["text"] == xml
        for path in ("/missing.xml", "/page.xml"):
            with pytest.raises(ValueError, match="sitemap"):
                await technical_fix.fetch_site_file(
                    f"https://example.com{path}",
                    host="example.com",
                    kind="sitemap",
                    resolver=resolver(),
                    client=client,
                )
        with pytest.raises(ValueError, match="Unsupported"):
            await technical_fix.fetch_site_file(
                "https://example.com/.env",
                host="example.com",
                kind="env",
                resolver=resolver(),
                client=client,
            )


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "fc00::1"])
async def test_site_file_private_dns_never_opens_a_socket(ip):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("network"))
    ) as client:
        with pytest.raises(ValueError, match="public network"):
            await technical_fix.fetch_site_file(
                "https://example.com/robots.txt",
                host="example.com",
                kind="robots",
                resolver=resolver(ip),
                client=client,
            )


@pytest.mark.parametrize(
    "location", ["https://other.example/robots.txt", "http://example.com/robots.txt"]
)
async def test_site_file_redirects_cannot_leave_the_audited_host(location):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(301, headers={"location": location})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="audited HTTPS host"):
            await technical_fix.fetch_site_file(
                "https://example.com/robots.txt",
                host="example.com",
                kind="robots",
                resolver=resolver(),
                client=client,
            )
    assert len(requests) == 1
