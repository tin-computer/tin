"""Cloud attempt and local handoff, under the same pinned collection contract."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime

from tin_lite.connection_collection import (
    CollectionError,
    cloud_ready,
    failure_transition,
    view_url,
)
from tin_lite.connection_collection_store import record, token_hash


async def _step(store, runtime, cipher, run_id):
    lease = secrets.token_urlsafe(32)
    async with store.locked(run_id) as (conn, job):
        store.current(job)
        if job["state"] != "cloud_ready" and not (
            job["state"] == "collecting" and job["execution_mode"] == "cloud"
        ):
            return
        if not cloud_ready(store.settings) or cipher is None:
            raise CollectionError("cloud_unavailable")
        connection = await conn.fetchrow(
            "SELECT * FROM integration_connections WHERE id=$1 FOR SHARE", job["connection_id"]
        )
        if not connection or not connection["credential_ciphertext"]:
            raise CollectionError("session_expired")
        sealed = json.loads(
            cipher.decrypt(
                connection["credential_ciphertext"],
                context=f"credential:{job['project_id']}:network.linkedin",
            )
        )
        if (
            sealed.get("version") != 1
            or sealed.get("run_id") != str(run_id)
            or datetime.fromisoformat(sealed["expires_at"]) <= datetime.now(UTC)
        ):
            raise CollectionError("session_expired")
        row = await conn.fetchrow(
            """UPDATE connection_collection_jobs SET state='collecting',
            execution_mode='cloud',generation=generation+1,lease_hash=$2,lease_device_id=NULL,
            lease_expires_at=LEAST(deadline,now()+interval '90 seconds') WHERE run_id=$1
            RETURNING *""",
            run_id,
            token_hash(lease),
        )
        job = record(row)
    try:
        source = dict(job["sources"][str(job["friend_index"])])
        source["collection_url"] = view_url(source["collection_url"], job["next_page"])
        response = await runtime.page(job, sealed["session"], source)
        if response.get("error"):
            raise CollectionError(response["error"])
        await store.page(
            run_id,
            job["project_id"],
            None,
            {
                "generation": job["generation"],
                "lease": lease,
                "friend_index": job["friend_index"],
                "page": job["next_page"],
                "actor_key": job["actor"]["key"],
                "source": source,
                "people": response["people"],
                "next_page": response["next_page"],
                "observed_at": response["observed_at"],
            },
            _cloud=True,
        )
    except CollectionError:
        raise


async def step(store, runtime, cipher, run_id):
    try:
        await _step(store, runtime, cipher, run_id)
    except CollectionError as exc:
        async with store.locked(run_id) as (conn, current):
            if current["state"] in {"completed", "partial", "failed", "stopped"}:
                return
            # An uncertain cleanup never dispatches a local attempt.
            if exc.code == "cloud_cleanup_pending" or (
                current["cloud_deadline"] is not None and not current["cloud_cleanup_confirmed"]
            ):
                state, reason = "paused", "cloud_cleanup_pending"
            else:
                reason = (
                    exc.code
                    if exc.code
                    in {
                        "session_expired",
                        "challenge",
                        "rate_limited",
                        "account_changed",
                        "access_denied",
                    }
                    else "cloud_failed"
                )
                state = failure_transition(current["inputs"]["execution"], "cloud", reason)
            await conn.execute(
                """UPDATE connection_collection_jobs SET state=$2,reason=$3,
                cloud_cleanup_confirmed=CASE WHEN cloud_deadline IS NULL THEN true ELSE
                cloud_cleanup_confirmed END,
                generation=generation+1,lease_hash=NULL,lease_expires_at=NULL WHERE run_id=$1""",
                run_id,
                state,
                reason,
            )
