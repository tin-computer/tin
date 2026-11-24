"""Public reads for the intel fetchers: paced, bounded, and never to a private address.

Source configs can come from project files, so every URL is treated as untrusted:

- HTTP and HTTPS only, no credentials in the URL.
- Every host is resolved first and refused unless all of its addresses are public; the
  connection goes to the vetted address with the original name for TLS (no rebinding).
- Redirects are followed by hand, at most five, each one vetted again.
- No cookies and no credentials are sent; bodies are capped (5 MB, 10 MB for sitemaps) before
  and after gzip or deflate.
- Hosts that answer bursts with 429 are paced process-wide (`news.google.com`, one request per
  1.5 s), and a 429 pauses that host for every concurrent fetch, honouring Retry-After. The
  world and client fetches share one switchboard process, so they share the pacing.

Everything read is data: nothing is executed or followed as an instruction.
"""

from __future__ import annotations

import asyncio
import gzip
import http.cookiejar
import io
import ipaddress
import json
import socket
import time
import urllib.parse
import zlib
from dataclasses import dataclass, field

import httpx

TIMEOUT_SECONDS = 25
MAX_BYTES = 5 * 1024 * 1024
SITEMAP_MAX_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
CONCURRENCY = 10
# Seconds between two requests to one host; a 429 also pauses the host for everyone.
HOST_GAP = {"news.google.com": 1.5, "api.gdeltproject.org": 5.0}
REDIRECTS = frozenset({301, 302, 303, 307, 308})
_host_next: dict[str, float] = {}


