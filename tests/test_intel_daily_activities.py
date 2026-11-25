"""intel.daily's steps against Postgres and an in-memory code.storage history.

Synthetic client, sources and stories; no network (fixture readers, a seeded link cache).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from disposable_db import migrated_database
from intel_client_fakes import FIXTURES, rss_reader
from intel_fixtures import run
from temporalio.exceptions import ApplicationError
from test_linkedin_engagers import Storage

from tin_lite import intel_daily, intel_store
from tin_lite.intel_brief import CLIENT_SCHEMA, BriefError, LinkIndex, validate
from tin_lite.intel_daily_activities import IntelDailyActivities, local_day, window_for
from tin_lite.workflow_inputs import WorkflowInputError

CREATED = datetime(2026, 11, 24, 15, 0, 30, tzinfo=UTC)  # 09:00 in Chicago
GN = "https://news.google.com/rss/articles/CBMiAAAexampleArticleOne0001"
PUBLISHER = "https://techdaily.example/on-demand-billing"
COMPANY = b"# Acme Data\n\n## 7. Hard rules\n\nNever claim a percentage cost cut.\n"


@pytest.fixture
async def db():
    async with migrated_database(max_size=8) as database:
        yield database


class Slack:
    def __init__(self):
        self.posts = []

    async def post(self, project_id, *, channel_id, text, blocks):
        self.posts.append((project_id, channel_id, text, blocks))
        return "1764000000.000100"


async def setup(db, *, files=None, inputs=None, channel="C0TESTCHAN"):
    storage = Storage()
    storage.repo.id = f"projects/{uuid4().hex}"
    project = await db.create_project(name="Acme Data", state_repo_id=storage.repo.id)
    await db.pool.execute(
        "UPDATE projects SET timezone = 'America/Chicago' WHERE id = $1", project.id
    )
    defaults = {
        "company.md": COMPANY,
        intel_daily.RSS_PATH: (FIXTURES / "rss-sources.json").read_bytes(),
    }
    storage.repo.edit({**defaults, **(files or {})}, message="client files")
    run_id = await run(db, project.id, intel_daily.KEY, status="pending", inputs=inputs or {})
    await db.pool.execute("UPDATE workflow_runs SET created_at = $2 WHERE id = $1", run_id, CREATED)
    if channel:
        await db.pool.execute(
            "INSERT INTO intel_projects (project_id, slack_channel_id, slack_channel_name) "
            "VALUES ($1, $2, 'test-channel')",
            project.id,
            channel,
        )
    host = uuid4()
    slack = Slack()
    acts = IntelDailyActivities(
        database=db,
        storage=storage,
        settings=SimpleNamespace(
            app_url="https://app.example",
            switchboard_public_url="https://app.example",
            intel_host_project_id=host,
        ),
        integrations=SimpleNamespace(slack=slack, x=None),
        reader_factory=lambda agent: rss_reader(),
    )
    return SimpleNamespace(
        acts=acts, storage=storage, project=project, run_id=run_id, slack=slack, host=host
    )


# Days and windows.


def test_the_day_is_the_clients_local_date_across_dst():
    # 2026-11-01: Chicago leaves daylight time at 02:00 local.
    assert local_day(datetime(2026, 11, 1, 4, 30, tzinfo=UTC), "America/Chicago") == date(
        2026, 10, 31
    )
    assert local_day(datetime(2026, 11, 1, 6, 30, tzinfo=UTC), "America/Chicago") == date(
        2026, 11, 1
    )
    # 09:00 local is 14:00 UTC in summer and 15:00 in winter; both are the local day.
    assert local_day(datetime(2026, 3, 9, 14, 0, tzinfo=UTC), "America/Chicago") == date(2026, 3, 9)
    assert local_day(datetime(2026, 3, 7, 15, 0, tzinfo=UTC), "America/Chicago") == date(2026, 3, 7)
    assert local_day(datetime(2026, 3, 7, 3, 0, tzinfo=UTC), "Not/AZone") == date(2026, 3, 7)


def test_the_window_continues_a_recent_fetch_and_otherwise_looks_back():
    created = datetime(2026, 3, 9, 14, 0, 42, tzinfo=UTC)
    # Across the spring change the previous 09:00 run was 15:00 UTC, 23 hours earlier.
    previous = datetime(2026, 3, 8, 15, 0, tzinfo=UTC)
    window = window_for(created, lookback_hours=24, previous_end=previous)
    assert window.since == previous - timedelta(hours=1)
    assert window.until == datetime(2026, 3, 9, 14, 0, tzinfo=UTC)
    stale = window_for(created, lookback_hours=36, previous_end=previous - timedelta(days=5))
    assert stale.since == window.until - timedelta(hours=36)
    assert window_for(
        created, lookback_hours=24, previous_end=None
    ).since == window.until - timedelta(hours=24)


# Prepare.


async def test_prepare_pins_the_day_window_revision_and_destination(db):
    f = await setup(
        db,
        files={
            intel_daily.HN_REDDIT_PATH: json.dumps(
                json.loads((FIXTURES / "hn-reddit.json").read_text())["config"]
            ).encode()
        },
        inputs={"x_list_id": "2125213001850495223", "post_brief": True},
    )
    head = f.storage.repo.head
    result = await f.acts.prepare(str(f.run_id))
    assert result == {"groups": ["rss", "gnews", "pages", "hn", "reddit", "x"]}
    fetch = await intel_store.get_fetch(db.pool, f.run_id)
    assert fetch["scope"] == "client" and fetch["project_id"] == f.project.id
    assert fetch["day"] == date(2026, 11, 24)
    assert fetch["window_end"] == datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
    assert fetch["window_start"] == datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
    assert fetch["config_revision"] == head
    pinned = (await db.get_effect(f"intel:{f.run_id}:prepare")).result
    assert pinned["channel_id"] == "C0TESTCHAN" and pinned["post_brief"] is True
    assert pinned["company_path"] == "company.md"
    # A retry keeps the first pin even after the files move on.
    f.storage.repo.edit({intel_daily.RSS_PATH: b'{"sources": []}'})
    assert await f.acts.prepare(str(f.run_id)) == result
    assert (await intel_store.get_fetch(db.pool, f.run_id))["config_revision"] == head


async def test_an_invalid_source_is_skipped_with_its_reason(db):
    cfg = json.loads((FIXTURES / "rss-sources.json").read_text())
    cfg["sources"].append({"id": "bad", "name": "Bad", "method": "rss", "url": "ftp://x"})
    f = await setup(db, files={intel_daily.RSS_PATH: json.dumps(cfg).encode()})
    await f.acts.prepare(str(f.run_id))
    statuses = (await intel_store.get_fetch(db.pool, f.run_id))["source_status"]
    assert [(s["id"], s["status"]) for s in statuses] == [("bad", "skipped")]


async def test_company_md_missing_refuses_before_any_fetch(db):
    f = await setup(db)
    f.storage.repo.edit({"company.md": None})
    with pytest.raises(WorkflowInputError, match=r"company\.md"):
        await f.acts.prepare(str(f.run_id))
    assert await intel_store.get_fetch(db.pool, f.run_id) is None


async def test_company_md_under_context_is_accepted(db):
    f = await setup(db)
    f.storage.repo.edit({"company.md": None, "context/company.md": COMPANY})
    await f.acts.prepare(str(f.run_id))
    pinned = (await db.get_effect(f"intel:{f.run_id}:prepare")).result
    assert pinned["company_path"] == "context/company.md"


async def test_no_source_config_refuses_before_any_fetch(db):
    f = await setup(db)
    f.storage.repo.edit({intel_daily.RSS_PATH: None})
    with pytest.raises(WorkflowInputError, match="source config"):
        await f.acts.prepare(str(f.run_id))
    assert await intel_store.get_fetch(db.pool, f.run_id) is None


# Fetch and load.


async def test_fetch_reads_the_pinned_revision_and_skips_done_sources(db):
    f = await setup(db)
    await f.acts.prepare(str(f.run_id))
    # Later edits to the config don't change this run.
    f.storage.repo.edit({intel_daily.RSS_PATH: b'{"sources": []}'})
    result = await f.acts.fetch({"run_id": str(f.run_id), "group": "rss"})
    assert result["items"] == 3 and result["sources"] == 1
    rows = await db.pool.fetch("SELECT source_id FROM intel_items WHERE run_id = $1", f.run_id)
    assert {r["source_id"] for r in rows} == {"health-weekly"}
    statuses = (await intel_store.get_fetch(db.pool, f.run_id))["source_status"]
    assert [s["id"] for s in statuses] == ["health-weekly"]
    # A retried group skips what it recorded.
    assert await f.acts.fetch({"run_id": str(f.run_id), "group": "rss"}) == {
        "items": 0,
        "sources": 0,
    }


async def test_x_without_a_connection_is_skipped_and_the_day_goes_on(db):
    f = await setup(db, inputs={"x_list_id": "2125213001850495223"})

    class NoX:
        async def connection(self, project_id, capability=None):
            from tin_lite.integrations import IntegrationAuthorizationError

            raise IntegrationAuthorizationError("Connect or reconnect X in this project")

    f.acts.integrations.x = NoX()
    await f.acts.prepare(str(f.run_id))
    result = await f.acts.fetch({"run_id": str(f.run_id), "group": "x"})
    assert result["x"] == "x_not_connected"
    statuses = (await intel_store.get_fetch(db.pool, f.run_id))["source_status"]
    assert statuses[0]["id"] == "x-list" and statuses[0]["status"] == "skipped"


async def test_load_with_no_items_fails_before_the_judge(db):
    f = await setup(db)
    await f.acts.prepare(str(f.run_id))
    with pytest.raises(ApplicationError, match="No items were fetched"):
        await f.acts.load(str(f.run_id))
    assert (await intel_store.get_fetch(db.pool, f.run_id))["status"] == "failed"
    assert await intel_store.get_snapshot(db.pool, f.run_id) is None


async def test_load_builds_one_snapshot_and_notes_a_missing_world_day(db):
    f = await setup(db)
    await f.acts.prepare(str(f.run_id))
    for name in ("rss", "gnews"):
        await f.acts.fetch({"run_id": str(f.run_id), "group": name})
    loaded = await f.acts.load(str(f.run_id))
    assert loaded == {"items": 4, "world": "missing"}
    body, sha = await intel_store.get_snapshot(db.pool, f.run_id)
    assert await f.acts.load(str(f.run_id)) == loaded
    assert (await intel_store.get_snapshot(db.pool, f.run_id))[1] == sha
    assert (await intel_store.get_fetch(db.pool, f.run_id))["status"] == "loaded"


async def test_a_crashed_group_records_each_of_its_sources(db, monkeypatch):
    """No `group:<name>` pseudo-row: each source the group would have read is in the status,
    the snapshot meta and sources.json with the group's reason."""
    from tin_lite import intel_daily_activities as module

    f = await setup(db)
    await f.acts.prepare(str(f.run_id))

    async def broken(*args, **kwargs):
        raise RuntimeError("the reader fell over")

    monkeypatch.setattr(module, "run_sources", broken)
    result = await f.acts.fetch({"run_id": str(f.run_id), "group": "pages"})
    assert result == {"items": 0, "sources": 3, "failed": 3}
    statuses = {
        s["id"]: s for s in (await intel_store.get_fetch(db.pool, f.run_id))["source_status"]
    }
    assert set(statuses) == {"vendor-newsroom", "vendor-blog", "jx-crossref"}
    assert all(
        s["status"] == "failed" and s["error"].startswith("group_failed: RuntimeError")
        for s in statuses.values()
    )


