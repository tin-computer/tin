"""Project-owned secrets and bounded HTTPS connections; no authored code runs here."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import re
import socket
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID, uuid4

import httpx

from tin_lite.integrations import (
    IntegrationAuthorizationError,
    IntegrationDefinition,
    IntegrationError,
    IntegrationNotConfiguredError,
    ServiceCallRefused,
    ServiceResponseTooLarge,
)
from tin_lite.usage_capture import borrowed_connection

CUSTOM_KEY = re.compile(r"custom\.api\.[a-z][a-z0-9_]{0,47}\Z")
SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
READ_METHODS = frozenset({"GET"})
CAPABILITIES = ("http.read", "http.write")


def custom_definition(key):
    if not isinstance(key, str) or not CUSTOM_KEY.fullmatch(key):
        raise ValueError("invalid custom API name")
    return IntegrationDefinition(
        key,
        key.removeprefix("custom.api.").replace("_", " ").title(),
        "API",
        "Project API connection. Credentials stay in Tin.",
        "Explicit methods on one HTTPS origin",
        CAPABILITIES,
        ("Private code workflows",),
    )


def configuration(value):
    if not isinstance(value, dict) or set(value) != {
        "origin",
        "auth",
        "header",
        "secret_name",
        "methods",
        "idempotency_header",
    }:
        raise ValueError("invalid custom API configuration")
    origin = value["origin"]
    if not isinstance(origin, str) or len(origin) > 253:
        raise ValueError("use one HTTPS origin")
    parsed = urlsplit(origin)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "." not in host
        or not re.fullmatch(r"[a-zA-Z0-9.-]+", host)
        or host.endswith(".")
    ):
        raise ValueError("use one public HTTPS origin without a path or credentials")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("use a public DNS hostname")
    if value["auth"] not in {"bearer", "header"}:
        raise ValueError("use bearer or named API-key header authentication")
    header = value["header"]
    if value["auth"] == "bearer":
        if header != "Authorization":
            raise ValueError("bearer authentication uses Authorization")
    elif (
        not isinstance(header, str)
        or not re.fullmatch(r"(?i)(?:x-[a-z0-9-]{1,60}|api-key|apikey)", header)
        or header.lower() in {"x-forwarded-host", "x-forwarded-for", "x-http-method-override"}
    ):
        raise ValueError("use a named API-key header")
    if not isinstance(value["secret_name"], str) or not SECRET_NAME.fullmatch(value["secret_name"]):
        raise ValueError("invalid project secret name")
    methods = value["methods"]
    if (
        not isinstance(methods, list)
        or not methods
        or any(not isinstance(method, str) or method not in METHODS for method in methods)
        or len(set(methods)) != len(methods)
    ):
        raise ValueError("select explicit HTTP methods")
    idem = value["idempotency_header"]
    if idem is not None and idem not in {"Idempotency-Key", "X-Idempotency-Key"}:
        raise ValueError("unsupported idempotency header")
    return {**value, "origin": f"https://{host.lower()}", "methods": sorted(methods)}


def secret_context(project_id, name, revision):
    return f"project-secret:{project_id}:{name}:{revision}"


async def rewrap_project_secrets(database, project_id, *, source, target):
    """Operator primitive: drain users, back up both keys, rewrap, then switch keys.

    No product route exposes this. Credential revisions and connection bindings stay
    unchanged; transaction failure leaves every secret on its original key.
    """
    async with database.pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            "SELECT * FROM project_secrets WHERE project_id=$1 FOR UPDATE", project_id
        )
        for row in rows:
            if row["encryption_key_id"] != source.key_id:
                raise IntegrationAuthorizationError("The source encryption key is unavailable.")
            context = secret_context(project_id, row["name"], row["revision"])
            value = source.decrypt(row["ciphertext"], context=context)
            await conn.execute(
                """UPDATE project_secrets SET ciphertext=$3,encryption_key_id=$4
                   WHERE project_id=$1 AND name=$2""",
                project_id,
                row["name"],
                target.encrypt(value, context=context),
                target.key_id,
            )


class ProjectConnections:
    def __init__(self, integrations):
        self.integrations = integrations
        self.db = integrations._database

    def cipher(self):
        cipher = self.integrations._cipher
        if cipher is None:
            raise IntegrationNotConfiguredError("project secret encryption is not configured")
        return cipher

    async def authorize(self, project_id, actor, *, conn=None):
        if not await self.db.has_project_access(
            project_id=project_id, clerk_user_id=actor, conn=conn
        ):
            raise LookupError("project not found")

    async def secrets(self, project_id, actor):
        await self.authorize(project_id, actor)
        rows = await self.db.pool.fetch(
            "SELECT name,revision,encryption_key_id,updated_at FROM project_secrets "
            "WHERE project_id=$1 ORDER BY name",
            project_id,
        )
        return [
            dict(row, revision=str(row["revision"]), updated_at=row["updated_at"].isoformat())
            for row in rows
        ]

    async def save_secrets(self, project_id, actor, entries):
        """Atomic selected import, with compare-and-swap for every replacement."""
        cipher = self.cipher()
        if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
            raise ValueError("select 1-32 secrets")
        prepared, seen = [], set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"name", "value", "expected_revision"}:
                raise ValueError("invalid secret import")
            name, value = entry["name"], entry["value"]
            if not isinstance(name, str) or not SECRET_NAME.fullmatch(name) or name in seen:
                raise ValueError("secret names must be unique uppercase environment names")
            if (
                not isinstance(value, str)
                or not 1 <= len(value.encode()) <= 8000
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
            ):
                raise ValueError(
                    "secret values must be nonempty, single-line and at most 8000 bytes"
                )
            expected = UUID(entry["expected_revision"]) if entry["expected_revision"] else None
            revision = uuid4()
            sealed = cipher.encrypt(value, context=secret_context(project_id, name, revision))
            prepared.append((name, expected, revision, sealed))
            seen.add(name)
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self.authorize(project_id, actor, conn=conn)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"secrets:{project_id}"
            )
            names = await conn.fetch(
                "SELECT name FROM project_secrets WHERE project_id=$1", project_id
            )
            if len({row["name"] for row in names} | seen) > 128:
                raise ValueError("a project supports at most 128 named secrets")
            for name, expected, revision, sealed in prepared:
                current = await conn.fetchval(
                    "SELECT revision FROM project_secrets WHERE project_id=$1 AND name=$2",
                    project_id,
                    name,
                )
                if current != expected:
                    raise IntegrationError(
                        "Secret changed; refresh the names and confirm the replacement."
                    )
                await conn.execute(
                    """INSERT INTO project_secrets
                       (project_id,name,revision,ciphertext,encryption_key_id,updated_by_clerk_user_id)
                       VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (project_id,name) DO UPDATE
                       SET revision=EXCLUDED.revision,ciphertext=EXCLUDED.ciphertext,
                           encryption_key_id=EXCLUDED.encryption_key_id,
                           updated_by_clerk_user_id=EXCLUDED.updated_by_clerk_user_id,updated_at=now()""",
                    project_id,
                    name,
                    revision,
                    sealed,
                    cipher.key_id,
                    actor,
                )
                await conn.execute(
                    """UPDATE integration_connections SET
                       configuration=jsonb_set(configuration,'{access_verified}','false'),
                       status=CASE WHEN last_error_code IN
                         ('authentication_failed','secret_removed')
                         THEN 'connected' ELSE status END,
                       last_error_code=CASE WHEN last_error_code IN
                         ('authentication_failed','secret_removed')
                         THEN NULL ELSE last_error_code END,
                       last_checked_at=NULL WHERE project_id=$1
                       AND provider_key LIKE 'custom.api.%' AND configuration->>'secret_name'=$2""",
                    project_id,
                    name,
                )
        return await self.secrets(project_id, actor)

    async def delete_secret(self, project_id, actor, name, revision):
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self.authorize(project_id, actor, conn=conn)
            result = await conn.execute(
                "DELETE FROM project_secrets WHERE project_id=$1 AND name=$2 AND revision=$3",
                project_id,
                name,
                UUID(revision),
            )
            if result != "DELETE 1":
                raise IntegrationError("Secret changed or was removed; refresh before deleting.")
            await conn.execute(
                """UPDATE integration_connections SET status='needs_attention',
                   last_error_code='secret_removed',last_checked_at=now(),
                   configuration=jsonb_set(configuration,'{access_verified}','false')
                   WHERE project_id=$1 AND provider_key LIKE 'custom.api.%'
                     AND configuration->>'secret_name'=$2""",
                project_id,
                name,
            )

    async def open_secret(self, project_id, name):
        value, _revision = await self.open_secret_record(project_id, name)
        return value

    async def open_secret_record(self, project_id, name):
        row = await (borrowed_connection(self.db) or self.db.pool).fetchrow(
            "SELECT * FROM project_secrets WHERE project_id=$1 AND name=$2", project_id, name
        )
        if row is None:
            raise IntegrationAuthorizationError("The connection needs its project secret.")
        cipher = self.cipher()
        if row["encryption_key_id"] != cipher.key_id:
            raise IntegrationAuthorizationError(
                "The stored secret's encryption key is unavailable."
            )
        value = cipher.decrypt(
            row["ciphertext"], context=secret_context(project_id, name, row["revision"])
        )
        return value, row["revision"]

    async def save(self, project_id, actor, key, config, expected_revision):
        custom_definition(key)
        config = configuration(config)
        await self.authorize(project_id, actor)
        await self.open_secret(project_id, config["secret_name"])
        revision = str(uuid4())
        config = {
            **config,
            "revision": revision,
            "access_verified": False,
            "granted_capabilities": (["http.read"] if "GET" in config["methods"] else [])
            + (["http.write"] if set(config["methods"]) - READ_METHODS else []),
        }
        # Serialize CAS with other setup mutations; never probe a paid API while saving.
        async with self.db.effect_lock(f"connection-setup:{project_id}:{key}", "connection_setup"):
            current = await self.db.get_integration_connection(
                project_id=project_id, provider_key=key
            )
            if (current.configuration.get("revision") if current else None) != expected_revision:
                raise IntegrationError("Connection changed; refresh before replacing it.")
            result = await self.db.upsert_integration_connection(
                project_id=project_id,
                provider_key=key,
                external_account_id=None,
                external_account_label=config["origin"],
                configuration=config,
                credential_ciphertext=None,
                credential_key_version=None,
                connected_by_clerk_user_id=actor,
            )
        return result

    async def ready(self, project_id, requirement):
        connection = await self.db.get_integration_connection(
            project_id=project_id, provider_key=requirement.provider_key
        )
        if (
            connection is None
            or connection.status != "connected"
            or not set(requirement.capabilities)
            <= set(connection.configuration.get("granted_capabilities", []))
        ):
            raise IntegrationAuthorizationError(
                "The custom API connection needs its declared permissions."
            )
        await self.open_secret(project_id, connection.configuration["secret_name"])
        return connection


def request_contract(payload):
    if not isinstance(payload, dict) or set(payload) != {"method", "path", "params", "body"}:
        raise ValueError("invalid API request")
    method, path, params, body = (payload[k] for k in ("method", "path", "params", "body"))
    if not isinstance(method, str) or method not in METHODS:
        raise ValueError("unsupported API method")
    if not isinstance(path, str) or len(path) > 2000 or not path.startswith("/"):
        raise ValueError("use an origin-relative API path")
    decoded = path
    for _ in range(3):
        decoded = unquote(decoded)
    if (
        any(c in decoded for c in "\\?#")
        or decoded.startswith("//")
        or any(ord(c) < 33 or ord(c) == 127 for c in decoded)
        or any(p in {".", ".."} for p in decoded.split("/"))
    ):
        raise ValueError("unsafe API path")
    if (
        not isinstance(params, dict)
        or len(params) > 32
        or any(
            not isinstance(k, str) or len(k) > 100 or not isinstance(v, (str, int, float, bool))
            for k, v in params.items()
        )
    ):
        raise ValueError("use bounded scalar query parameters")
    if method == "GET" and body is not None:
        raise ValueError("GET requests cannot carry a body")
    if len(json.dumps(payload, allow_nan=False).encode()) > 16_000:
        raise ValueError("API request exceeds its bound")
    return payload


class InvalidAPIResponse(ServiceCallRefused):
    """The API answered with a body that is not JSON: a known outcome; the body is withheld."""

    def __init__(self, status):
        super().__init__(
            f"The API answered HTTP {status} with a body that is not JSON; it was withheld.",
            code="invalid_response",
        )
        self.status = status


async def request_api(
    connection, secret, payload, *, maximum, operation_id, client=None, resolver=None
):
    """DNS pin before attaching auth; no redirects, proxy env, cookies or raw headers."""
    payload = request_contract(payload)
    config = connection.configuration
    if payload["method"] not in config["methods"]:
        raise IntegrationAuthorizationError("The connection does not allow this method.")
    host = urlsplit(config["origin"]).hostname
    resolver = resolver or asyncio.get_running_loop().getaddrinfo
    addresses = await resolver(host, 443, type=socket.SOCK_STREAM)
    # Keep the resolver's route preference; lexical sorting can select unreachable IPv6.
    ips = list(dict.fromkeys(row[4][0] for row in addresses))
    if not ips or any(
        not ipaddress.ip_address(ip).is_global
        or ipaddress.ip_address(ip).is_multicast
        or ipaddress.ip_address(ip).is_reserved
        for ip in ips
    ):
        raise IntegrationAuthorizationError("API destination is not a public address.")
    headers = {
        "Host": host,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "Tin-Project-API/1",
        config["header"]: f"Bearer {secret}" if config["auth"] == "bearer" else secret,
    }
    if config["idempotency_header"] and payload["method"] not in READ_METHODS:
        headers[config["idempotency_header"]] = hashlib.sha256(operation_id.encode()).hexdigest()
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15)
    try:
        async with (
            asyncio.timeout(20),
            client.stream(
                payload["method"],
                httpx.URL(config["origin"] + payload["path"]).copy_with(host=ips[0]),
                params=payload["params"],
                json=payload["body"],
                headers=headers,
                extensions={"sni_hostname": host},
                follow_redirects=False,
            ) as response,
        ):
            raw = bytearray()
            # Compressed responses are refused before decompression allocation.
            if response.headers.get("content-encoding", "identity") != "identity":
                raise IntegrationError("API response compression is unsupported.")
            if 300 <= response.status_code < 400:
                raise IntegrationError(
                    "API redirects are not followed; update the approved origin."
                )
            async for chunk in response.aiter_raw():
                raw.extend(chunk)
                if len(raw) > maximum:
                    raise ServiceResponseTooLarge("API response exceeds its declared bound.")
            if not raw.strip():
                # No content, such as a 204 to a DELETE, is an answer with nothing to return.
                return {"status": response.status_code, "data": None}
            try:
                value = json.loads(raw)
                safe = json.dumps(value, ensure_ascii=False, allow_nan=False)
            except (ValueError, UnicodeError, RecursionError):
                raise InvalidAPIResponse(response.status_code) from None
            # Never let an auth echo become model context, a receipt or an artifact.
            for sensitive in {
                secret,
                json.dumps(secret, ensure_ascii=False)[1:-1],
                json.dumps(secret, ensure_ascii=True)[1:-1],
                quote(secret, safe=""),
                base64.b64encode(secret.encode()).decode(),
            }:
                if sensitive in safe:
                    raise IntegrationError(
                        "API response reflected credential material and was withheld."
                    )
            return {"status": response.status_code, "data": value}
    finally:
        if own:
            await client.aclose()
