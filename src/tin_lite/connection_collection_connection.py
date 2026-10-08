"""Continuing, project-bound collection permission and encrypted session lifecycle."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tin_lite.connection_collection import (
    MODES,
    Actor,
    CollectionError,
    canonical_json,
    cloud_ready,
)
from tin_lite.connection_collection_store import document

CONSENT_VERSION = 1
SESSION_DAYS = 7


def permission(configuration):
    value = document(configuration).get("collection_permission", {})
    return value if value.get("version") == CONSENT_VERSION else {}


def setup_choice(value):
    if not isinstance(value, dict) or set(value) != {"actor", "mode", "consent_version"}:
        raise CollectionError("invalid_setup")
    if (
        type(value["consent_version"]) is not int
        or value["consent_version"] != CONSENT_VERSION
        or value["mode"] not in MODES
    ):
        raise CollectionError("invalid_setup")
    return {
        "actor": Actor.model_validate(value["actor"]).model_dump(),
        "mode": value["mode"],
        "version": CONSENT_VERSION,
        "confirmed_at": datetime.now(UTC).isoformat(),
    }


def usable(configuration, now=None):
    config = document(configuration)
    grant = permission(config)
    if not grant or grant["mode"] == "local_only" or config.get("session_state") != "available":
        return False
    try:
        return datetime.fromisoformat(config["session_expires_at"]) > (now or datetime.now(UTC))
    except (KeyError, TypeError, ValueError):
        return False


def status(configuration, settings):
    config = document(configuration)
    grant = permission(config)
    return {
        "permission": grant,
        "cloud_available": cloud_ready(settings),
        "session_available": usable(config),
        "session_expires_at": config.get("session_expires_at"),
        "session_generation": config.get("session_generation"),
        "session_state": config.get("session_state", "missing"),
    }


async def default_inputs(database, project_id, inputs):
    """Apply the saved choice only to new configurations with no explicit mode."""
    if "execution" in inputs:
        return inputs
    connection = await database.get_integration_connection(
        project_id=project_id, provider_key="network.linkedin"
    )
    mode = permission(connection.configuration).get("mode") if connection else None
    return {**inputs, "execution": mode} if mode in MODES else inputs


class CollectionConnection:
    def __init__(self, store, cipher=None, *, verify=None):
        from tin_lite.linkedin_session import check_account

        self.verify = verify or check_account
        self.store, self.db, self.settings, self.cipher = store, store.db, store.settings, cipher

    async def device_status(self, project_id, bearer):
        async with self.db.pool.acquire() as conn, conn.transaction():
            device = await self.store.device(conn, bearer, project_id)
            config = await conn.fetchval(
                "SELECT configuration FROM integration_connections WHERE id=$1",
                device["connection_id"],
            )
            await conn.execute(
                "UPDATE connection_extension_devices SET last_seen_at=now() WHERE id=$1",
                device["id"],
            )
            return status(config, self.settings)

    async def save_session(self, project_id, bearer, value):
        from tin_lite.linkedin_session import validate_session

        if not cloud_ready(self.settings) or self.cipher is None:
            raise CollectionError("cloud_unavailable")
        if not isinstance(value, dict) or set(value) != {
            "actor_key",
            "session",
            "query_id",
            "expected_generation",
        }:
            raise CollectionError("invalid_session")
        from tin_lite.connection_collection import CollectionSource

        query_id = CollectionSource.query_valid(value["query_id"])
        session = validate_session(value["session"])
        now = datetime.now(UTC)
        expiry = now + timedelta(days=SESSION_DAYS)
        for cookie in value["session"]["cookies"]:
            if cookie["name"] == "li_at":
                stamp = cookie.get("expiration_date")
                if type(stamp) not in {int, float}:
                    # Session cookies have no declared lifetime; retain for one day at most.
                    expiry = min(expiry, now + timedelta(days=1))
                else:
                    try:
                        expiry = min(expiry, datetime.fromtimestamp(stamp, UTC))
                    except (ValueError, OverflowError, OSError):
                        raise CollectionError("invalid_session") from None
        if expiry <= now + timedelta(minutes=1):
            raise CollectionError("session_expired")
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.fetchval("SELECT id FROM projects WHERE id=$1 FOR UPDATE", project_id)
            device = await self.store.device(conn, bearer, project_id)
            expiry = min(expiry, device["expires_at"])
            row = await conn.fetchrow(
                "SELECT * FROM integration_connections WHERE id=$1 FOR UPDATE",
                device["connection_id"],
            )
            config = document(row["configuration"])
            grant = permission(config)
            if not grant or grant["mode"] == "local_only":
                raise CollectionError("cloud_permission_required")
            if (
                value["actor_key"] != device["actor"]["key"]
                or value["actor_key"] != grant["actor"]["key"]
            ):
                raise CollectionError("account_changed")
            if value["expected_generation"] != config.get("session_generation"):
                raise CollectionError("session_superseded")
            # A running attempt pins its exact credential. Refresh only between attempts.
            if await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM connection_collection_jobs WHERE connection_id=$1 "
                "AND state NOT IN ('completed','partial','failed','stopped') "
                "AND (lease_expires_at>now() OR (cloud_deadline IS NOT NULL "
                "AND NOT cloud_cleanup_confirmed)))",
                row["id"],
            ):
                raise CollectionError("collection_active")
            # This fixed read checks the transferred login, not a submitted account label.
            # Keep the connection lock until storage commits so replacement cannot race it.
            await self.verify(session, device["actor"])
            generation = str(uuid4())
            sealed = {
                "version": 2,
                "generation": generation,
                "session": session,
                "actor": device["actor"],
                "device_id": str(device["id"]),
                "query_id": query_id,
                "expires_at": expiry.isoformat(),
            }
            config.update(
                {
                    "session_generation": generation,
                    "session_expires_at": expiry.isoformat(),
                    "session_state": "available",
                    "session_refreshed_at": now.isoformat(),
                    "session_verified_at": datetime.now(UTC).isoformat(),
                }
            )
            config.pop("cloud_run_id", None)
            await conn.execute(
                "UPDATE integration_connections SET "
                "credential_ciphertext=$2,credential_key_version=$3,"
                "configuration=$4::jsonb,updated_at=now() WHERE id=$1",
                row["id"],
                self.cipher.encrypt(
                    canonical_json(sealed), context=f"credential:{project_id}:network.linkedin"
                ),
                self.cipher.version,
                canonical_json(config),
            )
            return status(config, self.settings)

    async def preferences(self, project_id, user, value):
        choice = setup_choice(value)
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self.store.access(conn, project_id, user)
            await conn.fetchval("SELECT id FROM projects WHERE id=$1 FOR UPDATE", project_id)
            # Match run operations' job-before-connection lock order when revoking.
            await conn.fetch(
                "SELECT run_id FROM connection_collection_jobs WHERE project_id=$1 "
                "AND state NOT IN ('completed','partial','failed','stopped') "
                "ORDER BY run_id FOR UPDATE",
                project_id,
            )
            row = await conn.fetchrow(
                "SELECT * FROM integration_connections WHERE project_id=$1 AND "
                "provider_key='network.linkedin' FOR UPDATE",
                project_id,
            )
            if not row or row["connected_by_clerk_user_id"] != user or row["status"] != "connected":
                raise CollectionError("account_owner_required")
            config = document(row["configuration"])
            if config.get("actor", {}).get("key") != choice["actor"]["key"]:
                raise CollectionError("account_changed")
            choice["actor"] = config["actor"]
            config["collection_permission"] = choice
            revoke = choice["mode"] == "local_only"
            if revoke:
                for key in (
                    "session_generation",
                    "session_expires_at",
                    "session_refreshed_at",
                    "session_verified_at",
                    "cloud_run_id",
                ):
                    config.pop(key, None)
                config["session_state"] = "missing"
            await conn.execute(
                "UPDATE integration_connections SET configuration=$2::jsonb,"
                "credential_ciphertext=CASE WHEN $3 THEN NULL ELSE credential_ciphertext END,"
                "credential_key_version=CASE WHEN $3 THEN NULL ELSE "
                "credential_key_version END,updated_at=now() WHERE id=$1",
                row["id"],
                canonical_json(config),
                revoke,
            )
            if revoke:
                await conn.execute(
                    "UPDATE connection_collection_jobs SET "
                    "state='paused',reason='cloud_permission_required',"
                    "generation=generation+1,lease_hash=NULL,lease_expires_at=NULL "
                    "WHERE connection_id=$1 AND cloud_transport IN ('http_v1','http_v2') "
                    "AND state NOT IN ('completed','partial','failed','stopped')",
                    row["id"],
                )
            return status(config, self.settings)


async def purge_sessions(database):
    """Erase expired or unauthorized credentials even when no collection is running."""
    await database.pool.execute(
        """UPDATE integration_connections c SET credential_ciphertext=NULL,
        credential_key_version=NULL,
        configuration=jsonb_set(configuration,'{session_state}','"reconnect"'::jsonb)
        WHERE c.provider_key='network.linkedin' AND c.credential_ciphertext IS NOT NULL
        AND c.configuration->'collection_permission'->>'version'='1'
        AND ((c.configuration->>'session_expires_at')::timestamptz<=now()
          OR c.configuration->'collection_permission'->>'mode'='local_only'
          OR NOT EXISTS (SELECT 1 FROM project_memberships m WHERE m.project_id=c.project_id
                         AND m.clerk_user_id=c.connected_by_clerk_user_id)
          OR NOT EXISTS (SELECT 1 FROM connection_extension_devices d WHERE d.connection_id=c.id
                         AND d.revoked_at IS NULL AND d.expires_at>now()))"""
    )


async def session_cleanup_loop(database):
    import asyncio
    import logging

    while True:
        try:
            await purge_sessions(database)
        except Exception:
            logging.getLogger(__name__).warning("Connection session cleanup will retry.")
        await asyncio.sleep(60)
