"""Activities for `intel.daily`: a client's day, fetched, judged, published and posted.

Temporal history holds the run ID, group names, counts and the judge's identifiers. Each step
is safe to repeat:

- prepare pins the day, the window, the source configs' project revision and the Slack
  destination once (`intel:<run>:prepare`), and refuses before any spend when company.md or
  both source configs are missing;
- a fetch group skips the sources it already recorded, and X pages have their own receipts
  (intel_fetch.x_list);
- load records any source a fetch group never finished, pins the world day it adds (on the
  fetch row, which the brief and sources.json read too), gives every item its final id and
  keeps one snapshot;
- the judge starts once (intel_judge) and its brief falls back to Tin's own when missing;
- the publication is one commit under `intel:<run>:publish`, the Slack post is recorded
  before Slack is called (intel_slack.send), and the run's projection has its own receipt.

Failing well: a source, a group, X or the world feed missing is a recorded outcome, never the
run's failure; no items at all fails before the judge is paid for; a failed judge fails the run
with the judge's own reason. The skeleton both intel workflows share is
intel_steps.IntelActivities.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import intel_daily, intel_daily_slack, intel_judge, intel_slack, intel_snapshot
from tin_lite import intel_store as store
from tin_lite.document_handoff import document_url
from tin_lite.failures import failure_projection, split_failure_input
from tin_lite.intel_brief import CLIENT_SCHEMA, brief_links, fallback_client_brief
from tin_lite.intel_fetch import config as source_config
from tin_lite.intel_fetch import gnews, group, run_sources, x_list
from tin_lite.intel_fetch.http import PublicReader
from tin_lite.intel_fetch.items import Window, iso
from tin_lite.intel_steps import IntelActivities, beating, heartbeat
from tin_lite.product_urls import dashboard_url
from tin_lite.workflow_inputs import WorkflowInputError

logger = logging.getLogger(__name__)

KEY = intel_daily.KEY
STEPS = 6
FALLBACK = "Tin couldn't finish today's market brief. Tomorrow's run tries again."
NO_ITEMS = (
    "No items were fetched: every source failed or was empty. Check the sources in "
    "intel/sources/ and the source status of this run."
)
NO_COMPANY = (
    "The daily brief needs company.md in the project's files: who the client is, its buyers, "
    "competitors and the claims it may make. Add it, then start the brief again."
)
NO_CONFIG = (
    "The daily brief needs at least one source config in the project's files: "
    "intel/sources/rss.json or intel/sources/hn-reddit.json. Add one, then start it again."
)
MAX_LINKS = gnews.UNWRAP_LIMIT
X_SOURCE = {"id": x_list.SOURCE_ID, "name": "X List", "method": "x_list"}


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_day(at: datetime, timezone: str | None) -> date:
    """The client's day: the run's local date in the project's timezone."""
    return at.astimezone(zone(timezone)).date()


def window_for(created: datetime, *, lookback_hours: int, previous_end: datetime | None) -> Window:
    """Until the run's creation (to the minute); since the previous successful fetch's end less
    an hour when it is recent, else the lookback."""
    until = created.astimezone(UTC).replace(second=0, microsecond=0)
    if previous_end is not None and timedelta(0) < until - previous_end <= timedelta(
        hours=intel_daily.CONTINUE_WITHIN_HOURS
    ):
        return Window(previous_end - timedelta(hours=intel_daily.OVERLAP_HOURS), until)
    return Window(until - timedelta(hours=lookback_hours), until)


