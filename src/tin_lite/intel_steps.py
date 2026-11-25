"""The step skeleton intel.world's and intel.daily's activities share.

Each workflow keeps its own activity names and its own steps; this base holds what both did the
same way, so it can't drift apart again:

- `_active`, `_progress` and `_fetch`: the run must still be active, progress goes to the run,
  and the fetch row must exist;
- `beating`: heartbeat while a long read runs, the first beat at once;
- `_record_unfinished`: sources a fetch group never recorded become failed with a named
  reason, in the fetch step's `except` and again in the load step;
- `_publish`: one commit to the project's Files under `intel:<run>:publish`, the documents
  kept on the receipt before publishing so a retry publishes exactly what it meant to;
- `_finish_projection`: the run's result from that receipt, under `intel:<run>:projection`;
- `_fail_fetch`: the fetch row marked failed without hiding the run's own failure.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import intel_store
from tin_lite.organic_audit_publication import publish_artifacts

HEARTBEAT_SECONDS = 20


def heartbeat(detail: Any = None) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


@contextlib.asynccontextmanager
async def beating(detail: Any):
    """Heartbeat while a long read runs: one paced source can take minutes."""

    async def beat():
        while True:
            heartbeat(detail)
            await asyncio.sleep(HEARTBEAT_SECONDS)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class IntelActivities:
    """A base for one intel workflow's activities. Subclasses set KEY, STEPS and the sentences
    a run shows when it is no longer active, was never prepared or never published."""

    KEY: str = ""
    STEPS: int = 1
    INACTIVE = "This run is no longer active."
    NOT_PREPARED = "The day was never prepared; start it again."
    NOT_PUBLISHED = "The day was never published."

    def __init__(self, *, database, storage, settings, integrations=None):
        # intel_judge reads .db .storage .settings .integrations.
        self.db, self.storage = database, storage
        self.settings, self.integrations = settings, integrations

    async def _active(self, run_id, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or run.executor != self.KEY or run.status.value not in {"pending", "running"}:
            raise ApplicationError(self.INACTIVE, non_retryable=True)
        return run

    async def _progress(self, run, step: str, current: int, summary: str) -> None:
        await self.db.project_run_progress(
            run_id=run.id,
            mode="steps",
            step=step,
            current=current,
            total=self.STEPS,
            summary=summary,
        )
        heartbeat({"step": step})

    async def _fetch(self, run) -> dict:
        fetch = await intel_store.get_fetch(self.db.pool, run.id)
        if fetch is None:
            raise ApplicationError(self.NOT_PREPARED, non_retryable=True)
        return fetch

    async def _record_unfinished(self, run, sources: list[dict], reason: str) -> int:
        return await intel_store.record_unfinished(
            self.db.pool, run_id=run.id, sources=sources, reason=reason
        )

    async def _publish(
        self,
        run,
        project,
        *,
        paths: dict[str, str],
        limits: dict[str, int],
        artifact: str,
        documents: Callable[[], Awaitable[dict]],
    ) -> dict:
        """Publish the run's documents in one commit, once. `documents()` returns
        {"documents": {name: text}, "summary": str | None, ...extra} and is called only until
        the receipt keeps its answer; `artifact` names the run's result among `paths`. Returns
        the receipt's result: the commit, the artifact path, the summary and the extra keys."""
        key = f"intel:{run.id}:publish"
        async with self.db.effect_lock(key, self.KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result or {}
            if receipt is None:
                await self.db.start_effect(conn, execution_key=key, operation=self.KEY)
            saved = (receipt.result or {}) if receipt else {}
            if not isinstance(saved.get("documents"), dict):
                # Kept before publishing, so a retry publishes exactly what it meant to.
                saved = {**saved, **(await documents())}
                await self.db.save_effect_progress(conn, execution_key=key, result=saved)
            kept = saved["documents"]

            async def save_intent(value):
                await self.db.save_publication_intent(conn, execution_key=key, intent=value)

            async def validate_active():
                await self._active(run.id, conn=conn)

            async with self.db.project_state_lock(conn, project.id):
                revision = await publish_artifacts(
                    storage=self.storage,
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    documents={path: kept[name].encode() for name, path in paths.items()},
                    paths=paths,
                    limits=limits,
                    message=f"{self.KEY} {run.id} [{key}]",
                    intent=saved.get("publication"),
                    save_intent=save_intent,
                    validate_active=validate_active,
                    # A second run on the same day replaces the day's files; history keeps both.
                    replaceable=frozenset(paths.values()),
                )
            result = {
                key_: value
                for key_, value in saved.items()
                if key_ not in {"documents", "publication"}
            }
            result.update(canonical_commit_sha=revision, artifact_path=paths[artifact])
            await self.db.complete_effect(conn, execution_key=key, result=result)
        return result

    async def _finish_projection(self, run, *, summary_suffix: str = "") -> None:
        """The run succeeds with its published artifact as its result, once."""
        published = await self.db.get_effect(f"intel:{run.id}:publish")
        if not published or published.status != "completed":
            raise ApplicationError(self.NOT_PUBLISHED, non_retryable=True)
        result = published.result
        project = await self.db.get_project(run.project_id)
        projection = f"intel:{run.id}:projection"
        async with self.db.effect_lock(projection, self.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            if saved is None:
                await self.db.start_effect(conn, execution_key=projection, operation=self.KEY)
            sha = result["canonical_commit_sha"]
            summary = (result.get("summary") or "") + summary_suffix
            await self.db._complete_readonly_report_projection(
                conn,
                execution_key=projection,
                run_id=run.id,
                canonical_commit_sha=sha,
                artifact_path=result["artifact_path"],
                artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{result['artifact_path']}",
                summary=summary or None,
                workflow_key=self.KEY,
            )

    async def _fail_fetch(self, run) -> dict | None:
        """Mark the run's fetch failed; returns the fetch row. Never hides the run's failure."""
        with contextlib.suppress(Exception):
            fetch = await intel_store.get_fetch(self.db.pool, run.id)
            if fetch is not None:
                await intel_store.set_fetch(self.db.pool, run.id, status="failed")
            return fetch
        return None