async def test_load_records_the_sources_of_a_group_that_never_finished(db):
    f = await setup(db, inputs={"x_list_id": "2125213001850495223"})
    await f.acts.prepare(str(f.run_id))
    # rss ran; gnews, pages and x timed out or ran out of attempts.
    await f.acts.fetch({"run_id": str(f.run_id), "group": "rss"})
    await f.acts.load(str(f.run_id))
    statuses = {
        s["id"]: s for s in (await intel_store.get_fetch(db.pool, f.run_id))["source_status"]
    }
    unfinished = {
        k for k, s in statuses.items() if s["error"] == "group_failed: its fetch did not finish"
    }
    assert unfinished == {"gn-lakehouse", "vendor-newsroom", "vendor-blog", "jx-crossref", "x-list"}
    assert statuses["health-weekly"]["status"] == "ok"
    body, _sha = await intel_store.get_snapshot(db.pool, f.run_id)
    import gzip

    meta = json.loads(gzip.decompress(body).splitlines()[0])
    assert {s["id"] for s in meta["sources"]} == set(statuses)


async def test_the_brief_and_sources_json_use_the_world_day_the_load_pinned(db):
    """A world run that settles after the client's load can't make the brief disagree with
    the snapshot the analyst read."""
    f = await setup(db)
    world_run = await run(db, f.project.id, "intel.world")
    await intel_store.set_world_day(
        db.pool, day=date(2026, 11, 24), run_id=world_run, status="ready", stories=0, items=0
    )
    await db.pool.execute(
        "UPDATE intel_world_days SET updated_at = $1", CREATED - timedelta(hours=2)
    )
    await judged(db, f, brief=BRIEF)
    assert (await f.acts.load(str(f.run_id)))["world"] == "ready"
    # The world day is redone after the load and now fails.
    await intel_store.set_world_day(
        db.pool, day=date(2026, 11, 24), run_id=world_run, status="failed", stories=0, items=0
    )
    await f.acts.judge_collect(str(f.run_id))
    await f.acts.publish(str(f.run_id))
    tree = f.storage.repo.trees[f.storage.repo.head]
    paths = intel_daily.report_paths("2026-11-24")
    assert json.loads(tree[paths["sources.json"]][1])["world_day"]["status"] == "ready"
    assert "World stories unavailable" not in tree[paths["brief.md"]][1].decode()
    await f.acts.post(str(f.run_id))
    [(_poster, _channel, _text, blocks)] = f.slack.posts
    assert "World stories unavailable" not in json.dumps(blocks)