class FetchError(Exception):
    """A read that returned nothing usable, in a sentence for the source's status."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Response:
    status: int
    body: bytes
    url: str
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


async def pace(url: str) -> None:
    """Wait for this host's turn. The slot is taken before any await, so concurrent callers in
    one event loop queue up without a lock."""
    gap = HOST_GAP.get(_host(url))
    if not gap:
        return
    now = time.monotonic()
    slot = max(now, _host_next.get(_host(url), 0.0))
    _host_next[_host(url)] = slot + gap
    if slot > now:
        await asyncio.sleep(slot - now)


def cool_down(url: str, seconds: float) -> None:
    host = _host(url)
    _host_next[host] = max(_host_next.get(host, 0.0), time.monotonic() + seconds)


def retry_after(headers: dict[str, str]) -> int | None:
    try:
        value = int(headers.get("retry-after", ""))
    except ValueError:
        return None
    return value if 0 <= value <= 600 else None


def _no_cookies() -> http.cookiejar.CookieJar:
    return http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


def decode_body(raw: bytes, encoding: str, limit: int) -> bytes:
    """Undo gzip or deflate, refusing a body that grows past the cap."""
    encoding = (encoding or "").lower().strip()
    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    elif encoding in {"", "identity"}:
        return raw
    else:
        raise FetchError(f"unsupported content encoding {encoding[:20]}")
    try:
        data = inflater.decompress(raw, limit + 1)
    except zlib.error as exc:
        raise FetchError("the body could not be decompressed") from exc
    if len(data) > limit or inflater.unconsumed_tail:
        raise FetchError(f"the body is larger than {limit // (1024 * 1024)} MB")
    return data


def _gunzip_payload(body: bytes, limit: int) -> bytes:
    """A sitemap.xml.gz served as a plain body."""
    if body[:2] != b"\x1f\x8b":
        return body
    try:
        data = gzip.GzipFile(fileobj=io.BytesIO(body)).read(limit + 1)
    except OSError:
        return body
    if len(data) > limit:
        raise FetchError(f"the body is larger than {limit // (1024 * 1024)} MB")
    return data


class PublicReader:
    """One fetch session: a shared client, one DNS answer per host, ten requests at a time."""

    def __init__(
        self,
        *,
        user_agent: str,
        client: httpx.AsyncClient | None = None,
        resolver=None,
        concurrency: int = CONCURRENCY,
        sleep=asyncio.sleep,
    ) -> None:
        self.user_agent = user_agent
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            cookies=_no_cookies(),
            timeout=TIMEOUT_SECONDS,
        )
        self._resolver = resolver
        self._addresses: dict[tuple[str, int], str] = {}
        self._gate = asyncio.Semaphore(concurrency)
        self._sleep = sleep

    async def __aenter__(self) -> PublicReader:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    @staticmethod
    def allowed(url: str) -> bool:
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        return (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and not parts.username
            and not parts.password
            and len(url) <= 4000
            and not any(ord(c) < 33 for c in url)
        )

    async def address(self, host: str, port: int) -> str:
        """The host's first public address, or FetchError when any of its addresses isn't."""
        key = (host, port)
        if key not in self._addresses:
            try:
                literal = ipaddress.ip_address(host)
            except ValueError:
                literal = None
            if literal is not None:
                ips = [str(literal)]
            else:
                resolver = self._resolver or asyncio.get_running_loop().getaddrinfo
                try:
                    rows = await resolver(host, port, type=socket.SOCK_STREAM)
                except (OSError, UnicodeError) as exc:
                    raise FetchError(f"{host} did not resolve") from exc
                ips = list(dict.fromkeys(row[4][0] for row in rows))
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise FetchError(f"{host} is not a public address")
            self._addresses[key] = ips[0]
        return self._addresses[key]

    async def _once(
        self, url: str, *, data: bytes | None, headers: dict[str, str], limit: int
    ) -> Response:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        address = await self.address(host, port)
        request = self._client.build_request(
            "POST" if data is not None else "GET",
            httpx.URL(url).copy_with(host=address),
            content=data,
            headers={
                "Host": parts.netloc.rsplit("@", 1)[-1],
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                **headers,
            },
            extensions={"sni_hostname": host} if parts.scheme == "https" else {},
        )
        response = await self._client.send(request, stream=True)
        try:
            kept = {
                "location": response.headers.get("location", ""),
                "retry-after": response.headers.get("retry-after", ""),
                "content-type": response.headers.get("content-type", "")[:200],
            }
            if response.status_code in REDIRECTS:
                return Response(response.status_code, b"", url, None, kept)
            raw = bytearray()
            async for chunk in response.aiter_raw():
                raw.extend(chunk)
                if len(raw) > limit:
                    raise FetchError(f"the body is larger than {limit // (1024 * 1024)} MB")
            body = decode_body(bytes(raw), response.headers.get("content-encoding", ""), limit)
            return Response(response.status_code, body, url, None, kept)
        finally:
            await response.aclose()

    async def get(
        self,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 1,
        max_bytes: int = MAX_BYTES,
        seconds: float = TIMEOUT_SECONDS,
    ) -> Response:
        """One read with the prototype's retries: 429 waits (Retry-After, else 15 s doubling,
        at most 120 s) and pauses the host; 5xx and network errors wait 1.5 s per attempt.
        Never raises for an HTTP or network outcome: `status` 0 with `error` means no answer."""
        headers = dict(headers or {})
        if _host(url) in HOST_GAP:
            retries = max(retries, 4)
        last = Response(0, b"", url, "not attempted")
        for attempt in range(retries + 1):
            await pace(url)
            try:
                async with self._gate, asyncio.timeout(seconds + 5):
                    current = url
                    for _hop in range(MAX_REDIRECTS + 1):
                        if not self.allowed(current):
                            raise FetchError("the address is not a public http(s) URL")
                        response = await self._once(
                            current, data=data, headers=headers, limit=max_bytes
                        )
                        if response.status not in REDIRECTS:
                            break
                        current = urllib.parse.urljoin(current, response.headers["location"])
                        data = None if response.status in {301, 302, 303} else data
                    else:
                        raise FetchError("too many redirects")
            except FetchError as exc:
                return Response(0, b"", url, str(exc)[:300])
            except (httpx.HTTPError, OSError, TimeoutError, UnicodeError) as exc:
                last = Response(0, b"", url, f"{type(exc).__name__}: {str(exc)[:120]}")
                await self._sleep(1.5 * (attempt + 1))
                continue
            if response.status == 200:
                return response
            last = Response(response.status, b"", response.url, f"HTTP {response.status}")
            last.headers = response.headers
            if response.status == 429 and attempt < retries:
                wait = retry_after(response.headers) or min(120, 15 * 2**attempt)
                cool_down(url, wait)
                await self._sleep(wait)
                continue
            if response.status not in {500, 502, 503, 504}:
                break
            await self._sleep(1.5 * (attempt + 1))
        return last

    async def fetch(self, url: str, **kwargs) -> Response:
        """`get`, raising FetchError (with the HTTP code) unless the answer is a 200."""
        response = await self.get(url, **kwargs)
        if response.status != 200:
            raise FetchError(response.error or f"HTTP {response.status}", response.status or None)
        return response

    async def json(self, url: str, **kwargs):
        response = await self.fetch(url, **kwargs)
        try:
            return json.loads(response.body)
        except ValueError as exc:
            raise FetchError("the answer was not JSON") from exc


def sitemap_body(body: bytes) -> bytes:
    return _gunzip_payload(body, SITEMAP_MAX_BYTES)
