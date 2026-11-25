"""Daily intelligence in Postgres: items, stories, briefs, snapshots and retention."""

from __future__ import annotations

import gzip
import json
from datetime import timedelta

import pytest
from disposable_db import migrated_database
from intel_fixtures import DAY, NOW, fetch, item, project, run

from tin_lite import intel_snapshot, intel_store
from tin_lite.intel_fetch.items import canon, short_id


@pytest.fixture
async def db():
    async with migrated_database(max_size=5) as database:
        yield database


async def test_items_upsert_once_per_canonical_url_and_the_best_rank_wins(db):
    host = await project(db)
    run_id = await run(db, host, "intel.world")
    await fetch(db, run_id)
    first = [item("https://www.bbc.com/news/a", rank=4), item("https://bbc.com/news/b", rank=2)]
    assert await intel_store.put_items(db.pool, run_id=run_id, items=first) == 2
    # The same story again (www and a trailing slash differ), ranked higher elsewhere.
    again = [item("https://bbc.com/news/a/", source="Guardian", rank=1)]
    assert await intel_store.put_items(db.pool, run_id=run_id, items=again) == 0
    assert await intel_store.put_items(db.pool, run_id=run_id, items=first) == 0
    rows = await db.pool.fetch(
        "SELECT canon_url, source, rank FROM intel_items WHERE run_id = $1 ORDER BY canon_url",
        run_id,
    )
    assert [(r["canon_url"], r["source"], r["rank"]) for r in rows] == [
        ("bbc.com/news/a", "Guardian", 1),
        ("bbc.com/news/b", "BBC", 2),
    ]
    # Unusable URLs never reach the table.
    assert await intel_store.put_items(db.pool, run_id=run_id, items=[item("mailto:x")]) == 0


async def test_sources_are_recorded_once_each(db):
    host = await project(db)
    run_id = await run(db, host, "intel.world")
    await fetch(db, run_id)
    await intel_store.record_sources(
        db.pool, run_id=run_id, statuses=[{"id": "bbc", "status": "failed"}]
    )
    await intel_store.record_sources(
        db.pool,
        run_id=run_id,
        statuses=[{"id": "bbc", "status": "ok"}, {"id": "ft", "status": "empty"}],
    )
    assert await intel_store.done_sources(db.pool, run_id) == {"bbc", "ft"}
    stored = (await intel_store.get_fetch(db.pool, run_id))["source_status"]
    assert sorted((s["id"], s["status"]) for s in stored) == [("bbc", "ok"), ("ft", "empty")]


async def test_colliding_ids_get_longer_and_prior_ids_are_kept(db):
    host = await project(db)
    run_id = await run(db, host, "intel.world")
    await fetch(db, run_id)
    urls = [f"https://news.example/{n}" for n in range(40000)]
    # Find two URLs whose five-character ids collide.
    seen, pair = {}, None
    for url in urls:
        key = short_id(canon(url))
        if key in seen:
            pair = (seen[key], url)
            break
        seen[key] = url
    assert pair is not None
    await intel_store.put_items(
        db.pool,
        run_id=run_id,
        items=[item(pair[0], rank=2), item(pair[1], rank=1)],
    )
    await intel_store.assign_item_keys(db.pool, run_id)
    keys = {
        r["canon_url"]: r["item_key"]
        for r in await db.pool.fetch("SELECT * FROM intel_items WHERE run_id = $1", run_id)
    }
    # The better-ranked item keeps the five-character id.
    assert len(keys[canon(pair[1])]) == 5 and len(keys[canon(pair[0])]) == 6


async def story(db, run_id, key, *, project_id=None, day=DAY, relevance="high", **extra):
    async with db.pool.acquire() as conn:
        story_id, created = await intel_store.put_story(
            conn,
            project_id=project_id,
            key=key,
            day=day,
            run_id=run_id,
            title=extra.get("title", f"Title {key}"),
            summary=extra.get("summary", f"Summary {key}."),
            relevance=relevance,
            tags=extra.get("tags", ["ai"]),
            sensitive=extra.get("sensitive"),
            continues_id=None,
        )
    return story_id, created


