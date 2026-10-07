"""Postgres collection projection. Every mutation checks membership and lease fencing."""

from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tin_lite.connection_collection import (
    CAPABILITY,
    POLICY,
    PROVIDER,
    TERMINAL,
    Actor,
    CollectionError,
    CollectionInputs,
    PageBatch,
    canonical_json,
    digest,
    failure_transition,
    require_enabled,
)


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def document(value):
    return json.loads(value) if isinstance(value, str) else value


def record(row):
    result = dict(row)
    for key in ("actor", "inputs", "policy", "sources", "coverage", "records", "source"):
        if key in result:
            result[key] = document(result[key])
    return result


class CollectionStore:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    async def access(self, conn, project_id, user):
        require_enabled(self.settings, project_id)
        if not await self.db.has_project_access(
            project_id=project_id, clerk_user_id=user, conn=conn
        ):
            raise CollectionError("project_unavailable")

    async def grant(self, project_id, user):
        async with self.db.pool.acquire() as conn:
            await self.access(conn, project_id, user)
        value = secrets.token_urlsafe(32)
        await self.db.create_integration_auth_attempt(
            token_hash=token_hash(value),
            project_id=project_id,
            provider_key=PROVIDER,
            clerk_user_id=user,
            pkce_verifier_ciphertext=None,
            requested_capabilities=(CAPABILITY,),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        return {"grant": value, "project_id": str(project_id), "expires_in": 300}

    async def pair(self, grant, bearer_hash, actor):
        """The extension generates the bearer; only its hash crosses this one-use exchange.

        Retrying the identical grant/hash recovers a lost acknowledgement. Another device
        cannot replace an account or steal the pairing by replaying a consumed grant.
        """
        import re

        if not isinstance(grant, str) or not 32 <= len(grant) <= 128:
            raise CollectionError("invalid_pairing")
        if not isinstance(bearer_hash, str) or not re.fullmatch("[a-f0-9]{64}", bearer_hash):
            raise CollectionError("invalid_pairing")
        actor = Actor.model_validate(actor)
        hashed = token_hash(grant)
        async with self.db.pool.acquire() as conn, conn.transaction():
            attempt = await conn.fetchrow(
                "SELECT * FROM integration_auth_attempts WHERE token_hash=$1 FOR UPDATE", hashed
            )
            if (
                not attempt
                or attempt["provider_key"] != PROVIDER
                or attempt["expires_at"] <= datetime.now(UTC)
            ):
                raise CollectionError("pairing_expired")
            project_id, user = attempt["project_id"], attempt["clerk_user_id"]
            await self.access(conn, project_id, user)
            previous = await conn.fetchrow(
                "SELECT * FROM connection_extension_devices WHERE pairing_hash=$1", hashed
            )
            if previous:
                if (
                    previous["token_hash"] != bearer_hash
                    or previous["revoked_at"]
                    or document(previous["actor"]) != actor.model_dump()
                ):
                    raise CollectionError("pairing_consumed")
                return {"project_id": str(project_id), "device_id": str(previous["id"])}
            if attempt["used_at"]:
                raise CollectionError("pairing_consumed")
            # Serialize all pairings for the same project before replacing its binding.
            await conn.fetchval("SELECT id FROM projects WHERE id=$1 FOR UPDATE", project_id)
            if await conn.fetchval(
                """SELECT EXISTS(SELECT 1 FROM connection_collection_jobs
                    WHERE project_id=$1 AND state NOT IN
                    ('completed','partial','failed','stopped'))""",
                project_id,
            ):
                raise CollectionError("collection_active")
            connection_id = await conn.fetchval(
                """INSERT INTO integration_connections
                (id,project_id,provider_key,external_account_id,external_account_label,
                 configuration,connected_by_clerk_user_id)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)
                ON CONFLICT (project_id,provider_key) DO UPDATE SET
                    external_account_id=EXCLUDED.external_account_id,
                    external_account_label=EXCLUDED.external_account_label,
                    configuration=EXCLUDED.configuration,status='connected',
                    credential_ciphertext=NULL,credential_key_version=NULL,
                    connected_by_clerk_user_id=EXCLUDED.connected_by_clerk_user_id,
                    updated_at=now() RETURNING id""",
                uuid4(),
                project_id,
                PROVIDER,
                actor.key,
                actor.name,
                canonical_json(
                    {
                        "actor": actor.model_dump(),
                        "granted_capabilities": [CAPABILITY],
                        "cloud_session": False,
                    }
                ),
                user,
            )
            await conn.execute(
                """UPDATE connection_extension_devices SET revoked_at=now()
                WHERE project_id=$1 AND revoked_at IS NULL""",
                project_id,
            )
            device_id = uuid4()
            await conn.execute(
                """INSERT INTO connection_extension_devices
                (id,project_id,connection_id,clerk_user_id,token_hash,actor,pairing_hash,expires_at)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,now()+interval '30 days')""",
                device_id,
                project_id,
                connection_id,
                user,
                bearer_hash,
                canonical_json(actor.model_dump()),
                hashed,
            )
            await conn.execute(
                "UPDATE integration_auth_attempts SET used_at=now() WHERE token_hash=$1", hashed
            )
            return {"project_id": str(project_id), "device_id": str(device_id)}

    async def device(self, conn, bearer, project_id):
        if not isinstance(bearer, str) or not 32 <= len(bearer) <= 128:
            raise CollectionError("device_unavailable")
        row = await conn.fetchrow(
            """SELECT d.* FROM connection_extension_devices d
            JOIN integration_connections c ON c.id=d.connection_id
            WHERE d.token_hash=$1 AND d.project_id=$2 AND d.revoked_at IS NULL
                AND d.expires_at>now() AND c.status='connected' FOR SHARE OF d,c""",
            token_hash(bearer),
            project_id,
        )
        if not row:
            raise CollectionError("device_unavailable")
        await self.access(conn, project_id, row["clerk_user_id"])
        return record(row)

    async def prepare(self, run, policy):
        inputs = CollectionInputs.model_validate({"project_id": str(run.project_id), **run.input})
        if str(run.project_id) != inputs.project_id or policy != POLICY:
            raise CollectionError("unsupported_collection_contract")
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self.access(conn, run.project_id, run.started_by_clerk_user_id)
            existing = await conn.fetchrow(
                "SELECT * FROM connection_collection_jobs WHERE run_id=$1", run.id
            )
            if existing:
                return record(existing)
            connection = await conn.fetchrow(
                """SELECT * FROM integration_connections
                WHERE project_id=$1 AND provider_key=$2 AND status='connected' FOR SHARE""",
                run.project_id,
                PROVIDER,
            )
            if (
                not connection
                or connection["connected_by_clerk_user_id"] != run.started_by_clerk_user_id
            ):
                raise CollectionError("account_owner_required")
            device = await conn.fetchrow(
                """SELECT * FROM connection_extension_devices
                WHERE connection_id=$1 AND revoked_at IS NULL AND expires_at>now()
                ORDER BY created_at DESC LIMIT 1 FOR SHARE""",
                connection["id"],
            )
            if not device:
                raise CollectionError("browser_unavailable")
            actor = document(device["actor"])
            # Actor observations are not proof of ownership of another Tin user's account.
            # Lock this owner's observed account across every project they pair it to.
            account = digest([run.started_by_clerk_user_id, actor["key"]])
            deadline = datetime.now(UTC) + timedelta(seconds=policy["job_seconds"])
            locked = await conn.fetchval(
                """INSERT INTO connection_collection_account_leases
                (account_lock,run_id,expires_at) VALUES ($1,$2,$3)
                ON CONFLICT (account_lock) DO UPDATE SET run_id=$2,expires_at=$3
                WHERE connection_collection_account_leases.expires_at<=now()
                RETURNING run_id""",
                account,
                run.id,
                deadline + timedelta(seconds=120),
            )
            if locked != run.id:
                raise CollectionError("account_busy")
            row = await conn.fetchrow(
                """INSERT INTO connection_collection_jobs
                (run_id,project_id,connection_id,clerk_user_id,actor,account_lock,inputs,policy,
                 session_generation,deadline)
                VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb,$8::jsonb,$9,$10) RETURNING *""",
                run.id,
                run.project_id,
                connection["id"],
                run.started_by_clerk_user_id,
                canonical_json(actor),
                account,
                canonical_json(inputs.model_dump()),
                canonical_json(policy),
                device["session_generation"],
                deadline,
            )
            if inputs.execution != "local_only":
                await conn.execute(
                    "UPDATE connection_collection_jobs SET "
                    "cloud_template=$2,cloud_transport='http_v1' WHERE run_id=$1",
                    run.id,
                    self.settings.linkedin_cloud_template,
                )
            return record(row)

    @asynccontextmanager
    async def locked(self, run_id, project_id=None):
        async with self.db.pool.acquire() as conn, conn.transaction():
            # Same lock order as normal run cancellation; a stopped run never accepts a page.
            run = await conn.fetchrow("SELECT * FROM workflow_runs WHERE id=$1 FOR UPDATE", run_id)
            row = await conn.fetchrow(
                "SELECT * FROM connection_collection_jobs WHERE run_id=$1 FOR UPDATE", run_id
            )
            if not run or not row or (project_id is not None and row["project_id"] != project_id):
                raise CollectionError("collection_unavailable")
            job = record(row)
            await self.access(conn, job["project_id"], job["clerk_user_id"])
            if run["status"] in {"failed", "stopped", "superseded"}:
                raise CollectionError("collection_stopped")
            connection = await conn.fetchval(
                "SELECT id FROM integration_connections WHERE id=$1 AND status='connected'",
                job["connection_id"],
            )
            if not connection and job["state"] not in TERMINAL:
                raise CollectionError("device_unavailable")
            yield conn, job

    async def pending(self, project_id, bearer):
        async with self.db.pool.acquire() as conn, conn.transaction():
            device = await self.device(conn, bearer, project_id)
            rows = await conn.fetch(
                """SELECT j.* FROM connection_collection_jobs j
                JOIN workflow_runs r ON r.id=j.run_id
                WHERE j.project_id=$1 AND j.connection_id=$2 AND j.clerk_user_id=$3
                  AND j.state NOT IN ('completed','partial','failed','stopped')
                  AND r.status IN ('pending','running','needs_input')
                ORDER BY j.started_at LIMIT 1""",
                project_id,
                device["connection_id"],
                device["clerk_user_id"],
            )
            return self.public(record(rows[0])) if rows else None

    def public(self, job):
        return {
            key: str(job[key]) if key in {"run_id", "deadline"} else job[key]
            for key in (
                "run_id",
                "inputs",
                "actor",
                "state",
                "reason",
                "generation",
                "friend_index",
                "next_page",
                "sources",
                "coverage",
                "deadline",
                "execution_mode",
                "cloud_transport",
            )
        }

    def current(self, job):
        if job["state"] in TERMINAL or job["deadline"] <= datetime.now(UTC):
            raise CollectionError("collection_stopped")

    async def claim(self, run_id, project_id, bearer, actor_key):
        async with self.locked(run_id, project_id) as (conn, job):
            device = await self.device(conn, bearer, project_id)
            self.current(job)
            if (
                actor_key != job["actor"]["key"]
                or device["actor"]["key"] != actor_key
                or device["connection_id"] != job["connection_id"]
                or device["clerk_user_id"] != job["clerk_user_id"]
            ):
                raise CollectionError("account_changed")
            if job["state"] not in {"waiting_browser", "collecting", "handoff_pending"}:
                raise CollectionError("collection_paused")
            if job["execution_mode"] == "cloud" and job["state"] == "collecting":
                raise CollectionError("cloud_active")
            if job["execution_mode"] == "cloud" and not job["cloud_cleanup_confirmed"]:
                raise CollectionError("cloud_cleanup_pending")
            if job["lease_expires_at"] and job["lease_expires_at"] > datetime.now(UTC):
                raise CollectionError("lease_busy")
            if job["state"] == "handoff_pending" or job["execution_mode"] == "cloud":
                await conn.execute(
                    "UPDATE connection_collection_jobs SET cloud_transport='local_backup' "
                    "WHERE run_id=$1",
                    run_id,
                )
            lease = secrets.token_urlsafe(32)
            row = await conn.fetchrow(
                """UPDATE connection_collection_jobs SET
                state='collecting',execution_mode='local',generation=generation+1,
                lease_hash=$2,lease_device_id=$3,lease_expires_at=LEAST(deadline,now()+interval
                '90 seconds'),
                updated_at=now() WHERE run_id=$1 RETURNING *""",
                run_id,
                token_hash(lease),
                device["id"],
            )
            return {**self.public(record(row)), "lease": lease}

    def fence(self, job, device, generation, lease, *, cloud=False):
        self.current(job)
        if (
            job["state"] != "collecting"
            or job["execution_mode"] != ("cloud" if cloud else "local")
            or job["lease_device_id"] != device["id"]
            or job["generation"] != generation
            or not secrets.compare_digest(job["lease_hash"] or "", token_hash(lease))
            or not job["lease_expires_at"]
            or job["lease_expires_at"] <= datetime.now(UTC)
        ):
            raise CollectionError("stale_lease")

    async def heartbeat(self, run_id, project_id, bearer, generation, lease):
        async with self.locked(run_id, project_id) as (conn, job):
            device = await self.device(conn, bearer, project_id)
            self.fence(job, device, generation, lease)
            await conn.execute(
                """UPDATE connection_collection_jobs SET
                lease_expires_at=LEAST(deadline,now()+interval '90 seconds'),updated_at=now()
                WHERE run_id=$1""",
                run_id,
            )
            return self.public(job)

    async def page(self, run_id, project_id, bearer, batch, *, _cloud=False):
        batch = PageBatch.model_validate(batch)
        async with self.locked(run_id, project_id) as (conn, job):
            device = {"id": None} if _cloud else await self.device(conn, bearer, project_id)
            if job["state"] in TERMINAL:
                previous = await conn.fetchrow(
                    "SELECT batch_digest FROM connection_collection_pages WHERE run_id=$1 "
                    "AND friend_index=$2 AND page=$3",
                    run_id,
                    batch.friend_index,
                    batch.page,
                )
                if (
                    previous
                    and previous["batch_digest"]
                    == digest(batch.model_dump(exclude={"lease", "generation"}))
                    and device["id"] == job["lease_device_id"]
                    and batch.generation == job["generation"]
                    and secrets.compare_digest(job["lease_hash"] or "", token_hash(batch.lease))
                ):
                    return self.public(job)
                raise CollectionError("collection_stopped")
            self.fence(job, device, batch.generation, batch.lease, cloud=_cloud)
            batch.source.scope(keywords=job["inputs"]["keywords"])
            if batch.actor_key != job["actor"]["key"]:
                raise CollectionError("account_changed")
            friends = job["inputs"]["friends"]
            if (
                batch.friend_index >= len(friends)
                or friends[batch.friend_index] != batch.source.friend_url
            ):
                raise CollectionError("friend_changed")
            body = batch.model_dump(exclude={"lease", "generation"})
            stamp = digest(body)
            previous = await conn.fetchrow(
                """SELECT batch_digest FROM connection_collection_pages
                WHERE run_id=$1 AND friend_index=$2 AND page=$3""",
                run_id,
                batch.friend_index,
                batch.page,
            )
            if previous:
                if previous["batch_digest"] != stamp:
                    raise CollectionError("page_changed")
                return self.public(job)
            if batch.friend_index != job["friend_index"] or batch.page != job["next_page"]:
                raise CollectionError("page_out_of_order")
            from urllib.parse import parse_qs, urlsplit

            if parse_qs(urlsplit(batch.source.collection_url).query).get("page", ["1"]) != [
                str(batch.page)
            ]:
                raise CollectionError("page_out_of_order")
            if datetime.now(UTC) >= job["friend_started_at"] + timedelta(
                seconds=job["policy"]["seconds_per_friend"]
            ):
                raise CollectionError("friend_time_limit")
            # A profile cannot silently resolve to another connectionOf id after a restart.
            old_source = job["sources"].get(str(batch.friend_index))
            if old_source:
                from tin_lite.connection_collection import CollectionSource

                if CollectionSource.model_validate(old_source).scope(
                    keywords=job["inputs"]["keywords"]
                ) != batch.source.scope(keywords=job["inputs"]["keywords"]):
                    raise CollectionError("friend_changed")
            pages = await conn.fetch(
                """SELECT fingerprint,records FROM connection_collection_pages
                WHERE run_id=$1 AND friend_index=$2 ORDER BY page""",
                run_id,
                batch.friend_index,
            )
            if batch.people and any(p["fingerprint"] == batch.fingerprint() for p in pages):
                raise CollectionError("repeated_page")
            seen = {person["profile_url"] for p in pages for person in document(p["records"])}
            count = len(seen | {p.profile_url for p in batch.people})
            if count > job["policy"]["people_per_friend"]:
                raise CollectionError("people_limit")
            await conn.execute(
                """INSERT INTO connection_collection_pages
                (run_id,friend_index,page,generation,batch_digest,fingerprint,source,records,next_page,observed_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9,$10)""",
                run_id,
                batch.friend_index,
                batch.page,
                batch.generation,
                stamp,
                batch.fingerprint(),
                canonical_json(batch.source.model_dump()),
                canonical_json([p.model_dump() for p in batch.people]),
                batch.next_page,
                datetime.fromisoformat(batch.observed_at.replace("Z", "+00:00")),
            )
            sources = {**job["sources"], str(batch.friend_index): batch.source.model_dump()}
            reason = None
            if not batch.next_page:
                reason = "visible_results_exhausted"
            elif count >= job["policy"]["people_per_friend"]:
                reason = "people_limit"
            elif batch.page >= job["policy"]["pages_per_friend"]:
                reason = "page_limit"
            coverage = dict(job["coverage"])
            index, page, state = batch.friend_index, batch.page + 1, "collecting"
            if reason:
                coverage[str(index)] = {"reason": reason, "pages": batch.page, "people": count}
                index, page = index + 1, 1
                if index == len(friends):
                    state = (
                        "completed"
                        if all(
                            v["reason"] == "visible_results_exhausted" for v in coverage.values()
                        )
                        else "partial"
                    )
            row = await conn.fetchrow(
                """UPDATE connection_collection_jobs SET state=$2,
                sources=$3::jsonb,coverage=$4::jsonb,friend_index=$5,next_page=$6,
                friend_started_at=CASE WHEN friend_index<>$5 THEN now() ELSE friend_started_at END,
                updated_at=now() WHERE run_id=$1 RETURNING *""",
                run_id,
                state,
                canonical_json(sources),
                canonical_json(coverage),
                index,
                page,
            )
            return self.public(record(row))

    async def stop_attempt(self, run_id, project_id, bearer, generation, lease, reason):
        allowed = {
            "account_changed",
            "challenge",
            "rate_limited",
            "access_denied",
            "session_expired",
            "browser_unavailable",
            "filters_changed",
            "page_changed",
            "repeated_page",
            "unsupported_layout",
            "user_stopped",
            "people_limit",
            "friend_time_limit",
        }
        if reason not in allowed:
            raise CollectionError("invalid_stop_reason")
        async with self.locked(run_id, project_id) as (conn, job):
            device = await self.device(conn, bearer, project_id)
            self.fence(job, device, generation, lease)
            state = failure_transition(job["inputs"]["execution"], "local", reason)
            if reason in {"people_limit", "friend_time_limit", "user_stopped", "repeated_page"}:
                state = "partial"
            await conn.execute(
                """UPDATE connection_collection_jobs SET state=$2,reason=$3,
                generation=generation+1,lease_hash=NULL,lease_expires_at=NULL,updated_at=now()
                WHERE run_id=$1""",
                run_id,
                state,
                reason,
            )
            return {"state": state, "reason": reason}

    async def resume(self, run_id, project_id, user):
        async with self.locked(run_id, project_id) as (conn, job):
            await self.access(conn, project_id, user)
            self.current(job)
            if user != job["clerk_user_id"] or job["state"] != "paused":
                raise CollectionError("collection_unavailable")
            if job["execution_mode"] == "cloud" and job["inputs"]["execution"] == "cloud_only":
                raise CollectionError("cloud_only_paused")
            # A human retry does not reset the cumulative deadline or accepted pages.
            await conn.execute(
                """UPDATE connection_collection_jobs SET state='waiting_browser',
                reason=NULL,generation=generation+1,lease_hash=NULL,lease_expires_at=NULL
                WHERE run_id=$1""",
                run_id,
            )

    async def cloud_session(self, run_id, project_id, bearer, generation, lease, session, cipher):
        from tin_lite.connection_collection import cloud_ready
        from tin_lite.linkedin_session import validate_session

        if not cloud_ready(self.settings) or cipher is None:
            raise CollectionError("cloud_unavailable")
        envelope = validate_session(session)
        async with self.locked(run_id, project_id) as (conn, job):
            device = await self.device(conn, bearer, project_id)
            self.fence(job, device, generation, lease)
            if job["inputs"]["execution"] == "local_only" or job["cloud_transport"] != "http_v1":
                raise CollectionError("cloud_unavailable")
            value = {
                "version": 1,
                "session": envelope,
                "run_id": str(run_id),
                "expires_at": job["deadline"].isoformat(),
            }
            encrypted = cipher.encrypt(
                canonical_json(value), context=f"credential:{project_id}:network.linkedin"
            )
            await conn.execute(
                """UPDATE integration_connections SET credential_ciphertext=$2,
                credential_key_version=$3,
                configuration=jsonb_set(configuration,'{cloud_run_id}',to_jsonb($4::text)),
                updated_at=now() WHERE id=$1""",
                job["connection_id"],
                encrypted,
                cipher.version,
                str(run_id),
            )
            return {"transferred": True}

    async def cloud_source(self, run_id, project_id, bearer, generation, lease, source):
        from tin_lite.connection_collection import CollectionSource

        source = CollectionSource.model_validate(source)
        async with self.locked(run_id, project_id) as (conn, job):
            device = await self.device(conn, bearer, project_id)
            if (
                device["connection_id"] == job["connection_id"]
                and device["clerk_user_id"] == job["clerk_user_id"]
                and source.model_dump() in job["sources"].values()
            ):
                return self.public(job)
            self.fence(job, device, generation, lease)
            source.scope(keywords=job["inputs"]["keywords"])
            index = job["friend_index"]
            if (
                job["cloud_transport"] != "http_v1"
                or not source.query_id
                or source.actor.key != job["actor"]["key"]
                or source.friend_url != job["inputs"]["friends"][index]
            ):
                raise CollectionError("unsupported_search_contract")
            if not await conn.fetchval(
                "SELECT credential_ciphertext IS NOT NULL FROM integration_connections WHERE id=$1",
                job["connection_id"],
            ):
                raise CollectionError("session_expired")
            sources = {**job["sources"], str(index): source.model_dump()}
            complete = index + 1 == len(job["inputs"]["friends"])
            row = await conn.fetchrow(
                """UPDATE connection_collection_jobs SET sources=$2::jsonb,
                friend_index=$3,next_page=1,friend_started_at=now(),
                state=CASE WHEN $4 THEN 'cloud_ready' ELSE state END,
                generation=generation+CASE WHEN $4 THEN 1 ELSE 0 END,
                lease_hash=CASE WHEN $4 THEN NULL ELSE lease_hash END,
                lease_expires_at=CASE WHEN $4 THEN NULL ELSE lease_expires_at END
                WHERE run_id=$1 RETURNING *""",
                run_id,
                canonical_json(sources),
                0 if complete else index + 1,
                complete,
            )
            return self.public(record(row))

    async def resume_device(self, run_id, project_id, bearer, actor_key):
        async with self.db.pool.acquire() as conn, conn.transaction():
            device = await self.device(conn, bearer, project_id)
            if device["actor"]["key"] != actor_key:
                raise CollectionError("account_changed")
        await self.resume(run_id, project_id, device["clerk_user_id"])
        return {"resumed": True}

    async def cancel_device(self, run_id, project_id, bearer):
        async with self.locked(run_id, project_id) as (conn, job):
            device = await self.device(conn, bearer, project_id)
            if (
                device["clerk_user_id"] != job["clerk_user_id"]
                or device["connection_id"] != job["connection_id"]
            ):
                raise CollectionError("collection_unavailable")
            if job["state"] not in TERMINAL:
                await conn.execute(
                    """UPDATE connection_collection_jobs SET state='partial',
                    reason='user_stopped',generation=generation+1,lease_hash=NULL,lease_expires_at=NULL
                    WHERE run_id=$1""",
                    run_id,
                )
            return {"state": "partial"}
