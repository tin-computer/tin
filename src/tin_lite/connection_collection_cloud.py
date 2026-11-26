"""Cloud attempt and local handoff, under the same pinned collection contract."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from uuid import UUID

from tin_lite.connection_collection import (
    CollectionError,
    cloud_ready,
    failure_transition,
    view_url,
)
from tin_lite.connection_collection_connection import permission
from tin_lite.connection_collection_store import document, record, token_hash

logger = logging.getLogger(__name__)


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
        config = document(connection["configuration"])
        if datetime.fromisoformat(sealed["expires_at"]) <= datetime.now(UTC):
            raise CollectionError("session_expired")
        if job["cloud_transport"] == "http_v2":
            grant = permission(config)
            if not grant or grant["mode"] == "local_only":
                raise CollectionError("cloud_permission_required")
            # The sealed account also carries the member ID Tin recorded (`member`); the job
            # pinned the paired device's account, which names it without the ID.
            named = {k: v for k, v in (sealed.get("actor") or {}).items() if k != "member"}
            if sealed.get("version") != 2 or named != job["actor"]:
                raise CollectionError("account_changed")
            if (
                sealed.get("generation") != job["credential_generation"]
                or config.get("session_generation") != job["credential_generation"]
            ):
                raise CollectionError("session_superseded")
            if not await conn.fetchval(
                "SELECT id FROM connection_extension_devices WHERE id=$1 AND "
                "connection_id=$2 AND revoked_at IS NULL AND expires_at>now()",
                UUID(sealed["device_id"]),
                job["connection_id"],
            ):
                raise CollectionError("session_expired")
        elif sealed.get("version") != 1 or sealed.get("run_id") != str(run_id):
            raise CollectionError("session_expired")
        job = await store.start_clock(conn, job)
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
        source = job["sources"].get(str(job["friend_index"]))
        if source:
            source = {
                **source,
                "collection_url": view_url(source["collection_url"], job["next_page"]),
            }
        elif not job["inputs"]["friends"]:
            # The whole network: the first page resolves the location and pins the search.
            if job["cloud_transport"] != "http_v2":
                raise CollectionError("cloud_permission_required")
            source = {"actor": job["actor"], "location": job["inputs"].get("location", "")}
        else:
            source = {
                "friend_url": job["inputs"]["friends"][job["friend_index"]],
                "actor": job["actor"],
                "query_id": sealed.get("query_id"),
            }
            if not source["query_id"] and job["cloud_transport"] != "http_v2":
                raise CollectionError("browser_preparation_required")
        response = await runtime.page(job, sealed["session"], source)
        if response.get("error"):
            raise CollectionError(response["error"], response.get("detail"))
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
                "source": response.get("source", source),
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
            # One friend Tin can't search never holds up the others: a friend who isn't the
            # member's connection, or one the cloud couldn't prepare while Chrome is closed, is
            # left out with its reason and the run goes on.
            if (
                exc.code in {"friend_not_connected", "browser_preparation_required"}
                and store.preparing(current)
                and (current["cloud_deadline"] is None or current["cloud_cleanup_confirmed"])
                and (
                    exc.code == "friend_not_connected"
                    or not await store.chrome_online(conn, current)
                )
            ):
                await store.skip_friend(
                    conn,
                    current,
                    "friend_not_connected"
                    if exc.code == "friend_not_connected"
                    else "friend_not_prepared",
                    detail=exc.detail,
                )
                return
            if exc.code == "browser_preparation_required" and exc.detail:
                # Chrome may still prepare this friend; keep which check the cloud missed.
                logger.warning(
                    "Cloud could not prepare friend %s of run %s: %s",
                    current["friend_index"],
                    run_id,
                    exc.detail,
                )
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
                        "cloud_permission_required",
                        "session_superseded",
                        "browser_preparation_required",
                        "friend_not_connected",
                        "filters_changed",
                        "location_not_found",
                    }
                    else "cloud_failed"
                )
                state = failure_transition(current["inputs"]["execution"], "cloud", reason)
                if reason == "browser_preparation_required":
                    state = "waiting_browser"
                if reason in {
                    "cloud_permission_required",
                    "session_superseded",
                    "friend_not_connected",
                    "filters_changed",
                }:
                    state = "paused"
                config = document(
                    await conn.fetchval(
                        "SELECT configuration FROM integration_connections WHERE id=$1",
                        current["connection_id"],
                    )
                )
                if state == "handoff_pending" and permission(config).get("mode") == "cloud_only":
                    state = "paused"
                if not current["inputs"]["friends"] and state in {
                    "handoff_pending",
                    "waiting_browser",
                }:
                    # Chrome can't search the whole network: the member resumes it in the cloud.
                    state = "paused"
                if reason in {"session_expired", "account_changed", "challenge"} and current.get(
                    "credential_generation"
                ):
                    await conn.execute(
                        "UPDATE integration_connections SET "
                        "credential_ciphertext=NULL,credential_key_version=NULL,configuration=jsonb_set(configuration,'{session_state}','\"reconnect\"'::jsonb)"
                        " WHERE id=$1 AND configuration->>'session_generation'=$2",
                        current["connection_id"],
                        current["credential_generation"],
                    )
            await conn.execute(
                """UPDATE connection_collection_jobs SET state=$2,reason=$3,
                cloud_cleanup_confirmed=CASE WHEN cloud_deadline IS NULL THEN true ELSE
                cloud_cleanup_confirmed END,
                generation=generation+1,lease_hash=NULL,lease_expires_at=NULL WHERE run_id=$1""",
                run_id,
                state,
                reason,
            )
