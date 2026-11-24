"""Postgres for daily intelligence: fetched items, snapshots, stories, briefs and the world day.

World rows have `project_id` NULL and are shared by every client; client rows belong to their
project. Every function takes an asyncpg connection (or pool) so callers choose the
transaction. Writes are idempotent by key: items by (run, canonical URL), stories by (scope,
key), story items by (story, URL), briefs by the judge's run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from tin_lite.intel_fetch.gnews import link_cache_get, link_cache_put
from tin_lite.intel_fetch.items import Item, assign_keys, bounded, canon, parse_iso, short_id

__all__ = [
    "ITEM_RETENTION",
    "SNAPSHOT_RETENTION",
    "assign_item_keys",
    "attach_items",
    "begin_fetch",
    "done_sources",
    "get_brief",
    "get_fetch",
    "get_snapshot",
    "latest_world_day",
    "link_cache_get",
    "link_cache_put",
    "prune",
    "put_brief",
    "put_items",
    "put_story",
    "record_sources",
    "save_snapshot",
    "set_fetch",
    "stories",
    "story",
]

ITEM_RETENTION = timedelta(days=30)
SNAPSHOT_RETENTION = timedelta(days=14)
SNAPSHOT_FORMAT = "intel-snapshot/1"
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


def _json(value) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def begin_fetch(
    conn,
    *,
    run_id: UUID,
    scope: str,
    project_id: UUID | None,
    day: date,
    window_start: datetime,
    window_end: datetime,
    config_sha256: str,
    config_revision: str | None = None,
) -> None:
    """The run's pinned fetch: scope, day, window and config. A retry keeps the first pin."""
    await conn.execute(
        """INSERT INTO intel_fetches (run_id, scope, project_id, day, window_start, window_end,
               config_sha256, config_revision)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (run_id) DO NOTHING""",
        run_id,
        scope,
        project_id,
        day,
        window_start,
        window_end,
        config_sha256,
        config_revision,
    )


async def get_fetch(conn, run_id: UUID) -> dict | None:
    row = await conn.fetchrow("SELECT * FROM intel_fetches WHERE run_id = $1", run_id)
    if row is None:
        return None
    value = dict(row)
    value["source_status"] = _json(value["source_status"])
    return value


async def set_fetch(conn, run_id: UUID, *, status: str | None = None, items: int | None = None):
    await conn.execute(
        """UPDATE intel_fetches SET status = COALESCE($2, status),
               item_count = COALESCE($3, item_count), updated_at = now()
           WHERE run_id = $1""",
        run_id,
        status,
        items,
    )


async def put_items(conn, *, run_id: UUID, items: list[Item]) -> int:
    """Upsert a source's items; returns how many were new. The same canonical URL seen again
    keeps one row, the one with the better (lower) rank in its own source."""
    rows = []
    for raw in items:
        item = bounded(raw)
        if item is None:
            continue
        key = canon(item["url"])
        if not key or len(key) > 2000:
            continue
        rows.append(
            {
                "item_key": short_id(key),
                "canon_url": key,
                **{name: item.get(name) for name in Item.__annotations__},
            }
        )
    if not rows:
        return 0
    # One canonical URL once per statement, the best-ranked copy.
    best: dict[str, dict] = {}
    for row in rows:
        held = best.get(row["canon_url"])
        if held is None or (row["rank"] or 10**6) < (held["rank"] or 10**6):
            best[row["canon_url"]] = row
    inserted = await conn.fetch(
        """INSERT INTO intel_items (run_id, item_key, canon_url, source_type, source, source_id,
               source_url, url, discussion_url, publisher_site, title, author, published_at,
               rank, engagement, text, fetched_at)
           SELECT $1, x.item_key, x.canon_url, x.source_type, x.source, x.source_id, x.source_url,
               x.url, x.discussion_url, x.publisher_site, x.title, x.author, x.published_at,
               x.rank, COALESCE(x.engagement, '{}'::jsonb), COALESCE(x.text, ''), x.fetched_at
           FROM jsonb_to_recordset($2::jsonb) AS x(item_key text, canon_url text,
               source_type text, source text, source_id text, source_url text, url text,
               discussion_url text, publisher_site text, title text, author text,
               published_at timestamptz, rank integer, engagement jsonb, text text,
               fetched_at timestamptz)
           ON CONFLICT (run_id, canon_url) DO UPDATE SET
               source_type = EXCLUDED.source_type, source = EXCLUDED.source,
               source_id = EXCLUDED.source_id, source_url = EXCLUDED.source_url,
               url = EXCLUDED.url, discussion_url = EXCLUDED.discussion_url,
               publisher_site = EXCLUDED.publisher_site, title = EXCLUDED.title,
               author = EXCLUDED.author, published_at = EXCLUDED.published_at,
               rank = EXCLUDED.rank, engagement = EXCLUDED.engagement, text = EXCLUDED.text,
               fetched_at = EXCLUDED.fetched_at
           WHERE COALESCE(EXCLUDED.rank, 1000000) < COALESCE(intel_items.rank, 1000000)
           RETURNING (xmax = 0) AS inserted""",
        run_id,
        json.dumps(list(best.values()), ensure_ascii=False, default=str),
    )
    return sum(1 for row in inserted if row["inserted"])


