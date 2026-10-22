"""Continuing, project-bound collection permission and encrypted session lifecycle."""

import json
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


async def default_inputs(database, project_id, inputs, *, member):
    """Apply the member's own saved choice only to new configurations with no explicit mode."""
    if "execution" in inputs:
        return inputs
    configuration = await database.pool.fetchval(
        "SELECT configuration FROM integration_connections WHERE project_id=$1 "
        "AND provider_key='network.linkedin' AND connected_by_clerk_user_id=$2",
        project_id,
        member,
    )
    mode = permission(configuration).get("mode") if configuration else None
    return {**inputs, "execution": mode} if mode in MODES else inputs


def renamed_actor(actor: dict, key: str) -> dict:
    """The account as Tin for Chrome now names it: a profile link, or the profile photo's marker
    when the page shows no link. The display name stays."""

    url = key if key.startswith("https://www.linkedin.com/in/") else None
    return {**actor, "key": key, "profile_url": url}


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
            # The member ID recorded with the last saved session, when there is one. Tin for
            # Chrome names the account by its profile link or photo, which can change; the ID
            # can't, so a new link or photo with the same ID is still this member.
            known = self._known_member(project_id, row)
            renamed = (
                value["actor_key"] != device["actor"]["key"]
                or value["actor_key"] != grant["actor"]["key"]
            )
            if renamed and not known:
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
            actor = (
                renamed_actor(device["actor"], value["actor_key"]) if renamed else device["actor"]
            )
            member = await self.verify(
                session, {**actor, "member": known} if known else device["actor"]
            )
            if renamed:
                # The same member under a new link or photo: everything that names the account
                # follows it.
                await conn.execute(
                    "UPDATE connection_extension_devices SET actor=$2::jsonb WHERE id=$1",
                    device["id"],
                    canonical_json(actor),
                )
                grant["actor"] = actor
                config["collection_permission"] = grant
                if isinstance(config.get("actor"), dict):
                    config["actor"] = {**config["actor"], **actor}
            if member:
                actor = {**actor, "member": member}
            generation = str(uuid4())
            sealed = {
                "version": 2,
                "generation": generation,
                "session": session,
                "actor": actor,
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

    def _known_member(self, project_id, row) -> str | None:
        """The member ID sealed with the connection's last saved session, if any."""

        if not row["credential_ciphertext"]:
            return None
        try:
            sealed = json.loads(
                self.cipher.decrypt(
                    row["credential_ciphertext"],
                    context=f"credential:{project_id}:network.linkedin",
                )
            )
        except Exception:  # noqa: BLE001 — an unreadable old credential names no member
            return None
        member = (sealed.get("actor") or {}).get("member") if isinstance(sealed, dict) else None
        return member if isinstance(member, str) and member else None

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
            # Each member sets the choice on their own LinkedIn connection.
            row = await conn.fetchrow(
                "SELECT * FROM integration_connections WHERE project_id=$1 AND "
                "provider_key='network.linkedin' AND connected_by_clerk_user_id=$2 FOR UPDATE",
                project_id,
                user,
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