# The judge's results.


async def judged(db, f, *, status="succeeded", brief=None, report=None, error=None):
    """A finished judge child with its report in Files, as intel_judge saves it."""
    await f.acts.prepare(str(f.run_id))
    for name in ("rss", "gnews"):
        await f.acts.fetch({"run_id": str(f.run_id), "group": name})
    await f.acts.load(str(f.run_id))
    child = await run(db, f.project.id, intel_daily.JUDGE_KEY, status=status)
    path = f"intel/runs/{child}/report.md"
    revision = f.storage.repo.edit(
        {path: report or f"# Report\n\nSee [the news]({GN}).\n".encode()}
    )
    await db.pool.execute(
        """UPDATE workflow_runs SET canonical_commit_sha = $2, artifact_path = $3,
               error_message = $4, error_kind = $5 WHERE id = $1""",
        child,
        revision,
        path,
        error,
        "needs_you" if error else None,
    )
    key = f"intel:{f.run_id}:judge_child"
    async with db.effect_lock(key, intel_daily.KEY) as (conn, _):
        await db.start_effect(conn, execution_key=key, operation=intel_daily.KEY)
        await db.complete_effect(
            conn,
            execution_key=key,
            result={
                "run_id": str(child),
                "executor": "codex.procedure",
                "temporal_workflow_id": "t",
            },
        )
    item = await db.pool.fetchrow(
        "SELECT item_key, url FROM intel_items WHERE run_id = $1 AND url = $2",
        f.run_id,
        GN.replace("CBMiAAAexampleArticleOne0001", "CBMiAAAexampleArticleOne0001"),
    )
    sid, _ = await intel_store.put_story(
        db.pool,
        project_id=f.project.id,
        key="vendor-on-demand",
        day=date(2026, 11, 24),
        run_id=child,
        title="Vendor adds on-demand billing",
        summary="The vendor added on-demand billing.",
        relevance="high",
        tags=["competitor", "pricing"],
        sensitive=False,
        continues_id=None,
    )
    found, missing = await intel_store.attach_items(
        db.pool,
        story_id=sid,
        day=date(2026, 11, 24),
        run_id=child,
        fetch_run_id=f.run_id,
        project_id=f.project.id,
        keys=[item["item_key"], "zzzzz"],
    )
    assert (found, missing) == (1, ["zzzzz"])  # an unknown id is listed, the rest is kept
    if brief is not None:
        await intel_store.put_brief(
            db.pool,
            run_id=child,
            parent_run_id=f.run_id,
            project_id=f.project.id,
            day=date(2026, 11, 24),
            schema=CLIENT_SCHEMA,
            brief=brief,
            source="judge",
        )
    await db.pool.execute(
        "INSERT INTO intel_link_cache (gid, url) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        "CBMiAAAexampleArticleOne0001",
        PUBLISHER,
    )
    return child


