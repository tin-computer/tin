"""X OAuth lifecycle against disposable Postgres and a synthetic X transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_integrations import USER_ID, settings
from test_procedure_publication import publication_db as publication_db
from test_x_connection import X

from tin_lite.integrations import X_PROVIDER, IntegrationAuthorizationError, IntegrationService


async def authorize(integrations, wire, project_id, capabilities=None):
    start = await integrations.start_connect(
        project_id=project_id,
        provider_key=X_PROVIDER,
        clerk_user_id=USER_ID,
        capabilities=capabilities,
    )
    query = parse_qs(urlsplit(start.authorization_url).query)
    wire.scopes = query["scope"][0]
    wire.challenge = query["code_challenge"][0]
    return await integrations.x.complete(
        state=query["state"][0], code="synthetic-code", clerk_user_id=USER_ID
    )


@pytest.mark.asyncio
async def test_real_postgres_x_oauth_upgrade_rotation_and_disconnect(publication_db):
    db = publication_db
    project = await db.create_project(name="X connection", state_repo_id=f"projects/{uuid4()}")
    await db.grant_project_membership(project_id=project.id, clerk_user_id=USER_ID)
    wire = X()
    integrations = IntegrationService(
        database=db,
        settings=settings(
            x_oauth_client_id="synthetic-client",
            x_oauth_client_secret=SecretStr("synthetic-secret"),
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
    )
    try:
        first = await authorize(integrations, wire, project.id)
        assert first.external_account_id == "12345"
        assert first.configuration["protected"] is False
        assert first.configuration["granted_capabilities"] == ["x.posts.read"]
        assert b"access1" not in first.credential_ciphertext
        assert (await integrations.x.timeline(first, limit=10))["posts"][0]["id"] == "100"
        with pytest.raises(IntegrationAuthorizationError):
            await integrations.x.connection(project.id, "x.posts.publish")

        upgraded = await authorize(
            integrations,
            wire,
            project.id,
            capabilities=("x.posts.publish", "x.media.upload"),
        )
        assert upgraded.id == first.id
        assert set(upgraded.configuration["granted_capabilities"]) == {
            "x.posts.read",
            "x.posts.publish",
            "x.media.upload",
        }
        row = await db.pool.fetchrow(
            "SELECT provider_key, external_account_id, credential_ciphertext "
            "FROM integration_connections WHERE project_id=$1",
            project.id,
        )
        assert row["provider_key"] == X_PROVIDER and row["external_account_id"] == "12345"
        assert b"refresh1" not in row["credential_ciphertext"]

        tokens = {"access_token": "access1", "refresh_token": "refresh1", "expires_at": 0}
        ciphertext = integrations._cipher.encrypt(
            json.dumps(tokens), context=f"credential:{project.id}:{X_PROVIDER}"
        )
        assert await db.update_integration_credential(
            project_id=project.id,
            provider_key=X_PROVIDER,
            connection_id=upgraded.id,
            credential_ciphertext=ciphertext,
            credential_key_version=integrations._cipher.version,
        )
        current = await integrations.x.connection(project.id, "x.posts.publish")
        assert await asyncio.gather(
            integrations.x.access_token(current), integrations.x.access_token(current)
        ) == ["access2", "access2"]
        assert wire.refreshes == 1
        rotated = await db.get_integration_connection(
            project_id=project.id, provider_key=X_PROVIDER
        )
        plaintext = integrations._cipher.decrypt(
            rotated.credential_ciphertext,
            context=f"credential:{project.id}:{X_PROVIDER}",
        )
        assert json.loads(plaintext)["refresh_token"] == "refresh2"  # noqa: S105
        assert await integrations.disconnect(project_id=project.id, provider_key=X_PROVIDER)
        assert (
            await db.get_integration_connection(project_id=project.id, provider_key=X_PROVIDER)
            is None
        )
        assert any(call.url.path == "/2/oauth2/revoke" for call in wire.calls)
        attempts = await db.pool.fetchval(
            "SELECT count(*) FROM integration_auth_attempts "
            "WHERE provider_key=$1 AND project_id=$2",
            X_PROVIDER,
            project.id,
        )
        assert attempts == 2
        assert (
            len(
                {
                    hashlib.sha256(call.content).hexdigest()
                    for call in wire.calls
                    if call.url.path == "/2/oauth2/token"
                }
            )
            >= 2
        )
    finally:
        await integrations.close()
