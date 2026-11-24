"""The intel CLI and Tin's fetchers give every URL the same canonical form and id."""

from __future__ import annotations

import pytest
from intel_cli_loader import load

from tin_lite.intel_fetch import items

CLI = load()
URLS = [
    "https://www.bbc.com/news/world-1?utm_source=rss&id=3",
    "https://bbc.com/news/world-1/?id=3",
    "HTTPS://WWW.Example.COM/Path/?ref=x&fbclid=1&keep=yes",
    "https://news.google.com/rss/articles/CBMiVkFVX3lxTE?oc=5",
    "https://news.ycombinator.com/item?id=4242",
    "not a url",
    "",
    "https://例え.jp/パス?q=日本",
    "http://[::1]:8080/a",
]


@pytest.mark.parametrize("url", URLS)
def test_canon_and_short_id_match_the_cli(url):
    assert items.canon(url) == CLI.canon(url)
    key = items.canon(url)
    for n in (5, 6, 12):
        assert items.short_id(key, n) == CLI.short_id(key, n)


def test_www_trailing_slash_and_tracking_share_one_id():
    a, b = (items.canon(u) for u in URLS[:2])
    assert a == b == "bbc.com/news/world-1?id=3"
    assert items.short_id(a) == CLI.short_id(b)
    assert items.KEY_LENGTH == 5 and len(items.short_id(a)) == 5
