"""The contract item every intel fetcher writes, and the ids the `intel` CLI gives them.

`canon` and `short_id` are byte-for-byte the CLI's (sandbox/intel/intel.py): an item's id is
the same in Postgres, in the snapshot and in the agent's shell, and the same URL keeps its id
across days. A test holds the two copies together.
"""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.parse
from datetime import UTC, datetime
from typing import Any, NamedTuple, TypedDict

SOURCE_TYPES = ("world", "rss", "hn", "reddit", "x")
MAX_TEXT = 8000
KEY_LENGTH = 5
MAX_KEY_LENGTH = 12


class Item(TypedDict, total=False):
    """One fetched item: an article, a post, a thread or a front-page entry.

    Fetchers make no judgment calls: no relevance, no notes, no merging by meaning.
    `rank` is the item's place in its source's own order (front-page position, feed position,
    views rank); `published_at` and `fetched_at` are ISO 8601 UTC strings.
    """

    source_type: str
    source: str
    source_id: str
    source_url: str | None
    url: str
    discussion_url: str | None
    publisher_site: str | None
    title: str | None
    author: str | None
    published_at: str | None
    text: str
    engagement: dict[str, Any]
    rank: int | None
    fetched_at: str


class Window(NamedTuple):
    since: datetime
    until: datetime


def canon(url: str | None) -> str:
    u = (url or "").strip()
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return u.lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    q = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        if not re.match(r"^(utm_|ref$|ref_src|cmpid|smid|guccounter|fbclid|gclid|mc_)", k, re.I)
    ]
    return f"{host}{p.path.rstrip('/')}" + (f"?{urllib.parse.urlencode(q)}" if q else "")


def short_id(key: str, n: int = KEY_LENGTH) -> str:
    return base64.b32encode(hashlib.sha1(key.encode()).digest()).decode().lower()[:n]  # noqa: S324 — an id, not a secret


def assign_keys(canons: list[str], taken: dict[str, str] | None = None) -> dict[str, str]:
    """Ids for `canons` in order: the first to want an id keeps it, a later different URL with
    the same prefix gets a longer one. `taken` maps ids already in use to their canon URL (the
    prior stories' items, which keep their ids)."""
    used = dict(taken or {})
    keys: dict[str, str] = {}
    for value in canons:
        if value in keys:
            continue
        n = KEY_LENGTH
        key = short_id(value, n)
        while key in used and used[key] != value and n < MAX_KEY_LENGTH:
            n += 1
            key = short_id(value, n)
        used[key] = value
        keys[value] = key
    return keys


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _cut(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "")
    return text[:limit] if text else None


def bounded(item: Item) -> Item | None:
    """The item as Postgres keeps it: each field cut to its column's bound. None when it has no
    usable URL, which nothing downstream can link or dedupe."""
    url = _cut(item.get("url"), 2000)
    if not url or not url.startswith(("http://", "https://")):
        return None
    source_type = item.get("source_type")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"unknown intel source type: {source_type!r}")
    engagement = item.get("engagement") or {}
    return Item(
        source_type=source_type,
        source=_cut(item.get("source"), 200) or "unknown",
        source_id=_cut(item.get("source_id"), 100) or "unknown",
        source_url=_cut(item.get("source_url"), 2000),
        url=url,
        discussion_url=_cut(item.get("discussion_url"), 2000),
        publisher_site=_cut(item.get("publisher_site"), 300),
        title=_cut(item.get("title"), 500),
        author=_cut(item.get("author"), 200),
        published_at=item.get("published_at"),
        text=(_cut(item.get("text"), MAX_TEXT) or ""),
        engagement=engagement if isinstance(engagement, dict) else {},
        rank=item.get("rank"),
        fetched_at=item.get("fetched_at") or iso(datetime.now(UTC)),
    )