class IntelDailyActivities(IntelActivities):
    KEY = KEY
    STEPS = STEPS
    INACTIVE = "This brief's run is no longer active."
    NOT_PUBLISHED = "The report was never published."

    def __init__(self, *, database, storage, settings, integrations, reader_factory=None):
        super().__init__(
            database=database, storage=storage, settings=settings, integrations=integrations
        )
        self._reader = reader_factory or (lambda agent: PublicReader(user_agent=agent))

    async def _pinned(self, run) -> dict:
        saved = await self.db.get_effect(f"intel:{run.id}:prepare")
        if not saved or saved.status != "completed":
            raise ApplicationError(self.NOT_PREPARED, non_retryable=True)
        return saved.result or {}

    async def _files(self, project, revision: str, paths) -> dict[str, bytes | None]:
        found = {}
        for path in paths:
            found[path] = await self.storage.read_canonical_artifact_if_exists(
                repo_id=project.state_repo_id, commit_sha=revision, path=path
            )
        return found

    async def _config(self, project, revision: str) -> tuple[dict, list[dict]]:
        raw = await self._files(project, revision, intel_daily.CONFIG_PATHS)
        too_big = [
            p for p, body in raw.items() if body and len(body) > intel_daily.CONFIG_MAX_BYTES
        ]
        for path in too_big:
            raw[path] = None
        config, problems = intel_daily.combine(raw)
        problems += [
            intel_daily._file_problem(path, "the file is larger than 512 KB") for path in too_big
        ]
        return config, problems

    # Steps.

    @activity.defn(name="intel_daily_prepare")
    async def prepare(self, run_id: str) -> dict:
        """Pin the day, window, source configs and destination. Refuses before any spend when
        company.md or both configs are missing. Returns the fetch groups to run."""
        run = await self._active(run_id)
        await self.db.mark_run_running(run.id)
        await self._progress(run, "prepare", 1, "Reading the client's sources")
        key = f"intel:{run.id}:prepare"
        async with self.db.effect_lock(key, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return {"groups": saved.result["groups"]}
            project = await self.db.get_project(run.project_id)
            repo = await self.storage.get_repo(project.state_repo_id)
            head = await self.storage.head_sha(repo, project.canonical_branch)
            if not head:
                raise WorkflowInputError(NO_COMPANY)
            company = await self._files(project, head, intel_daily.COMPANY_PATHS)
            company_path = next((p for p, body in company.items() if body and body.strip()), None)
            if company_path is None:
                raise WorkflowInputError(NO_COMPANY)
            try:
                config, problems = await self._config(project, head)
            except intel_daily.ConfigMissing:
                raise WorkflowInputError(NO_CONFIG) from None
            ready, skipped = source_config.select(config, intel_daily.SCOPE)
            inputs = run.input or {}
            created = run.created_at or datetime.now(UTC)
            previous = await conn.fetchval(
                """SELECT f.window_end FROM intel_fetches f
                   JOIN workflow_runs r ON r.id = f.run_id
                   WHERE f.project_id = $1 AND f.scope = 'client' AND f.run_id <> $2
                     AND r.status = 'succeeded' AND f.window_end < $3
                   ORDER BY f.window_end DESC LIMIT 1""",
                run.project_id,
                run.id,
                created,
            )
            window = window_for(
                created,
                lookback_hours=int(inputs.get("lookback_hours") or 24),
                previous_end=previous,
            )
            day = local_day(created, project.timezone)
            await store.begin_fetch(
                conn,
                run_id=run.id,
                scope=intel_daily.SCOPE,
                project_id=run.project_id,
                day=day,
                window_start=window.since,
                window_end=window.until,
                config_sha256=source_config.digest(config),
                config_revision=head,
            )
            # No saved List: X is recorded as skipped with its reason, never silently absent.
            unread_x = [] if inputs.get("x_list_id") else [x_list.not_saved()]
            await store.record_sources(conn, run_id=run.id, statuses=skipped + problems + unread_x)
            groups = [
                name
                for name in intel_daily.GROUP_METHODS
                if group(intel_daily.SCOPE, name)[0](ready)
            ]
            if inputs.get("x_list_id"):
                groups.append(intel_daily.X_GROUP)
            channel = await conn.fetchrow(
                "SELECT slack_channel_id, slack_channel_name FROM intel_projects "
                "WHERE project_id = $1",
                run.project_id,
            )
            pinned = {
                "groups": groups,
                "company_path": company_path,
                "post_brief": inputs.get("post_brief", True) is not False,
                "channel_id": channel["slack_channel_id"] if channel else None,
                "channel_name": channel["slack_channel_name"] if channel else None,
            }
            if saved is None:
                await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(conn, execution_key=key, result=pinned)
        return {"groups": groups}

    @activity.defn(name="intel_daily_fetch")
    async def fetch(self, control: dict) -> dict:
        """One fetch group. Sources already recorded are skipped, so a retry continues; a
        source's failure is its status, and a whole group's failure is recorded, never fatal."""
        run = await self._active(control["run_id"])
        name = control["group"]
        if name not in intel_daily.GROUPS:
            raise ApplicationError(f"Unknown fetch group {name[:40]}.", non_retryable=True)
        await self._progress(run, "fetch", 2, "Reading the market")
        fetch = await self._fetch(run)
        window = Window(fetch["window_start"], fetch["window_end"])
        try:
            async with beating(name):
                if name == intel_daily.X_GROUP:
                    return await self._fetch_x(run, window)
                return await self._fetch_group(run, fetch, name, window)
        except (asyncio.CancelledError, ApplicationError):
            raise
        except Exception as exc:  # noqa: BLE001 — one group's failure is recorded, not the run's
            logger.warning("intel.daily fetch group %s failed: %s", name, type(exc).__name__)
            reason = f"group_failed: {type(exc).__name__}: {exc}"
            try:
                sources = await self._group_sources(run, fetch, name)
            except Exception:  # noqa: BLE001 — the load step sweeps what this can't name
                sources = []
            failed = await self._record_unfinished(run, sources, reason)
            return {"items": 0, "sources": failed, "failed": failed}

    async def _group_sources(self, run, fetch: dict, name: str) -> list[dict]:
        """The sources a fetch group reads, from the pinned configs (X: the saved List)."""
        if name == intel_daily.X_GROUP:
            return [X_SOURCE] if (run.input or {}).get("x_list_id") else []
        project = await self.db.get_project(run.project_id)
        config, _problems = await self._config(project, fetch["config_revision"])
        ready, _skipped = source_config.select(config, intel_daily.SCOPE)
        return group(intel_daily.SCOPE, name)[0](ready)

    async def _fetch_group(self, run, fetch: dict, name: str, window: Window) -> dict:
        project = await self.db.get_project(run.project_id)
        config, _problems = await self._config(project, fetch["config_revision"])
        ready, _skipped = source_config.select(config, intel_daily.SCOPE)
        select, methods = group(intel_daily.SCOPE, name)
        done = await store.done_sources(self.db.pool, run.id)
        todo = [source for source in select(ready) if source["id"] not in done]
        if not todo:
            return {"items": 0, "sources": 0}
        settings = {
            **intel_daily.group_settings(name, config),
            "fetched_at": iso(datetime.now(UTC)),
            # Shared across the group's sources: one thread read once.
            "hn_trees": {},
            "reddit_trees": {},
            "reddit_posts": {},
        }

        async def done_one(source: dict, items: list, status: dict) -> None:
            await store.put_items(self.db.pool, run_id=run.id, items=items)
            await store.record_sources(self.db.pool, run_id=run.id, statuses=[status])

        reader = self._reader(intel_daily.GROUP_USER_AGENT[name])
        try:
            items, statuses = await run_sources(
                methods, todo, window, reader, settings=settings, on_done=done_one
            )
        finally:
            await reader.aclose()
        # Again with the duplicates each source lost to an earlier one.
        await store.record_sources(self.db.pool, run_id=run.id, statuses=statuses)
        return {
            "items": len(items),
            "sources": len(statuses),
            "failed": sum(1 for s in statuses if s["status"] == "failed"),
        }

    async def _fetch_x(self, run, window: Window) -> dict:
        if x_list.SOURCE_ID in await store.done_sources(self.db.pool, run.id):
            return {"items": 0, "sources": 0}
        inputs = run.input or {}

        async def put(conn, items):
            await store.put_items(conn, run_id=run.id, items=items)

        status = await x_list.read_list(
            self.db,
            self.integrations.x,
            run_id=run.id,
            project_id=run.project_id,
            member=run.started_by_clerk_user_id,
            list_id=str(inputs["x_list_id"]),
            window=window,
            keep_retweets=inputs.get("x_keep_retweets") is True,
            put=put,
            heartbeat=heartbeat,
        )
        await store.record_sources(self.db.pool, run_id=run.id, statuses=[status])
        return {"items": status["items_in_window"], "sources": 1, "x": status["reason"] or "ok"}

    @activity.defn(name="intel_daily_load")
    async def load(self, run_id: str) -> dict:
        """Sources no fetch group recorded, today's world stories as items, final ids, and the
        judge's snapshot. No items from the client's own sources fails here, before the judge
        is paid for. The world day used is pinned on the fetch row."""
        run = await self._active(run_id)
        await self._progress(run, "load", 3, "Gathering the day for the analyst")
        project = await self.db.get_project(run.project_id)
        now = run.created_at or datetime.now(UTC)
        fetch = await self._fetch(run)
        if await store.get_snapshot(self.db.pool, run.id) is None:
            # A group whose activity never finished (it timed out or ran out of attempts) left
            # its sources unrecorded: they fail here with that reason.
            for name in (await self._pinned(run)).get("groups") or []:
                try:
                    sources = await self._group_sources(run, fetch, name)
                except Exception:  # noqa: BLE001 — a config that won't read was recorded at prepare
                    sources = []
                await self._record_unfinished(
                    run, sources, "group_failed: its fetch did not finish"
                )
        async with self.db.pool.acquire() as conn:
            if await store.get_snapshot(conn, run.id) is None:
                own = await conn.fetchval(
                    "SELECT count(*) FROM intel_items WHERE run_id = $1 AND source_type <> 'world'",
                    run.id,
                )
                if not own:
                    await store.set_fetch(conn, run.id, status="failed")
                    raise ApplicationError(NO_ITEMS, non_retryable=True)
                await intel_snapshot.prepare(conn, fetch_run_id=run.id, now=now)
                body = await intel_snapshot.build(
                    conn, fetch_run_id=run.id, client_name=project.name
                )
                await store.save_snapshot(conn, run_id=run.id, body=body)
                await store.set_fetch(conn, run.id, status="loaded")
            fetch = await store.get_fetch(conn, run.id)
        world = fetch["world_day"] or store.WORLD_MISSING
        return {"items": int(fetch["item_count"]), "world": world["status"]}

    @activity.defn(name="intel_daily_judge_start")
    async def judge_start(self, run_id: str) -> dict:
        run = await self._active(run_id)
        await self._progress(run, "judge", 4, "The analyst is reading the day")
        project = await self.db.get_project(run.project_id)
        return await intel_judge.start_child(
            self,
            run,
            key=intel_daily.JUDGE_KEY,
            scope=intel_daily.SCOPE,
            client_name=project.name,
        )

    @activity.defn(name="intel_daily_judge_collect")
    async def judge_collect(self, run_id: str) -> dict:
        """What the judge left. A failed judge fails the run with its own reason (its stories
        stay); a missing brief becomes Tin's own from the day's top stories."""
        run = await self._active(run_id)
        found = await intel_judge.collect(self, run)
        if found["child_status"] != "succeeded":
            await store.set_fetch(self.db.pool, run.id, status="failed")
            child = (
                await self.db.get_run(UUID(found["child_run_id"]))
                if found.get("child_run_id")
                else None
            )
            message = (child.error_message if child else None) or (
                "The analyst didn't finish today's brief."
            )
            raise ApplicationError(
                message,
                {
                    "tin_failure": {
                        "kind": (child.error_kind if child else None) or "explained",
                        "message": message,
                        "action": child.error_action if child else None,
                    }
                },
                non_retryable=True,
            )
        brief = "judge" if found["brief"] else "none"
        if not found["brief"]:
            fetch = await self._fetch(run)
            fallback = fallback_client_brief(
                await store.today_stories(self.db.pool, project_id=run.project_id, day=fetch["day"])
            )
            if fallback is not None:
                await store.put_brief(
                    self.db.pool,
                    run_id=UUID(found["child_run_id"]),
                    parent_run_id=run.id,
                    project_id=run.project_id,
                    day=fetch["day"],
                    schema=CLIENT_SCHEMA,
                    brief=fallback,
                    source="fallback",
                )
                brief = "fallback"
        await store.set_fetch(self.db.pool, run.id, status="judged")
        return {"stories": found["stories"], "brief": brief}

    async def _brief_parts(self, run, fetch: dict) -> tuple[dict | None, dict[str, dict]]:
        saved = await store.get_brief(self.db.pool, parent_run_id=run.id)
        if saved is None:
            return None, {}
        found = await store.stories(
            self.db.pool,
            project_id=run.project_id,
            since_day=fetch["day"] - timedelta(days=14),
            limit=400,
        )
        return saved, {story["key"]: story for story in found}

    def _counts(self, fetch: dict) -> tuple[int, int, dict[str, int]]:
        statuses = [s for s in fetch.get("source_status") or [] if isinstance(s, dict)]
        sources = sum(1 for s in statuses if s.get("status") in {"ok", "empty"})
        kinds: dict[str, int] = {}
        for status in statuses:
            method = status.get("method") or ""
            kind = (
                "x"
                if method == "x_list"
                else "hn"
                if method == "hn"
                else "reddit"
                if method == "reddit"
                else "rss"
            )
            kinds[kind] = kinds.get(kind, 0) + int(status.get("items_in_window") or 0)
        return int(fetch.get("item_count") or 0), sources, kinds

    @activity.defn(name="intel_daily_publish")
    async def publish(self, run_id: str) -> dict:
        """report.md, brief.md and sources.json under intel/reports/<day>/, with the Google News
        links the report and brief use unwrapped to their publishers. One commit, once."""
        run = await self._active(run_id)
        await self._progress(run, "publish", 5, "Publishing the report and brief")
        project = await self.db.get_project(run.project_id)
        fetch = await self._fetch(run)
        paths = intel_daily.report_paths(fetch["day"].isoformat())

        async def documents() -> dict:
            texts, links, headline = await self._documents(run, project, fetch, paths)
            return {
                "documents": texts,
                "links": links,
                "headline": headline,
                "summary": headline[:300] or None,
            }

        result = await self._publish(
            run,
            project,
            paths=paths,
            limits=intel_daily.LIMITS,
            artifact="report.md",
            documents=documents,
        )
        return {"links": len(result.get("links") or {})}

    async def _documents(self, run, project, fetch, paths) -> tuple[dict, dict, str]:
        found = await intel_judge.collect(self, run)
        child = await self.db.get_run(UUID(found["child_run_id"]))
        if child is None or not child.artifact_path or not child.canonical_commit_sha:
            raise ApplicationError("The analyst's report is missing.", non_retryable=True)
        report = (
            await self.storage.read_canonical_artifact(
                repo_id=project.state_repo_id,
                commit_sha=child.canonical_commit_sha,
                path=child.artifact_path,
            )
        ).decode("utf-8", "replace")
        saved, stories = await self._brief_parts(run, fetch)
        brief = (saved or {}).get("brief")
        reader = self._reader(intel_daily.GROUP_USER_AGENT["gnews"])
        try:
            report, _count = await gnews.unwrap_text(self.db.pool, reader, report)
            links = await gnews.resolve_many(
                self.db.pool, reader, brief_links(CLIENT_SCHEMA, brief, stories), limit=MAX_LINKS
            )
        finally:
            await reader.aclose()
        items, sources, _kinds = self._counts(fetch)
        world = fetch["world_day"] or store.WORLD_MISSING
        extra = intel_daily_slack.notes(
            fetch, brief_source=(saved or {}).get("source"), world_status=world["status"]
        )
        if brief:
            brief_md = intel_daily_slack.brief_markdown(
                brief,
                stories,
                client_name=project.name,
                day=fetch["day"],
                items=items,
                sources=sources,
                links=links,
                extra_notes=extra,
            )
        else:
            brief_md = (
                f"# {project.name} market brief\n\nNo brief today: the analyst recorded no "
                "stories. The report has the day's reading.\n"
            )
        status = {
            "day": fetch["day"].isoformat(),
            "window": {"since": iso(fetch["window_start"]), "until": iso(fetch["window_end"])},
            "config_revision": fetch["config_revision"],
            "items": items,
            "world_day": world,
            "sources": fetch.get("source_status") or [],
        }
        documents = {
            "report.md": report,
            "brief.md": brief_md,
            "sources.json": json.dumps(status, ensure_ascii=False, indent=1, default=str) + "\n",
        }
        return documents, links, (brief or {}).get("headline") or ""

    @activity.defn(name="intel_daily_post")
    async def post(self, run_id: str) -> dict:
        """The brief in the client's Slack channel, through Tin's own Slack app (the host
        project's connection), when the saved workflow asks and an operator set a channel.
        Recorded before Slack is called; never fails the run."""
        run = await self._active(run_id)
        pinned = await self._pinned(run)
        if not pinned.get("post_brief"):
            return {"outcome": "off"}
        poster = getattr(self.settings, "intel_host_project_id", None)
        channel = pinned.get("channel_id")
        if poster is None or not channel:
            await self._progress(run, "post", 6, "No Slack channel is set for the brief")
            return {"outcome": "no_destination"}
        fetch = await self._fetch(run)
        saved, stories = await self._brief_parts(run, fetch)
        if saved is None:
            return {"outcome": "no_brief"}
        published = await self.db.get_effect(f"intel:{run.id}:publish")
        result = (published.result or {}) if published else {}
        project = await self.db.get_project(run.project_id)
        items, sources, kinds = self._counts(fetch)
        world = fetch["world_day"] or store.WORLD_MISSING
        base = dashboard_url(self.settings)
        sources_url = None
        if result.get("canonical_commit_sha"):
            sources_url = document_url(
                self.settings,
                _Doc(
                    run.id,
                    run.project_id,
                    intel_daily.report_paths(fetch["day"].isoformat())["sources.json"],
                    result["canonical_commit_sha"],
                ),
            )
        body = intel_daily_slack.client_message(
            saved["brief"],
            stories,
            client_name=project.name,
            day=fetch["day"],
            items=items,
            sources=sources,
            kinds=intel_daily_slack.source_kinds(kinds),
            report_url=f"{base}/document/{run.id}?project={run.project_id}",
            sources_url=sources_url,
            links=result.get("links") or {},
            extra_notes=intel_daily_slack.notes(
                fetch,
                brief_source=saved.get("source"),
                world_status=world["status"],
            ),
        )
        await self._progress(run, "post", 6, "Posting the brief in Slack")
        await intel_slack.recover(self.db)
        outcome = await intel_slack.send(
            self.db,
            self.integrations.slack,
            poster_project_id=poster,
            project_id=run.project_id,
            key=f"brief:{run.id}",
            channel_id=channel,
            body=body,
        )
        intel_slack.retry_refused(outcome)
        return {"outcome": outcome}

    @activity.defn(name="intel_daily_finish")
    async def finish(self, run_id: str) -> None:
        """The run succeeds with report.md as its artifact."""
        run = await self._active(run_id)
        await self._finish_projection(run)

    @activity.defn(name="intel_daily_failure")
    async def failure(self, argument: str | dict) -> None:
        run_id, passed = split_failure_input(argument)
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != KEY:
            return
        await self._fail_fetch(run)
        await self.db.project_failure(run_id=run.id, **failure_projection(passed, FALLBACK))


class _Doc:
    """The fields document_url reads, for a file of this run's publication."""

    def __init__(self, run_id, project_id, path, revision):
        self.id, self.project_id = run_id, project_id
        self.artifact_path, self.canonical_commit_sha = path, revision
        self.status = "succeeded"


def activities_of(instance: IntelDailyActivities) -> list:
    return [
        instance.prepare,
        instance.fetch,
        instance.load,
        instance.judge_start,
        instance.judge_collect,
        instance.publish,
        instance.post,
        instance.finish,
        instance.failure,
    ]
