"""Activities for `social.linkedin_profile_viewers`.

Reads run in the LinkedIn sandbox with the founder's saved session, each under an effect
receipt, as for engagers: a read's answer stays in its receipt only until its people are
written, then the receipt keeps counts and LinkedIn's request for the next page, which the
next read takes from there. An interrupted read is never silently bought again. Temporal sees
read numbers and counts. The ICP read and the scoring are the engagers workflow's own.
"""

from __future__ import annotations

from datetime import UTC, datetime

from temporalio import activity

from tin_lite import linkedin_profile_viewers as viewers
from tin_lite import linkedin_requests as requests
from tin_lite.linkedin_cloud import ENGAGEMENT_OPERATION
from tin_lite.linkedin_engagers_activities import (
    SAYS,
    STOPS,
    LinkedInEngagersActivities,
    _heartbeat,
    refusal,
)
from tin_lite.linkedin_viewers import ProfileIdsResult, ViewersResult
from tin_lite.organic_audit_publication import publish_artifacts

STEPS = 5
FALLBACK = "Tin couldn't finish reading who viewed your LinkedIn profile. Try again later."
# What a read hands Temporal: counts, never the people or LinkedIn's next-page request.
RETURNED = ("outcome", "reason", "people", "new", "anonymous", "pages", "total_views", "more")


def _returned(result: dict) -> dict:
    return {name: result.get(name) for name in RETURNED}


