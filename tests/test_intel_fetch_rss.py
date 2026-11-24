"""The client's RSS-family fetchers keep the prototype's items on the same bodies.

`rss-expected.json` was written once by the prototype's rss.py (its methods and finish_item,
Google News unwrapping off, as Tin unwraps only after the judge) on the fixture bodies here.
"""

from __future__ import annotations

import json

from intel_client_fakes import FIXTURES, WINDOW, rss_reader

from tin_lite import intel_daily  # noqa: F401 — registers the client's checks and groups
from tin_lite.intel_fetch import config, group, rss, run_sources

FIELDS = ("url", "title", "author", "published_at", "text", "source", "source_url", "rank")


def _config():
    return json.loads((FIXTURES / "rss-sources.json").read_text())


async def _run(sources, cfg):
    settings = {**rss.settings(cfg), "fetched_at": "2026-11-24T13:00:00Z"}
    return await run_sources(rss.METHODS, sources, WINDOW, rss_reader(), settings=settings)


async def test_each_method_matches_the_prototype_on_the_same_bodies():
    cfg = _config()
    expected = json.loads((FIXTURES / "rss-expected.json").read_text())
    for source in cfg["sources"]:
        items, statuses = await _run([source], cfg)
        want = expected[source["id"]]
        status = statuses[0]
        assert status["status"] == want["status"], source["id"]
        assert status["items_seen"] == want["items_seen"], source["id"]
        assert status["excluded_outlets"] == want["excluded_outlets"], source["id"]
        got = [{name: item.get(name) for name in FIELDS} for item in items]
        assert got == want["items"], source["id"]
        for item in items:
            assert item["source_type"] == "rss" and item["source_id"] == source["id"]


async def test_google_news_drops_excluded_outlets_and_keeps_the_headline_as_text():
    cfg = _config()
    source = next(s for s in cfg["sources"] if s["method"] == "google_news")
    items, statuses = await _run([source], cfg)
    assert statuses[0]["excluded_outlets"] == 2  # MarketBeat by name, openpr.com by domain
    [only] = items
    assert only["source"] == "Tech Daily" and only["publisher_site"] == "https://techdaily.example"
    assert only["title"] == "Vendor adds on-demand billing to its warehouse"
    # No unwrapping at fetch time: the link stays Google News's, tracking dropped.
    assert only["url"].startswith("https://news.google.com/rss/articles/")
    assert "oc=" not in only["url"]
    assert only["text"] == only["title"]


async def test_short_feed_text_is_enriched_from_the_page():
    cfg = _config()
    source = cfg["sources"][0]
    items, _ = await _run([source], cfg)
    rule = next(i for i in items if i["url"].endswith("payer-rule"))
    assert "standard API" in rule["text"] and rule["author"] == "Lee Park"
    undated = next(i for i in items if i["url"].endswith("undated-feature"))
    assert undated["published_at"] == "2026-11-24T02:00:00Z"


async def test_a_source_that_fails_is_its_status_not_the_run():
    cfg = _config()
    broken = {"id": "gone", "name": "Gone", "method": "rss", "url": "https://feeds.example/gone"}
    items, statuses = await _run([broken, cfg["sources"][0]], cfg)
    assert statuses[0]["status"] == "failed" and "404" in statuses[0]["error"]
    assert statuses[1]["status"] == "ok" and len(items) == 3


def test_invalid_sources_are_skipped_with_a_reason():
    cfg = {
        "sources": [
            {"id": "ok", "name": "OK", "method": "rss", "url": "https://feeds.example/a.xml"},
            {"id": "local", "name": "Local", "method": "rss", "url": "file:///etc/passwd"},
            {
                "id": "badre",
                "name": "Bad",
                "method": "sitemap",
                "url": "https://x.example/s.xml",
                "url_pattern": "(",
            },
            {"id": "issn", "name": "ISSN", "method": "crossref", "issn": "12345"},
            {"id": "q", "name": "Q", "method": "google_news", "query": ""},
            {
                "id": "off",
                "name": "Off",
                "method": "rss",
                "url": "https://x.example",
                "enabled": False,
            },
            {"id": "what", "name": "What", "method": "ftp"},
        ]
    }
    ready, skipped = config.select(cfg, "client")
    assert [s["id"] for s in ready] == ["ok"]
    reasons = {s["id"]: s["error"] for s in skipped}
    assert set(reasons) == {"local", "badre", "issn", "q", "what"}
    assert "public http(s)" in reasons["local"] and "url_pattern" in reasons["badre"]
    assert all(s["status"] == "skipped" for s in skipped)


def test_groups_split_the_client_sources_by_method():
    cfg = _config()
    pick = {name: group("client", name)[0](cfg) for name in ("rss", "gnews", "pages")}
    assert [s["id"] for s in pick["rss"]] == ["health-weekly"]
    assert [s["id"] for s in pick["gnews"]] == ["gn-lakehouse"]
    assert [s["id"] for s in pick["pages"]] == ["vendor-newsroom", "vendor-blog", "jx-crossref"]
