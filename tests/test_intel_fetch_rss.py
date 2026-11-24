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
    assert "public http(s)" in reasons["local"] and rss.REGEX_GONE in reasons["badre"]
    assert all(s["status"] == "skipped" for s in skipped)


def test_groups_split_the_client_sources_by_method():
    cfg = _config()
    pick = {name: group("client", name)[0](cfg) for name in ("rss", "gnews", "pages")}
    assert [s["id"] for s in pick["rss"]] == ["health-weekly"]
    assert [s["id"] for s in pick["gnews"]] == ["gn-lakehouse"]
    assert [s["id"] for s in pick["pages"]] == ["vendor-newsroom", "vendor-blog", "jx-crossref"]


# Untrusted configs on Tin's worker: patterns, fan-out and deadlines are bounded.


def test_path_globs_match_segment_by_segment():
    match = rss.glob_match
    assert match("vendor.example/blog/*", "https://vendor.example/blog/launch")
    assert match("vendor.example/blog/*", "https://www.vendor.example/blog/launch/")
    assert match("www.vendor.example/blog/*", "http://VENDOR.example/blog/launch?utm=1#top")
    assert not match("vendor.example/blog/*", "https://vendor.example/blog/")
    assert not match("vendor.example/blog/*", "https://vendor.example/blog/a/b")
    assert not match("vendor.example/blog/*", "https://vendor.example/Blog/a")
    assert not match("vendor.example/blog/*", "https://evil.example/blog/a")
    assert not match("vendor.example/blog/*", "https://blog.vendor.example/blog/a")
    assert not match("vendor.example/blog/*", "https://vendor.example//a")
    assert match("vendor.example/news/**", "https://vendor.example/news/2026/11/a")
    assert match("vendor.example/news/**", "https://vendor.example/news/a")
    assert not match("vendor.example/news/**", "https://vendor.example/news/")
    assert match("vendor.example/epic/post/*", "https://www.vendor.example/epic/post/x")
    assert rss.matching(
        ["vendor.example/blog/*", "vendor.example/press/*"],
        [
            "https://vendor.example/press/a",
            "https://vendor.example/about",
            "https://vendor.example/blog/b",
        ],
    ) == ["https://vendor.example/press/a", "https://vendor.example/blog/b"]


def test_bad_globs_and_old_regexes_are_skipped_with_a_reason():
    for globs, reason in (
        (["vendor.example/**/x"], "only as its last part"),
        (["vendor.example/blog-*"], "whole segment"),
        (["*.vendor.example/blog/*"], "host and a path"),
        (["https://vendor.example/blog/*"], "host and a path"),
        (["vendor.example/blog/*?x"], "host and a path"),
        (["x" * 201], "longer than 200"),
        ([f"vendor.example/{n}/*" for n in range(21)], "1 to 20"),
        ([], "1 to 20"),
    ):
        assert reason in rss.globs_problem(globs), globs
    cfg = {
        "sources": [
            {
                "id": "old",
                "name": "Old",
                "method": "html_links",
                "url": "https://x.example/",
                "link_pattern": "(a+)+$",
                "link_paths": ["x.example/a/*"],
            },
            {
                "id": "old2",
                "name": "Old 2",
                "method": "sitemap",
                "url": "https://x.example/s.xml",
                "url_pattern": "x",
            },
            {
                "id": "bad",
                "name": "Bad",
                "method": "sitemap",
                "url": "https://x.example/s.xml",
                "url_paths": ["x.example/**/y"],
            },
            {
                "id": "good",
                "name": "Good",
                "method": "sitemap",
                "url": "https://x.example/s.xml",
                "url_paths": ["x.example/blog/*", "x.example/news/**"],
            },
        ]
    }
    ready, skipped = config.select(cfg, "client")
    assert [s["id"] for s in ready] == ["good"]
    reasons = {s["id"]: s["error"] for s in skipped}
    assert rss.REGEX_GONE in reasons["old"] and rss.REGEX_GONE in reasons["old2"]
    assert "url_paths" in reasons["bad"]


def test_hostile_urls_stay_linear():
    import time

    glob = "vendor.example/" + "/".join(["*"] * 10) + "/**"
    many = "https://vendor.example/" + "/".join(["a"] * 50_000)
    long_segment = "https://vendor.example/" + "a" * 2_000_000
    started = time.monotonic()
    for _ in range(20):
        assert rss.glob_match(glob, many)
        assert not rss.glob_match(glob, long_segment)
        assert not rss.glob_match("vendor.example/a/b", many)
    assert time.monotonic() - started < 2
    candidates = [f"https://vendor.example/blog/{n}" for n in range(rss.MAX_CANDIDATES + 50)]
    assert len(rss.matching(["vendor.example/blog/*"], candidates)) == rss.MAX_CANDIDATES


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
