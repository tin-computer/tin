"""Source configs: which sources to pull and how, checked per method before any fetch.

A config is `{"sources": [{"id", "name", "method", ...}], ...settings}`. A source that fails
its method's check is skipped with a reason in the run's source status, never fatal: one typo
in a 200-source file must not cost the day. Configs from project files are untrusted, so URLs
must be public http(s) and every field is bounded.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from tin_lite.intel_fetch.http import PublicReader

MAX_SOURCES = 600
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")


class ConfigError(ValueError):
    """The config as a whole can't be read (not JSON, not an object, no sources list)."""


def digest(config: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load(raw: bytes | str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ConfigError("the source config is not valid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
        raise ConfigError("the source config needs a `sources` list")
    if len(value["sources"]) > MAX_SOURCES:
        raise ConfigError(f"the source config lists more than {MAX_SOURCES} sources")
    return value


def _url(value: object) -> bool:
    return isinstance(value, str) and PublicReader.allowed(value)


def _text(value: object, limit: int = 300) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _regex(value: object) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= 500:
        return False
    try:
        re.compile(value)
    except re.error:
        return False
    return True


def _int(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


# Each method's required fields and bounded optional ones. A check returns a reason or None.
def _check_world(source: dict) -> str | None:
    method = source["method"]
    if method in {
        "rss",
        "atom",
        "google_news_rss",
        "google_trends_rss",
        "techmeme_river",
        "gdelt_doc",
    }:
        if not _url(source.get("url")):
            return "its url is not a public http(s) address"
    if method == "google_trends_rss" and not _text(source.get("geo", "US"), 10):
        return "its geo is not a short country code"
    if method == "techmeme_river" and not _int(source.get("utc_offset_hours", 5), -12, 14):
        return "its utc_offset_hours is out of range"
    if method == "wikimedia_top":
        if not re.fullmatch(r"[a-z\-]{2,20}\.wikipedia", str(source.get("project", ""))):
            return "its project is not a Wikipedia like en.wikipedia"
        if "exclude_regex" in source and not _regex(source["exclude_regex"]):
            return "its exclude_regex is not a valid pattern"
        if not _int(source.get("limit", 50), 1, 1000):
            return "its limit is out of range"
    if method == "hn_front_page":
        for name, default, high in (
            ("topstories_limit", 150, 500),
            ("beststories_limit", 150, 500),
            ("points_floor", 100, 100_000),
            ("comments_per_story", 4, 20),
        ):
            if not _int(source.get(name, default), 0, high):
                return f"its {name} is out of range"
    return None


METHOD_CHECKS = {
    "world": (
        {
            "rss",
            "atom",
            "google_news_rss",
            "google_trends_rss",
            "techmeme_river",
            "hn_front_page",
            "wikimedia_top",
            "gdelt_doc",
        },
        _check_world,
    ),
}


def register_checks(scope: str, methods: set[str], check) -> None:
    """A client package's methods and their checks (package B registers rss, hn, reddit)."""
    METHOD_CHECKS[scope] = (set(methods), check)


def select(config: dict[str, Any], scope: str) -> tuple[list[dict], list[dict]]:
    """The enabled sources fit to run, and a skipped status for each one that isn't."""
    methods, check = METHOD_CHECKS[scope]
    ready: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for source in config.get("sources", []):
        if not isinstance(source, dict):
            continue
        if source.get("enabled", True) is False:
            continue
        sid = source.get("id")
        reason = None
        if not isinstance(sid, str) or not _ID.fullmatch(sid):
            reason, sid = "its id is missing or not a short slug", str(sid)[:100]
        elif sid in seen:
            reason = "its id repeats another source's"
        elif not _text(source.get("name"), 200):
            reason = "its name is missing"
        elif source.get("method") not in methods:
            reason = f"its method {str(source.get('method'))[:40]!r} is not one Tin runs"
        else:
            reason = check(source)
        seen.add(sid)
        if reason:
            skipped.append(
                {
                    "id": sid,
                    "name": str(source.get("name") or sid)[:200],
                    "method": str(source.get("method"))[:40],
                    "status": "skipped",
                    "error": f"Skipped: {reason}."[:300],
                    "items_in_window": 0,
                }
            )
        else:
            ready.append(source)
    return ready, skipped
