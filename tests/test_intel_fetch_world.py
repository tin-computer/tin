"""Tin's world fetch methods give the same items as the prototype on the same bodies.

The bodies in tests/fixtures/intel_world are small excerpts of real feeds plus synthetic HN and
GDELT answers; expected.json is what the prototype's world.py produced from them, once.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tin_lite.intel_fetch import config as source_config
from tin_lite.intel_fetch import group, run_sources, world
from tin_lite.intel_fetch.http import FetchError, Response
from tin_lite.intel_fetch.items import Window

FIXTURES = Path(__file__).parent / "fixtures" / "intel_world"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())
CONFIG = json.loads((FIXTURES / "sources.json").read_text())
WINDOW = Window(*(datetime.fromisoformat(value) for value in EXPECTED["window"]))
GN_URL = "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"
WIKI_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/2026/11/23"
)
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc?query=x&mode=artlist&format=json"
PROTOTYPE_FIELDS = (
    "source_type",
    "source",
    "source_url",
    "url",
    "discussion_url",
    "title",
    "author",
    "published_at",
    "text",
    "engagement",
    "fetched_at",
)


def routes() -> dict[str, bytes]:
    hn = json.loads((FIXTURES / "hn.json").read_text())
    found = {
        "https://feeds.bbci.co.uk/news/world/rss.xml": "bbc_world.xml",
        GN_URL: "gn_business.xml",
        "https://trends.google.com/trending/rss?geo=US": "trends_us.xml",
        "https://www.techmeme.com/river": "techmeme_river.html",
        WIKI_URL: "wiki_en_2026-11-23.json",
        GDELT_URL: "gdelt.json",
    }
    bodies = {url: (FIXTURES / name).read_bytes() for url, name in found.items()}
    fb = "https://hacker-news.firebaseio.com/v0"
    bodies[f"{fb}/topstories.json"] = json.dumps(hn["topstories"]).encode()
    bodies[f"{fb}/beststories.json"] = json.dumps(hn["beststories"]).encode()
    for key, value in hn["items"].items():
        bodies[f"{fb}/item/{key}.json"] = json.dumps(value).encode()
    bodies["algolia:front_page"] = json.dumps(hn["front_page"]).encode()
    bodies["algolia:points"] = json.dumps(hn["points"]).encode()
    return bodies


class FakeReader:
    """Serves the fixture bodies; any other URL is a 404, like a missing Wikimedia day."""

    def __init__(self, bodies=None, failing=()):
        self.bodies = bodies or routes()
        self.failing = set(failing)
        self.asked: list[str] = []

    def _body(self, url):
        if url.startswith("https://hn.algolia.com/api/v1/search?tags=front_page"):
            return self.bodies["algolia:front_page"]
        if url.startswith("https://hn.algolia.com/api/v1/search_by_date"):
            return self.bodies["algolia:points"]
        return self.bodies.get(url)

    async def get(self, url, **kwargs):
        self.asked.append(url)
        if url in self.failing:
            return Response(503, b"", url, "HTTP 503")
        body = self._body(url)
        return Response(200, body, url) if body is not None else Response(404, b"", url, "HTTP 404")

    async def fetch(self, url, **kwargs):
        response = await self.get(url, **kwargs)
        if response.status != 200:
            raise FetchError(response.error, response.status)
        return response

    async def json(self, url, **kwargs):
        return json.loads((await self.fetch(url, **kwargs)).body)


def settings(reader):
    return {"now": EXPECTED["now"], "max_text": 6000, "reader": reader}


@pytest.mark.parametrize("source", CONFIG["sources"], ids=lambda s: s["id"])
async def test_each_method_matches_the_prototype(source):
    reader = FakeReader()
    found = await world.METHODS[source["method"]](source, WINDOW, settings(reader))
    expected = EXPECTED["items"][source["id"]]
    assert len(found) == len(expected) > 0
    for ours, theirs in zip(found, expected, strict=True):
        assert {k: ours.get(k) for k in PROTOTYPE_FIELDS} == {
            k: theirs[k] for k in PROTOTYPE_FIELDS
        }
        assert ours["source_id"] == source["id"]


async def test_run_sources_ranks_dedupes_and_records_failures():
    reader = FakeReader(failing={"https://www.techmeme.com/river"})
    ready, skipped = source_config.select(CONFIG, "world")
    assert skipped == []
    done = []

    async def on_done(src, items, status):
        done.append((src["id"], len(items), status["status"]))

    items, statuses = await run_sources(
        world.METHODS, ready, WINDOW, reader, settings=settings(reader) | {}, on_done=on_done
    )
    by_id = {s["id"]: s for s in statuses}
    assert by_id["techmeme_river"]["status"] == "failed"
    assert by_id["techmeme_river"]["error"].startswith("FetchError: HTTP 503")
    assert by_id["bbc_world"]["status"] == "ok" and by_id["bbc_world"]["items_in_window"] == 3
    assert {d[0] for d in done} == {s["id"] for s in ready}
    bbc = [i for i in items if i["source_id"] == "bbc_world"]
    assert [i["rank"] for i in bbc] == [1, 2, 3]
    assert len({i["url"] for i in items}) == len(items)


def test_groups_split_the_world_sources():
    ready, _ = source_config.select(CONFIG, "world")
    seen = []
    for name in world.GROUP_NAMES:
        select, methods = group("world", name)
        chosen = select(ready)
        assert all(s["method"] in methods for s in chosen)
        seen += [s["id"] for s in chosen]
    assert sorted(seen) == sorted(s["id"] for s in ready)


def test_invalid_sources_are_skipped_with_a_reason():
    config = {
        "sources": [
            {"id": "ok", "name": "OK", "method": "rss", "url": "https://feeds.example/rss"},
            {"id": "inside", "name": "Inside", "method": "rss", "url": "file:///etc/passwd"},
            {"id": "ok", "name": "Twice", "method": "rss", "url": "https://feeds.example/2"},
            {"id": "what", "name": "What", "method": "scrape_everything"},
            {"id": "wiki", "name": "Wiki", "method": "wikimedia_top", "project": "evil.example"},
            {"id": "off", "name": "Off", "method": "rss", "url": "nope", "enabled": False},
        ]
    }
    ready, skipped = source_config.select(config, "world")
    assert [s["id"] for s in ready] == ["ok"]
    reasons = {s["id"] + ":" + s["name"]: s["error"] for s in skipped}
    assert "public http(s)" in reasons["inside:Inside"]
    assert "repeats" in reasons["ok:Twice"]
    assert "not one Tin runs" in reasons["what:What"]
    assert "Wikipedia" in reasons["wiki:Wiki"]
    assert all(s["status"] == "skipped" for s in skipped)


def test_the_packaged_world_config_is_valid():
    path = Path(world.__file__).resolve().parents[1] / "intel_sources" / "world.json"
    config = source_config.load(path.read_bytes())
    ready, skipped = source_config.select(config, "world")
    assert skipped == [] and len(ready) >= 50


async def test_a_stuck_source_times_out_with_a_named_reason_and_the_rest_go_on(monkeypatch):
    import asyncio

    import tin_lite.intel_fetch as intel_fetch

    monkeypatch.setattr(intel_fetch, "MIN_TIMEOUT_SECONDS", 0.01)

    async def stuck(src, window, ctx):
        await asyncio.sleep(30)
        return []

    async def quick(src, window, ctx):
        return [world.item(src, "https://a.example/1", "A", None, "", fetched_at="x")]

    sources = [
        {"id": "slow", "name": "Slow", "method": "stuck", "timeout_seconds": 0.05},
        {"id": "fast", "name": "Fast", "method": "quick"},
    ]
    items, statuses = await run_sources(
        {"stuck": stuck, "quick": quick}, sources, WINDOW, FakeReader(), settings={}
    )
    by_id = {s["id"]: s for s in statuses}
    assert by_id["slow"]["status"] == "failed"
    assert by_id["slow"]["error"] == "timed out after 0.05 s"
    assert by_id["fast"]["status"] == "ok" and len(items) == 1
    assert (
        intel_fetch.deadline({"method": "google_news_rss"}, world.settings({}, WINDOW.until)) == 900
    )
    assert intel_fetch.deadline({"method": "rss"}, {}) == intel_fetch.SOURCE_TIMEOUT_SECONDS