async def test_story_keys_are_unique_per_scope(db):
    host = await project(db)
    client = await project(db, "Client")
    world_run = await run(db, host, "intel.world")
    client_run = await run(db, client, "intel.daily")
    first, created = await story(db, world_run, "chip-curbs")
    assert created
    again, created = await story(db, world_run, "chip-curbs", relevance=None, title=None)
    assert again == first and not created
    # A client's story may share a world story's key.
    theirs, created = await story(db, client_run, "chip-curbs", project_id=client)
    assert created and theirs != first
    kept = await intel_store.story(db.pool, project_id=None, key="chip-curbs")
    assert kept["relevance"] == "high" and kept["title"] == "Title chip-curbs"
    days = await db.pool.fetchval(
        "SELECT count(*) FROM intel_story_days WHERE story_id = $1", first
    )
    assert days == 1


async def test_items_attach_by_id_from_today_or_earlier_stories(db):
    host = await project(db)
    run_id = await run(db, host, "intel.world")
    await fetch(db, run_id)
    await intel_store.put_items(db.pool, run_id=run_id, items=[item("https://a.example/x")])
    await intel_store.assign_item_keys(db.pool, run_id)
    key = short_id(canon("https://a.example/x"))
    story_id, _ = await story(db, run_id, "first")
    async with db.pool.acquire() as conn:
        added, missing = await intel_store.attach_items(
            conn,
            story_id=story_id,
            day=DAY,
            run_id=run_id,
            fetch_run_id=run_id,
            project_id=None,
            keys=[key, "zzzzz", key],
        )
    assert (added, missing) == (1, ["zzzzz"])
    # Tomorrow's run reaches yesterday's item through the earlier story.
    later = await run(db, host, "intel.world")
    await fetch(db, later, day=DAY + timedelta(days=1))
    other, _ = await story(db, later, "second", day=DAY + timedelta(days=1))
    async with db.pool.acquire() as conn:
        added, missing = await intel_store.attach_items(
            conn,
            story_id=other,
            day=DAY + timedelta(days=1),
            run_id=later,
            fetch_run_id=later,
            project_id=None,
            keys=[key],
        )
    assert (added, missing) == (1, [])


async def test_a_fallback_brief_never_replaces_the_judges(db):
    host = await project(db)
    parent = await run(db, host, "intel.world")
    child = await run(db, host, "intel.world_judge")
    args = {"run_id": child, "parent_run_id": parent, "project_id": None, "day": DAY}
    await intel_store.put_brief(
        db.pool, **args, schema="intel-world-brief/1", brief={"a": 1}, source="judge"
    )
    await intel_store.put_brief(
        db.pool, **args, schema="intel-world-brief/1", brief={"b": 2}, source="fallback"
    )
    kept = await intel_store.get_brief(db.pool, parent_run_id=parent)
    assert kept["brief"] == {"a": 1} and kept["source"] == "judge"


async def test_latest_world_day_is_recent_and_usable(db):
    host = await project(db)
    run_id = await run(db, host, "intel.world")
    await intel_store.set_world_day(
        db.pool, day=DAY, run_id=run_id, status="failed", stories=0, items=9
    )
    assert await intel_store.latest_world_day(db.pool, now=NOW) is None
    await intel_store.set_world_day(
        db.pool, day=DAY, run_id=run_id, status="partial", stories=12, items=9
    )
    from datetime import UTC, datetime

    found = await intel_store.latest_world_day(db.pool, now=datetime.now(UTC))
    assert found["status"] == "partial" and found["day"] == DAY.isoformat()
    later = datetime.now(UTC) + timedelta(hours=31)
    assert await intel_store.latest_world_day(db.pool, now=later) is None


