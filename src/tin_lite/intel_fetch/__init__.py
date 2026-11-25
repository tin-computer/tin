"""Code-owned fetchers for daily intelligence: every source in a config, read in one window.

Each method is `async def m_<name>(src, window, ctx) -> list[Item]` and records what it saw in
`ctx` (`http`, `seen`, `excluded`). `run_sources` runs a group's sources concurrently through
one PublicReader, numbers each source's items in its own order (`rank`), and keeps the
prototype's status fields per source: `status` ok|empty|failed, `error` (at most 300
characters), `http`, `items_seen`, `items_in_window`, `excluded_outlets` and `seconds`.
A failing source is recorded and skipped, never fatal.

Groups split one scope's sources into activities that can retry on their own: a scope
registers its groups by the methods each runs (`register_groups`), and a group's selector picks
its sources from the validated source list.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from tin_lite.intel_fetch.http import PublicReader
from tin_lite.intel_fetch.items import Item, Window

__all__ = ["GROUPS", "Item", "Window", "group", "register_groups", "run_sources"]

Method = Callable[[dict, Window, dict], Awaitable[list[Item]]]
# Every source gets a deadline, so one stuck read can't hold its group: five minutes unless the
# source sets `timeout_seconds` or its scope sets one for the method (settings
# "method_timeouts"). A method's own shorter timeouts sit inside it. Runaway guards.
SOURCE_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 1800


def deadline(src: dict, settings: dict[str, Any]) -> float:
    value = src.get("timeout_seconds")
    if value is None:
        value = (settings.get("method_timeouts") or {}).get(src.get("method"))
    if value is None:
        value = settings.get("source_timeout", SOURCE_TIMEOUT_SECONDS)
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = SOURCE_TIMEOUT_SECONDS
    return min(max(number, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)


Select = Callable[[list[dict]], list[dict]]
GROUPS: dict[tuple[str, str], tuple[Select, dict[str, Method]]] = {}


def _selector(wanted: frozenset[str]) -> Select:
    def select(sources: list[dict]) -> list[dict]:
        return [s for s in sources if s.get("method") in wanted]

    return select


def register_groups(
    scope: str, group_methods: Mapping[str, Iterable[str]], methods: dict[str, Method]
) -> None:
    """A scope's fetch groups: each runs the sources whose method it names. Its selector picks
    them from the validated source list (config.select's `ready`), for every scope alike."""
    for name, wanted in group_methods.items():
        chosen = frozenset(wanted)
        GROUPS[(scope, name)] = (
            _selector(chosen),
            {method: methods[method] for method in sorted(chosen) if method in methods},
        )


def group(scope: str, name: str) -> tuple[Select, dict[str, Method]]:
    if (scope, name) not in GROUPS:
        raise KeyError(f"no intel fetch group {scope}/{name}")
    return GROUPS[(scope, name)]


async def run_source(
    method: Method, src: dict, window: Window, settings: dict[str, Any]
) -> tuple[list[Item], dict]:
    ctx: dict[str, Any] = {**settings, "http": None, "seen": 0, "excluded": 0}
    started = time.monotonic()
    limit = deadline(src, settings)
    try:
        async with asyncio.timeout(limit) as clock:
            items = list(await method(src, window, ctx))
        for rank, item in enumerate(items, 1):
            item["rank"] = rank  # the source's own order (front page, feed, views rank)
            item.setdefault("source_id", src["id"])
        status = {"status": "ok" if items else "empty", "error": None}
    except asyncio.CancelledError:
        raise
    except TimeoutError as exc:
        items = []
        if clock.expired():
            status = {"status": "failed", "error": f"timed out after {limit:g} s"}
        else:
            status = {"status": "failed", "error": f"TimeoutError: {exc}"[:300]}
    except Exception as exc:  # noqa: BLE001 — one source's failure is its status, not the run's
        items = []
        status = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
    return items, {
        "id": src["id"],
        "name": src.get("name", src["id"]),
        "method": src.get("method"),
        **status,
        "http": ctx["http"],
        "items_seen": ctx["seen"],
        "items_in_window": len(items),
        "excluded_outlets": ctx["excluded"],
        "seconds": round(time.monotonic() - started, 1),
    }


async def run_sources(
    methods: dict[str, Method],
    sources: list[dict],
    window: Window,
    reader: PublicReader,
    *,
    settings: dict[str, Any] | None = None,
    on_done: Callable[[dict, list[Item], dict], Awaitable[None]] | None = None,
) -> tuple[list[Item], list[dict]]:
    """Run every source; returns the items deduped by URL in config order (the first source
    listed keeps an item) and each source's status, in config order. `on_done` is awaited as
    each source finishes, so a retried activity can skip the sources already recorded."""
    shared = {**(settings or {}), "reader": reader}

    async def one(src: dict) -> tuple[list[Item], dict]:
        method = methods.get(src.get("method"))
        if method is None:
            result: tuple[list[Item], dict] = (
                [],
                {
                    "id": src["id"],
                    "name": src.get("name", src["id"]),
                    "method": src.get("method"),
                    "status": "failed",
                    "error": "no fetch method for this source",
                    "items_in_window": 0,
                },
            )
        else:
            result = await run_source(method, src, window, shared)
        if on_done is not None:
            await on_done(src, *result)
        return result

    results = await asyncio.gather(*(one(src) for src in sources))
    seen: set[str] = set()
    items: list[Item] = []
    statuses: list[dict] = []
    for found, status in results:
        kept = 0
        for item in found:
            if not item.get("url") or item["url"] in seen:
                continue
            seen.add(item["url"])
            items.append(item)
            kept += 1
        statuses.append({**status, "duplicates_dropped": len(found) - kept})
    return items, statuses
