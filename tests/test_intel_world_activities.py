"""intel.world's activities against Postgres: the host refusal, windows, resumable fetches,
the no-items stop, judge outcomes and the fallback brief, posting once, the world day."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from disposable_db import migrated_database
from intel_fixtures import project, run
from temporalio.exceptions import ApplicationError
from test_intel_fetch_world import FakeReader

from tin_lite import intel_steps, intel_store
from tin_lite import intel_world_activities as module
from tin_lite.intel_world_activities import IntelWorldActivities
from tin_lite.run_service import WorkflowInputError

CONFIG = (Path(__file__).parent / "fixtures" / "intel_world" / "sources.json").read_bytes()


class Storage:
    async def read_canonical_artifact(self, *, repo_id, commit_sha, path):
        assert path == "workflows/intel.world/sources.json"
        return CONFIG


class Slack:
    def __init__(self):
        self.posts = []

    async def post(self, project_id, *, channel_id, text, blocks):
        self.posts.append((project_id, channel_id, text, blocks))
        return "1764000000.000100"


@pytest.fixture
async def db():
    async with migrated_database(max_size=5) as database:
        yield database


@pytest.fixture
def reader(monkeypatch):
    fake = FakeReader()

    class Session:
        async def __aenter__(self):
            return fake

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(module.world_fetch, "reader", lambda **kwargs: Session())
    return fake


async def world_run(db, host, created_at=None):
    run_id = await run(db, host, "intel.world", status="pending")
    await db.pool.execute(
        "UPDATE workflow_runs SET created_at = $2 WHERE id = $1",
        run_id,
        created_at or datetime(2026, 11, 24, 20, 0, tzinfo=UTC),
    )
    return run_id


def acts(db, host, slack=None, channel="C0B5W5325JP"):
    settings = SimpleNamespace(
        intel_host_project_id=host,
        intel_world_slack_channel=channel,
        app_url="https://app.tin.test",
        switchboard_public_url="https://tin.test",
    )
    return IntelWorldActivities(
        database=db,
        storage=Storage(),
        settings=settings,
        integrations=SimpleNamespace(slack=slack or Slack()),
    )


async def test_any_project_but_the_host_is_refused(db):
    host = await project(db)
    other = await project(db, "Client")
    run_id = await world_run(db, other)
    with pytest.raises(WorkflowInputError, match="Tin's own intelligence project"):
        await acts(db, host).prepare(str(run_id))
    with pytest.raises(WorkflowInputError):
        await acts(db, None).prepare(str(await world_run(db, host)))


async def test_the_window_continues_from_the_last_day_and_fetches_resume(db, reader):
    host = await project(db)
    activities = acts(db, host)
    first = await world_run(db, host)
    assert (await activities.prepare(str(first)))["groups"] == ["gnews", "feeds", "special"]
    fetch = await intel_store.get_fetch(db.pool, first)
    assert fetch["window_end"] - fetch["window_start"] == timedelta(hours=24)
    for group in ("gnews", "feeds", "special"):
        await activities.fetch({"run_id": str(first), "group": group})
    statuses = {s["id"]: s for s in (await intel_store.get_fetch(db.pool, first))["source_status"]}
    assert len(statuses) == 7 and statuses["bbc_world"]["status"] == "ok"
    # A retried group skips what it already recorded.
    asked = len(reader.asked)
    again = await activities.fetch({"run_id": str(first), "group": "feeds"})
    assert again["sources"] == 0 and len(reader.asked) == asked
    loaded = await activities.load(str(first))
    assert loaded["items"] > 10
    assert await intel_store.get_snapshot(db.pool, first) is not None
    # The next day starts where this one ended.
    second = await world_run(db, host, datetime(2026, 11, 25, 18, 0, tzinfo=UTC))
    await activities.prepare(str(second))
    nxt = await intel_store.get_fetch(db.pool, second)
    assert nxt["window_start"] == fetch["window_end"]


async def test_a_day_with_no_items_stops_before_the_judge(db, reader):
    host = await project(db)
    activities = acts(db, host)
    run_id = await world_run(db, host, datetime(2020, 1, 1, tzinfo=UTC))
    await activities.prepare(str(run_id))
    for group in ("gnews", "feeds", "special"):
        await activities.fetch({"run_id": str(run_id), "group": group})
    with pytest.raises(ApplicationError, match="No items were fetched"):
        await activities.load(str(run_id))
    assert (await intel_store.get_fetch(db.pool, run_id))["status"] == "failed"


async def judged(db, host, *, child_status, stories):
    """A loaded world day and its judge's run, which wrote `stories` stories and no brief."""
    activities = acts(db, host)
    run_id = await world_run(db, host)
    await activities.prepare(str(run_id))
    fetch = await intel_store.get_fetch(db.pool, run_id)
    child = await run(
        db,
        host,
        "intel.world_judge",
        status=child_status,
        inputs={"parent_run_id": str(run_id), "scope": "world", "day": fetch["day"].isoformat()},
    )
    key = f"intel:{run_id}:judge_child"
    async with db.effect_lock(key, "intel.world") as (conn, _):
        await db.start_effect(conn, execution_key=key, operation="intel.world")
        await db.complete_effect(
            conn,
            execution_key=key,
            result={
                "run_id": str(child),
                "executor": "codex.procedure",
                "temporal_workflow_id": "x",
            },
        )
    async with db.pool.acquire() as conn:
        for n in range(stories):
            story_id, _ = await intel_store.put_story(
                conn,
                project_id=None,
                key=f"story-{n:02d}",
                day=fetch["day"],
                run_id=child,
                title=f"Story {n}",
                summary=f"What happened in story {n}.",
                relevance="high" if n < 6 else "medium",
                tags=["conflict"] if n == 0 else ["ai"],
                sensitive=n == 0,
                continues_id=None,
            )
            await conn.execute(
                """INSERT INTO intel_story_items (story_id, day, run_id, item_key, url, title,
                       source) VALUES ($1, $2, $3, 'abcde', $4, 'T', 'Reuters')""",
                story_id,
                fetch["day"],
                child,
                f"https://www.reuters.com/{n}",
            )
    return activities, run_id, UUID(str(child))