class LinkedInProfileViewersActivities(LinkedInEngagersActivities):
    key = viewers.KEY
    prefix = "viewers"
    score_step = (2, STEPS)
    fallback = FALLBACK

    @activity.defn(name="linkedin_viewers_prepare")
    async def viewers_prepare(self, run_id: str) -> None:
        run = await self._active(run_id)
        await self._session(run)
        await self._prepare_icp(run)
        await self.db.mark_run_running(run.id)
        await self._progress(
            run.id, "viewers", 0, STEPS, "Reading who viewed your LinkedIn profile"
        )

    @activity.defn(name="linkedin_viewers_read")
    async def viewers_read(self, control: dict) -> dict:
        """One read of up to ten pages of the list (the first also reads the overview), from
        where the last read stopped."""

        run = await self._active(control["run_id"])
        index = int(control["read"])
        key = f"{run.id}:viewers:read:{index}"
        request = None
        if index:
            previous = await self.db.get_effect(f"{run.id}:viewers:read:{index - 1}")
            request = ((previous.result or {}) if previous else {}).get("next")
            if not request:
                return _returned({"outcome": "read", "people": 0, "new": 0, "anonymous": 0})
        # The first run reads back as far as it may; later ones start with the newest pages.
        earlier = await self.db.pool.fetchval(
            """SELECT EXISTS (SELECT 1 FROM record_events e
                              JOIN workflow_runs w ON w.id = e.run_id
                              WHERE e.project_id = $1 AND e.kind = 'viewed'
                                AND w.started_by_clerk_user_id = $2 AND e.run_id <> $3)""",
            run.project_id,
            run.started_by_clerk_user_id,
            run.id,
        )
        parsed, reason = await self._read(
            run,
            key,
            {
                "kind": "linkedin_viewers",
                "request": request,
                "overview": index == 0,
                "pages": viewers.LATER_PAGES if earlier else viewers.PAGES_PER_READ,
            },
            ViewersResult,
        )
        if reason == "done":
            return _returned(parsed)
        if reason:
            # Nothing read yet: the founder can act on why, or try again later.
            if index == 0:
                raise refusal(reason)
            outcome = "stopped" if reason in STOPS else "unread"
            return _returned(
                {"outcome": outcome, "reason": reason, "people": 0, "new": 0, "anonymous": 0}
            )
        async with self.db.effect_lock(key, ENGAGEMENT_OPERATION) as (conn, _receipt):
            async with conn.transaction():
                counts = await viewers.record_viewers(
                    conn,
                    project_id=run.project_id,
                    run_id=run.id,
                    member=run.started_by_clerk_user_id,
                    viewers=[viewer.model_dump() for viewer in parsed.viewers],
                    read_at=datetime.fromisoformat(parsed.read_at),
                )
                result = {
                    "outcome": "read",
                    "reason": parsed.reason,
                    "people": counts["people"],
                    "new": counts["new"],
                    "anonymous": parsed.anonymous,
                    "pages": parsed.pages,
                    "total_views": parsed.total_views,
                    "more": parsed.next is not None,
                    # The next read starts from LinkedIn's own request for the next page.
                    "next": parsed.next,
                }
                # The answer leaves the receipt with this; only counts and the request stay.
                await self.db.complete_effect(conn, execution_key=key, result=result)
        _heartbeat({"read": index})
        if index == 0:
            await self._progress(
                run.id, "viewers", 1, STEPS, "Reading who viewed your LinkedIn profile"
            )
        return _returned(result)

    @activity.defn(name="linkedin_viewers_score")
    async def viewers_score(self, run_id: str) -> None:
        await self._score(run_id)

    @activity.defn(name="linkedin_viewers_ids")
    async def viewers_ids(self, run_id: str) -> dict:
        """Look up the profile IDs of viewers who fit and are known only by their slug, so
        they can be asked to connect."""

        run = await self._active(run_id)
        key = f"{run.id}:viewers:ids"
        receipt = await self.db.get_effect(key)
        if receipt is None:
            async with self.db.pool.acquire() as conn:
                people = await viewers.need_ids(
                    conn,
                    run.project_id,
                    run.started_by_clerk_user_id,
                    limit=viewers.IDS_PER_RUN,
                )
            if not people:
                return {"looked_up": 0, "reason": None}
            slugs = [person["slug"] for person in people]
        else:
            slugs = []
        await self._progress(run.id, "ids", 3, STEPS, "Looking up the people who fit")
        parsed, reason = await self._read(
            run, key, {"kind": "linkedin_profile_ids", "slugs": slugs}, ProfileIdsResult
        )
        if reason == "done":
            return {"looked_up": parsed.get("looked_up", 0), "reason": None}
        if reason:
            # The list is still written; the next run looks them up again.
            return {"looked_up": 0, "reason": reason}
        async with self.db.effect_lock(key, ENGAGEMENT_OPERATION) as (conn, _receipt):
            async with conn.transaction():
                found = await viewers.record_ids(
                    conn,
                    project_id=run.project_id,
                    run_id=run.id,
                    member=run.started_by_clerk_user_id,
                    people=[person.model_dump() for person in parsed.people],
                )
                await self.db.complete_effect(conn, execution_key=key, result={"looked_up": found})
        return {"looked_up": found, "reason": None}

    @activity.defn(name="linkedin_viewers_accepted")
    async def viewers_accepted(self, run_id: str) -> dict:
        return await self._accepted(run_id)

    @activity.defn(name="linkedin_viewers_publish")
    async def viewers_publish(self, control: dict) -> None:
        run = await self._active(control["run_id"])
        notes = dict(control["notes"])
        icp = (await self.db.get_effect(f"{run.id}:viewers:icp")).result or {}
        notes["icp"] = icp.get("path")
        if notes.get("stopped"):
            notes["notice"] = SAYS.get(notes["stopped"], SAYS["access_denied"])
        notes["new_people"] = await self.db.pool.fetchval(
            "SELECT count(*) FROM records WHERE project_id = $1 AND source_run_id = $2",
            run.project_id,
            run.id,
        )
        # The people who fit and viewed the starter's profile wait for their approval.
        async with self.db.pool.acquire() as conn, conn.transaction():
            await requests.propose(
                conn, run.project_id, run.started_by_clerk_user_id, run_id=run.id
            )
        notes["proposed"] = await self.db.pool.fetchval(
            """SELECT count(*) FROM linkedin_requests q
               JOIN linkedin_request_batches b ON b.id = q.batch_id
               WHERE b.project_id = $1 AND b.member = $2 AND b.status = 'open'
                 AND q.status = 'proposed'""",
            run.project_id,
            run.started_by_clerk_user_id,
        )
        await self._progress(run.id, "publish", 4, STEPS, "Writing the list")
        project = await self.db.get_project(run.project_id)
        key = f"{run.id}:viewers:files"
        async with self.db.effect_lock(key, self.key) as (conn, receipt):
            if receipt and receipt.status == "completed":
                publication = receipt.result
            else:
                async with self.db.project_state_lock(conn, project.id):
                    saved = (receipt.result or {}) if receipt else {}
                    documents = saved.get("documents")
                    intent = saved.get("publication")
                    if not isinstance(documents, dict):
                        if receipt is None:
                            await self.db.start_effect(conn, execution_key=key, operation=self.key)
                        documents = viewers.render(
                            await viewers.viewers(
                                conn, run.project_id, run.started_by_clerk_user_id
                            ),
                            notes=notes,
                            now=datetime.now(UTC),
                        )
                        await self.db.save_effect_progress(
                            conn, execution_key=key, result={"documents": documents}
                        )
                        intent = None

                    async def save_intent(value):
                        await self.db.save_publication_intent(conn, execution_key=key, intent=value)

                    async def validate_active():
                        await self._active(run.id, conn=conn)

                    revision = await publish_artifacts(
                        storage=self.storage,
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        documents={path: text.encode() for path, text in documents.items()},
                        paths=viewers.PATHS,
                        limits=viewers.LIMITS,
                        message=f"{self.key} {run.id} [{key}]",
                        intent=intent,
                        save_intent=save_intent,
                        validate_active=validate_active,
                        # Tin's own list: each run replaces the last one.
                        replaceable=frozenset(viewers.PATHS.values()),
                    )
                publication = {
                    "canonical_commit_sha": revision,
                    "artifact_path": viewers.SUMMARY_PATH,
                }
                await self.db.complete_effect(conn, execution_key=key, result=publication)
        projection = f"{run.id}:viewers:projection"
        async with self.db.effect_lock(projection, self.key) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=projection, operation=self.key)
            sha, path = publication["canonical_commit_sha"], publication["artifact_path"]
            # What the founder can do comes first: the run's summary line is short.
            summary = " ".join(filter(None, (notes.get("notice"), viewers.sentence(notes))))
            await self.db._complete_readonly_report_projection(
                conn,
                execution_key=projection,
                run_id=run.id,
                canonical_commit_sha=sha,
                artifact_path=path,
                artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{path}",
                summary=summary.strip(),
                workflow_key=self.key,
            )

    @activity.defn(name="linkedin_viewers_failure")
    async def viewers_failure(self, argument: str | dict) -> None:
        await self._failure(argument)
