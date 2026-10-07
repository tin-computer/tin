"""Identifier-only durable orchestration for extension collection."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite.connection_collection import KEY, TERMINAL, CollectionError, canonical_json
from tin_lite.connection_collection_store import CollectionStore, record
from tin_lite.organic_audit_publication import publish_artifacts
from tin_lite.workflow_packages import load_workflow_source


def artifacts(job, pages):
    """Deduplicate people, preserve every observed friend path, and bound each JSON read."""
    root = f"connections/{job['run_id']}"
    people, paths = {}, []
    for page in pages:
        source = page["source"]
        for person in page["records"]:
            people.setdefault(person["profile_url"], person)
            paths.append(
                {
                    "person_profile_url": person["profile_url"],
                    "friend_profile_url": source["friend_url"],
                    "friend_name": source["friend_name"],
                    "page": page["page"],
                    "observed_at": page["observed_at"].isoformat(),
                    "degree": "2nd",
                }
            )
    documents = {}
    chunks = []
    for label, rows in [("people", list(people.values())), ("paths", paths)]:
        chunk, number = [], 1
        for row in rows:
            if len(canonical_json([*chunk, row]).encode()) > 60_000 and chunk:
                path = f"{root}/{label}-{number:03}.json"
                documents[path] = canonical_json(chunk).encode()
                chunks.append(path)
                chunk, number = [], number + 1
            chunk.append(row)
        path = f"{root}/{label}-{number:03}.json"
        documents[path] = canonical_json(chunk).encode()
        chunks.append(path)
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(
        [
            "name",
            "profile_url",
            "headline",
            "location",
            "friend_name",
            "friend_profile_url",
            "degree",
            "source_page",
            "observed_at",
        ]
    )

    def safe(value):
        text = str(value)
        return (
            "'" + text
            if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n"))
            else text
        )

    for path in paths:
        p = people[path["person_profile_url"]]
        writer.writerow(
            [
                safe(value)
                for value in (
                    p["name"],
                    p["profile_url"],
                    p["headline"],
                    p["location"],
                    path["friend_name"],
                    path["friend_profile_url"],
                    "2nd",
                    path["page"],
                    path["observed_at"],
                )
            ]
        )
    documents[f"{root}/connections.csv"] = out.getvalue().encode()
    documents[f"{root}/manifest.json"] = canonical_json(
        {
            "schema": "tin.connections.v1",
            "run_id": str(job["run_id"]),
            "status": job["state"],
            "reason": job["reason"],
            "coverage": job["coverage"],
            "scope": {
                "friends": job["inputs"]["friends"],
                "keywords": job["inputs"]["keywords"],
                "degree": "2nd",
                "evidence": "visible_results_only",
            },
            "limits": job["policy"],
            "people_count": len(people),
            "path_count": len(paths),
            "chunks": chunks,
            "csv": f"{root}/connections.csv",
        }
    ).encode()
    return documents


class CollectionActivities:
    def __init__(self, *, database, storage, settings, cloud=None, cipher=None):
        self.cloud, self.cipher = cloud, cipher
        self.db, self.storage = database, storage
        self.store = CollectionStore(database, settings)

    async def run(self, run_id):
        run = await self.db.get_run(UUID(run_id))
        if (
            not run
            or run.executor != KEY
            or run.status.value in {"failed", "stopped", "superseded"}
        ):
            raise CollectionError("collection_stopped")
        return run

    @activity.defn(name="collection_prepare")
    async def prepare(self, run_id: str):
        try:
            run = await self.run(run_id)
            workflow = await self.db.get_workflow(run.workflow_id)
            source = await load_workflow_source(
                storage=self.storage,
                repo_id=workflow.definition_repo_id,
                commit_sha=run.definition_commit_sha,
                definition_path=workflow.definition_path,
            )
            if source.definition.get("executor") != KEY:
                raise CollectionError("unsupported_collection_contract")
            await self.store.prepare(run, source.definition.get("collection_policy"))
            await self.db.mark_run_running(run.id)
        except CollectionError as exc:
            raise ApplicationError(exc.code, non_retryable=True) from None

    @activity.defn(name="collection_poll")
    async def poll(self, run_id: str) -> bool:
        run = await self.run(run_id)
        await self.store.activate_cloud(run.id)
        pending = await self.db.pool.fetchrow(
            "SELECT * FROM connection_collection_jobs WHERE run_id=$1", run.id
        )
        if pending and (
            pending["state"] == "cloud_ready"
            or (pending["state"] == "collecting" and pending["execution_mode"] == "cloud")
        ):
            from tin_lite.connection_collection_cloud import step

            await step(self.store, self.cloud, self.cipher, run.id)
        async with self.store.locked(run.id) as (conn, job):
            if job["deadline"] <= datetime.now(UTC) and job["state"] not in TERMINAL:
                row = await conn.fetchrow(
                    """UPDATE connection_collection_jobs SET state='partial',
                    reason=CASE WHEN waiting_deadline IS NOT NULL AND collection_started_at IS NULL
                    THEN 'browser_wait_expired' ELSE 'time_limit' END,
                    generation=generation+1,lease_hash=NULL,lease_expires_at=NULL
                    WHERE run_id=$1 RETURNING *""",
                    run.id,
                )
                job = record(row)
            if job["state"] in TERMINAL:
                return True
            count = await conn.fetchval(
                "SELECT count(*) FROM connection_collection_pages WHERE run_id=$1", run.id
            )
        summary = {
            "waiting_browser": "Waiting for your browser. Open Chrome with LinkedIn signed in.",
            "paused": "Collection paused. Check LinkedIn in Integrations.",
            "handoff_pending": "Waiting to continue in Chrome. Your saved pages are kept.",
        }.get(job["state"], f"Collecting connections. {count} pages saved.")
        if job["state"] == "waiting_browser" and job["reason"] == "cloud_unavailable":
            summary = (
                "Cloud collection is unavailable. Tin will collect in Chrome; "
                "keep Chrome open and awake."
            )
        reasons = {
            "local_permission_required": "Allow browser backup in LinkedIn settings to continue.",
            "cloud_permission_required": "Allow cloud collection in LinkedIn settings.",
            "browser_preparation_required": "Open Chrome so Tin can prepare this LinkedIn search.",
            "session_expired": "Reconnect LinkedIn in Integrations to continue.",
            "account_changed": "The LinkedIn account changed. Check the account in Integrations.",
            "challenge": "LinkedIn needs your attention. Open LinkedIn in Chrome.",
            "rate_limited": "LinkedIn asked us to wait. Saved pages are kept.",
            "access_denied": "LinkedIn did not allow this collection. Saved pages are kept.",
        }
        summary = reasons.get(job["reason"], summary)
        await self.db.project_run_progress(
            run_id=run.id, mode="indeterminate", step="collect", summary=summary
        )
        return False

    @activity.defn(name="collection_publish")
    async def publish(self, run_id: str):
        run = await self.run(run_id)
        if run.status.value == "succeeded":
            return
        async with self.store.locked(run.id) as (conn, job):
            if job["state"] not in TERMINAL:
                raise CollectionError("collection_active")
            pages = [
                record(row)
                for row in await conn.fetch(
                    "SELECT * FROM connection_collection_pages WHERE run_id=$1 ORDER BY "
                    "friend_index,page",
                    run.id,
                )
            ]
        documents = artifacts(job, pages)
        project = await self.db.get_project(run.project_id)
        key = f"collection:{run.id}:publish"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                revision = receipt.result["canonical_commit_sha"]
            else:
                await self.db.start_effect(conn, execution_key=key, operation=KEY)

                async def save_intent(intent):
                    await self.db.save_publication_intent(conn, execution_key=key, intent=intent)

                async def validate_active():
                    current = await self.db.get_run(run.id, conn=conn)
                    await self.store.access(conn, run.project_id, run.started_by_clerk_user_id)
                    if current.status.value not in {"pending", "running"}:
                        raise CollectionError("collection_stopped")

                async with self.db.project_state_lock(conn, project.id):
                    revision = await publish_artifacts(
                        storage=self.storage,
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        documents=documents,
                        paths={p: p for p in documents},
                        limits={p: 8_000_000 if p.endswith(".csv") else 64_000 for p in documents},
                        message=f"{KEY} {run.id} [{key}]",
                        intent=(receipt.result or {}).get("publication") if receipt else None,
                        save_intent=save_intent,
                        validate_active=validate_active,
                    )
                await self.db.complete_effect(
                    conn, execution_key=key, result={"canonical_commit_sha": revision}
                )
        if job["state"] == "failed":
            # Keep the diagnostic and any accepted rows discoverable without reporting
            # a failed collection as a successful run.
            await self.db.pool.execute(
                """UPDATE workflow_runs SET canonical_commit_sha=$2,artifact_ref=$2,
                artifact_path=$3 WHERE id=$1 AND status IN ('pending','running')""",
                run.id,
                revision,
                f"connections/{run.id}/manifest.json",
            )
            await self.db.project_failure(
                run_id=run.id,
                error_message=(
                    "Connection collection failed. Saved results include its coverage and reason."
                ),
            )
            await self.release(run.id)
            raise ApplicationError("collection_failed", non_retryable=True)
        await self.db.project_success(
            run_id=run.id,
            canonical_commit_sha=revision,
            artifact_ref=revision,
            artifact_path=f"connections/{run.id}/manifest.json",
        )
        await self.release(run.id)

    @activity.defn(name="collection_failure")
    async def failure(self, run_id: str):
        identifier = UUID(run_id)
        await self.db.pool.execute(
            """UPDATE connection_collection_jobs SET state='failed',
            generation=generation+1,lease_hash=NULL,lease_expires_at=NULL,reason='collection_failed'
            WHERE run_id=$1 AND state NOT IN ('completed','partial','stopped')""",
            identifier,
        )
        await self.release(identifier)
        await self.db.project_failure(
            run_id=identifier,
            error_message="Connection collection could not finish. Accepted pages were retained.",
        )

    async def release(self, run_id):
        job = await self.db.pool.fetchrow(
            "SELECT * FROM connection_collection_jobs WHERE run_id=$1", run_id
        )
        clean = not job or job["cloud_deadline"] is None or job["cloud_cleanup_confirmed"]
        if not clean and job["cloud_sandbox_id"] and self.cloud:
            try:
                import asyncio

                async with asyncio.timeout(20):
                    await self.cloud.cleanup(job["cloud_sandbox_id"])
                clean = True
            except Exception:
                # Keep the account locked while cleanup remains unconfirmed.
                clean = False
        if clean:
            await self.db.pool.execute(
                "DELETE FROM connection_collection_account_leases WHERE run_id=$1", run_id
            )
        await self.db.pool.execute(
            """UPDATE integration_connections SET credential_ciphertext=NULL,
            credential_key_version=NULL,configuration=configuration-'cloud_run_id'
            WHERE provider_key='network.linkedin' AND configuration->>'cloud_run_id'=$1""",
            str(run_id),
        )