async def record_sources(conn, *, run_id: UUID, statuses: list[dict]) -> None:
    """Record sources' statuses, replacing an earlier status of the same source."""
    if not statuses:
        return
    if not hasattr(conn, "transaction"):
        async with conn.acquire() as acquired:
            return await record_sources(acquired, run_id=run_id, statuses=statuses)
    async with conn.transaction():
        current = await conn.fetchval(
            "SELECT source_status FROM intel_fetches WHERE run_id = $1 FOR UPDATE", run_id
        )
        if current is None:
            raise LookupError("this run has no intel fetch")
        merged = {entry["id"]: entry for entry in _json(current) if isinstance(entry, dict)}
        for status in statuses:
            merged[str(status["id"])] = status
        await conn.execute(
            "UPDATE intel_fetches SET source_status = $2::jsonb, updated_at = now() "
            "WHERE run_id = $1",
            run_id,
            json.dumps(list(merged.values()), ensure_ascii=False, default=str),
        )


async def done_sources(conn, run_id: UUID) -> set[str]:
    current = await conn.fetchval(
        "SELECT source_status FROM intel_fetches WHERE run_id = $1", run_id
    )
    return {str(entry["id"]) for entry in _json(current or "[]") if isinstance(entry, dict)}


async def assign_item_keys(conn, run_id: UUID, *, taken: dict[str, str] | None = None) -> int:
    """Give every item of the run its final id: the best-ranked item claims a 5-character id
    first, and a different URL with the same prefix gets a longer one. `taken` holds ids the
    prior stories' items keep. Returns the number of items."""
    rows = await conn.fetch(
        "SELECT id, canon_url, item_key FROM intel_items WHERE run_id = $1 "
        "ORDER BY rank NULLS LAST, canon_url",
        run_id,
    )
    keys = assign_keys([row["canon_url"] for row in rows], taken)
    changed = [
        (row["id"], keys[row["canon_url"]])
        for row in rows
        if row["item_key"] != keys[row["canon_url"]]
    ]
    if changed:
        await conn.executemany("UPDATE intel_items SET item_key = $2 WHERE id = $1", changed)
    return len(rows)


async def save_snapshot(conn, *, run_id: UUID, body: bytes) -> str:
    """Keep the run's snapshot once; returns its SHA-256 (the stored one on a retry)."""
    if not 0 < len(body) <= MAX_SNAPSHOT_BYTES:
        raise ValueError("The day's snapshot is empty or larger than 32 MB.")
    sha = hashlib.sha256(body).hexdigest()
    stored = await conn.fetchval(
        """INSERT INTO intel_snapshots (run_id, format, sha256, bytes, body)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (run_id) DO UPDATE SET run_id = EXCLUDED.run_id
           RETURNING sha256""",
        run_id,
        SNAPSHOT_FORMAT,
        sha,
        len(body),
        body,
    )
    return stored


async def get_snapshot(conn, run_id: UUID) -> tuple[bytes, str] | None:
    row = await conn.fetchrow("SELECT body, sha256 FROM intel_snapshots WHERE run_id = $1", run_id)
    return (bytes(row["body"]), row["sha256"]) if row else None


# Stories.


def sid_of(key: str) -> str:
    return f"s-{key}"


