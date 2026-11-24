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


# Untrusted configs on Tin's worker: patterns, fan-out and deadlines are bounded.


def test_patterns_that_backtrack_or_backreference_are_skipped():
    for pattern in ("(a+)+$", r"(\w+\s?)*$", "(a|aa)+$", r"(a)\1", "(?P<x>a)(?P=x)", "x" * 201):
        assert rss.safe_pattern(pattern), pattern
    for pattern in (
        r"^https://www\.vendor\.example/epic/post/[^/]+/?$",
        r"vendor\.example/(blog|press|news)/[^/]+/?$",
        r"vendor\.example/(news|insights)[^?]*/[^/]+/?$",
        r"a{2,}(b{1,3})?",
    ):
        assert rss.safe_pattern(pattern) is None, pattern
    cfg = {
        "sources": [
            {
                "id": "evil",
                "name": "Evil",
                "method": "html_links",
                "url": "https://x.example/",
                "link_pattern": "(a+)+$",
            },
            {
                "id": "evil2",
                "name": "Evil 2",
                "method": "sitemap",
                "url": "https://x.example/s.xml",
                "url_pattern": "(.*a){12}",
            },
        ]
    }
    ready, skipped = config.select(cfg, "client")
    assert ready == [] and len(skipped) == 2
    assert "repeats" in skipped[0]["error"] and "repeats" in skipped[1]["error"]


async def test_matching_runs_off_the_loop_and_a_slow_pattern_fails_its_source(monkeypatch):
    import time

    from tin_lite.intel_fetch.http import FetchError

    def slow(pattern, candidates):
        time.sleep(0.5)
        return candidates

    monkeypatch.setattr(rss, "_match_all", slow)
    monkeypatch.setattr(rss, "MATCH_SECONDS", 0.05)
    try:
        await rss.matching("a", ["a"])
    except FetchError as exc:
        assert "too long" in str(exc)
    else:
        raise AssertionError("a slow match must fail its source")
    monkeypatch.undo()
    many = [f"https://x.example/blog/{n}" for n in range(rss.MAX_CANDIDATES + 50)]
    assert len(await rss.matching(r"/blog/\d+$", many)) == rss.MAX_CANDIDATES


async def test_sitemap_indexes_are_followed_only_so_far():
    from intel_client_fakes import FakeReader

    children = "".join(
        f"<sitemap><loc>https://big.example/s{n}.xml</loc></sitemap>" for n in range(40)
    )
    index = f'<sitemapindex xmlns="x">{children}</sitemapindex>'.encode()
    entries = "".join(
        f"<url><loc>https://big.example/blog/{n}</loc><lastmod>2026-11-24</lastmod></url>"
        for n in range(400)
    )
    child = f'<urlset xmlns="x">{entries}</urlset>'.encode()
    reader = FakeReader(lambda url: index if url.endswith("/sitemap.xml") else child)
    ctx = {"reader": reader}
    found = await rss._sitemap_entries(ctx, "https://big.example/sitemap.xml")
    fetched_children = [u for u in reader.requests if "/s" in u and not u.endswith("sitemap.xml")]
    assert len(fetched_children) <= rss.MAX_CHILD_SITEMAPS
    assert len(found) == rss.MAX_SITEMAP_ENTRIES


async def test_a_source_past_its_deadline_fails_alone():
    import asyncio

    async def endless(src, window, ctx):
        await asyncio.sleep(10)
        return []

    slow = rss.bounded(endless, 0.05)
    ok = {
        "id": "health-weekly",
        "name": "H",
        "method": "rss",
        "url": "https://feeds.example/health.xml",
    }
    cfg = _config()
    items, statuses = await run_sources(
        {"slow": slow, "rss": rss.METHODS["rss"]},
        [{"id": "slow", "name": "Slow", "method": "slow"}, ok],
        WINDOW,
        rss_reader(),
        settings={**rss.settings(cfg), "fetched_at": "2026-11-24T13:00:00Z"},
    )
    assert statuses[0]["status"] == "failed" and "TimeoutError" in statuses[0]["error"]
    assert statuses[1]["status"] == "ok" and len(items) == 3