async def test_a_world_day_rewritten_after_the_client_started_still_counts(db):
    # Staging 2026-11-25: a same-day world re-run rewrote the day row (updated_at 09:52) after
    # the client run started (09:45), and the client's load then saw no world day at all.
    from datetime import UTC, datetime

    host = await project(db)
    run_id = await run(db, host, "intel.world")
    client_started = datetime.now(UTC) - timedelta(minutes=20)
    await intel_store.set_world_day(
        db.pool, day=DAY, run_id=run_id, status="ready", stories=113, items=1668
    )
    found = await intel_store.latest_world_day(db.pool, now=client_started)
    assert found["status"] == "ready" and found["day"] == DAY.isoformat()


async def test_prune_removes_only_old_items_and_snapshots(db):
    host = await project(db)
    old, new = await run(db, host, "intel.world"), await run(db, host, "intel.world")
    for run_id in (old, new):
        await fetch(db, run_id)
        await intel_store.put_items(db.pool, run_id=run_id, items=[item("https://a.example/1")])
        await intel_store.save_snapshot(db.pool, run_id=run_id, body=b"x")
    story_id, _ = await story(db, old, "kept")
    await db.pool.execute(
        "UPDATE intel_fetches SET created_at = now() - interval '31 days' WHERE run_id = $1", old
    )
    await db.pool.execute(
        "UPDATE intel_snapshots SET created_at = now() - interval '15 days' WHERE run_id = $1", old
    )
    from datetime import UTC, datetime

    await intel_store.prune(db.pool, now=datetime.now(UTC))
    counts = {
        r["run_id"]: r["n"]
        for r in await db.pool.fetch("SELECT run_id, count(*) n FROM intel_items GROUP BY 1")
    }
    assert counts == {new: 1}
    assert await db.pool.fetchval("SELECT count(*) FROM intel_snapshots") == 1
    assert await db.pool.fetchval("SELECT count(*) FROM intel_stories WHERE id = $1", story_id) == 1


async def test_snapshot_for_a_client_carries_world_stories_and_prior_stories(db):
    from datetime import UTC, datetime

    host = await project(db)
    client = await project(db, "Client")
    world_run = await run(db, host, "intel.world")
    await fetch(db, world_run)
    await intel_store.put_items(
        db.pool, run_id=world_run, items=[item("https://www.reuters.com/world/quake")]
    )
    await intel_store.assign_item_keys(db.pool, world_run)
    quake, _ = await story(db, world_run, "quake", tags=["disasters"], sensitive=True)
    async with db.pool.acquire() as conn:
        await intel_store.attach_items(
            conn,
            story_id=quake,
            day=DAY,
            run_id=world_run,
            fetch_run_id=world_run,
            project_id=None,
            keys=[short_id(canon("https://www.reuters.com/world/quake"))],
        )
    await intel_store.set_world_day(
        db.pool, day=DAY, run_id=world_run, status="ready", stories=1, items=1
    )
    # The client's own story from yesterday.
    yesterday = await run(db, client, "intel.daily")
    await story(db, yesterday, "fabric-mirroring", project_id=client, day=DAY - timedelta(days=1))
    run_id = await run(db, client, "intel.daily")
    await fetch(db, run_id, scope="client", project_id=client)
    await intel_store.put_items(
        db.pool, run_id=run_id, items=[item("https://news.example/fabric", source_type="rss")]
    )
    prepared = await intel_snapshot.prepare(db.pool, fetch_run_id=run_id, now=datetime.now(UTC))
    assert prepared["items"] == 2 and prepared["world_day"]["status"] == "ready"
    body = await intel_snapshot.build(db.pool, fetch_run_id=run_id, client_name="Acme")
    assert body == await intel_snapshot.build(db.pool, fetch_run_id=run_id, client_name="Acme")
    lines = [json.loads(line) for line in gzip.decompress(body).splitlines()]
    meta = lines[0]
    assert meta["kind"] == "meta" and meta["scope"] == "client"
    assert meta["client_name"] == "Acme" and meta["world_status"] == "ready"
    world = [x for x in lines if x["kind"] == "item" and x["source"] == "World stories"]
    assert len(world) == 1
    assert world[0]["url"] == "https://www.reuters.com/world/quake"
    assert "SENSITIVE" in world[0]["text"] and "disasters" in world[0]["text"]
    assert world[0]["item_key"] == short_id(canon("https://www.reuters.com/world/quake"))
    stories = [x for x in lines if x["kind"] == "story"]
    assert [s["sid"] for s in stories] == ["s-fabric-mirroring"]


