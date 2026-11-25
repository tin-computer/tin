"""The day's snapshot a judge reads: `intel-snapshot/1`, gzipped JSONL built from Postgres.

The switchboard uploads it to the judge's sandbox, where a root helper loads it into a
read-only SQLite file for the `intel` CLI (sandbox/intel/intel.py). Lines, in order:

- `{"kind": "meta", "format", "scope", "day", "now", "client_name", "world_day",
  "world_status", "sources": [{"id", "name", "status", "items", "error"}]}`
- `{"kind": "item", ...contract item..., "item_key"}` for every item the run fetched;
- `{"kind": "story", "sid", "key", "title", "summary", "relevance", "tags", "sensitive",
  "continues", "first_day", "last_day"}` for the scope's earlier stories (world: 7 days,
  client: 14), and `{"kind": "story_item", "sid", "day", "item_key", "url", "title",
  "source"}` for their items;
- for a client, today's world stories are among the items (source "World stories"), put
  there by its load step (intel_store.put_world_items). The world day it used is pinned on the
  fetch row (`world_day`), so the snapshot, the brief and sources.json name the same one.

The same inputs give the same bytes (gzip without a timestamp).
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from tin_lite import intel_store
from tin_lite.intel_fetch.items import canon, iso

FORMAT = "intel-snapshot/1"
PRIOR_DAYS = {"world": 7, "client": 14}


def _line(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode()


async def build(conn, *, fetch_run_id: UUID, client_name: str | None = None) -> bytes:
    fetch = await intel_store.get_fetch(conn, fetch_run_id)
    if fetch is None:
        raise LookupError("this run has no intel fetch")
    scope, day = fetch["scope"], fetch["day"]
    project_id = fetch["project_id"]
    prior = await intel_store.stories(
        conn,
        project_id=project_id,
        since_day=day - timedelta(days=PRIOR_DAYS[scope]),
        until_day=day,
        limit=2000,
    )
    world_day = fetch["world_day"] if scope == "client" else None
    rows = await conn.fetch(
        """SELECT item_key, source_type, source, source_id, source_url, url, discussion_url,
               publisher_site, title, author, published_at, rank, engagement, text, fetched_at
           FROM intel_items WHERE run_id = $1 ORDER BY item_key""",
        fetch_run_id,
    )
    statuses = fetch["source_status"] or []
    meta = {
        "kind": "meta",
        "format": FORMAT,
        "scope": scope,
        "day": day.isoformat(),
        "now": iso(fetch["window_end"]),
        "client_name": client_name,
        "world_day": (world_day or {}).get("day"),
        "world_status": (world_day or intel_store.WORLD_MISSING)["status"]
        if scope == "client"
        else None,
        "sources": [
            {
                "id": status.get("id"),
                "name": status.get("name"),
                "status": status.get("status"),
                "items": status.get("items_in_window", 0),
                "error": status.get("error"),
            }
            for status in statuses
        ],
    }
    out = [_line(meta)]
    for row in rows:
        value = dict(row)
        value["engagement"] = intel_store._json(value["engagement"]) or {}
        for name in ("published_at", "fetched_at"):
            value[name] = iso(value[name]) if value[name] else None
        out.append(_line({"kind": "item", **value}))
    for story in prior:
        out.append(
            _line(
                {
                    "kind": "story",
                    **{key: story[key] for key in _STORY_FIELDS},
                }
            )
        )
        for entry in story.get("items", []):
            out.append(
                _line(
                    {
                        "kind": "story_item",
                        "sid": story["sid"],
                        "day": entry["day"],
                        "item_key": entry["item_key"],
                        "url": entry["url"],
                        "title": entry["title"],
                        "source": entry["source"],
                    }
                )
            )
    return gzip.compress(b"".join(out), mtime=0)


_STORY_FIELDS = (
    "sid",
    "key",
    "title",
    "summary",
    "relevance",
    "tags",
    "sensitive",
    "continues",
    "first_day",
    "last_day",
)


async def prepare(conn, *, fetch_run_id: UUID, now: datetime | None = None) -> dict:
    """Ready the run's items for a snapshot, once all fetch groups are done: a client run pins
    its world day on the fetch row (the newest usable one, once; intel_store.pin_world_day) and
    gets that day's stories as items (intel_store.put_world_items), and every item gets its
    final id, the earlier stories' items keeping theirs. Returns the item count and the world
    day (for a client, the pinned row or {"status": "missing"})."""
    fetch = await intel_store.get_fetch(conn, fetch_run_id)
    if fetch is None:
        raise LookupError("this run has no intel fetch")
    world_day = None
    if fetch["scope"] == "client":
        now = now or datetime.now(UTC)
        world_day = fetch["world_day"]
        if world_day is None:
            found = await intel_store.latest_world_day(conn, now=now)
            world_day = await intel_store.pin_world_day(
                conn, fetch_run_id, found or intel_store.WORLD_MISSING
            )
        if world_day.get("day"):
            await intel_store.put_world_items(conn, run_id=fetch_run_id, world=world_day, now=now)
    taken = {
        row["item_key"]: canon(row["url"])
        for row in await conn.fetch(
            """SELECT DISTINCT ON (i.item_key) i.item_key, i.url FROM intel_story_items i
               JOIN intel_stories s ON s.id = i.story_id
               WHERE s.project_id IS NOT DISTINCT FROM $1 AND i.day >= $2
               ORDER BY i.item_key, i.day DESC""",
            fetch["project_id"],
            fetch["day"] - timedelta(days=PRIOR_DAYS[fetch["scope"]]),
        )
    }
    items = await intel_store.assign_item_keys(conn, fetch_run_id, taken=taken)
    await intel_store.set_fetch(conn, fetch_run_id, items=items)
    return {"items": items, "world_day": world_day}
