"""Activities for `intel.world`. Temporal history holds the run ID, group names and counts.

prepare  pin the day, window and source config on the host project, or refuse another project
fetch    one group's sources, each recorded as it finishes, so a retry skips the done ones
load     final item ids and the snapshot; no items fails here, before any spend
judge    start the judge child once; collect what it wrote and decide ready, partial or failed
unwrap   publisher URLs for the Google News links the Slack summary shows, cached
publish  STORIES.md in the host project's Files
post     the Slack summary, receipted, never failing the run
finish   the world day, retention, the run's result
"""

from __future__ import annotations

import asyncio
import contextlib
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
from tin_lite.organic_audit_publication import publish_artifacts
from tin_lite.product_urls import dashboard_url

logger = logging.getLogger(__name__)
KEY = world.KEY
STEPS = 8
FALLBACK = "Tin couldn't finish today's world stories. Tomorrow's run starts fresh."
HEARTBEAT_SECONDS = 20
UNWRAP_LIMIT = 60


def _heartbeat(detail) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


class IntelWorldActivities:
    def __init__(self, *, database, storage, settings, integrations=None):
        self.db, self.storage, self.settings = database, storage, settings
        self.integrations = integrations

    async def _active(self, run_id):
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != KEY or run.status.value not in {"pending", "running"}:
            raise ApplicationError("Today's world run is no longer active.", non_retryable=True)
        return run

    async def _progress(self, run, step: str, current: int, summary: str) -> None:
        await self.db.project_run_progress(
            run_id=run.id, mode="steps", step=step, current=current, total=STEPS, summary=summary
        )
        _heartbeat({"step": step})

    async def _config(self, run) -> dict:
        raw = await self.storage.read_canonical_artifact(
            repo_id="registry/workflows",
            commit_sha=run.definition_commit_sha,
            path=world.SOURCES_PATH,
        )
        return source_config.load(raw)

    async def _fetch_row(self, run) -> dict:
        fetch = await intel_store.get_fetch(self.db.pool, run.id)
        if fetch is None:
            raise ApplicationError("Today's world fetch was never prepared.", non_retryable=True)
        return fetch

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
        fetch = await self._fetch_row(run)
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

        async def beat():
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                _heartbeat({"group": name, "items": added})

        beating = asyncio.create_task(beat())
        try:
            async with world_fetch.reader() as reader:
                await run_sources(
                    methods,
                    sources,
                    window,
                    reader,
                    settings=world_fetch.settings(config, datetime.now(UTC)),
                    on_done=recorded,
                )
        except Exception as exc:  # noqa: BLE001 — a group's failure is its sources' status
            done = await intel_store.done_sources(self.db.pool, run.id)
            await intel_store.record_sources(
                self.db.pool,
                run_id=run.id,
                statuses=[
                    {
                        "id": s["id"],
                        "name": s.get("name"),
                        "method": s.get("method"),
                        "status": "failed",
                        "error": f"group_failed: {type(exc).__name__}: {exc}"[:300],
                        "items_in_window": 0,
                    }
                    for s in sources
                    if s["id"] not in done
                ],
            )
        finally:
            beating.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beating
        return {"group": name, "sources": len(sources), "items": added}

    @activity.defn(name="intel_world_load")
    async def load(self, run_id: str) -> dict:
        """Final ids and the snapshot. A day with no items fails here, before any spend."""
        run = await self._active(run_id)
        await self._progress(run, "load", 3, "Preparing the day's items for the judge")
        config = await self._config(run)
        ready, _ = source_config.select(config, "world")
        done = await intel_store.done_sources(self.db.pool, run.id)
        missing = [s for s in ready if s["id"] not in done]
        if missing:
            # A group that never finished (its activity gave up) leaves sources unrecorded.
            await intel_store.record_sources(
                self.db.pool,
                run_id=run.id,
                statuses=[
                    {
                        "id": s["id"],
                        "name": s.get("name"),
                        "method": s.get("method"),
                        "status": "failed",
                        "error": "group_failed: its fetch did not finish",
                        "items_in_window": 0,
                    }
                    for s in missing
                ],
            )
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
        fetch = await self._fetch_row(run)
        found = await intel_store.stories(
            self.db.pool,
            project_id=None,
            since_day=fetch["day"],
            until_day=fetch["day"],
            limit=2000,
        )
        keys = {
            row["key"]
            for row in await self.db.pool.fetch(
                """SELECT s.key FROM intel_story_days d JOIN intel_stories s ON s.id = d.story_id
                   WHERE s.project_id IS NULL AND d.day = $1""",
                fetch["day"],
            )
        }
        return fetch, [s for s in found if s["key"] in keys]

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
        key = f"intel:{run.id}:links"
        saved = await self.db.get_effect(key)
        if saved and saved.status == "completed":
            return {"links": len(saved.result.get("links") or {})}
        _fetch, stories = await self._today(run)
        brief = await intel_store.get_brief(self.db.pool, parent_run_id=run.id)
        wanted: list[str] = []
        if brief:
            by_key = {s["key"]: s for s in stories}
            for section in brief["brief"].get("groups") or []:
                for entry in section.get("items") or []:
                    wanted += [m.group(2) for m in intel_brief.LINK.finditer(entry.get("line", ""))]
                    url = intel_slack.default_link(by_key.get(entry.get("story"), {}))
                    if url:
                        wanted.append(url)
        wanted = [
            u for u in dict.fromkeys(wanted) if gnews.article_id(u) and "news.google.com" in u
        ]
        links: dict[str, str] = {}
        async with world_fetch.reader() as reader, self.db.pool.acquire() as conn:
            for url in wanted[:UNWRAP_LIMIT]:
                try:
                    found = await gnews.resolve(conn, reader, url)
                except Exception:  # noqa: BLE001 — a link that won't unwrap keeps working
                    found = None
                if found:
                    links[url] = found
                _heartbeat({"links": len(links)})
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if not (receipt and receipt.status == "completed"):
                await self.db.start_effect(conn, execution_key=key, operation=KEY)
                await self.db.complete_effect(conn, execution_key=key, result={"links": links})
        return {"links": len(links)}

    @activity.defn(name="intel_world_publish")
    async def publish(self, run_id: str) -> dict:
        """intel/world/<day>/STORIES.md in the host project's Files, written by code."""
        run = await self._active(run_id)
        await self._progress(run, "publish", 6, "Saving today's world stories in Files")
        fetch, stories = await self._today(run)
        day = fetch["day"].isoformat()
        path = world.stories_path(day)
        sources = sum(1 for s in fetch["source_status"] if s.get("status") == "ok")
        project = await self.db.get_project(run.project_id)
        key = f"intel:{run.id}:publish"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return {"stories": len(stories)}
            if receipt is None:
                await self.db.start_effect(conn, execution_key=key, operation=KEY)
            saved = (receipt.result or {}) if receipt else {}
            text = saved.get("text")
            if not isinstance(text, str):
                text = world.render_stories(
                    day, stories, items=fetch["item_count"], sources=sources
                )
                await self.db.save_effect_progress(
                    conn, execution_key=key, result={**saved, "text": text}
                )

            async def save_intent(value):
                await self.db.save_publication_intent(conn, execution_key=key, intent=value)

            async def validate_active():
                await self._active(run.id)

            async with self.db.project_state_lock(conn, project.id):
                revision = await publish_artifacts(
                    storage=self.storage,
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    documents={path: text.encode()},
                    paths={"STORIES.md": path},
                    limits={"STORIES.md": 2_000_000},
                    message=f"{KEY} {run.id} [{key}]",
                    intent=saved.get("publication"),
                    save_intent=save_intent,
                    validate_active=validate_active,
                    # A second run on the same day replaces the day's file.
                    replaceable=frozenset({path}),
                )
            high = sum(1 for s in stories if s.get("relevance") == "high")
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={
                    "canonical_commit_sha": revision,
                    "artifact_path": path,
                    "summary": (
                        f"{len(stories)} world stories ({high} high) from "
                        f"{fetch['item_count']:,} items."
                    ),
                },
            )
        return {"stories": len(stories)}

    @activity.defn(name="intel_world_post")
    async def post(self, run_id: str) -> dict:
        """The Slack summary through Tin's own Slack app; a Slack outcome never fails the run."""
        run = await self._active(run_id)
        channel = getattr(self.settings, "intel_world_slack_channel", None)
        if not channel or self.integrations is None:
            await self._progress(run, "post", 7, "No world channel is set, so nothing is posted")
            return {"posted": "not_configured"}
        await self._progress(run, "post", 7, "Posting today's world summary in Slack")
        fetch, stories = await self._today(run)
        brief = await intel_store.get_brief(self.db.pool, parent_run_id=run.id)
        if brief is None:
            return {"posted": "no_brief"}
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
        return {"posted": outcome}

    @activity.defn(name="intel_world_finish")
    async def finish(self, run_id: str) -> None:
        """The world day clients read, retention, and the run's result."""
        run = await self._active(run_id)
        fetch, stories = await self._today(run)
        status = "partial" if fetch["status"] == "partial" else "ready"
        async with self.db.pool.acquire() as conn:
            await intel_store.set_world_day(
                conn,
                day=fetch["day"],
                run_id=run.id,
                status=status,
                stories=len(stories),
                items=fetch["item_count"],
            )
            await intel_store.prune(conn, now=datetime.now(UTC))
        published = await self.db.get_effect(f"intel:{run.id}:publish")
        if not published or published.status != "completed":
            raise ApplicationError("Today's stories were never published.", non_retryable=True)
        result = published.result
        project = await self.db.get_project(run.project_id)
        projection = f"intel:{run.id}:projection"
        async with self.db.effect_lock(projection, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=projection, operation=KEY)
            sha = result["canonical_commit_sha"]
            await self.db._complete_readonly_report_projection(
                conn,
                execution_key=projection,
                run_id=run.id,
                canonical_commit_sha=sha,
                artifact_path=result["artifact_path"],
                artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{result['artifact_path']}",
                summary=result["summary"]
                + (
                    " The judge stopped early, so this is a partial day."
                    if status == "partial"
                    else ""
                ),
                workflow_key=KEY,
            )

    @activity.defn(name="intel_world_failure")
    async def failure(self, argument: str | dict) -> None:
        run_id, passed = split_failure_input(argument)
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != KEY:
            return
        fetch = await intel_store.get_fetch(self.db.pool, run.id)
        if fetch is not None:
            await intel_store.set_fetch(self.db.pool, run.id, status="failed")
            current = await intel_store.world_day(self.db.pool, fetch["day"])
            # A failed rerun never hides a day that is already usable.
            if current is None or current["status"] == "failed" or current["run_id"] == str(run.id):
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
