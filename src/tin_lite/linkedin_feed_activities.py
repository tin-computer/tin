"""Activities for `social.linkedin_feed`.

Reads run in the LinkedIn sandbox with the member's saved session, each under an effect
receipt, as for engagers: a read's answer stays in its receipt only until its posts are
written, then the receipt keeps counts and where the next read starts. An interrupted read is
never silently bought again. Temporal sees read numbers and counts.
"""

from __future__ import annotations

from datetime import UTC, datetime

from temporalio import activity

from tin_lite import linkedin_feed_store as store
from tin_lite.linkedin_cloud import ENGAGEMENT_OPERATION
from tin_lite.linkedin_connections_activities import LinkedInConnectionsActivities
from tin_lite.linkedin_engagers_activities import SAYS, STOPS, _heartbeat, refusal
from tin_lite.linkedin_feed import FEED_PAGES_PER_READ, DetailsResult, FeedResult
from tin_lite.organic_audit_publication import publish_artifacts

STEPS = 3
FALLBACK = "Tin couldn't finish reading your LinkedIn feed. Try again later."
# What a read hands Temporal: counts, never the posts.
RETURNED = ("outcome", "reason", "posts", "new", "skipped", "pages", "detailed", "more")


def _returned(result: dict) -> dict:
    return {name: result.get(name) for name in RETURNED}


