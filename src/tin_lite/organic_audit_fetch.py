"""Bounded reads of the audited site: robots.txt, sitemaps and the static HTML of chosen pages.

Only HTTPS URLs on the audit's verified hosts are contacted. Every host is resolved first and
refused unless all its addresses are public; the connection then goes to the vetted address
with the original name for TLS. Redirects are recorded, never followed. No cookies,
credentials or page bodies are kept; only the facts `organic_audit_site` extracts.
Network exceptions never reach the report: each read returns a Tin-owned status instead.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
import zlib
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from tin_lite.organic_audit_site import (
    FETCH_AGENT,
    html_facts,
    parse_robots,
    parse_sitemap,
    robots_allows,
    x_robots_directives,
)

USER_AGENT = f"Mozilla/5.0 (compatible; {FETCH_AGENT}/1.0)"
REQUEST_SECONDS = 10
SITE_FILES_SECONDS = 60
MAX_ROBOTS_BYTES = 500_000
MAX_SITEMAP_BYTES = 10_000_000
PAGESPEED_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
# Bot protection answers a reader it does not trust with these, or with a challenge page.
# That says nothing about the page itself, so the page's facts stay unknown.
REFUSAL_CODES = frozenset({401, 403, 429})
CHALLENGE = re.compile(
    rb"(?i)just a moment|cf-chl|captcha|access denied|px-captcha|datadome|attention required"
)


def refused(response: dict) -> bool:
    code = response.get("status_code")
    return code in REFUSAL_CODES or (
        code == 503 and bool(CHALLENGE.search(response.get("body", b"")[:8000]))
    )


MAX_PAGESPEED_BYTES = 12_000_000


class SiteReader:
    """One bounded read session: shared client, one DNS answer per host."""

    def __init__(self, hosts: tuple[str, ...], *, client=None, resolver=None) -> None:
        self.hosts = hosts
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            trust_env=False,
            timeout=REQUEST_SECONDS,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )
        self._resolver = resolver
        self._addresses: dict[str, str] = {}

    async def __aenter__(self) -> SiteReader:
        if self._resolver is None:
            self._resolver = asyncio.get_running_loop().getaddrinfo
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own_client:
            await self._client.aclose()

    def in_scope(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            return (
                parts.scheme == "https"
                and parts.hostname in self.hosts
                and parts.port in {None, 443}
                and not parts.username
                and not parts.password
                and len(url) <= 2000
                and not any(ord(c) < 33 for c in url)
            )
        except ValueError:
            return False

    async def _address(self, host: str) -> str:
        if host not in self._addresses:
            rows = await self._resolver(host, 443, type=socket.SOCK_STREAM)
            # Keep the resolver's route preference; lexical sorting can pick unreachable IPv6.
            ips = list(dict.fromkeys(row[4][0] for row in rows))
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise ValueError("non_public_address")
            self._addresses[host] = ips[0]
        return self._addresses[host]

    async def get(self, url: str, *, max_bytes: int) -> dict:
        """One GET. Returns status, selected headers and at most `max_bytes` of the body."""
        if not self.in_scope(url):
            return {"status": "out_of_scope"}
        host = urlsplit(url).hostname
        try:
            address = await self._address(host)
            request = self._client.build_request(
                "GET",
                httpx.URL(url).copy_with(host=address),
                headers={"Host": host},
                extensions={"sni_hostname": host},
            )
            async with asyncio.timeout(REQUEST_SECONDS + 5):
                response = await self._client.send(request, stream=True)
                try:
                    headers = {
                        "content_type": response.headers.get("content-type", "")[:200],
                        "location": response.headers.get("location"),
                        "x_robots_tag": response.headers.get_list("x-robots-tag")[:10],
                    }
                    body = bytearray()
                    truncated = False
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > max_bytes:
                                truncated = True
                                del body[max_bytes:]
                                break
                    return {
                        "status": "observed",
                        "status_code": response.status_code,
                        "charset": response.charset_encoding,
                        "body": bytes(body),
                        "truncated": truncated,
                        **headers,
                    }
                finally:
                    await response.aclose()
        except ValueError as exc:
            return {"status": "non_public_address" if str(exc) == "non_public_address" else "error"}
        except (httpx.HTTPError, OSError, TimeoutError, LookupError, UnicodeError):
            return {"status": "unreachable"}

    async def get_following(self, url: str, *, max_bytes: int, hops: int = 3) -> dict:
        """Site files may redirect within the verified site, as crawlers allow."""
        for _ in range(hops + 1):
            response = await self.get(url, max_bytes=max_bytes)
            if response["status"] != "observed" or response["status_code"] not in {
                301,
                302,
                303,
                307,
                308,
            }:
                return {**response, "final_url": url}
            target = urljoin(url, response.get("location") or "")
            if not self.in_scope(target):
                return {**response, "final_url": url}
            url = target
        return {"status": "redirect_limit"}


def _decompress(body: bytes, limit: int) -> bytes | None:
    if not body.startswith(b"\x1f\x8b"):
        return body
    try:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        data = inflater.decompress(body, limit)
        return None if inflater.unconsumed_tail else data
    except zlib.error:
        return None


async def read_site_files(reader: SiteReader, origin: str, policy: dict) -> dict:
    """robots.txt and the sitemaps it names (or /sitemap.xml), with sitemap indexes followed."""
    started = time.monotonic()
    result: dict = {"observed_at": datetime.now(UTC).isoformat()}
    robots_url = urljoin(origin, "/robots.txt")
    response = await reader.get_following(robots_url, max_bytes=MAX_ROBOTS_BYTES)
    robots: dict = {"url": robots_url}
    if response["status"] != "observed":
        robots["status"] = "unreachable"
    elif response["status_code"] == 200:
        text = response["body"].decode(response.get("charset") or "utf-8", "replace")
        robots.update(status="observed", **parse_robots(text))
        robots["truncated"] = response["truncated"]
    elif refused(response):
        robots.update(status="refused", status_code=response["status_code"])
    elif 400 <= response["status_code"] < 500:
        robots.update(status="missing", status_code=response["status_code"])
    elif response["status_code"] in {301, 302, 303, 307, 308}:
        robots.update(status="redirect", status_code=response["status_code"])
    else:
        robots.update(status="server_error", status_code=response["status_code"])
    result["robots"] = robots

    referenced = [url for url in robots.get("sitemaps", []) if reader.in_scope(url)]
    queue = list(dict.fromkeys(referenced)) or [urljoin(origin, "/sitemap.xml")]
    files: list[dict] = []
    urls: list[dict] = []
    seen: set[str] = set()
    total = 0
    while queue and len(files) < policy["max_sitemap_files"]:
        if time.monotonic() - started > SITE_FILES_SECONDS:
            files.append({"url": queue[0], "status": "not_read_time_limit"})
            break
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        response = await reader.get_following(url, max_bytes=MAX_SITEMAP_BYTES)
        entry: dict = {"url": url}
        if response["status"] != "observed":
            entry["status"] = response["status"]
        elif response["status_code"] != 200:
            entry.update(
                status="refused" if refused(response) else "http_error",
                status_code=response["status_code"],
            )
        else:
            body = _decompress(response["body"], MAX_SITEMAP_BYTES)
            if body is None or response["truncated"]:
                entry["status"] = "too_large_or_invalid"
            else:
                parsed = parse_sitemap(
                    body.decode("utf-8", "replace"),
                    max_urls=max(0, policy["max_sitemap_urls"] - len(urls)),
                )
                entry.update(status="observed", kind=parsed["kind"], entries=parsed["total"])
                if parsed["kind"] == "index":
                    queue.extend(
                        row["loc"] for row in parsed["entries"] if reader.in_scope(row["loc"])
                    )
                    entry["outside_scope"] = sum(
                        not reader.in_scope(row["loc"]) for row in parsed["entries"]
                    )
                elif parsed["kind"] == "urlset":
                    total += parsed["total"]
                    urls.extend(parsed["entries"])
        files.append(entry)
    result["sitemaps"] = {
        "referenced_in_robots": bool(robots.get("sitemaps")),
        "files": files[: policy["max_sitemap_files"] + 1],
        "unread": len(queue),
        "total_urls": total,
        "urls": urls[: policy["max_sitemap_urls"]],
        "urls_capped": total > len(urls),
    }
    return result


def _page_record(url: str, response: dict, reader: SiteReader) -> dict:
    if response["status"] != "observed":
        return {"url": url, "fetch": "unavailable", "reason": response["status"]}
    record = {
        "url": url,
        "status_code": response["status_code"],
        "x_robots_tag": x_robots_directives(response.get("x_robots_tag", [])),
    }
    code = response["status_code"]
    if code in {301, 302, 303, 307, 308}:
        target = urljoin(url, response.get("location") or "")
        record.update(
            fetch="redirect",
            location=target[:2000] if reader.in_scope(target) else "(outside the audited site)",
        )
    elif refused(response):
        record["fetch"] = "refused"
    elif code >= 400:
        record["fetch"] = "http_error"
    elif "html" not in (response.get("content_type") or "").lower():
        record["fetch"] = "not_html"
    else:
        record.update(
            fetch="observed",
            **html_facts(
                response["body"],
                url=url,
                charset=response.get("charset"),
                truncated=response["truncated"],
            ),
        )
    return record


async def read_pages(
    reader: SiteReader,
    urls: list[str],
    *,
    robots: dict | None,
    policy: dict,
    seconds: float,
) -> dict[str, dict]:
    """Static HTML facts for each URL, honoring robots.txt for Tin's own user agent.

    Stops starting new reads after `seconds`; unread URLs are simply absent from the result.
    """
    deadline = time.monotonic() + seconds
    limit = asyncio.Semaphore(policy["page_fetch_concurrency"])
    results: dict[str, dict] = {}

    async def one(url: str) -> None:
        async with limit:
            if time.monotonic() > deadline:
                return
            if not reader.in_scope(url):
                results[url] = {"url": url, "fetch": "unavailable", "reason": "out_of_scope"}
                return
            if robots_allows(robots, FETCH_AGENT, url) is False:
                results[url] = {"url": url, "fetch": "blocked_by_robots"}
                return
            response = await reader.get(url, max_bytes=policy["max_page_bytes"])
            results[url] = _page_record(url, response, reader)

    await asyncio.gather(*(one(url) for url in urls))
    return results


def _number(value, *, scale: float = 1.0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value) * scale, 3)


def pagespeed_summary(payload: dict) -> dict:
    """Core Web Vitals from field data when Google has it, otherwise lab values only."""
    metrics = (payload.get("loadingExperience") or {}).get("metrics") or {}
    audits = (payload.get("lighthouseResult") or {}).get("audits") or {}
    categories = (payload.get("lighthouseResult") or {}).get("categories") or {}

    def field(name, scale=1.0):
        return _number((metrics.get(name) or {}).get("percentile"), scale=scale)

    def lab(name):
        return _number((audits.get(name) or {}).get("numericValue"))

    score = (categories.get("performance") or {}).get("score")
    experience = payload.get("loadingExperience") or {}
    return {
        "field": {
            "lcp_ms": field("LARGEST_CONTENTFUL_PAINT_MS"),
            "inp_ms": field("INTERACTION_TO_NEXT_PAINT"),
            "cls": field("CUMULATIVE_LAYOUT_SHIFT_SCORE", 0.01),
            "origin_fallback": bool(experience.get("origin_fallback")),
        },
        "lab": {
            "lcp_ms": lab("largest-contentful-paint"),
            "cls": lab("cumulative-layout-shift"),
            "tbt_ms": lab("total-blocking-time"),
            "performance_score": _number(score),
        },
    }


async def read_pagespeed(url: str, api_key: str, *, client=None) -> dict:
    """One PageSpeed Insights run (mobile). Google fetches the page; Tin sends only the URL."""
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=60, follow_redirects=False)
    try:
        async with asyncio.timeout(70):
            async with client.stream(
                "GET",
                PAGESPEED_ENDPOINT,
                params={
                    "url": url,
                    "strategy": "mobile",
                    "category": "performance",
                    "key": api_key,
                },
            ) as response:
                if response.status_code != 200:
                    return {"status": "unavailable", "reason": "provider_http_error"}
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_PAGESPEED_BYTES:
                        return {"status": "unavailable", "reason": "response_too_large"}
        return {"status": "observed", **pagespeed_summary(json.loads(body))}
    except (httpx.HTTPError, OSError, TimeoutError, ValueError, TypeError, AttributeError):
        return {"status": "unavailable", "reason": "provider_request_failed"}
    finally:
        if own:
            await client.aclose()