def _story_row(row, items: list[dict] | None = None) -> dict:
    value = {
        "id": str(row["id"]),
        "sid": sid_of(row["key"]),
        "key": row["key"],
        "title": row["title"],
        "summary": row["summary"],
        "relevance": row["relevance"],
        "tags": list(row["tags"] or []),
        "sensitive": row["sensitive"],
        "continues": sid_of(row["continues_key"]) if row.get("continues_key") else None,
        "first_day": row["first_day"].isoformat(),
        "last_day": row["last_day"].isoformat(),
    }
    if items is not None:
        value["items"] = items
    return value


_STORY_SELECT = """
    SELECT s.*, c.key AS continues_key FROM intel_stories s
    LEFT JOIN intel_stories c ON c.id = s.continues_id
"""


async def stories(
    conn,
    *,
    project_id: UUID | None,
    since_day: date,
    query: str | None = None,
    limit: int = 200,
    until_day: date | None = None,
    with_items: bool = True,
) -> list[dict]:
    """A scope's stories touched on or after `since_day`, newest first, with their items."""
    where = ["s.project_id IS NOT DISTINCT FROM $1", "s.last_day >= $2"]
    args: list[Any] = [project_id, since_day]
    if until_day is not None:
        args.append(until_day)
        where.append(f"s.last_day <= ${len(args)}")
    for word in (query or "").split()[:8]:
        args.append(f"%{word}%")
        n = len(args)
        where.append(
            f"(s.title ILIKE ${n} OR s.summary ILIKE ${n} OR s.key ILIKE ${n} "
            f"OR array_to_string(s.tags, ',') ILIKE ${n})"
        )
    args.append(max(1, min(int(limit), 2000)))
    rows = await conn.fetch(
        _STORY_SELECT
        + " WHERE "
        + " AND ".join(where)
        + " ORDER BY s.last_day DESC, CASE s.relevance WHEN 'high' THEN 0 WHEN 'medium' THEN 1"
        " WHEN 'low' THEN 2 ELSE 3 END, s.key" + f" LIMIT ${len(args)}",
        *args,
    )
    if not with_items or not rows:
        return [_story_row(dict(row)) for row in rows]
    items = await conn.fetch(
        """SELECT story_id, day, run_id, item_key, url, title, source, rank
           FROM intel_story_items WHERE story_id = ANY($1::uuid[])
           ORDER BY day, attached_at, item_key""",
        [row["id"] for row in rows],
    )
    grouped: dict[UUID, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["story_id"], []).append(
            {
                "day": item["day"].isoformat(),
                "item_key": item["item_key"],
                "url": item["url"],
                "title": item["title"],
                "source": item["source"],
                "rank": item["rank"],
            }
        )
    return [_story_row(dict(row), grouped.get(row["id"], [])) for row in rows]


async def story(conn, *, project_id: UUID | None, key: str) -> dict | None:
    row = await conn.fetchrow(
        _STORY_SELECT + " WHERE s.project_id IS NOT DISTINCT FROM $1 AND s.key = $2",
        project_id,
        key,
    )
    if row is None:
        return None
    items = await conn.fetch(
        """SELECT day, item_key, url, title, source, rank FROM intel_story_items
           WHERE story_id = $1 ORDER BY day, attached_at, item_key""",
        row["id"],
    )
    return _story_row(
        dict(row),
        [
            {
                "day": item["day"].isoformat(),
                "item_key": item["item_key"],
                "url": item["url"],
                "title": item["title"],
                "source": item["source"],
                "rank": item["rank"],
            }
            for item in items
        ],
    )


