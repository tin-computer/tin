"""Activities for `people.rb2b_visitor`: one visitor RB2B posted, recorded, enriched and rated.

Temporal history holds the run ID, the visit's outcome and how long to wait for PostHog; the
payload, the person and the visit stay in Postgres. Each step is safe to repeat: People writes
are keyed (the person by their identifiers, the visit by its event key), the rating has its own
receipt, and the list is published under one.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import people_icp, people_rating, rb2b, rb2b_slack
from tin_lite import people_records as records
from tin_lite.failures import failure_projection, split_failure_input
from tin_lite.integrations import POSTHOG_PROVIDER
from tin_lite.organic_audit_publication import publish_artifacts
from tin_lite.product_urls import dashboard_url

logger = logging.getLogger(__name__)

KEY = rb2b.KEY
FALLBACK = "Tin couldn't finish reading this visitor. The next visitor RB2B posts tries again."
STEPS = 5


def _heartbeat(detail) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


class RB2BActivities:
    def __init__(self, *, database, storage, router, integrations, settings=None):
        self.db, self.storage, self.router = database, storage, router
        self.integrations, self.settings = integrations, settings

    async def _active(self, run_id, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or run.executor != KEY or run.status.value not in {"pending", "running"}:
            raise ApplicationError("This visitor's run is no longer active.", non_retryable=True)
        return run

    async def _progress(self, run, step: str, current: int, summary: str) -> None:
        await self.db.project_run_progress(
            run_id=run.id, mode="steps", step=step, current=current, total=STEPS, summary=summary
        )
        _heartbeat({"step": step})

    async def _visit(self, run):
        row = await self.db.pool.fetchrow(
            "SELECT * FROM rb2b_visits WHERE id = $1 AND project_id = $2",
            UUID(str(run.input["visit_id"])),
            run.project_id,
        )
        if row is None:
            raise ApplicationError("RB2B's visit is no longer kept.", non_retryable=True)
        return row

    async def _record_id(self, run) -> UUID | None:
        saved = await self.db.get_effect(f"{run.id}:rb2b:record")
        found = (saved.result or {}).get("record_id") if saved else None
        return UUID(found) if found else None

    @activity.defn(name="rb2b_record")
    async def record(self, run_id: str) -> dict:
        """Mark the run running and keep the person in People. Returns the outcome and how many
        seconds to wait before PostHog has the visit."""

        run = await self._active(run_id)
        await self.db.mark_run_running(run.id)
        await self._progress(run, "record", 1, "Keeping the visitor in People")
        row = await self._visit(run)
        found = rb2b.lead(_payload(row))
        outcome = row["kind"]
        key = f"{run.id}:rb2b:record"
        async with self.db.effect_lock(key, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                outcome = saved.result["outcome"]
            else:
                if saved is None:
                    await self.db.start_effect(conn, execution_key=key, operation=KEY)
                record_id = None
                if outcome == "person":
                    try:
                        ids = rb2b.identifiers(found)
                    except records.RecordError:
                        ids, outcome = None, "no_profile"
                    if ids is not None:
                        record_id = await self._keep(conn, run, found, ids)
                        outcome = "person" if record_id else "forgotten"
                await self.db.complete_effect(
                    conn,
                    execution_key=key,
                    result={"outcome": outcome, "record_id": str(record_id or "") or None},
                )
        wait = 0
        if outcome == "person" and found["at"] is not None:
            wait = max(
                0, int((found["at"] + rb2b.ENRICH_AFTER - datetime.now(UTC)).total_seconds())
            )
        return {"outcome": outcome, "wait": min(wait, int(rb2b.ENRICH_AFTER.total_seconds()))}

    async def _keep(self, conn, run, found, ids) -> UUID | None:
        """The person, with the fields nobody else set yet; None when they were forgotten."""

        await records.ensure_fields(conn, run.project_id)
        record_id = await records.upsert_person(
            conn, run.project_id, ids, source=rb2b.SOURCE, run_id=run.id
        )
        if record_id is None:
            return None
        record_id = await records.current(conn, run.project_id, record_id)
        have = {
            row["field"]
            for row in await conn.fetch(
                """SELECT field FROM record_values WHERE record_id = $1
                   AND (value_text IS NOT NULL OR value_number IS NOT NULL)""",
                record_id,
            )
        }
        values = [value for value in rb2b.person_values(found) if value.field not in have]
        if values:
            await records.set_values(
                conn, run.project_id, record_id, values, set_by_kind="run", set_by=str(run.id)
            )
        return record_id

    @activity.defn(name="rb2b_enrich")
    async def enrich(self, run_id: str) -> None:
        """The visit from the project's PostHog, as a `visited` event and "On the site"."""

        run = await self._active(run_id)
        record_id = await self._record_id(run)
        if record_id is None:
            return
        await self._progress(run, "enrich", 2, "Reading the visit in PostHog")
        row = await self._visit(run)
        found = rb2b.lead(_payload(row))
        if found["at"] is None:
            found["at"] = row["received_at"]
        the_visit = await self._posthog_visit(run, found)
        async with self.db.pool.acquire() as conn:
            ids = rb2b.identifiers(found)
            try:
                await records.add_event(
                    conn,
                    run.project_id,
                    record_id,
                    kind="visited",
                    event_key=rb2b.event_key(run.project_id, ids, found["at"]),
                    occurred_at=found["at"],
                    detail={
                        "tier": "lead",
                        "summary": the_visit["summary"],
                        "stopped_at": the_visit["stopped_at"],
                        "action": "",
                        **(
                            {"replay_url": the_visit["replay_url"]}
                            if the_visit["replay_url"]
                            else {}
                        ),
                        "source": rb2b.SOURCE,
                        "page": (found["url"] or "")[:500],
                        "channel": the_visit["channel"],
                        **(
                            {"first_touch": the_visit["first_touch"]}
                            if the_visit.get("first_touch")
                            else {}
                        ),
                        **({"repeat": True} if found["repeat"] else {}),
                    },
                    run_id=run.id,
                )
                await records.set_values(
                    conn,
                    run.project_id,
                    record_id,
                    rb2b.web_values(the_visit),
                    set_by_kind="run",
                    set_by=str(run.id),
                )
            except records.RecordError as error:
                # The founder forgot this person while Tin read the visit.
                if error.code != "record_gone":
                    raise

    async def _posthog_visit(self, run, found) -> dict:
        connection = await self.db.get_integration_connection(
            project_id=run.project_id, provider_key=POSTHOG_PROVIDER
        )
        config = (connection.configuration or {}) if connection else {}
        if connection is None or connection.status != "connected" or not found["url"]:
            return rb2b.unmatched(found, 0, why="PostHog isn't connected")
        if not config.get("selected_project_id"):
            return rb2b.unmatched(found, 0, why="no PostHog project chosen")

        async def query(name: str, hogql: str) -> list:
            result = await self.integrations.posthog.call(
                "query.hogql",
                {"query": hogql, "name": f"rb2b visitor {name}"},
                connection=connection,
                run_id=run.id,
                execution_key=f"{run.id}:rb2b:posthog:{name}",
                max_response_bytes=None,
            )
            return result.get("rows") or []

        try:
            rows = await query("match", rb2b.match_query(found))
            session = rb2b.matched(found, rows)
            if session is None:
                return rb2b.unmatched(found, len(rows))
            session_id, person_id = session[0], session[3]
            info = {
                "session_id": session_id,
                "events": await query("session", rb2b.session_query(session_id)),
                "other_sessions": 0,
                "first_touch": None,
            }
            replay = await query("replay", rb2b.replay_query(session_id))
            info["active_ms"] = (replay[0][1] or 0) if replay else None
            if person_id:
                others = await query("others", rb2b.others_query(person_id, session_id))
                info["other_sessions"] = int(others[0][0] or 0) if others else 0
                if info["other_sessions"]:
                    first = await query("first", rb2b.first_touch_query(person_id))
                    if first:
                        info["first_touch"] = rb2b.channel(*first[0])
        except Exception as error:  # noqa: BLE001 - the visit without PostHog is still a visit
            logger.warning("PostHog read failed for run %s: %s", run.id, type(error).__name__)
            return rb2b.unmatched(found, 0, why="PostHog didn't answer")
        base = f"{config['api_host'].rstrip('/')}/project/{config['selected_project_id']}/replay/"
        return rb2b.visit(found, info, base)

    @activity.defn(name="rb2b_rate")
    async def rate(self, run_id: str) -> None:
        """Fit and intent against the ICP file, for this person only."""

        run = await self._active(run_id)
        record_id = await self._record_id(run)
        if record_id is None or not people_rating.available(self.router):
            return
        project = await self.db.get_project(run.project_id)
        icp = await people_icp.read_icp(self.storage, project)
        if not icp.get("profiles"):
            return
        await self._progress(run, "rate", 3, "Rating the visitor against your ICP")
        await people_rating.rate_unrated(
            self.db,
            self.router,
            run,
            text=await people_rating.icp_text(self.storage, project, icp),
            profiles=icp["profiles"],
            definition=people_rating.definition_for(icp),
            operation=KEY,
            step="rb2b",
            only=record_id,
            heartbeat=_heartbeat,
        )

    @activity.defn(name="rb2b_notify")
    async def notify(self, run_id: str) -> None:
        """A good or strong fit's own message in the project's Slack channel; the rest wait
        for the evening digest. Recorded before Slack is called, so a retry never posts twice."""

        run = await self._active(run_id)
        record_id = await self._record_id(run)
        if record_id is None:
            return
        place = await rb2b_slack.destination(self.db, run.project_id)
        if place is None:
            return
        async with self.db.pool.acquire() as conn:
            found = await records.people(conn, run.project_id, only=record_id)
            visit = await conn.fetchval(
                """SELECT detail FROM record_events
                   WHERE project_id = $1 AND kind = 'visited' AND run_id = $2
                   ORDER BY recorded_at DESC LIMIT 1""",
                run.project_id,
                run.id,
            )
        if not found:
            return
        project = await self.db.get_project(run.project_id)
        base = dashboard_url(self.settings) if self.settings is not None else ""
        body = rb2b_slack.visitor_message(
            found[0],
            json.loads(visit) if isinstance(visit, str) else visit,
            people_url=f"{base}/people?project={run.project_id}&person={found[0]['id']}",
            project_name=project.name,
        )
        if body is None:
            return
        await self._progress(run, "notify", 4, "Posting the visitor in Slack")
        await rb2b_slack.send(
            self.db,
            self.integrations.slack,
            project_id=run.project_id,
            key=f"visitor:{run.id}",
            place=place,
            body=body,
        )

    @activity.defn(name="rb2b_publish")
    async def publish(self, control: dict) -> None:
        """The latest visitors in people/rb2b-visitors.md, and the run's one line."""

        run = await self._active(control["run_id"])
        await self._progress(run, "publish", 5, "Updating the list of visitors")
        project = await self.db.get_project(run.project_id)
        record_id = await self._record_id(run)
        key = f"{run.id}:rb2b:files"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                publication = receipt.result
            else:
                if receipt is None:
                    await self.db.start_effect(conn, execution_key=key, operation=KEY)
                person = None
                if record_id is not None:
                    found = await records.people(conn, run.project_id, only=record_id)
                    person = found[0] if found else None
                summary = rb2b.summary(person, control["outcome"])
                async with self.db.project_state_lock(conn, project.id):

                    async def save_intent(value):
                        await self.db.save_publication_intent(conn, execution_key=key, intent=value)

                    async def validate_active():
                        await self._active(run.id, conn=conn)

                    revision = await publish_artifacts(
                        storage=self.storage,
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        documents={
                            rb2b.LIST_PATH: rb2b.report(
                                await visitors(conn, run.project_id), now=datetime.now(UTC)
                            ).encode()
                        },
                        paths=rb2b.PATHS,
                        limits=rb2b.LIMITS,
                        message=f"{KEY} {run.id} [{key}]",
                        intent=(receipt.result or {}).get("publication") if receipt else None,
                        save_intent=save_intent,
                        validate_active=validate_active,
                        # Tin's own list: each run replaces the last one.
                        replaceable=frozenset(rb2b.PATHS.values()),
                    )
                publication = {
                    "canonical_commit_sha": revision,
                    "artifact_path": rb2b.LIST_PATH,
                    "summary": summary,
                }
                await self.db.complete_effect(conn, execution_key=key, result=publication)
        projection = f"{run.id}:rb2b:projection"
        async with self.db.effect_lock(projection, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=projection, operation=KEY)
            sha = publication["canonical_commit_sha"]
            await self.db._complete_readonly_report_projection(
                conn,
                execution_key=projection,
                run_id=run.id,
                canonical_commit_sha=sha,
                artifact_path=rb2b.LIST_PATH,
                artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{rb2b.LIST_PATH}",
                summary=publication["summary"],
                workflow_key=KEY,
            )

    @activity.defn(name="rb2b_failure")
    async def failure(self, argument: str | dict) -> None:
        run_id, passed = split_failure_input(argument)
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != KEY:
            return
        await self.db.project_failure(run_id=run.id, **failure_projection(passed, FALLBACK))


def _payload(row) -> dict:
    payload = row["payload"]
    return json.loads(payload) if isinstance(payload, str) else dict(payload)


async def visitors(conn, project_id: UUID) -> list[dict]:
    """The people RB2B named most recently, newest first."""

    rows = await conn.fetch(
        """SELECT COALESCE(r.merged_into, r.id) AS id,
               max(COALESCE(e.occurred_at, e.recorded_at)) AS seen
           FROM record_events e JOIN records r ON r.id = e.record_id
           WHERE e.project_id = $1 AND e.kind = 'visited' AND e.detail->>'source' = $2
           GROUP BY 1 ORDER BY 2 DESC LIMIT $3""",
        project_id,
        rb2b.SOURCE,
        rb2b.LISTED,
    )
    people = []
    for row in rows:
        found = await records.people(conn, project_id, only=row["id"])
        if found:
            people.append(found[0])
    return people