class LinkedInFeedActivities(LinkedInConnectionsActivities):
    key = store.KEY
    prefix = "feed"
    fallback = FALLBACK

    @activity.defn(name="linkedin_feed_prepare")
    async def feed_prepare(self, run_id: str) -> None:
        run = await self._active(run_id)
        await self._session(run)
        await self.db.mark_run_running(run.id)
        await self._progress(run.id, "feed", 0, STEPS, "Reading your LinkedIn feed")

    @activity.defn(name="linkedin_feed_read")
    async def feed_read(self, control: dict) -> dict:
        """One read of up to three feed pages, from where the last read stopped, until the
        run's pages are read or the feed ends."""

        run = await self._active(control["run_id"])
        index = int(control["read"])
        pages, _reactors = store.options(run.input)
        left = pages - index * FEED_PAGES_PER_READ
        if left <= 0:
            return _returned({"outcome": "read", "posts": 0, "new": 0, "more": False})
        key = f"{run.id}:feed:read:{index}"
        start, token = 0, None
        if index:
            previous = await self.db.get_effect(f"{run.id}:feed:read:{index - 1}")
            saved = (previous.result or {}) if previous else {}
            start, token = saved.get("next_start"), saved.get("token")
            if start is None:
                return _returned({"outcome": "read", "posts": 0, "new": 0, "more": False})
        parsed, reason = await self._read_beating(
            run,
            key,
            {
                "kind": "linkedin_feed",
                "start": start,
                "token": token,
                "pages": min(left, FEED_PAGES_PER_READ),
            },
            FeedResult,
        )
        if reason == "done":
            return _returned(parsed)
        if reason:
            # Nothing read yet: the founder can act on why, or try again later.
            if index == 0:
                raise refusal(reason)
            outcome = "stopped" if reason in STOPS else "unread"
            return _returned({"outcome": outcome, "reason": reason, "posts": 0, "new": 0})
        async with self.db.effect_lock(key, ENGAGEMENT_OPERATION) as (conn, _receipt):
            async with conn.transaction():
                counts = await store.record_posts(
                    conn,
                    project_id=run.project_id,
                    run_id=run.id,
                    member=run.started_by_clerk_user_id,
                    posts=[post.model_dump() for post in parsed.posts],
                )
                more = parsed.next_start is not None and left > parsed.pages
                result = {
                    "outcome": "read",
                    "reason": None,
                    **counts,
                    "skipped": parsed.skipped,
                    "pages": parsed.pages,
                    "more": more,
                    # The next read carries on where LinkedIn's feed left off.
                    "next_start": parsed.next_start if more else None,
                    "token": parsed.token if more else None,
                }
                # The answer leaves the receipt with this; only counts and the place stay.
                await self.db.complete_effect(conn, execution_key=key, result=result)
        _heartbeat({"read": index})
        return _returned(result)

    @activity.defn(name="linkedin_feed_details")
    async def feed_details(self, control: dict) -> dict:
        """The first comments and reactors of the next posts this run found that Tin hasn't
        read before, as many as one sandbox read takes."""

        run = await self._active(control["run_id"])
        index = int(control["read"])
        key = f"{run.id}:feed:details:{index}"
        _pages, reactors = store.options(run.input)
        posts: list[dict] = []
        if await self.db.get_effect(key) is None:
            async with self.db.pool.acquire() as conn:
                posts = await store.need_details(
                    conn, project_id=run.project_id, run_id=run.id, reactors=reactors
                )
            if not posts:
                return _returned({"outcome": "read", "detailed": 0, "more": False})
        if index == 0:
            await self._progress(
                run.id, "details", 1, STEPS, "Reading comments and reactions on new posts"
            )
        parsed, reason = await self._read_beating(
            run, key, {"kind": "linkedin_feed_details", "posts": posts}, DetailsResult
        )
        if reason == "done":
            return _returned(parsed)
        if reason:
            # The posts are kept; their comments and reactors wait for a later run.
            outcome = "stopped" if reason in STOPS else "unread"
            return _returned({"outcome": outcome, "reason": reason, "detailed": 0})
        async with self.db.effect_lock(key, ENGAGEMENT_OPERATION) as (conn, _receipt):
            async with conn.transaction():
                detailed = await store.record_details(
                    conn,
                    project_id=run.project_id,
                    details=[item.model_dump() for item in parsed.posts],
                )
                more = await store.details_left(conn, project_id=run.project_id, run_id=run.id)
                result = {"outcome": "read", "detailed": detailed, "more": more}
                await self.db.complete_effect(conn, execution_key=key, result=result)
        _heartbeat({"details": index})
        return _returned(result)

    @activity.defn(name="linkedin_feed_publish")
    async def feed_publish(self, control: dict) -> None:
        run = await self._active(control["run_id"])
        notes = dict(control["notes"])
        if notes.get("stopped"):
            notes["notice"] = SAYS.get(notes["stopped"], SAYS["access_denied"])
        await self._progress(run.id, "publish", 2, STEPS, "Writing the feed to Files")
        project = await self.db.get_project(run.project_id)
        key = f"{run.id}:feed:files"
        async with self.db.effect_lock(key, self.key) as (conn, receipt):
            if receipt and receipt.status == "completed":
                publication = receipt.result
            else:
                async with self.db.project_state_lock(conn, project.id):
                    saved = (receipt.result or {}) if receipt else {}
                    documents, named = saved.get("documents"), saved.get("paths")
                    intent = saved.get("publication")
                    if not isinstance(documents, dict) or not isinstance(named, dict):
                        if receipt is None:
                            await self.db.start_effect(conn, execution_key=key, operation=self.key)
                        now = datetime.now(UTC)
                        named = store.paths(run.id, now)
                        rendered = store.render(
                            await store.run_posts(conn, project_id=run.project_id, run_id=run.id),
                            run_id=run.id,
                            notes=notes,
                            now=now,
                        )
                        documents = {named[name]: text for name, text in rendered.items()}
                        await self.db.save_effect_progress(
                            conn,
                            execution_key=key,
                            result={"documents": documents, "paths": named},
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
                        paths=named,
                        limits=store.LIMITS,
                        message=f"{self.key} {run.id} [{key}]",
                        intent=intent,
                        save_intent=save_intent,
                        validate_active=validate_active,
                    )
                publication = {"canonical_commit_sha": revision, "artifact_path": named["feed.md"]}
                await self.db.complete_effect(conn, execution_key=key, result=publication)
        projection = f"{run.id}:feed:projection"
        async with self.db.effect_lock(projection, self.key) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=projection, operation=self.key)
            sha, path = publication["canonical_commit_sha"], publication["artifact_path"]
            summary = " ".join(filter(None, (notes.get("notice"), store.sentence(notes))))
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

    @activity.defn(name="linkedin_feed_failure")
    async def feed_failure(self, argument: str | dict) -> None:
        await self._failure(argument)