async def test_a_judge_that_stopped_with_few_stories_fails_the_day(db):
    host = await project(db)
    activities, run_id, _child = await judged(db, host, child_status="failed", stories=3)
    with pytest.raises(ApplicationError, match="stopped with 3 stories"):
        await activities.judge_collect(str(run_id))
    await activities.failure({"run_id": str(run_id), "reason": "x", "kind": "explained"})
    day = await intel_store.latest_world_status(db.pool)
    assert day["status"] == "failed"


async def test_a_partial_day_posts_tins_own_brief_once_and_finishes(db, monkeypatch):
    host = await project(db)
    slack = Slack()
    activities, run_id, child = await judged(db, host, child_status="failed", stories=12)
    activities.integrations.slack = slack
    assert await activities.judge_collect(str(run_id)) == {"stories": 12, "status": "partial"}
    brief = await intel_store.get_brief(db.pool, parent_run_id=run_id)
    assert brief["source"] == "fallback" and brief["run_id"] == str(child)
    assert await activities.post(str(run_id)) == {"outcome": "sent"}
    assert await activities.post(str(run_id)) == {"outcome": "already"}
    assert len(slack.posts) == 1
    _poster, channel, text, blocks = slack.posts[0]
    assert channel == "C0B5W5325JP" and text.startswith("The world today, Tue 24 Nov: ")
    assert "⚠️" in json.dumps(blocks, ensure_ascii=False)

    async def published(**kwargs):
        assert set(kwargs["documents"]) == {"intel/world/2026-11-24/STORIES.md"}
        assert "Story 0" in kwargs["documents"]["intel/world/2026-11-24/STORIES.md"].decode()
        return "c" * 40

    monkeypatch.setattr(intel_steps, "publish_artifacts", published)
    await db.pool.execute("UPDATE projects SET canonical_branch = 'main' WHERE id = $1", host)
    await activities.publish(str(run_id))
    await activities.finish(str(run_id))
    day = await intel_store.latest_world_status(db.pool)
    assert (day["status"], day["stories"]) == ("partial", 12)
    finished = await db.get_run(run_id)
    assert finished.status.value == "succeeded"
    assert finished.artifact_path == "intel/world/2026-11-24/STORIES.md"


async def test_no_channel_means_nothing_posts(db):
    host = await project(db)
    activities, run_id, _child = await judged(db, host, child_status="succeeded", stories=6)
    activities.settings.intel_world_slack_channel = None
    assert await activities.post(str(run_id)) == {"outcome": "not_configured"}


async def test_a_failed_slack_post_never_hides_a_published_day(db, monkeypatch):
    """The world day is written with the stories, before Slack: a post that raises something
    other than a refusal fails nothing the clients read."""
    host = await project(db)
    activities, run_id, _child = await judged(db, host, child_status="succeeded", stories=6)
    await activities.judge_collect(str(run_id))

    async def published(**kwargs):
        return "c" * 40

    monkeypatch.setattr(intel_steps, "publish_artifacts", published)
    await db.pool.execute("UPDATE projects SET canonical_branch = 'main' WHERE id = $1", host)
    await activities.publish(str(run_id))
    day = await intel_store.latest_world_status(db.pool)
    assert (day["status"], day["stories"], day["run_id"]) == ("ready", 6, str(run_id))

    class Broken:
        async def post(self, *args, **kwargs):
            raise RuntimeError("the renderer broke")

    activities.integrations.slack = Broken()
    assert await activities.post(str(run_id)) == {"outcome": "unknown"}
    # Even a run that fails after publishing keeps its day usable.
    await activities.failure({"run_id": str(run_id), "reason": "x", "kind": "explained"})
    day = await intel_store.latest_world_status(db.pool)
    assert day["status"] == "ready" and day["run_id"] == str(run_id)
    assert (await intel_store.get_fetch(db.pool, run_id))["status"] == "failed"


async def test_a_group_that_raised_records_each_unfinished_source(db, reader, monkeypatch):
    host = await project(db)
    activities = acts(db, host)
    run_id = await world_run(db, host)
    await activities.prepare(str(run_id))

    async def broken(*args, **kwargs):
        raise RuntimeError("the reader fell over")

    monkeypatch.setattr(module, "run_sources", broken)
    await activities.fetch({"run_id": str(run_id), "group": "special"})
    statuses = {s["id"]: s for s in (await intel_store.get_fetch(db.pool, run_id))["source_status"]}
    special = [s for s in statuses.values() if s["method"] in {"techmeme_river", "gdelt_doc"}]
    assert len(special) == 2
    assert all(s["status"] == "failed" for s in special)
    assert all(s["error"].startswith("group_failed: RuntimeError") for s in special)