async def put_story(
    conn,
    *,
    project_id: UUID | None,
    key: str,
    day: date,
    run_id: UUID,
    title: str | None,
    summary: str | None,
    relevance: str | None,
    tags: list[str] | None,
    sensitive: bool | None,
    continues_id: UUID | None,
) -> tuple[UUID, bool]:
    """Create or update the scope's story `key` as of `day`. Fields left None keep their value.
    Returns the story's id and whether it was created. The caller validates the fields."""
    row = await conn.fetchrow(
        "SELECT * FROM intel_stories WHERE project_id IS NOT DISTINCT FROM $1 AND key = $2 "
        "FOR UPDATE",
        project_id,
        key,
    )
    if row is None:
        if not title or not summary:
            raise ValueError("a new story needs a title and a summary")
        story_id = uuid4()
        await conn.execute(
            """INSERT INTO intel_stories (id, project_id, key, title, summary, relevance, tags,
                   sensitive, continues_id, first_day, last_day, created_run_id, updated_run_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10, $11, $11)""",
            story_id,
            project_id,
            key,
            title,
            summary,
            relevance,
            tags or [],
            bool(sensitive),
            continues_id,
            day,
            run_id,
        )
        created = True
    else:
        story_id = row["id"]
        await conn.execute(
            """UPDATE intel_stories SET title = COALESCE($2, title),
                   summary = COALESCE($3, summary), relevance = COALESCE($4, relevance),
                   tags = COALESCE($5, tags), sensitive = COALESCE($6, sensitive),
                   continues_id = COALESCE($7, continues_id),
                   last_day = GREATEST(last_day, $8), updated_run_id = $9, updated_at = now()
               WHERE id = $1""",
            story_id,
            title,
            summary,
            relevance,
            tags,
            sensitive,
            continues_id,
            day,
            run_id,
        )
        created = False
    await conn.execute(
        """INSERT INTO intel_story_days (story_id, day, run_id, title, summary, relevance,
               sensitive)
           SELECT id, $2, $3, title, summary, relevance, sensitive FROM intel_stories
           WHERE id = $1
           ON CONFLICT (story_id, day) DO UPDATE SET run_id = EXCLUDED.run_id,
               title = EXCLUDED.title, summary = EXCLUDED.summary,
               relevance = EXCLUDED.relevance, sensitive = EXCLUDED.sensitive""",
        story_id,
        day,
        run_id,
    )
    return story_id, created


async def attach_items(
    conn,
    *,
    story_id: UUID,
    day: date,
    run_id: UUID,
    fetch_run_id: UUID,
    project_id: UUID | None,
    keys: list[str],
) -> tuple[int, list[str]]:
    """Attach items by their ids: today's (the fetch run's) first, then items the scope's
    earlier stories already carry. Returns how many were new and the ids found nowhere."""
    keys = list(dict.fromkeys(k for k in keys if isinstance(k, str) and k))
    if not keys:
        return 0, []
    found = {
        row["item_key"]: dict(row)
        for row in await conn.fetch(
            """SELECT item_key, url, title, source, rank FROM intel_items
               WHERE run_id = $1 AND item_key = ANY($2::text[])""",
            fetch_run_id,
            keys,
        )
    }
    rest = [k for k in keys if k not in found]
    if rest:
        for row in await conn.fetch(
            """SELECT DISTINCT ON (i.item_key) i.item_key, i.url, i.title, i.source, i.rank
               FROM intel_story_items i JOIN intel_stories s ON s.id = i.story_id
               WHERE s.project_id IS NOT DISTINCT FROM $1 AND i.item_key = ANY($2::text[])
               ORDER BY i.item_key, i.day DESC""",
            project_id,
            rest,
        ):
            found[row["item_key"]] = dict(row)
    missing = [k for k in keys if k not in found]
    added = 0
    for key in keys:
        item = found.get(key)
        if item is None:
            continue
        result = await conn.execute(
            """INSERT INTO intel_story_items (story_id, day, run_id, item_key, url, title,
                   source, rank)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (story_id, url) DO NOTHING""",
            story_id,
            day,
            run_id,
            key,
            item["url"],
            item["title"],
            item["source"],
            item["rank"],
        )
        added += result == "INSERT 0 1"
    return added, missing


# Briefs and the world day.


async def put_brief(
    conn,
    *,
    run_id: UUID,
    parent_run_id: UUID,
    project_id: UUID | None,
    day: date,
    schema: str,
    brief: dict,
    source: str,
) -> None:
    """The judge's brief (or Tin's fallback) for its run; a later put replaces it, except that
    a fallback never replaces the judge's own brief."""
    await conn.execute(
        """INSERT INTO intel_briefs (run_id, parent_run_id, project_id, day, schema, brief,
               source)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
           ON CONFLICT (run_id) DO UPDATE SET brief = EXCLUDED.brief, schema = EXCLUDED.schema,
               source = EXCLUDED.source, updated_at = now()
           WHERE intel_briefs.source = 'fallback' OR EXCLUDED.source = 'judge'""",
        run_id,
        parent_run_id,
        project_id,
        day,
        schema,
        json.dumps(brief, ensure_ascii=False),
        source,
    )


