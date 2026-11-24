"""Offline readers for the client fetcher tests: fixture bodies routed by URL (no network)."""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from tin_lite.intel_fetch.http import FetchError, Response
from tin_lite.intel_fetch.items import Window

FIXTURES = Path(__file__).parent / "fixtures" / "intel_client"
WINDOW = Window(
    datetime(2026, 11, 23, 13, 0, tzinfo=UTC), datetime(2026, 11, 24, 13, 0, tzinfo=UTC)
)


class FakeReader:
    """PublicReader's surface over a route table: get never raises, fetch raises unless 200."""

    def __init__(self, route) -> None:
        self.route = route
        self.requests: list[str] = []

    async def get(self, url, *, data=None, headers=None, retries=1, max_bytes=0, seconds=0):
        self.requests.append(url)
        found = self.route(url)
        if found is None:
            return Response(404, b"", url, "HTTP 404")
        if isinstance(found, Response):
            return found
        return Response(200, found, url)

    async def fetch(self, url, **kwargs):
        response = await self.get(url, **kwargs)
        if response.status != 200:
            raise FetchError(response.error or "HTTP 404", response.status or None)
        return response

    async def json(self, url, **kwargs):
        return json.loads((await self.fetch(url, **kwargs)).body)

    async def aclose(self):
        return None


def rss_reader() -> FakeReader:
    routes = json.loads((FIXTURES / "routes.json").read_text())

    def route(url):
        parts = urllib.parse.urlsplit(url)
        name = routes.get(parts.netloc + parts.path)
        return (FIXTURES / name).read_bytes() if name else None

    return FakeReader(route)


def hn_reddit_reader() -> tuple[FakeReader, dict]:
    data = json.loads((FIXTURES / "hn-reddit.json").read_text())

    def route(url):
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        for response in data["responses"]:
            if response["path"] == parts.path and all(
                query.get(k) == v for k, v in response["params"].items()
            ):
                return json.dumps(response["body"]).encode()
        return None

    return FakeReader(route), data["config"]