async def test_snapshot_without_a_world_day_says_missing(db):
    host = await project(db)
    run_id = await run(db, host, "intel.daily")
    await fetch(db, run_id, scope="client", project_id=host)
    await intel_snapshot.prepare(db.pool, fetch_run_id=run_id)
    body = await intel_snapshot.build(db.pool, fetch_run_id=run_id)
    meta = json.loads(gzip.decompress(body).splitlines()[0])
    assert meta["world_status"] == "missing" and meta["world_day"] is None


async def world_story_day(db, url: str, *, key="quake", title="Earthquake hits the coast"):
    """A ready world day with one story whose strongest item is `url`."""
    host = await project(db)
    world_run = await run(db, host, "intel.world")
    await fetch(db, world_run)
    await intel_store.put_items(db.pool, run_id=world_run, items=[item(url)])
    await intel_store.assign_item_keys(db.pool, world_run)
    story_id, _ = await story(db, world_run, key, title=title, tags=["disasters"])
    async with db.pool.acquire() as conn:
        await intel_store.attach_items(
            conn,
            story_id=story_id,
            day=DAY,
            run_id=world_run,
            fetch_run_id=world_run,
            project_id=None,
            keys=[short_id(canon(url))],
        )
    await intel_store.set_world_day(
        db.pool, day=DAY, run_id=world_run, status="ready", stories=1, items=1
    )
    return world_run


async def test_a_clients_own_item_keeps_its_row_when_a_world_story_shares_its_url(db):
    """Ranks are positions inside one source: a world story ranked 1 must not replace the
    client's own copy of the same article, nor vanish because the client's ranked first."""
    from datetime import UTC, datetime

    await world_story_day(db, "https://www.reuters.com/world/quake")
    client = await project(db, "Client")
    run_id = await run(db, client, "intel.daily")
    await fetch(db, run_id, scope="client", project_id=client)
    own = item(
        "https://reuters.com/world/quake/?utm_source=feed",
        source="Reuters",
        source_type="rss",
        rank=7,
        text="The client feed's full article text.",
    )
    own["author"] = "A Reporter"
    await intel_store.put_items(db.pool, run_id=run_id, items=[own])
    for _ in range(2):  # a retried load changes nothing more
        await intel_snapshot.prepare(db.pool, fetch_run_id=run_id, now=datetime.now(UTC))
    rows = await db.pool.fetch(
        "SELECT source_type, source, author, text FROM intel_items WHERE run_id = $1", run_id
    )
    assert len(rows) == 1
    row = rows[0]
    assert (row["source_type"], row["source"], row["author"]) == ("rss", "Reuters", "A Reporter")
    assert row["text"] == (
        "The client feed's full article text.\n\nAlso a world story: Earthquake hits the coast"
    )


