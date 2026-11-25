"""Activities for `intel.world`. Temporal history holds the run ID, group names and counts.

prepare  pin the day, window and source config on the host project, or refuse another project
fetch    one group's sources, each recorded as it finishes, so a retry skips the done ones
load     final item ids and the snapshot; no items fails here, before any spend
judge    start the judge child once; collect what it wrote and decide ready, partial or failed
unwrap   publisher URLs for the Google News links the Slack summary shows, cached
publish  STORIES.md in the host project's Files, then the world day clients read
post     the Slack summary, receipted, never failing the run
finish   retention and the run's result

The world day is written once the stories are published, before Slack: a post or projection
that fails later never hides a usable day from the clients (intel_world_failure keeps it).
The skeleton both intel workflows share is intel_steps.IntelActivities.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import intel_brief, intel_judge, intel_slack, intel_snapshot, intel_store
from tin_lite import intel_world as world
from tin_lite.failures import failure_projection, split_failure_input
from tin_lite.intel_fetch import config as source_config
from tin_lite.intel_fetch import gnews, group, run_sources
from tin_lite.intel_fetch import world as world_fetch
from tin_lite.intel_fetch.items import Window
from tin_lite.intel_steps import IntelActivities, beating, heartbeat
from tin_lite.product_urls import dashboard_url

logger = logging.getLogger(__name__)
KEY = world.KEY
STEPS = 7
FALLBACK = "Tin couldn't finish today's world stories. Tomorrow's run starts fresh."


class IntelWorldActivities(IntelActivities):
    KEY = KEY
    STEPS = STEPS
    INACTIVE = "Today's world run is no longer active."
    NOT_PREPARED = "Today's world fetch was never prepared."
    NOT_PUBLISHED = "Today's stories were never published."

    async def _config(self, run) -> dict:
        raw = await self.storage.read_canonical_artifact(
            repo_id="registry/workflows",
            commit_sha=run.definition_commit_sha,
            path=world.SOURCES_PATH,
        )
        return source_config.load(raw)

    @activity.defn(name="intel_world_prepare")
    async def prepare(self, run_id: str) -> dict:
        """Refuse any project but the host; pin the day, the window and the source config."""
        from tin_lite.run_service import WorkflowInputError

        run = await self._active(run_id)
        host = getattr(self.settings, "intel_host_project_id", None)
        if host is None or run.project_id != host:
            raise WorkflowInputError(
                "The world feed runs only on Tin's own intelligence project; client projects "
                "read its stories through intel.daily."
            )
        await self.db.mark_run_running(run.id)
        await self._progress(run, "prepare", 1, "Pinning today's world sources and window")
        config = await self._config(run)
        until = (
            (run.created_at or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
        )
        previous = await self.db.pool.fetchval(
            """SELECT window_end FROM intel_fetches
               WHERE scope = 'world' AND run_id <> $1 AND status IN ('loaded', 'judged', 'partial')
                 AND window_end < $2
               ORDER BY window_end DESC LIMIT 1""",
            run.id,
            until,
        )
        since = (
            previous
            if previous and until - previous <= timedelta(hours=world.CONTINUE_HOURS)
            else until - timedelta(hours=world.WINDOW_HOURS)
        )
        await intel_store.begin_fetch(
            self.db.pool,
            run_id=run.id,
            scope="world",
            project_id=None,
            day=until.date(),
            window_start=since,
            window_end=until,
            config_sha256=source_config.digest(config),
        )
        _ready, skipped = source_config.select(config, "world")
        await intel_store.record_sources(self.db.pool, run_id=run.id, statuses=skipped)
        return {"groups": list(world_fetch.GROUP_NAMES)}

    @activity.defn(name="intel_world_fetch")
    async def fetch(self, control: dict) -> dict:
        """One group's sources. Each source's items and status are recorded as it finishes."""
        run = await self._active(control["run_id"])
        name = control["group"]
        fetch = await self._fetch(run)
        config = await self._config(run)
        if source_config.digest(config) != fetch["config_sha256"]:
            raise ApplicationError("The world source config changed under this run.")
        ready, _skipped = source_config.select(config, "world")
        select, methods = group("world", name)
        done = await intel_store.done_sources(self.db.pool, run.id)
        sources = [s for s in select(ready) if s["id"] not in done]
        if not sources:
            return {"group": name, "sources": 0, "items": 0}
        await self._progress(run, "fetch", 2, "Pulling the last day from the world's sources")
        window = Window(fetch["window_start"], fetch["window_end"])
        added = 0

        async def recorded(src, items, status):
            nonlocal added
            new = await intel_store.put_items(self.db.pool, run_id=run.id, items=items)
            added += new
            await intel_store.record_sources(
                self.db.pool, run_id=run.id, statuses=[{**status, "new_items": new}]
            )

        try:
            async with beating({"group": name}), world_fetch.reader() as reader:
                await run_sources(
                    methods,
                    sources,
                    window,
                    reader,
                    settings=world_fetch.settings(config, datetime.now(UTC)),
                    on_done=recorded,
                )
        except Exception as exc:  # noqa: BLE001 — a group's failure is its sources' status
            await self._record_unfinished(
                run, sources, f"group_failed: {type(exc).__name__}: {exc}"
            )
        return {"group": name, "sources": len(sources), "items": added}

    @activity.defn(name="intel_world_load")
    async def load(self, run_id: str) -> dict:
        """Final ids and the snapshot. A day with no items fails here, before any spend."""
        run = await self._active(run_id)
        await self._progress(run, "load", 3, "Preparing the day's items for the judge")
        config = await self._config(run)
        ready, _ = source_config.select(config, "world")
        # A group that never finished (its activity gave up) leaves sources unrecorded.
        await self._record_unfinished(run, ready, "group_failed: its fetch did not finish")
        async with self.db.pool.acquire() as conn:
            prepared = await intel_snapshot.prepare(conn, fetch_run_id=run.id)
            if not prepared["items"]:
                await intel_store.set_fetch(conn, run.id, status="failed")
                raise ApplicationError(
                    "No items were fetched: every world source failed or was empty.",
                    non_retryable=True,
                )
            if await intel_store.get_snapshot(conn, run.id) is None:
                body = await intel_snapshot.build(conn, fetch_run_id=run.id)
                await intel_store.save_snapshot(conn, run_id=run.id, body=body)
            await intel_store.set_fetch(conn, run.id, status="loaded")
        return {"items": prepared["items"]}

    @activity.defn(name="intel_world_judge_start")
    async def judge_start(self, run_id: str) -> dict:
        run = await self._active(run_id)
        await self._progress(run, "judge", 4, "The world judge is writing today's stories")
        return await intel_judge.start_child(
            self, run, key=intel_judge.WORLD_JUDGE_KEY, scope="world"
        )

    async def _today(self, run) -> tuple[dict, list[dict]]:
        fetch = await self._fetch(run)
        found = await intel_store.today_stories(self.db.pool, project_id=None, day=fetch["day"])
        return fetch, found

    @activity.defn(name="intel_world_judge_collect")
    async def judge_collect(self, run_id: str) -> dict:
        """Ready when the judge finished; partial when it stopped with enough stories (the
        summary then comes from Tin's fallback); otherwise the run fails with its reason."""
        run = await self._active(run_id)
        found = await intel_judge.collect(self, run)
        fetch, stories = await self._today(run)
        status = "ready" if found["child_status"] == "succeeded" else "partial"
        if status == "partial" and len(stories) < world.PARTIAL_MIN_STORIES:
            await intel_store.set_fetch(self.db.pool, run.id, status="failed")
            reason = found["reason"] or f"it ended {found['child_status']}"
            raise ApplicationError(
                f"The world judge stopped with {len(stories)} stories: {reason}"[:500],
                non_retryable=True,
            )
        if not found["brief"] and found.get("child_run_id"):
            made = intel_brief.fallback_world_brief(stories)
            if made is not None:
                await intel_store.put_brief(
                    self.db.pool,
                    run_id=UUID(found["child_run_id"]),
                    parent_run_id=run.id,
                    project_id=None,
                    day=fetch["day"],
                    schema=intel_brief.WORLD_SCHEMA,
                    brief=made,
                    source="fallback",
                )
        await intel_store.set_fetch(
            self.db.pool, run.id, status="judged" if status == "ready" else "partial"
        )
        if status == "partial":
            await self._progress(
                run,
                "judge",
                4,
                f"The judge stopped early; posting its {len(stories)} stories as a partial day",
            )
        return {"stories": len(stories), "status": status}

    @activity.defn(name="intel_world_unwrap")
    async def unwrap(self, run_id: str) -> dict:
        """Publisher URLs for the Google News links the summary will show, cached for all runs."""
        run = await self._active(run_id)
        await self._progress(run, "unwrap", 5, "Finding the publishers behind Google News links")
        key = f"intel:{run.id}:links"
        saved = await self.db.get_effect(key)
        if saved and saved.status == "completed":
            return {"links": len(saved.result.get("links") or {})}
        _fetch, stories = await self._today(run)
        brief = await intel_store.get_brief(self.db.pool, parent_run_id=run.id)
        wanted = intel_brief.brief_links(
            intel_brief.WORLD_SCHEMA,
            brief["brief"] if brief else None,
            {s["key"]: s for s in stories},
        )
        async with world_fetch.reader() as reader:
            links = await gnews.resolve_many(
                self.db.pool, reader, wanted, heartbeat=lambda n: heartbeat({"links": n})
            )
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if not (receipt and receipt.status == "completed"):
                await self.db.start_effect(conn, execution_key=key, operation=KEY)
                await self.db.complete_effect(conn, execution_key=key, result={"links": links})
        return {"links": len(links)}

    @activity.defn(name="intel_world_publish")
    async def publish(self, run_id: str) -> dict:
        """intel/world/<day>/STORIES.md in the host project's Files, written by code; then the
        world day clients read, so nothing after this step can hide a usable day."""
        run = await self._active(run_id)
        await self._progress(run, "publish", 6, "Saving today's world stories in Files")
        fetch, stories = await self._today(run)
        day = fetch["day"].isoformat()
        path = world.stories_path(day)
        project = await self.db.get_project(run.project_id)

        async def documents() -> dict:
            sources = sum(1 for s in fetch["source_status"] if s.get("status") == "ok")
            high = sum(1 for s in stories if s.get("relevance") == "high")
            text = world.render_stories(day, stories, items=fetch["item_count"], sources=sources)
            return {
                "documents": {"STORIES.md": text},
                "summary": (
                    f"{len(stories)} world stories ({high} high) from "
                    f"{fetch['item_count']:,} items."
                ),
            }

        await self._publish(
            run,
            project,
            paths={"STORIES.md": path},
            limits={"STORIES.md": 2_000_000},
            artifact="STORIES.md",
            documents=documents,
        )
        await intel_store.set_world_day(
            self.db.pool,
            day=fetch["day"],
            run_id=run.id,
            status="partial" if fetch["status"] == "partial" else "ready",
            stories=len(stories),
            items=fetch["item_count"],
        )
        return {"stories": len(stories)}

    @activity.defn(name="intel_world_post")
    async def post(self, run_id: str) -> dict:
        """The Slack summary through Tin's own Slack app; a Slack outcome never fails the run."""
        run = await self._active(run_id)
        channel = getattr(self.settings, "intel_world_slack_channel", None)
        if not channel or self.integrations is None:
            await self._progress(run, "post", 7, "No world channel is set, so nothing is posted")
            return {"outcome": "not_configured"}
        await self._progress(run, "post", 7, "Posting today's world summary in Slack")
        fetch, stories = await self._today(run)
        brief = await intel_store.get_brief(self.db.pool, parent_run_id=run.id)
        if brief is None:
            return {"outcome": "no_brief"}
        links = await self.db.get_effect(f"intel:{run.id}:links")
        body = intel_slack.world_message(
            brief["brief"],
            {s["key"]: s for s in stories},
            day=fetch["day"],
            items=fetch["item_count"],
            sources=sum(1 for s in fetch["source_status"] if s.get("status") == "ok"),
            story_count=len(stories),
            high_count=sum(1 for s in stories if s.get("relevance") == "high"),
            stories_url=f"{dashboard_url(self.settings)}/document/{run.id}?project={run.project_id}",
            links=(links.result or {}).get("links") if links else None,
            fallback=brief["source"] == "fallback",
        )
        await intel_slack.recover(self.db)
        outcome = await intel_slack.send(
            self.db,
            self.integrations.slack,
            poster_project_id=run.project_id,
            project_id=None,
            key=f"world:{fetch['day'].isoformat()}",
            channel_id=channel,
            body=body,
        )
        intel_slack.retry_refused(outcome)
        return {"outcome": outcome}

    @activity.defn(name="intel_world_finish")
    async def finish(self, run_id: str) -> None:
        """Retention and the run's result; the world day was written when the stories were."""
        run = await self._active(run_id)
        fetch = await self._fetch(run)
        await intel_store.prune(self.db.pool, now=datetime.now(UTC))
        await self._finish_projection(
            run,
            summary_suffix=" The judge stopped early, so this is a partial day."
            if fetch["status"] == "partial"
            else "",
        )

    @activity.defn(name="intel_world_failure")
    async def failure(self, argument: str | dict) -> None:
        run_id, passed = split_failure_input(argument)
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != KEY:
            return
        fetch = await self._fail_fetch(run)
        if fetch is not None:
            current = await intel_store.world_day(self.db.pool, fetch["day"])
            # A failure never hides a usable day: an earlier run's, or this run's own once its
            # stories were published (a later Slack post or projection failed).
            if current is None or current["status"] == "failed":
                await intel_store.set_world_day(
                    self.db.pool,
                    day=fetch["day"],
                    run_id=run.id,
                    status="failed",
                    stories=0,
                    items=fetch["item_count"],
                )
        await self.db.project_failure(run_id=run.id, **failure_projection(passed, FALLBACK))


def activities_of(instance: IntelWorldActivities) -> list:
    return [
        instance.prepare,
        instance.fetch,
        instance.load,
        instance.judge_start,
        instance.judge_collect,
        instance.unwrap,
        instance.publish,
        instance.post,
        instance.finish,
        instance.failure,
    ]
