"""Fixture-only X OAuth, token rotation, bounded reads, and publication outcomes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from test_integrations import PROJECT_ID, USER_ID, FakeIntegrationDatabase, settings

from tin_lite.integrations import (
    X_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationDeliveryUnknownError,
    IntegrationInputError,
    IntegrationRateLimitedError,
    IntegrationService,
    IntegrationUpstreamError,
)
from tin_lite.x_connection import MAX_IMAGE_BYTES


class Database(FakeIntegrationDatabase):
    def __init__(self):
        super().__init__()
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def integration_refresh_lock(self, **values):
        del values
        async with self.lock:
            yield


class X:
    def __init__(self):
        self.calls = []
        self.scopes = ""
        self.challenge = ""
        self.refreshes = 0
        self.post_status = 201
        self.post_timeout = False
        self.reject_access1 = False
        self.timeline_status = 200
        self.account = "12345"
        self.protected = False

    @staticmethod
    def reply(status: int, body: dict, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(
            status, stream=httpx.ByteStream(json.dumps(body).encode()), headers=headers
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/2/oauth2/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form["grant_type"] == "authorization_code":
                verifier_hash = hashlib.sha256(form["code_verifier"].encode()).digest()
                assert (
                    base64.urlsafe_b64encode(verifier_hash).rstrip(b"=").decode() == self.challenge
                )
                return self.reply(
                    200,
                    {
                        "access_token": "access1",
                        "refresh_token": "refresh1",
                        "expires_in": 7200,
                        "scope": self.scopes,
                    },
                )
            self.refreshes += 1
            assert form["refresh_token"] == "refresh1"  # noqa: S105 — synthetic token
            return self.reply(
                200,
                {
                    "access_token": "access2",
                    "refresh_token": "refresh2",
                    "expires_in": 7200,
                },
            )
        if path == "/2/oauth2/revoke":
            return self.reply(200, {})
        if path == "/2/users/me":
            return self.reply(
                200,
                {"data": {"id": self.account, "username": "founder", "protected": self.protected}},
            )
        if path.endswith("/tweets") and request.method == "GET":
            if self.timeline_status != 200:
                return self.reply(self.timeline_status, {}, {"retry-after": "37"})
            return self.reply(
                200,
                {
                    "data": [
                        {"id": "100", "text": "Original", "created_at": "2026-09-01T00:00:00Z"}
                    ],
                    "meta": {"next_token": "CURSOR"},
                },
            )
        if path == "/2/tweets" and request.method == "POST":
            if self.reject_access1 and request.headers["Authorization"] == "Bearer access1":
                return self.reply(401, {})
            if self.post_timeout:
                raise httpx.ReadTimeout("lost response")
            return self.reply(
                self.post_status,
                {"data": {"id": "200", "text": json.loads(request.content)["text"]}}
                if self.post_status == 201
                else {"error": "provider body must not leak"},
            )
        if path == "/2/media/upload" and request.method == "POST":
            return self.reply(200, {"data": {"id": "300", "expires_after_secs": 3600}})
        if path == "/2/media/upload/initialize":
            return self.reply(200, {"data": {"id": "301", "expires_after_secs": 3600}})
        if path == "/2/media/upload/301/append":
            return self.reply(200, {"data": {"expires_at": 999999}})
        if path == "/2/media/upload/301/finalize":
            return self.reply(200, {"data": {"id": "301", "processing_info": {"state": "pending"}}})
        if path == "/2/media/upload" and request.method == "GET":
            assert request.url.params["media_id"] == "301"
            assert request.url.params["command"] == "STATUS"
            return self.reply(
                200, {"data": {"id": "301", "processing_info": {"state": "succeeded"}}}
            )
        if path == "/2/media/metadata":
            body = json.loads(request.content)
            assert body == {"id": "300", "metadata": {"alt_text": {"text": "Dashboard chart"}}}
            return self.reply(200, {"data": {"id": "300"}})
        raise AssertionError(f"unexpected fixture route {path}")


async def service(api=None):
    api = api or X()
    db = Database()
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    integrations = IntegrationService(
        database=db,
        settings=settings(
            x_oauth_client_id="client",
            x_oauth_client_secret=SecretStr("synthetic-secret"),
        ),
        client=client,
    )
    return integrations, db, api


async def connect(integrations, api, *, capabilities=None):
    start = await integrations.start_connect(
        project_id=PROJECT_ID,
        provider_key=X_PROVIDER,
        clerk_user_id=USER_ID,
        capabilities=capabilities,
    )
    query = parse_qs(urlsplit(start.authorization_url).query)
    api.scopes = query["scope"][0]
    api.challenge = query["code_challenge"][0]
    result = await integrations.x.complete(
        state=query["state"][0], code="good", clerk_user_id=USER_ID
    )
    return result, query


@pytest.mark.asyncio
async def test_read_only_first_connection_upgrade_and_account_identity():
    integrations, db, api = await service()
    assert integrations.is_configured(X_PROVIDER)
    first, query = await connect(integrations, api)
    assert query["scope"][0].split() == ["offline.access", "tweet.read", "users.read"]
    assert first.external_account_id == "12345"
    assert first.configuration["protected"] is False
    assert first.configuration["granted_capabilities"] == ["x.posts.read"]
    with pytest.raises(IntegrationAuthorizationError):
        await integrations.x.connection(PROJECT_ID, "x.posts.publish")
    second, query = await connect(
        integrations, api, capabilities=("x.posts.publish", "x.media.upload")
    )
    assert "tweet.read" in query["scope"][0]
    assert "tweet.write" in query["scope"][0]
    assert "media.write" in query["scope"][0]
    assert set(second.configuration["granted_capabilities"]) == {
        "x.posts.read",
        "x.posts.publish",
        "x.media.upload",
    }
    assert await integrations.x.connection(PROJECT_ID, "x.posts.publish")
    assert (
        await integrations.x.complete(state=query["state"][0], code="good", clerk_user_id=USER_ID)
        == second
    )
    api.account = "99999"
    with pytest.raises(IntegrationAuthorizationError, match="Disconnect"):
        await connect(integrations, api)
    assert db.connections[(PROJECT_ID, X_PROVIDER)].external_account_id == "12345"
    api.account = "12345"
    direct, direct_query = await connect(integrations, api, capabilities=("x.posts.publish",))
    assert "users.read" in direct_query["scope"][0]
    assert direct.external_account_id == "12345"
    await integrations.close()


@pytest.mark.asyncio
async def test_timeline_bounds_and_rate_limit():
    integrations, _, api = await service()
    connection, _ = await connect(integrations, api)
    page = await integrations.x.timeline(connection, limit=20, start_time="2026-09-01T00:00:00Z")
    assert page["posts"][0]["text"] == "Original"
    assert page["next_cursor"] == "CURSOR"
    request = api.calls[-1]
    assert request.url.host == "api.x.com"
    assert request.url.params["start_time"] == "2026-09-01T00:00:00Z"
    assert request.url.params["post.fields"] == "created_at,public_metrics,note_post"
    assert request.url.params["expansions"] == "referenced_posts"
    with pytest.raises(IntegrationInputError):
        await integrations.x.timeline(connection, limit=101)
    with pytest.raises(IntegrationInputError):
        await integrations.x.timeline(connection, pagination_token="../elsewhere")  # noqa: S106
    api.timeline_status = 429
    with pytest.raises(IntegrationRateLimitedError) as exc:
        await integrations.x.timeline(connection)
    assert exc.value.retry_after == 37
    await integrations.close()


@pytest.mark.asyncio
async def test_serialized_refresh_and_exact_post_with_unknown_outcome():
    integrations, db, api = await service()
    connection, _ = await connect(
        integrations, api, capabilities=("x.posts.read", "x.posts.publish")
    )
    tokens = {"access_token": "access1", "refresh_token": "refresh1", "expires_at": 0}
    ciphertext = integrations._cipher.encrypt(
        json.dumps(tokens), context=f"credential:{PROJECT_ID}:{X_PROVIDER}"
    )
    await db.update_integration_credential(
        project_id=PROJECT_ID,
        provider_key=X_PROVIDER,
        connection_id=connection.id,
        credential_ciphertext=ciphertext,
        credential_key_version=integrations._cipher.version,
    )
    connection = await integrations.x.connection(PROJECT_ID, "x.posts.publish")
    assert await asyncio.gather(
        integrations.x.access_token(connection), integrations.x.access_token(connection)
    ) == ["access2", "access2"]
    assert api.refreshes == 1
    result = await integrations.x.create_post(connection, text="A technical update")
    assert result["post_id"] == "200"
    assert result["url"].endswith("/200")
    api.post_timeout = True
    with pytest.raises(IntegrationDeliveryUnknownError):
        await integrations.x.create_post(connection, text="Potentially sent")
    api.post_timeout = False
    api.post_status = 503
    with pytest.raises(IntegrationDeliveryUnknownError):
        await integrations.x.create_post(connection, text="Potentially sent")
    api.post_status = 400
    with pytest.raises(IntegrationUpstreamError) as exc:
        await integrations.x.create_post(connection, text="Known refusal")
    assert "provider body" not in str(exc.value)
    await integrations.close()


@pytest.mark.asyncio
async def test_definitive_401_refreshes_once_before_post_retry():
    integrations, _, api = await service()
    connection, _ = await connect(
        integrations, api, capabilities=("x.posts.read", "x.posts.publish")
    )
    api.reject_access1 = True
    result = await integrations.x.create_post(connection, text="Approved text")
    assert result["post_id"] == "200"
    assert api.refreshes == 1
    writes = [request for request in api.calls if request.url.path == "/2/tweets"]
    assert [request.headers["Authorization"] for request in writes] == [
        "Bearer access1",
        "Bearer access2",
    ]
    await integrations.close()


@pytest.mark.asyncio
async def test_media_paths_and_bounds():
    integrations, _, api = await service()
    connection, _ = await connect(
        integrations,
        api,
        capabilities=("x.posts.read", "x.posts.publish", "x.media.upload"),
    )
    assert (await integrations.x.upload_image(connection, content=b"jpg", media_type="image/jpeg"))[
        "media_id"
    ] == "300"
    with pytest.raises(IntegrationInputError):
        await integrations.x.upload_image(
            connection, content=b"x" * (MAX_IMAGE_BYTES + 1), media_type="image/jpeg"
        )
    assert (await integrations.x.set_alt_text(connection, media_id="300", text="Dashboard chart"))[
        "media_id"
    ] == "300"
    initialized = await integrations.x.initialize_video(connection, total_bytes=6)
    assert initialized["media_id"] == "301"
    await integrations.x.append_video(
        connection, media_id="301", segment_index=0, content=b"video!"
    )
    assert (await integrations.x.finalize_video(connection, media_id="301"))["data"][
        "processing_info"
    ]["state"] == "pending"
    assert (await integrations.x.media_status(connection, media_id="301"))["data"][
        "processing_info"
    ]["state"] == "succeeded"
    with pytest.raises(IntegrationInputError):
        await integrations.x.media_status(connection, media_id="https://other.site")
    await integrations.close()
