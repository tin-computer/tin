"""Activities for `people.rate`: rate the project's unrated people, then list them."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import people_icp, people_rate, people_rating
from tin_lite.failures import failure_projection, split_failure_input
from tin_lite.organic_audit_publication import publish_artifacts

KEY = people_rate.KEY
STEPS = 2
FALLBACK = "Tin couldn't finish rating your people. Starting it again continues where it stopped."


def _heartbeat(detail) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


class PeopleRateActivities:
    def __init__(self, *, database, storage, router):
        self.db, self.storage, self.router = database, storage, router

    async def _active(self, run_id, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or run.executor != KEY or run.status.value not in {"pending", "running"}:
            raise ApplicationError("This rating run is no longer active.", non_retryable=True)
        return run

    async def _progress(self, run, step: str, current: int, summary: str) -> None:
        await self.db.project_run_progress(
            run_id=run.id, mode="steps", step=step, current=current, total=STEPS, summary=summary
        )
        _heartbeat({"step": step})

    @activity.defn(name="people_rate_score")
    async def score(self, run_id: str) -> dict:
        """Rate everyone unrated who did something, up to the run's guard. Returns counts."""

        run = await self._active(run_id)
        await self.db.mark_run_running(run.id)
        available = people_rating.available(self.router)
        project = await self.db.get_project(run.project_id)
        icp = await people_icp.read_icp(self.storage, project)
        has_icp = bool(icp.get("profiles"))
        counts = {"rated": 0, "left": 0, "available": available, "has_icp": has_icp}
        if not (available and has_icp):
            return counts
        await self._progress(run, "rate", 1, "Rating your people against your ICP")
        definition = people_rating.definition_for(icp)
        counts["rated"] = await people_rating.rate_unrated(
            self.db,
            self.router,
            run,
            text=await people_rating.icp_text(self.storage, project, icp),
            profiles=icp["profiles"],
            definition=definition,
            operation=KEY,
            step="people_rate",
            heartbeat=_heartbeat,
        )
        async with self.db.pool.acquire() as conn:
            left = await people_icp.unscored(
                conn, run.project_id, definition, limit=people_icp.MAX_SCORED
            )
        counts["left"] = len(left)
        return counts

    @activity.defn(name="people_rate_publish")
    async def publish(self, control: dict) -> None:
        """people/ratings.md and the run's one line."""

        run = await self._active(control["run_id"])
        await self._progress(run, "publish", 2, "Listing whom Tin rated")
        project = await self.db.get_project(run.project_id)
        key = f"{run.id}:people_rate:files"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                publication = receipt.result
            else:
                if receipt is None:
                    await self.db.start_effect(conn, execution_key=key, operation=KEY)
                people = await rated_by(conn, run.project_id, run.id)
                good = sum(1 for person in people if (person.get("fit") or 0) >= 3)
                summary = people_rate.summary(
                    rated=len(people),
                    good=good,
                    left=int(control.get("left") or 0),
                    available=bool(control.get("available")),
                    has_icp=bool(control.get("has_icp")),
                )
                document = people_rate.report(
                    people, rated=len(people), left=int(control.get("left") or 0)
                )
                async with self.db.project_state_lock(conn, project.id):

                    async def save_intent(value):
                        await self.db.save_publication_intent(conn, execution_key=key, intent=value)

                    async def validate_active():
                        await self._active(run.id, conn=conn)

                    revision = await publish_artifacts(
                        storage=self.storage,
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        documents={people_rate.PATH: document.encode()},
                        paths=people_rate.PATHS,
                        limits=people_rate.LIMITS,
                        message=f"{KEY} {run.id} [{key}]",
                        intent=(receipt.result or {}).get("publication") if receipt else None,
                        save_intent=save_intent,
                        validate_active=validate_active,
                        # Tin's own list: each run replaces the last one.
                        replaceable=frozenset(people_rate.PATHS.values()),
                    )
                publication = {
                    "canonical_commit_sha": revision,
                    "summary": summary,
                    "at": datetime.now(UTC).isoformat(),
                }
                await self.db.complete_effect(conn, execution_key=key, result=publication)
        projection = f"{run.id}:people_rate:projection"
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
                artifact_path=people_rate.PATH,
                artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{people_rate.PATH}",
                summary=publication["summary"],
                workflow_key=KEY,
            )

    @activity.defn(name="people_rate_failure")
    async def failure(self, argument: str | dict) -> None:
        run_id, passed = split_failure_input(argument)
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != KEY:
            return
        await self.db.project_failure(run_id=run.id, **failure_projection(passed, FALLBACK))


async def rated_by(conn, project_id: UUID, run_id: UUID) -> list[dict]:
    """The people this run rated, best fit first."""

    rows = await conn.fetch(
        """SELECT r.id, f.value_number AS fit, f.reason,
               (SELECT value_text FROM record_values WHERE record_id = r.id AND field = 'name')
                   AS name,
               (SELECT value_text FROM record_values WHERE record_id = r.id AND field = 'headline')
                   AS headline,
               (SELECT value_text FROM record_values WHERE record_id = r.id AND field = 'intent')
                   AS intent
           FROM record_values f JOIN records r ON r.id = f.record_id
           WHERE r.project_id = $1 AND r.merged_into IS NULL AND f.field = 'icp_fit'
             AND f.set_by_kind = 'run' AND f.set_by = $2
           ORDER BY f.value_number DESC NULLS LAST, name""",
        project_id,
        str(run_id),
    )
    return [
        {**dict(row), "fit": int(row["fit"]) if row["fit"] is not None else None} for row in rows
    ]
