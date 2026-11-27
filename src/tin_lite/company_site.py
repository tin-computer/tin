"""Whether a company's website still opens, and where it lands: a site that doesn't resolve or
that redirects to another company's often means the company was bought, merged or renamed.

The domain comes from the visit's identification service, so every hop is resolved first and
refused unless all its addresses are public; the request then goes to the vetted address. Only
the status and the redirect's Location header are read, never a page's body.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

MAX_REDIRECTS = 5
REQUEST_SECONDS = 8
TOTAL_SECONDS = 25
USER_AGENT = "Mozilla/5.0 (compatible; Tin-Company-Check/1.0)"


def _bare(host: str | None) -> str:
    return (host or "").lower().removeprefix("www.")


async def _public_ip(host: str, port: int) -> str:
    loop = asyncio.get_running_loop()
    rows = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(row[4][0] for row in rows))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("non_public_address")
    return ips[0]


async def _land(client: httpx.AsyncClient, url: str) -> tuple[str, int]:
    """Where a GET of the URL ends after redirects, and its status."""

    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("unsupported_url")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        ip = await _public_ip(parts.hostname, port)
        request = client.build_request(
            "GET",
            httpx.URL(url).copy_with(host=ip),
            headers={"Host": parts.netloc, "User-Agent": USER_AGENT},
            extensions={"sni_hostname": parts.hostname},
        )
        response = await client.send(request, stream=True)
        await response.aclose()
        if response.status_code in {301, 302, 303, 307, 308}:
            url = urljoin(url, response.headers.get("location", ""))
            continue
        return url, response.status_code
    raise ValueError("redirect_limit")


async def check(site: str) -> dict[str, Any]:
    """{"site", "opens", "lands_on", "status"}: whether https://site or https://www.site
    answers, and the host it ends on when that's another site. Never raises."""

    tries = [f"https://{site}/"]
    if not site.startswith("www."):
        tries.append(f"https://www.{site}/")
    async with httpx.AsyncClient(
        timeout=REQUEST_SECONDS, follow_redirects=False, trust_env=False
    ) as client:
        for url in tries:
            try:
                async with asyncio.timeout(TOTAL_SECONDS):
                    final, status = await _land(client, url)
            except (httpx.HTTPError, OSError, TimeoutError, ValueError, LookupError):
                continue
            host = _bare(urlsplit(final).hostname)
            return {
                "site": site,
                "opens": status < 500,
                "lands_on": host if host and host != _bare(site) else None,
                "status": status,
            }
    return {"site": site, "opens": False, "lands_on": None, "status": None}


def said(checked: dict[str, Any] | None) -> str | None:
    """The check as one sentence for the model, or None when there was none."""

    if not checked:
        return None
    site = checked["site"]
    if checked.get("lands_on"):
        return f"{site} redirects to {checked['lands_on']}"
    if not checked.get("opens"):
        return f"{site} doesn't open (no answer, or an error)"
    return f"{site} opens"


def shown(checked: dict[str, Any] | None, site: str) -> str:
    """The site as a message shows it: "a.com → b.com", "a.com (doesn't open)" or "a.com"."""

    if checked and checked.get("lands_on"):
        return f"{site} → {checked['lands_on']}"
    if checked and checked.get("opens") is False:
        return f"{site} (doesn't open)"
    return site
