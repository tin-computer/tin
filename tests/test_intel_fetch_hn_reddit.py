"""The client's HN and Reddit fetchers keep the prototype's items on the same answers.

`hn-reddit-expected.json` was written once by the prototype's hn_reddit.py on the routed
Algolia and Arctic Shift answers in `hn-reddit.json`.
"""

from __future__ import annotations

import json

from intel_client_fakes import FIXTURES, WINDOW, hn_reddit_reader

from tin_lite import intel_daily
from tin_lite.intel_fetch import config, hn_reddit, run_sources
from tin_lite.intel_fetch.http import Response

FIELDS = (
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
)


async def _nap(_seconds):
    return None


def _settings(found):
    return {
        **found,
        "fetched_at": "2026-11-24T13:00:00Z",
        "hn_trees": {},
        "reddit_trees": {},
        "reddit_posts": {},
        "sleep": _nap,
    }


async def test_items_match_the_prototype():
    reader, cfg = hn_reddit_reader()
    sources, found = hn_reddit.expand(cfg)
    ready, skipped = config.select({"sources": sources}, "client")
    assert not skipped
    items, statuses = await run_sources(
        hn_reddit.METHODS, ready, WINDOW, reader, settings=_settings(found)
    )
    expected = {
        row["url"]: row for row in json.loads((FIXTURES / "hn-reddit-expected.json").read_text())
    }
    got = {item["url"]: {name: item.get(name) for name in FIELDS} for item in items}
    assert set(got) == set(expected)
    for url, row in expected.items():
        assert got[url] == {name: row[name] for name in FIELDS}, url
    by_id = {s["id"]: s for s in statuses}
    assert by_id["hn:caboodle"]["status"] == "empty"
    assert by_id["reddit:healthit"]["status"] == "ok"
    # "Fabricated" doesn't match the whole word "Fabric".
    assert by_id["reddit:dataengineering"]["items_in_window"] == 1


def test_expand_makes_one_source_per_query_and_subreddit_with_tin_endpoints():
    _reader, cfg = hn_reddit_reader()
    cfg = json.loads(json.dumps(cfg))
    cfg["hn"]["endpoint"] = "http://169.254.169.254/latest"
    cfg["hn"]["queries"].append({"q": "Epic Clarity", "tags": ["comment"]})
    sources, found = hn_reddit.expand(cfg)
    assert [s["id"] for s in sources] == [
        "hn:epic-clarity",
        "hn:caboodle",
        "hn:epic-clarity-2",
        "reddit:healthit",
        "reddit:dataengineering",
    ]
    assert found["hn"]["extra_params"] == {"typoTolerance": "false"}
    # The config names an endpoint; Tin reads its own.
    assert "169.254" not in json.dumps(found)


def test_combine_reads_both_files_and_reports_a_broken_one():
    _reader, cfg = hn_reddit_reader()
    rss_raw = (FIXTURES / "rss-sources.json").read_bytes()
    combined, problems = intel_daily.combine(
        {intel_daily.RSS_PATH: rss_raw, intel_daily.HN_REDDIT_PATH: json.dumps(cfg).encode()}
    )
    assert not problems
    methods = [s["method"] for s in combined["sources"]]
    assert methods.count("hn") == 2 and methods.count("reddit") == 2 and "crossref" in methods
    assert combined["settings"]["rss"]["exclude_outlets"] == ["MarketBeat"]
    broken, problems = intel_daily.combine(
        {intel_daily.RSS_PATH: rss_raw, intel_daily.HN_REDDIT_PATH: b"{not json"}
    )
    assert [p["name"] for p in problems] == [intel_daily.HN_REDDIT_PATH]
    assert all(s["method"] not in {"hn", "reddit"} for s in broken["sources"])


async def test_arctic_shift_budget_in_a_200_body_is_waited_out():
    reader, cfg = hn_reddit_reader()
    route = reader.route
    calls = {"n": 0}

    def busy_first(url):
        if "posts/search" in url and calls["n"] == 0:
            calls["n"] += 1
            return Response(
                200, b'{"error": "Too many requests"}', url, None, {"x-ratelimit-reset": "3"}
            )
        return route(url)

    reader.route = busy_first
    waits = []

    async def nap(seconds):
        waits.append(seconds)

    sources, found = hn_reddit.expand(cfg)
    sub = next(s for s in sources if s["id"] == "reddit:dataengineering")
    items, statuses = await run_sources(
        hn_reddit.METHODS, [sub], WINDOW, reader, settings={**_settings(found), "sleep": nap}
    )
    assert waits == [4.0]
    assert statuses[0]["status"] == "ok" and len(items) == 1


async def test_arctic_shift_errors_fail_only_their_subreddit():
    reader, cfg = hn_reddit_reader()
    reader.route = lambda url: Response(422, b"", url, "HTTP 422: bad query")
    sources, found = hn_reddit.expand(cfg)
    sub = next(s for s in sources if s["id"] == "reddit:healthit")
    items, statuses = await run_sources(
        hn_reddit.METHODS, [sub], WINDOW, reader, settings=_settings(found)
    )
    assert items == [] and statuses[0]["status"] == "failed"
    assert "Arctic Shift" in statuses[0]["error"]
