"""Synthetic public websites for organic-audit tests. Nothing here touches the network."""

from __future__ import annotations

import httpx

from tin_lite.organic_audit_fetch import SiteReader

PUBLIC_ADDRESS = "93.184.215.14"


def html_page(
    *,
    title: str = "Example",
    lang: str | None = "en",
    h1: int = 1,
    canonical: str | None = None,
    robots: str | None = None,
    hreflang: list[tuple[str, str]] = (),
    json_ld: str | None = None,
    body: str = "",
) -> bytes:
    head = [f"<title>{title}</title>"]
    if canonical:
        head.append(f'<link rel="canonical" href="{canonical}">')
    if robots:
        head.append(f'<meta name="robots" content="{robots}">')
    head.extend(
        f'<link rel="alternate" hreflang="{lang_}" href="{href}">' for lang_, href in hreflang
    )
    if json_ld:
        head.append(f'<script type="application/ld+json">{json_ld}</script>')
    heading = "".join(f"<h1>Heading {index}</h1>" for index in range(h1))
    lang_attr = f' lang="{lang}"' if lang else ""
    return (
        f"<!doctype html><html{lang_attr}><head>{''.join(head)}</head>"
        f"<body>{heading}{body}</body></html>"
    ).encode()


def sitemap(urls: list[str], *, lastmod: str | None = None) -> bytes:
    rows = "".join(
        f"<url><loc>{url}</loc>" + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "") + "</url>"
        for url in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'
    ).encode()


class SyntheticSite:
    """Routes keyed by absolute URL: (status, headers, body). Unknown URLs return 404."""

    def __init__(self, routes: dict[str, tuple[int, dict, bytes]] | None = None) -> None:
        self.routes = dict(routes or {})
        self.requests: list[str] = []

    def page(self, url: str, body: bytes, *, status: int = 200, headers: dict | None = None):
        self.routes[url] = (
            status,
            {"content-type": "text/html; charset=utf-8", **(headers or {})},
            body,
        )

    def text(self, url: str, body: bytes | str, *, content_type: str = "text/plain"):
        self.routes[url] = (
            200,
            {"content-type": content_type},
            body.encode() if isinstance(body, str) else body,
        )

    def redirect(self, url: str, location: str, status: int = 301):
        self.routes[url] = (status, {"location": location}, b"")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = f"https://{request.headers['host']}{request.url.raw_path.decode()}"
        self.requests.append(url)
        status, headers, body = self.routes.get(
            url, (404, {"content-type": "text/html"}, b"<html><body>Not found</body></html>")
        )
        return httpx.Response(status, headers=headers, content=body)

    def reader(self, hosts):
        async def resolver(host, port, type=None):
            return [(2, 1, 6, "", (PUBLIC_ADDRESS, port))]

        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        return SiteReader(hosts, client=client, resolver=resolver)


def minimal_site(host: str = "example.com") -> SyntheticSite:
    site = SyntheticSite()
    site.text(
        f"https://{host}/robots.txt",
        f"User-agent: *\nAllow: /\nSitemap: https://{host}/sitemap.xml\n",
    )
    site.text(
        f"https://{host}/sitemap.xml",
        sitemap([f"https://{host}/"]),
        content_type="application/xml",
    )
    site.page(f"https://{host}/", html_page(title="Example product", canonical=f"https://{host}/"))
    return site