async def test_no_brief_from_the_judge_gets_tins_fallback(db):
    f = await setup(db)
    child = await judged(db, f)
    result = await f.acts.judge_collect(str(f.run_id))
    assert result == {"stories": 1, "brief": "fallback"}
    saved = await intel_store.get_brief(db.pool, parent_run_id=f.run_id)
    assert saved["source"] == "fallback" and saved["run_id"] == str(child)
    assert saved["brief"]["items"][0]["story"] == "vendor-on-demand"
    assert saved["brief"]["items"][0]["kind"] == "competitor"


async def test_an_unusable_brief_keeps_what_is_usable(db):
    """The judge's plausible but unusable output: a link outside the day is dropped, a brief
    with two items is refused (the judge is told to fix it), and with no brief put Tin's
    fallback carries the day."""
    f = await setup(db)
    await judged(db, f)
    urls = await db.pool.fetch("SELECT url FROM intel_items WHERE run_id = $1", f.run_id)
    links = LinkIndex(r["url"] for r in urls)
    item = {
        "lead": "Vendor adds on-demand billing",
        "body": "Per [a made-up page](https://invented.example/x), and [the news](" + GN + ").",
        "kind": "competitor",
        "story": "vendor-on-demand",
    }
    kept, warnings = validate(
        CLIENT_SCHEMA, {"headline": "A day", "items": [item, item, item]}, links=links
    )
    assert "invented.example" not in json.dumps(kept) and GN in json.dumps(kept)
    assert any("isn't an item from today's data" in w for w in warnings)
    with pytest.raises(BriefError):
        validate(CLIENT_SCHEMA, {"headline": "A day", "items": [item, item]}, links=links)
    assert (await f.acts.judge_collect(str(f.run_id)))["brief"] == "fallback"


