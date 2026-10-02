"""Project-owned X OAuth connection and fixed, bounded native API operations.

This adapter never exposes tokens to workflow code. Publishing callers must persist an
approved intent and effect receipt before calling ``create_post``; the X API does not
document a create-post idempotency key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx

from tin_lite.connection_records import open_credential, read_bounded_json, seal_credential
from tin_lite.integrations import (
    X_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationDeliveryUnknownError,
    IntegrationInputError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
    ServiceCallRefused,
)

API = "https://api.x.com"
AUTHORIZE = "https://x.com/i/oauth2/authorize"
TOKEN_URL = f"{API}/2/oauth2/token"  # noqa: S105
REVOKE_URL = f"{API}/2/oauth2/revoke"
CALLBACK = "x"
MAX_UPSTREAM_BYTES = 512_000
MAX_ERROR_BYTES = 4_096
MAX_IMAGE_BYTES = 5_000_000
MAX_CHUNK_BYTES = 5_000_000
MAX_VIDEO_BYTES = 64_000_000
REFRESH_MARGIN_SECONDS = 120
ID = re.compile(r"^[0-9]{1,19}$")
CAPABILITY_SCOPES = {
    "x.posts.read": {"tweet.read", "users.read"},
    "x.posts.publish": {"tweet.write"},
    "x.media.upload": {"tweet.write", "media.write"},
}


class _XUnauthorized(IntegrationAuthorizationError):
    """A definite 401; refreshing once is safe even for a write."""


def oauth_ready(settings: Any) -> bool:
    return bool(
        getattr(settings, "x_oauth_client_id", None)
        and getattr(settings, "x_oauth_client_secret", None)
    )


def scopes_for(capabilities: tuple[str, ...] | list[str]) -> set[str]:
    # Account identity is read during every callback, including a direct publish upgrade.
    scopes = {"offline.access", "tweet.read", "users.read"}
    for capability in capabilities:
        if capability not in CAPABILITY_SCOPES:
            raise IntegrationAuthorizationError("X access request is unsupported")
        scopes.update(CAPABILITY_SCOPES[capability])
    return scopes


def capabilities_for(scopes: set[str]) -> list[str]:
    return [key for key, required in CAPABILITY_SCOPES.items() if required <= scopes]


def authorization_url(
    settings: Any,
    *,
    state: str,
    challenge: str,
    redirect: str,
    capabilities: tuple[str, ...],
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.x_oauth_client_id,
            "redirect_uri": redirect,
            "scope": " ".join(sorted(scopes_for(capabilities))),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE}?{query}"


def _tokens(value: Any, *, previous_refresh: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IntegrationUpstreamError("X returned an invalid token response")
    access = value.get("access_token")
    refresh = value.get("refresh_token") or previous_refresh
    expires = value.get("expires_in")
    if (
        not isinstance(access, str)
        or not access
        or not isinstance(refresh, str)
        or not refresh
        or type(expires) is not int
        or not 60 <= expires <= 365 * 86400
    ):
        raise IntegrationUpstreamError("X returned an incomplete token response")
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + expires,
    }


def _credential(integrations: Any, connection: Any) -> dict[str, Any]:
    try:
        tokens = json.loads(open_credential(integrations, connection))
    except (ValueError, TypeError):
        raise IntegrationAuthorizationError("Reconnect X to restore its credential") from None
    if not isinstance(tokens, dict) or not all(
        isinstance(tokens.get(key), str) and tokens[key]
        for key in ("access_token", "refresh_token")
    ):
        raise IntegrationAuthorizationError("Reconnect X to restore its credential")
    return tokens


def _seal(integrations: Any, project_id: UUID, tokens: dict[str, Any]):
    return seal_credential(
        integrations,
        project_id=project_id,
        provider_key=X_PROVIDER,
        value=json.dumps(tokens),
    )


def _id(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise IntegrationInputError(f"X {name} is invalid")
    return value


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if raw is not None and raw.isdigit():
        return min(int(raw), 3600)
    reset = response.headers.get("x-rate-limit-reset")
    if reset is not None and reset.isdigit():
        return max(0, min(int(reset) - int(time.time()), 3600))
    return None


class XConnection:
    def __init__(self, integrations: Any) -> None:
        self.integrations = integrations
        self.db = integrations._database

    @property
    def _client(self) -> httpx.AsyncClient:
        return self.integrations._client

    async def _attempt(self, state: str, clerk_user_id: str, *, include_used: bool):
        if not isinstance(state, str) or not 1 <= len(state) <= 256:
            raise IntegrationAuthorizationError("X connection attempt has expired")
        attempt = await self.db.get_integration_auth_attempt(
            token_hash=hashlib.sha256(state.encode()).hexdigest(),
            provider_key=X_PROVIDER,
            clerk_user_id=clerk_user_id,
            include_used=include_used,
        )
        if attempt is None:
            raise IntegrationAuthorizationError("X connection attempt has expired")
        return attempt

    async def pending_project(self, *, state: str, clerk_user_id: str) -> UUID:
        return (await self._attempt(state, clerk_user_id, include_used=True)).project_id

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        settings = self.integrations._settings
        try:
            async with self._client.stream(
                "POST",
                TOKEN_URL,
                data=data,
                auth=(
                    settings.x_oauth_client_id,
                    settings.x_oauth_client_secret.get_secret_value(),
                ),
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                follow_redirects=False,
                timeout=20.0,
            ) as response:
                payload = await read_bounded_json(response, maximum=MAX_ERROR_BYTES, provider="X")
                if response.status_code in {400, 401, 403}:
                    raise IntegrationAuthorizationError(
                        "Reconnect X and approve the requested access"
                    )
                if response.status_code != 200 or not isinstance(payload, dict):
                    raise IntegrationUpstreamError("X authorization is temporarily unavailable")
                return payload
        except httpx.HTTPError:
            raise IntegrationUpstreamError("X authorization is temporarily unavailable") from None

    async def complete(self, *, state: str, code: str, clerk_user_id: str):
        self.integrations._require_configured(X_PROVIDER)
        state_hash = hashlib.sha256((state or "").encode()).hexdigest()
        attempt = await self.db.consume_integration_auth_attempt(
            token_hash=state_hash, provider_key=X_PROVIDER, clerk_user_id=clerk_user_id
        )
        if attempt is None:
            used = await self._attempt(state, clerk_user_id, include_used=True)
            existing = await self.db.get_integration_connection(
                project_id=used.project_id, provider_key=X_PROVIDER
            )
            if existing is not None and existing.configuration.get("auth_attempt") == state_hash:
                return existing
            raise IntegrationAuthorizationError("X connection attempt has expired")
        if not isinstance(code, str) or not 1 <= len(code) <= 2048:
            raise IntegrationAuthorizationError("X did not return an authorization code")
        if attempt.pkce_verifier_ciphertext is None or self.integrations._cipher is None:
            raise IntegrationAuthorizationError("X connection attempt is invalid")
        verifier = self.integrations._cipher.decrypt(
            attempt.pkce_verifier_ciphertext,
            context=f"auth:{attempt.project_id}:{X_PROVIDER}",
        )
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.integrations._callback_url(CALLBACK),
                "code_verifier": verifier,
            }
        )
        scopes = {part for part in str(payload.get("scope") or "").split() if part}
        requested = scopes_for(attempt.requested_capabilities)
        if not requested <= scopes:
            raise IntegrationAuthorizationError("X did not grant the requested access")
        tokens = _tokens(payload)
        identity = await self._request_token(
            "GET", "/2/users/me", tokens["access_token"], params={"user.fields": "protected"}
        )
        user = identity.get("data")
        if (
            not isinstance(user, dict)
            or not isinstance(user.get("id"), str)
            or not ID.fullmatch(user["id"])
            or not isinstance(user.get("username"), str)
            or not user["username"]
            or type(user.get("protected")) is not bool
        ):
            raise IntegrationAuthorizationError("X did not identify the signed-in account")
        async with self.db.integration_refresh_lock(
            project_id=attempt.project_id, provider_key=X_PROVIDER
        ):
            existing = await self.db.get_integration_connection(
                project_id=attempt.project_id, provider_key=X_PROVIDER
            )
            if existing is not None and existing.external_account_id != user["id"]:
                # An approved delivery pins numeric identity. Serialize competing callbacks.
                raise IntegrationAuthorizationError(
                    "Disconnect the current X account before connecting a different account"
                )
            ciphertext, version = _seal(self.integrations, attempt.project_id, tokens)
            connection = await self.db.upsert_integration_connection(
                project_id=attempt.project_id,
                provider_key=X_PROVIDER,
                external_account_id=user["id"],
                external_account_label=f"@{user['username'][:100]}",
                configuration={
                    "username": user["username"][:100],
                    "protected": user["protected"],
                    "granted_scopes": sorted(scopes),
                    "granted_capabilities": capabilities_for(scopes),
                    "oauth_client_id": self.integrations._settings.x_oauth_client_id,
                    "auth_attempt": state_hash,
                },
                credential_ciphertext=ciphertext,
                credential_key_version=version,
                connected_by_clerk_user_id=clerk_user_id,
            )
        await self.integrations._record_activity(
            connection,
            "integration_connected",
            f"X account @{user['username'][:100]} connected.",
            suffix=str(uuid4()),
        )
        return connection

    async def connection(self, project_id: UUID, capability: str | None = None):
        self.integrations._require_configured(X_PROVIDER)
        connection = await self.db.get_integration_connection(
            project_id=project_id, provider_key=X_PROVIDER
        )
        if connection is None or connection.status != "connected":
            raise IntegrationAuthorizationError("Connect or reconnect X in this project")
        if connection.credential_ciphertext is None or not ID.fullmatch(
            connection.external_account_id or ""
        ):
            raise IntegrationAuthorizationError("Reconnect X in this project")
        granted = connection.configuration.get("granted_capabilities")
        if capability is not None and (
            capability not in CAPABILITY_SCOPES
            or not isinstance(granted, list)
            or capability not in granted
        ):
            raise IntegrationAuthorizationError("X needs additional access for this action")
        return connection

    async def access_token(self, connection: Any, *, rejected: str | None = None) -> str:
        tokens = _credential(self.integrations, connection)
        if rejected is None and tokens.get("expires_at", 0) - time.time() > REFRESH_MARGIN_SECONDS:
            return tokens["access_token"]
        async with self.db.integration_refresh_lock(
            project_id=connection.project_id, provider_key=X_PROVIDER
        ):
            current = await self.db.get_integration_connection(
                project_id=connection.project_id, provider_key=X_PROVIDER
            )
            if current is None or current.id != connection.id or current.status != "connected":
                raise IntegrationAuthorizationError("X connection changed; start again")
            tokens = _credential(self.integrations, current)
            fresh = (
                tokens["access_token"] != rejected
                if rejected is not None
                else tokens.get("expires_at", 0) - time.time() > REFRESH_MARGIN_SECONDS
            )
            if fresh:
                return tokens["access_token"]
            try:
                payload = await self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
                )
            except IntegrationAuthorizationError:
                await self.db.mark_integration_attention(
                    project_id=current.project_id,
                    provider_key=X_PROVIDER,
                    error_code="reauthorization_required",
                )
                raise
            renewed = _tokens(payload, previous_refresh=tokens["refresh_token"])
            ciphertext, version = _seal(self.integrations, current.project_id, renewed)
            stored = await self.db.update_integration_credential(
                project_id=current.project_id,
                provider_key=X_PROVIDER,
                connection_id=current.id,
                credential_ciphertext=ciphertext,
                credential_key_version=version,
            )
            if not stored:
                raise IntegrationAuthorizationError("X connection changed; start again")
            return renewed["access_token"]

    async def _request_token(
        self, method: str, path: str, token: str, *, ambiguous_write: bool = False, **kwargs
    ) -> dict[str, Any]:
        # Every caller supplies a fixed path or a validated numeric X id; never accept a URL.
        try:
            async with self._client.stream(
                method,
                f"{API}{path}",
                headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
                follow_redirects=False,
                timeout=30.0,
                **kwargs,
            ) as response:
                status = response.status_code
                if status == 429:
                    raise IntegrationRateLimitedError(
                        "X rate limited this request; try again later",
                        retry_after=_retry_after(response),
                    )
                if status == 401:
                    raise _XUnauthorized("X access token expired")
                if status == 403:
                    raise IntegrationAuthorizationError(
                        "X rejected this access; reconnect or check your X plan"
                    )
                if status >= 500 and ambiguous_write:
                    raise IntegrationDeliveryUnknownError(
                        "X may have completed the action; check its status before retrying"
                    )
                if status >= 500 or 300 <= status < 400:
                    raise IntegrationUpstreamError("X could not complete the request")
                try:
                    value = await read_bounded_json(
                        response,
                        maximum=MAX_UPSTREAM_BYTES if status < 400 else MAX_ERROR_BYTES,
                        provider="X",
                    )
                except (IntegrationUpstreamError, ServiceCallRefused):
                    if ambiguous_write and status < 400:
                        raise IntegrationDeliveryUnknownError(
                            "X returned an unreadable result; check before retrying"
                        ) from None
                    raise
                if status >= 400:
                    raise IntegrationUpstreamError(f"X refused this request (HTTP {status})")
                # X reports a referenced post it can't return (deleted or protected) as a
                # partial error beside the data it did return. A read keeps that data; a write,
                # or errors with no data, is still incomplete.
                if not isinstance(value, dict) or (
                    value.get("errors") and (method != "GET" or "data" not in value)
                ):
                    if ambiguous_write:
                        raise IntegrationDeliveryUnknownError(
                            "X returned an incomplete result; check before retrying"
                        )
                    raise IntegrationUpstreamError("X returned an incomplete result")
                return value
        except httpx.HTTPError:
            if ambiguous_write:
                raise IntegrationDeliveryUnknownError(
                    "X may have completed the action; check before retrying"
                ) from None
            raise IntegrationUpstreamError("X could not be reached; try again") from None

    async def _request(
        self,
        connection: Any,
        capability: str,
        method: str,
        path: str,
        *,
        ambiguous_write: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        current = await self.connection(connection.project_id, capability)
        if (
            current.id != connection.id
            or current.external_account_id != connection.external_account_id
        ):
            raise IntegrationAuthorizationError("X connection changed; start again")
        token = await self.access_token(current)
        try:
            return await self._request_token(
                method, path, token, ambiguous_write=ambiguous_write, **kwargs
            )
        except _XUnauthorized:
            token = await self.access_token(current, rejected=token)
            try:
                return await self._request_token(
                    method, path, token, ambiguous_write=ambiguous_write, **kwargs
                )
            except _XUnauthorized:
                await self.db.mark_integration_attention(
                    project_id=current.project_id,
                    provider_key=X_PROVIDER,
                    error_code="reauthorization_required",
                )
                raise IntegrationAuthorizationError("Reconnect X to restore access") from None

    async def timeline(
        self,
        connection: Any,
        *,
        limit: int = 50,
        pagination_token: str | None = None,
        exclude_replies: bool = False,
        exclude_retweets: bool = True,
        start_time: str | None = None,
        end_time: str | None = None,
        until_id: str | None = None,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 5 <= limit <= 100:
            raise IntegrationInputError("X timeline limit must be between 5 and 100")
        if pagination_token is not None and (
            not isinstance(pagination_token, str)
            or not 1 <= len(pagination_token) <= 256
            or not re.fullmatch(r"[A-Za-z0-9]+", pagination_token)
        ):
            raise IntegrationInputError("X timeline cursor is invalid")
        if until_id is not None:
            _id(until_id, name="until ID")
        params: dict[str, Any] = {
            "max_results": limit,
            "post.fields": "created_at,public_metrics,note_post",
            "expansions": "referenced_posts",
        }
        exclusions = []
        if exclude_replies:
            exclusions.append("replies")
        if exclude_retweets:
            exclusions.append("retweets")
        if exclusions:
            params["exclude"] = ",".join(exclusions)
        if pagination_token:
            params["pagination_token"] = pagination_token
        for key, value in (("start_time", start_time), ("end_time", end_time)):
            if value is not None:
                if not isinstance(value, str) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
                ):
                    raise IntegrationInputError(f"X {key} is invalid")
                params[key] = value
        if until_id:
            params["until_id"] = until_id
        result = await self._request(
            connection,
            "x.posts.read",
            "GET",
            f"/2/users/{_id(connection.external_account_id, name='account ID')}/tweets",
            params=params,
        )
        posts = result.get("data", [])
        meta = result.get("meta", {})
        if not isinstance(posts, list) or not isinstance(meta, dict) or len(posts) > limit:
            raise IntegrationUpstreamError("X returned an invalid timeline")
        clean = []
        for post in posts:
            if not isinstance(post, dict) or not isinstance(post.get("id"), str):
                raise IntegrationUpstreamError("X returned an invalid timeline")
            text = (
                post.get("note_post", {}).get("text")
                if isinstance(post.get("note_post"), dict)
                else None
            )
            if not isinstance(text, str):
                text = post.get("text")
            if not isinstance(text, str) or len(text.encode()) > 20_000:
                raise IntegrationUpstreamError("X returned an incomplete post")
            clean.append(
                {
                    "id": _id(post["id"], name="post ID"),
                    "text": text,
                    "created_at": post.get("created_at"),
                    "referenced_posts": post.get("referenced_posts", []),
                    "public_metrics": post.get("public_metrics", {}),
                    "has_note_post": isinstance(post.get("note_post"), dict),
                }
            )
        cursor = meta.get("next_token")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 256):
            raise IntegrationUpstreamError("X returned an invalid timeline cursor")
        return {"posts": clean, "next_cursor": cursor}

    async def upload_image(self, connection: Any, *, content: bytes, media_type: str):
        if (
            media_type not in {"image/jpeg", "image/png"}
            or not isinstance(content, bytes)
            or not 1 <= len(content) <= MAX_IMAGE_BYTES
        ):
            raise IntegrationInputError("X image must be a bounded JPEG or PNG")
        result = await self._request(
            connection,
            "x.media.upload",
            "POST",
            "/2/media/upload",
            ambiguous_write=True,
            json={"media": base64.b64encode(content).decode(), "media_category": "tweet_image"},
        )
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise IntegrationDeliveryUnknownError("X image upload result is uncertain")
        return {
            "media_id": _id(data["id"], name="media ID"),
            "expires_after_secs": data.get("expires_after_secs"),
        }

    async def initialize_video(
        self, connection: Any, *, total_bytes: int, media_type: str = "video/mp4"
    ):
        if (
            media_type != "video/mp4"
            or type(total_bytes) is not int
            or not 1 <= total_bytes <= MAX_VIDEO_BYTES
        ):
            raise IntegrationInputError("X video must be a bounded MP4")
        result = await self._request(
            connection,
            "x.media.upload",
            "POST",
            "/2/media/upload/initialize",
            ambiguous_write=True,
            json={
                "total_bytes": total_bytes,
                "media_type": media_type,
                "media_category": "tweet_video",
                "shared": False,
            },
        )
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise IntegrationDeliveryUnknownError("X video initialization result is uncertain")
        return {
            "media_id": _id(data["id"], name="media ID"),
            "expires_after_secs": data.get("expires_after_secs"),
        }

    async def append_video(
        self, connection: Any, *, media_id: str, segment_index: int, content: bytes
    ):
        media_id = _id(media_id, name="media ID")
        if (
            type(segment_index) is not int
            or not 0 <= segment_index <= 9999
            or not isinstance(content, bytes)
            or not 1 <= len(content) <= MAX_CHUNK_BYTES
        ):
            raise IntegrationInputError("X video segment is invalid")
        return await self._request(
            connection,
            "x.media.upload",
            "POST",
            f"/2/media/upload/{media_id}/append",
            ambiguous_write=True,
            json={"media": base64.b64encode(content).decode(), "segment_index": segment_index},
        )

    async def finalize_video(self, connection: Any, *, media_id: str):
        media_id = _id(media_id, name="media ID")
        return await self._request(
            connection,
            "x.media.upload",
            "POST",
            f"/2/media/upload/{media_id}/finalize",
            ambiguous_write=True,
        )

    async def media_status(self, connection: Any, *, media_id: str):
        media_id = _id(media_id, name="media ID")
        return await self._request(
            connection,
            "x.media.upload",
            "GET",
            "/2/media/upload",
            params={"media_id": media_id, "command": "STATUS"},
        )

    async def set_alt_text(self, connection: Any, *, media_id: str, text: str):
        media_id = _id(media_id, name="media ID")
        if not isinstance(text, str) or not 1 <= len(text) <= 1000:
            raise IntegrationInputError("X image description is invalid")
        # The rendered reference omits this nested field, but X's generated OpenAPI SDK
        # defines CreateMediaMetadataMetadata.alt_text.text.
        result = await self._request(
            connection,
            "x.media.upload",
            "POST",
            "/2/media/metadata",
            ambiguous_write=True,
            json={"id": media_id, "metadata": {"alt_text": {"text": text}}},
        )
        data = result.get("data")
        if not isinstance(data, dict) or data.get("id") != media_id:
            raise IntegrationDeliveryUnknownError("X image description result is uncertain")
        return {"media_id": media_id}

    async def create_post(self, connection: Any, *, text: str, media_ids: list[str] | None = None):
        # Publication service owns weighted character validation and exact approval.
        if not isinstance(text, str) or not text or len(text.encode()) > 4000:
            raise IntegrationInputError("X post text is invalid")
        if media_ids is not None and (
            not isinstance(media_ids, list) or not 1 <= len(media_ids) <= 4
        ):
            raise IntegrationInputError("X attachments are invalid")
        body: dict[str, Any] = {"text": text}
        if media_ids:
            body["media"] = {"media_ids": [_id(item, name="media ID") for item in media_ids]}
        result = await self._request(
            connection,
            "x.posts.publish",
            "POST",
            "/2/tweets",
            ambiguous_write=True,
            json=body,
        )
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise IntegrationDeliveryUnknownError(
                "X post result is uncertain; check before retrying"
            )
        post_id = _id(data["id"], name="post ID")
        return {
            "post_id": post_id,
            "text": data.get("text"),
            "url": f"https://x.com/i/web/status/{post_id}",
        }

    async def health(self, connection: Any) -> dict[str, Any]:
        result = await self._request(
            connection, "x.posts.read", "GET", "/2/users/me", params={"user.fields": "protected"}
        )
        data = result.get("data")
        if not isinstance(data, dict) or data.get("id") != connection.external_account_id:
            await self.db.mark_integration_attention(
                project_id=connection.project_id,
                provider_key=X_PROVIDER,
                error_code="account_changed",
            )
            raise IntegrationAuthorizationError("X account changed; reconnect it")
        return {
            "account_id": connection.external_account_id,
            "username": data.get("username"),
            "protected": data.get("protected") if type(data.get("protected")) is bool else None,
        }

    async def revoke(self, connection: Any) -> None:
        try:
            tokens = _credential(self.integrations, connection)
            settings = self.integrations._settings
            async with self._client.stream(
                "POST",
                REVOKE_URL,
                data={"token": tokens["refresh_token"], "token_type_hint": "refresh_token"},
                auth=(
                    settings.x_oauth_client_id,
                    settings.x_oauth_client_secret.get_secret_value(),
                ),
                follow_redirects=False,
                timeout=10.0,
            ):
                pass
        except Exception:  # noqa: BLE001 — local disconnect remains authoritative
            return