async def get_brief(conn, *, parent_run_id: UUID) -> dict | None:
    row = await conn.fetchrow(
        """SELECT run_id, schema, brief, source, day FROM intel_briefs
           WHERE parent_run_id = $1 ORDER BY (source = 'judge') DESC, updated_at DESC LIMIT 1""",
        parent_run_id,
    )
    if row is None:
        return None
    return {
        "run_id": str(row["run_id"]),
        "schema": row["schema"],
        "brief": _json(row["brief"]),
        "source": row["source"],
        "day": row["day"].isoformat(),
    }


async def set_world_day(
    conn, *, day: date, run_id: UUID, status: str, stories: int, items: int
) -> None:
    await conn.execute(
        """INSERT INTO intel_world_days (day, run_id, status, stories, items)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (day) DO UPDATE SET run_id = EXCLUDED.run_id, status = EXCLUDED.status,
               stories = EXCLUDED.stories, items = EXCLUDED.items, updated_at = now()""",
        day,
        run_id,
        status,
        stories,
        items,
    )


async def world_day(conn, day: date) -> dict | None:
    row = await conn.fetchrow("SELECT * FROM intel_world_days WHERE day = $1", day)
    return _world_day(row)


def _world_day(row) -> dict | None:
    if row is None:
        return None
    return {
        "day": row["day"].isoformat(),
        "run_id": str(row["run_id"]),
        "status": row["status"],
        "stories": row["stories"],
        "items": row["items"],
        "updated_at": row["updated_at"].isoformat(),
    }


async def latest_world_day(
    conn, *, now: datetime, within: timedelta = timedelta(hours=30)
) -> dict | None:
    """The newest usable world day (ready or partial) settled within `within` of `now`."""
    row = await conn.fetchrow(
        """SELECT * FROM intel_world_days WHERE status IN ('ready', 'partial')
             AND updated_at >= $1 AND updated_at <= $2
           ORDER BY day DESC LIMIT 1""",
        now - within,
        now + timedelta(minutes=5),
    )
    return _world_day(row)


async def latest_world_status(conn) -> dict | None:
    row = await conn.fetchrow("SELECT * FROM intel_world_days ORDER BY day DESC LIMIT 1")
    return _world_day(row)


async def prune(conn, *, now: datetime) -> None:
    """Items older than 30 days and snapshots older than 14; stories are kept."""
    await conn.execute(
        """DELETE FROM intel_items WHERE run_id IN (
               SELECT run_id FROM intel_fetches WHERE created_at < $1)""",
        now - ITEM_RETENTION,
    )
    await conn.execute(
        "DELETE FROM intel_snapshots WHERE created_at < $1", now - SNAPSHOT_RETENTION
    )


def window_iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def published(value: str | None) -> datetime | None:
    return parse_iso(value)


WORLD_SOURCE = "World stories"
_RELEVANCE_ORDER = {"high": 1, "medium": 2, "low": 3}


async def put_world_items(conn, *, run_id: UUID, now: datetime) -> dict | None:
    """A client run's copy of today's world stories, as items from "World stories" that its
    judge can search, cite and attach: the story's strongest item as the URL, its summary (and
    SENSITIVE, and its tags) as the text, relevance order as the rank. Returns the world day
    used, or None when no usable world day is recent enough."""
    day = await latest_world_day(conn, now=now)
    if day is None:
        return None
    found = await stories(
        conn, project_id=None, since_day=date.fromisoformat(day["day"]), limit=400
    )
    ordered = sorted(
        found, key=lambda s: (_RELEVANCE_ORDER.get(s.get("relevance") or "", 4), s["key"])
    )
    items: list[Item] = []
    for story in ordered:
        url = next(
            (i["url"] for i in story.get("items") or [] if str(i["url"]).startswith("https://")),
            None,
        )
        if url is None:
            continue
        text = story["summary"] + ("\nSENSITIVE" if story.get("sensitive") else "")
        if story.get("tags"):
            text += "\nTags: " + ", ".join(story["tags"])
        items.append(
            Item(
                source_type="world",
                source=WORLD_SOURCE,
                source_id="world-stories",
                source_url=None,
                url=url,
                discussion_url=None,
                publisher_site=None,
                title=story["title"],
                author=None,
                published_at=window_iso(now),
                text=text,
                engagement={},
                rank=len(items) + 1,
                fetched_at=window_iso(now),
            )
        )
    await put_items(conn, run_id=run_id, items=items)
    return day