async def test_a_failed_judge_fails_the_run_with_its_own_reason(db):
    f = await setup(db)
    await judged(db, f, status="failed", error="Your Tin credits ran out. Add credits in Billing.")
    with pytest.raises(ApplicationError) as raised:
        await f.acts.judge_collect(str(f.run_id))
    assert "credits ran out" in raised.value.message
    carried = raised.value.details[0]["tin_failure"]
    assert carried["kind"] == "needs_you"
    assert (await intel_store.get_fetch(db.pool, f.run_id))["status"] == "failed"
    # The judge's stories stay.
    assert await intel_store.story(db.pool, project_id=f.project.id, key="vendor-on-demand")


# Publish, post and finish.


BRIEF = {
    "schema": CLIENT_SCHEMA,
    "headline": "A vendor made its warehouse cheaper to start",
    "items": [
        {
            "lead": "On-demand billing",
            "body": f"Per [Tech Daily]({GN}).",
            "kind": "competitor",
            "verify": False,
            "story": "vendor-on-demand",
        }
    ]
    * 3,
    "worth_doing": [],
}


async def test_publish_unwraps_links_and_is_idempotent(db):
    f = await setup(db)
    await judged(db, f, brief=BRIEF)
    await f.acts.judge_collect(str(f.run_id))
    first = await f.acts.publish(str(f.run_id))
    head = f.storage.repo.head
    assert first == {"links": 1}
    assert await f.acts.publish(str(f.run_id)) == first
    assert f.storage.repo.head == head  # one commit
    paths = intel_daily.report_paths("2026-11-24")
    tree = f.storage.repo.trees[head]
    report = tree[paths["report.md"]][1].decode()
    brief = tree[paths["brief.md"]][1].decode()
    assert PUBLISHER in report and GN not in report
    assert f"[Tech Daily]({PUBLISHER})" in brief and GN not in brief
    status = json.loads(tree[paths["sources.json"]][1])
    assert status["day"] == "2026-11-24" and status["items"] == 4
    by_id = {s["id"]: s for s in status["sources"]}
    assert by_id["health-weekly"]["status"] == "ok" and by_id["gn-lakehouse"]["status"] == "ok"
    # The pages group never ran in this test: load recorded its sources as unfinished.
    pages = {"vendor-newsroom", "vendor-blog", "jx-crossref"}
    assert set(by_id) == {"health-weekly", "gn-lakehouse"} | pages
    assert all(by_id[p]["error"] == "group_failed: its fetch did not finish" for p in pages)
    assert status["world_day"] == {"status": "missing"}


