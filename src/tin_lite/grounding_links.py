"""The pages a grounded Gemini answer rested on, under their own addresses.

Gemini's search grounding names each page by a redirect link on vertexaisearch.cloud.google.com
that stops working after a while, and the model copies those links into its own `sources`. Tin
asks Google where each link points (the redirect's Location header, never the page itself) and
keeps that address instead, so a verdict or a log can still be checked later. Only Google's
redirect host is ever contacted; a link that doesn't answer in time stays as it was.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

REDIRECT = re.compile(
    r"https://vertexaisearch\.cloud\.google\.com/grounding-api-redirect/[A-Za-z0-9_\-=]{8,2000}"
)
# Runaway guards: a call cites a handful of pages.
MOST = 40
SECONDS = 5
PARALLEL = 8


def links_in(value: Any) -> set[str]:
    """Every grounding redirect link anywhere in a JSON-like value."""

    if isinstance(value, str):
        return set(REDIRECT.findall(value))
    if isinstance(value, dict):
        return set().union(*(links_in(item) for item in value.values())) if value else set()
    if isinstance(value, list | tuple):
        return set().union(*(links_in(item) for item in value)) if value else set()
    return set()


def replaced(value: Any, found: dict[str, str]) -> Any:
    """The value with each resolved redirect link replaced by the page's own address."""

    if not found:
        return value
    if isinstance(value, str):
        return REDIRECT.sub(lambda match: found.get(match[0], match[0]), value)
    if isinstance(value, dict):
        return {key: replaced(item, found) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [replaced(item, found) for item in value]
    return value


def _page(location: str | None) -> str | None:
    if not location:
        return None
    # Google sometimes keeps a page's stray markup on the address ("…/2022&quot;&gt;2022").
    location = re.split(r"&quot;|&gt;|&lt;|[\"'<>\s]", location, maxsplit=1)[0]
    try:
        parts = urlsplit(location)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return location[:2000]


async def resolve(links: set[str], *, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """{redirect link: the page's address} for the links Google answered for."""

    wanted = sorted(link for link in links if REDIRECT.fullmatch(link))[:MOST]
    if not wanted:
        return {}
    gate = asyncio.Semaphore(PARALLEL)
    found: dict[str, str] = {}
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=SECONDS, trust_env=False, transport=transport
    ) as client:

        async def one(link: str) -> None:
            async with gate:
                try:
                    response = await client.head(link)
                except httpx.HTTPError:
                    return
                if response.status_code in {301, 302, 303, 307, 308}:
                    page = _page(response.headers.get("location"))
                    if page and not REDIRECT.match(page):
                        found[link] = page

        await asyncio.gather(*(one(link) for link in wanted))
    return found


async def resolved(value: Any, **kwargs) -> Any:
    """The value with its grounding redirect links resolved where Google answered."""

    links = links_in(value)
    return replaced(value, await resolve(links, **kwargs)) if links else value
