"""Activities for `social.linkedin_connections`.

Reads run in the LinkedIn sandbox with the founder's saved session, each under an effect
receipt, as for engagers: a read's answer stays in its receipt only until its people are
written, then the receipt keeps counts and where the next read starts. An interrupted read is
never silently bought again. Temporal sees read numbers and counts.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from temporalio import activity

from tin_lite import linkedin_connections_export as export
from tin_lite import people_records as records
from tin_lite.linkedin_cloud import ENGAGEMENT_OPERATION
from tin_lite.linkedin_connections import (
    PAGES_PER_READ,
    PROFILES_PER_READ,
    ConnectionsResult,
    PositionsResult,
)
from tin_lite.linkedin_engagers_activities import (
    SAYS,
    STOPS,
    LinkedInEngagersActivities,
    _heartbeat,
    refusal,
)
from tin_lite.organic_audit_publication import publish_artifacts

STEPS = 3
FALLBACK = "Tin couldn't finish reading your LinkedIn connections. Try again later."
# What a read hands Temporal: counts, never the people.
RETURNED = ("outcome", "reason", "people", "new", "unnamed", "pages", "more", "read", "found")
# A profile read waits up to three minutes on the sandbox; the activity heartbeats meanwhile,
# well inside the workflow's two-minute heartbeat timeout.
BEAT_SECONDS = 30


def _returned(result: dict) -> dict:
    return {name: result.get(name) for name in RETURNED}


class LinkedInConnectionsActivities(LinkedInEngagersActivities):
    key = export.KEY
    prefix = "connections"
    fallback = FALLBACK

    async def _read_beating(self, run, key: str, packet: dict, model):
        """A receipted sandbox read that heartbeats while it waits, so a paced read longer than
        the heartbeat timeout isn't cancelled and then refused as interrupted."""

        async def beat():
            while True:
                await asyncio.sleep(BEAT_SECONDS)
                _heartbeat({"waiting": packet["kind"]})

        beating = asyncio.create_task(beat())
        try:
            return await self._read(run, key, packet, model)
        finally:
            beating.cancel()

    @activity.defn(name="linkedin_connections_prepare")
    async def connections_prepare(self, run_id: str) -> None:
        run = await self._active(run_id)
        await self._session(run)
        async with self.db.pool.acquire() as conn:
            await records.ensure_fields(conn, run.project_id)
        await self.db.mark_run_running(run.id)
        await self._progress(run.id, "connections", 0, STEPS, "Reading your LinkedIn connections")

    @activity.defn(name="linkedin_connections_read")
    async def connections_read(self, control: dict) -> dict:
        """One read of up to ten pages of the member's connections, from where the last read
        stopped."""

        run = await self._active(control["run_id"])
        index = int(control["read"])
        key = f"{run.id}:connections:read:{index}"
        start = 0
        if index:
            previous = await self.db.get_effect(f"{run.id}:connections:read:{index - 1}")
            start = ((previous.result or {}) if previous else {}).get("next")
            if start is None:
                return _returned({"outcome": "read", "people": 0, "new": 0, "more": False})
        parsed, reason = await self._read_beating(
            run,
            key,
            {"kind": "linkedin_connections", "start": start, "pages": PAGES_PER_READ},
            ConnectionsResult,
        )
        if reason == "done":
            return _returned(parsed)
        if reason:
            # Nothing read yet: the founder can act on why, or try again later.
            if index == 0:
                raise refusal(reason)
            outcome = "stopped" if reason in STOPS else "unread"
            return _returned({"outcome": outcome, "reason": reason, "people": 0, "new": 0})
        async with self.db.effect_lock(key, ENGAGEMENT_OPERATION) as (conn, _receipt):
            async with conn.transaction():
                counts = await export.record_connections(
                    conn,
                    project_id=run.project_id,
                    run_id=run.id,
                    member=run.started_by_clerk_user_id,
                    connections=[item.model_dump() for item in parsed.connections],
                )
                result = {
                    "outcome": "read",
                    "reason": None,
                    "people": counts["people"],
                    "new": counts["new"],
                    "unnamed": parsed.unnamed,
                    "pages": parsed.pages,
                    "more": parsed.next is not None,
                    # The next read starts where LinkedIn's list left off.
                    "next": parsed.next,
                }
                # The answer leaves the receipt with this; only counts and the offset stay.
                await self.db.complete_effect(conn, execution_key=key, result=result)
        _heartbeat({"read": index})
        return _returned(result)

    @activity.defn(name="linkedin_connections_positions")
    async def connections_positions(self, control: dict) -> dict:
        """Read the current company and position of up to 20 connections, never-read first."""

        run = await self._active(control["run_id"])
        index = int(control["read"])
        key = f"{run.id}:connections:positions:{index}"
        members: list[str] = []
        if await self.db.get_effect(key) is None:
            async with self.db.pool.acquire() as conn:
                members = await export.need_positions(
                    conn,
                    run.project_id,
                    run.started_by_clerk_user_id,
                    limit=PROFILES_PER_READ,
                )
            if not members:
                return _returned({"outcome": "read", "read": 0, "found": 0, "more": False})
        if index == 0:
            await self._progress(
                run.id, "positions", 1, STEPS, "Reading your connections' current positions"
            )
        parsed, reason = await self._read_beating(
            run, key, {"kind": "linkedin_positions", "members": members}, PositionsResult
        )
        if reason == "done":
            return _returned(parsed)
        if reason:
            # The list is still written; the next run reads these people again.
            outcome = "stopped" if reason in STOPS else "unread"
            return _returned({"outcome": outcome, "reason": reason, "read": 0, "found": 0})
        async with self.db.effect_lock(key, ENGAGEMENT_OPERATION) as (conn, _receipt):
            async with conn.transaction():
                found = await export.record_positions(
                    conn,
                    project_id=run.project_id,
                    run_id=run.id,
                    people=[person.model_dump() for person in parsed.people],
                )
                result = {
                    "outcome": "read",
                    "read": sum(person.found for person in parsed.people),
                    "found": found,
                    "more": len(parsed.people) == PROFILES_PER_READ,
                }
                await self.db.complete_effect(conn, execution_key=key, result=result)
        _heartbeat({"positions": index})
        return _returned(result)

    @activity.defn(name="linkedin_connections_publish")
    async def connections_publish(self, control: dict) -> None:
        run = await self._active(control["run_id"])
        notes = dict(control["notes"])
        if notes.get("stopped"):
            notes["notice"] = SAYS.get(notes["stopped"], SAYS["access_denied"])
        await self._progress(run.id, "publish", 2, STEPS, "Writing the list")
        project = await self.db.get_project(run.project_id)
        key = f"{run.id}:connections:files"
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
                        documents = export.render(
                            await export.connections(
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
                        paths=export.PATHS,
                        limits=export.LIMITS,
                        message=f"{self.key} {run.id} [{key}]",
                        intent=intent,
                        save_intent=save_intent,
                        validate_active=validate_active,
                        # Tin's own list: each run replaces the last one.
                        replaceable=frozenset(export.PATHS.values()),
                    )
                publication = {
                    "canonical_commit_sha": revision,
                    "artifact_path": export.CSV_PATH,
                }
                await self.db.complete_effect(conn, execution_key=key, result=publication)
        projection = f"{run.id}:connections:projection"
        async with self.db.effect_lock(projection, self.key) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=projection, operation=self.key)
            sha, path = publication["canonical_commit_sha"], publication["artifact_path"]
            summary = " ".join(filter(None, (notes.get("notice"), export.sentence(notes))))
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

    @activity.defn(name="linkedin_connections_failure")
    async def connections_failure(self, argument: str | dict) -> None:
        await self._failure(argument)