async def test_post_sends_once_through_the_host_project_and_finish_succeeds(db):
    f = await setup(db)
    await judged(db, f, brief=BRIEF)
    await f.acts.judge_collect(str(f.run_id))
    await f.acts.publish(str(f.run_id))
    assert await f.acts.post(str(f.run_id)) == {"outcome": "sent"}
    assert await f.acts.post(str(f.run_id)) == {"outcome": "already"}
    [(poster, channel, text, blocks)] = f.slack.posts
    assert poster == f.host and channel == "C0TESTCHAN"
    assert text.startswith("Acme Data market brief, Tue 24 Nov")
    rendered = json.dumps(blocks)
    assert PUBLISHER in rendered and GN not in rendered
    assert "World stories unavailable today" in rendered
    await f.acts.finish(str(f.run_id))
    done = await db.get_run(f.run_id)
    assert done.status.value == "succeeded"
    assert done.artifact_path == "intel/reports/2026-11-24/report.md"
    assert done.result_summary == BRIEF["headline"]


async def test_post_off_or_without_a_channel_never_posts(db):
    f = await setup(db, inputs={"post_brief": False})
    await judged(db, f, brief=BRIEF)
    assert await f.acts.post(str(f.run_id)) == {"outcome": "off"}
    g = await setup(db, channel=None)
    await judged(db, g, brief=BRIEF)
    assert await g.acts.post(str(g.run_id)) == {"outcome": "no_destination"}
    assert f.slack.posts == [] and g.slack.posts == []


async def test_failure_marks_the_run_and_the_fetch(db):
    f = await setup(db)
    await f.acts.prepare(str(f.run_id))
    await f.acts.failure({"run_id": str(f.run_id), "kind": "explained", "reason": "Stopped."})
    failed = await db.get_run(f.run_id)
    assert failed.status.value == "failed" and failed.error_message == "Stopped."
    assert (await intel_store.get_fetch(db.pool, f.run_id))["status"] == "failed"