async def test_a_client_run_pins_the_world_day_its_load_used(db):
    """The snapshot, brief and sources.json read the fetch row's world day, so a world run that
    settles after the client's load can't make them disagree."""
    from datetime import UTC, datetime

    world_run = await world_story_day(db, "https://www.reuters.com/world/quake")
    client = await project(db, "Client")
    run_id = await run(db, client, "intel.daily")
    await fetch(db, run_id, scope="client", project_id=client)
    await intel_store.put_items(
        db.pool, run_id=run_id, items=[item("https://news.example/a", source_type="rss")]
    )
    prepared = await intel_snapshot.prepare(db.pool, fetch_run_id=run_id, now=datetime.now(UTC))
    assert prepared["world_day"]["run_id"] == str(world_run)
    # The world day moves on (a rerun failed, or finished later): the client's day doesn't.
    await db.pool.execute("UPDATE intel_world_days SET status = 'failed'")
    pinned = (await intel_store.get_fetch(db.pool, run_id))["world_day"]
    assert pinned["status"] == "ready" and pinned["run_id"] == str(world_run)
    meta = json.loads(
        gzip.decompress(await intel_snapshot.build(db.pool, fetch_run_id=run_id)).splitlines()[0]
    )
    assert meta["world_status"] == "ready" and meta["world_day"] == DAY.isoformat()
    again = await intel_snapshot.prepare(db.pool, fetch_run_id=run_id, now=datetime.now(UTC))
    assert again["world_day"] == pinned
    # With no usable day, the miss itself is pinned.
    other = await run(db, client, "intel.daily")
    await fetch(db, other, scope="client", project_id=client)
    missed = await intel_snapshot.prepare(db.pool, fetch_run_id=other, now=datetime.now(UTC))
    assert missed["world_day"] == {"status": "missing"}
    await db.pool.execute("UPDATE intel_world_days SET status = 'ready'")
    assert (await intel_store.get_fetch(db.pool, other))["world_day"] == {"status": "missing"}


async def test_unfinished_sources_are_recorded_failed_once(db):
    host = await project(db)
    run_id = await run(db, host, "intel.world")
    await fetch(db, run_id)
    await intel_store.record_sources(
        db.pool, run_id=run_id, statuses=[{"id": "bbc", "status": "ok"}]
    )
    sources = [
        {"id": "bbc", "name": "BBC", "method": "rss"},
        {"id": "ft", "name": "FT", "method": "rss"},
    ]
    recorded = await intel_store.record_unfinished(
        db.pool, run_id=run_id, sources=sources, reason="group_failed: its fetch did not finish"
    )
    assert recorded == 1
    assert (
        await intel_store.record_unfinished(db.pool, run_id=run_id, sources=sources, reason="again")
        == 0
    )
    stored = {s["id"]: s for s in (await intel_store.get_fetch(db.pool, run_id))["source_status"]}
    assert stored["bbc"]["status"] == "ok"
    assert stored["ft"]["status"] == "failed" and stored["ft"]["name"] == "FT"
    assert stored["ft"]["error"] == "group_failed: its fetch did not finish"


async def test_todays_stories_are_the_ones_touched_today(db):
    host = await project(db)
    run_id = await run(db, host, "intel.world")
    await story(db, run_id, "yesterday-only", day=DAY - timedelta(days=1))
    await story(db, run_id, "today", day=DAY)
    found = await intel_store.today_stories(db.pool, project_id=None, day=DAY)
    assert [s["key"] for s in found] == ["today"]


async def test_an_operator_pins_one_x_list_per_client(db):
    """The List a client's runs read through the host's X is the operator's pin, never the
    client's own input, and one List belongs to one client."""
    acme, other = await project(db, "Acme"), await project(db, "Other")
    assert await intel_store.client_setup(db.pool, acme) is None
    await intel_store.set_x_list(db.pool, project_id=acme, list_id="2125213001850495223")
    setup = await intel_store.client_setup(db.pool, acme)
    assert setup == {
        "slack_channel_id": None,
        "slack_channel_name": "",
        "x_list_id": "2125213001850495223",
    }
    with pytest.raises(intel_store.ListTaken, match="already pinned"):
        await intel_store.set_x_list(db.pool, project_id=other, list_id="2125213001850495223")
    with pytest.raises(ValueError, match="not an X List ID"):
        await intel_store.set_x_list(db.pool, project_id=other, list_id="lists/1")
    # Setting the channel keeps the pin; clearing the pin keeps the client set up.
    await db.pool.execute(
        "UPDATE intel_projects SET slack_channel_id = 'C0TEST' WHERE project_id = $1", acme
    )
    await intel_store.set_x_list(db.pool, project_id=acme, list_id=None)
    setup = await intel_store.client_setup(db.pool, acme)
    assert setup["x_list_id"] is None and setup["slack_channel_id"] == "C0TEST"
    await intel_store.set_x_list(db.pool, project_id=other, list_id="2125213001850495223")
