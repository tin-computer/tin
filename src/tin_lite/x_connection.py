"""Project-owned X OAuth connection and fixed, bounded native API operations.

This adapter never exposes tokens to workflow code. Publishing callers must persist an
approved intent and effect receipt before calling ``create_post``; the X API does not
document a create-post idempotency key.

Code packages read public posts through the service gateway (``call``): recent search and
one public account's recent posts. Each is a paid X read on Tin's X app, made with the
project's own connection; the package's declared call limit is the runaway guard.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx

from tin_lite.connection_records import (
    ServiceArgumentError,
    json_size,
    open_credential,
    read_bounded_json,
    record_call,
    seal_credential,
)
from tin_lite.connection_records import text as bounded
from tin_lite.integrations import (
    X_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationDeliveryUnknownError,
    IntegrationError,
    IntegrationInputError,
    IntegrationProviderError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
    ServiceCallRefused,
    ServiceResponseTooLarge,
)
from tin_lite.provider_errors import ProviderErrorDetail
from tin_lite.provider_errors import detail as provider_error_detail

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
PUBLIC_READ = "x.public.read"
CAPABILITY_SCOPES = {
    "x.posts.read": {"tweet.read", "users.read"},
    "x.posts.publish": {"tweet.write"},
    "x.media.upload": {"tweet.write", "media.write"},
    # The scopes every connection already holds, recorded as its own capability so a project
    # opts in to reading other accounts' public posts explicitly (see capabilities_for).
    PUBLIC_READ: {"tweet.read", "users.read"},
}
# Granted only when a connection asked for them, never inferred from the scopes alone.
OPT_IN_CAPABILITIES = frozenset({PUBLIC_READ})

# Public reads through the service gateway: operation -> closed argument names.
OPERATIONS = {
    "x.posts.search": frozenset({"query", "max_results", "start_time", "end_time", "next_token"}),
    "x.user.posts": frozenset(
        {
            "username",
            "max_results",
            "start_time",
            "end_time",
            "pagination_token",
            "exclude_replies",
        }
    ),
}
USERNAME = re.compile(r"[A-Za-z0-9_]{1,15}")
CURSOR = re.compile(r"[A-Za-z0-9]{1,256}")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MEDIA_KEY = re.compile(r"[A-Za-z0-9_]{1,64}")
MAX_QUERY_CHARACTERS = 512
# X's long-post maximum; a longer text is not a post X documents.
MAX_POST_CHARACTERS = 25_000
MAX_POST_URLS = 10
MAX_POST_MEDIA = 4
POST_METRICS = (
    "repost_count",
    "reply_count",
    "like_count",
    "quote_count",
    "bookmark_count",
    "impression_count",
)
# The same field names timeline() sends; X's OpenAPI document names them post.fields.
PUBLIC_POST_FIELDS = "created_at,public_metrics,entities,attachments,note_post"
PUBLIC_MEDIA_FIELDS = "type,url,preview_image_url,width,height"


class _XUnauthorized(IntegrationAuthorizationError):
    """A definite 401; refreshing once is safe even for a write."""


class _XForbidden(IntegrationAuthorizationError):
    """A 403; keeps what X said so a gateway read can name the refusal."""

    def __init__(self, message: str, *, provider_error: ProviderErrorDetail | None) -> None:
        super().__init__(message)
        self.provider_error = provider_error


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


def capabilities_for(scopes: set[str], requested: tuple[str, ...] | list[str] = ()) -> list[str]:
    return [
        key
        for key, required in CAPABILITY_SCOPES.items()
        if required <= scopes and (key not in OPT_IN_CAPABILITIES or key in requested)
    ]


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


def _problem(status: int | None, body: Any, token: str | None = None) -> ProviderErrorDetail:
    """What X said, from its problem shape: `{type, title, detail, reason, errors[]}`.

    The type is the last part of X's problem URL (`invalid-request`, `client-forbidden`); the
    message is the first error's own message (an invalid search query names its fault there),
    else X's detail or title.
    """
    if not isinstance(body, dict):
        return provider_error_detail("X", status, None)
    kind = body.get("type")
    if isinstance(kind, str):
        kind = kind.rstrip("/").rsplit("/", 1)[-1]
    errors = body.get("errors")
    first = errors[0] if isinstance(errors, list) and errors else None
    message = next(
        (
            value
            for value in (
                first.get("message") if isinstance(first, dict) else None,
                body.get("detail"),
                body.get("title"),
            )
            if isinstance(value, str) and value.strip()
        ),
        None,
    )
    return provider_error_detail(
        "X",
        status,
        {"type": kind, "code": body.get("reason"), "message": message},
        secrets=(token,) if token else (),
    )


async def _read_problem(response: httpx.Response, token: str) -> ProviderErrorDetail:
    # The status already settles the outcome; an unreadable body only loses X's words.
    try:
        body = await read_bounded_json(response, maximum=MAX_ERROR_BYTES, provider="X")
    except (IntegrationError, httpx.HTTPError):
        body = None
    return _problem(response.status_code, body, token)


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if raw is not None and raw.isdigit():
        return min(int(raw), 3600)
    reset = response.headers.get("x-rate-limit-reset")
    if reset is not None and reset.isdigit():
        return max(0, min(int(reset) - int(time.time()), 3600))
    return None


# ---------------------------------------------------------------- public reads: arguments


def _limit(value: Any, low: int) -> int:
    if type(value) is not int or not low <= value <= 100:
        raise IntegrationInputError(f"X max_results must be an integer from {low} to 100")
    return value


def _cursor(value: Any, name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not CURSOR.fullmatch(value)):
        raise IntegrationInputError(f"X {name} is invalid; pass back the next_token X returned")
    return value


def _window(start_time: Any, end_time: Any) -> dict[str, str]:
    params: dict[str, str] = {}
    moments = {}
    for key, value in (("start_time", start_time), ("end_time", end_time)):
        if value is None:
            continue
        try:
            if not isinstance(value, str) or not TIME.fullmatch(value):
                raise ValueError
            moments[key] = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise IntegrationInputError(
                f"X {key} must be a UTC time such as 2026-10-01T00:00:00Z"
            ) from None
        params[key] = value
    if len(moments) == 2 and moments["start_time"] >= moments["end_time"]:
        raise IntegrationInputError("X start_time must be before end_time")
    return params


def _username(value: Any) -> str:
    if not isinstance(value, str) or not USERNAME.fullmatch(value):
        raise IntegrationInputError(
            "X username must be 1-15 letters, digits or underscores, without @ or a URL"
        )
    return value


def _search_params(
    *,
    query: Any,
    max_results: Any = 10,
    start_time: Any = None,
    end_time: Any = None,
    next_token: Any = None,
) -> dict[str, Any]:
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > MAX_QUERY_CHARACTERS
        or any(unicodedata.category(char) == "Cc" for char in query)
    ):
        raise IntegrationInputError(
            f"X search query must be 1-{MAX_QUERY_CHARACTERS} characters without line breaks "
            "or control characters"
        )
    params: dict[str, Any] = {
        "query": query,
        "max_results": _limit(max_results, 10),
        "post.fields": PUBLIC_POST_FIELDS,
        "expansions": "author_id,attachments.media_keys",
        "user.fields": "protected",
        "media.fields": PUBLIC_MEDIA_FIELDS,
        **_window(start_time, end_time),
    }
    if _cursor(next_token, "next_token"):
        params["next_token"] = next_token
    return params


def _user_posts_params(
    *,
    max_results: Any = 10,
    start_time: Any = None,
    end_time: Any = None,
    pagination_token: Any = None,
    exclude_replies: Any = True,
) -> dict[str, Any]:
    if type(exclude_replies) is not bool:
        raise IntegrationInputError("X exclude_replies must be true or false")
    params: dict[str, Any] = {
        "max_results": _limit(max_results, 5),
        "post.fields": PUBLIC_POST_FIELDS,
        "expansions": "attachments.media_keys",
        "media.fields": PUBLIC_MEDIA_FIELDS,
        # Reposts are other accounts' posts; this reads what the account itself posted.
        "exclude": "replies,retweets" if exclude_replies else "retweets",
        **_window(start_time, end_time),
    }
    if _cursor(pagination_token, "pagination_token"):
        params["pagination_token"] = pagination_token
    return params


def check_arguments(operation: str, arguments: Any) -> None:
    """Gateway hook: reject a malformed call before any receipt or X request exists."""
    if operation not in OPERATIONS:
        raise ServiceArgumentError("unknown X operation")
    if not isinstance(arguments, dict):
        raise ServiceArgumentError("arguments must be an object")
    unknown = sorted(set(arguments) - OPERATIONS[operation])
    if unknown:
        raise ServiceArgumentError(f"unsupported argument {unknown[0]}")
    try:
        if operation == "x.posts.search":
            _search_params(**{"query": None, **arguments})
        else:
            args = dict(arguments)
            _username(args.pop("username", None))
            _user_posts_params(**args)
    except IntegrationInputError as exc:
        raise ServiceArgumentError(str(exc)) from None


# ---------------------------------------------------------------- public reads: results


def _web_url(value: Any, *, secure: bool = False) -> str | None:
    schemes = ("https://",) if secure else ("https://", "http://")
    if (
        isinstance(value, str)
        and value.startswith(schemes)
        and len(value) <= 2048
        and not any(char.isspace() or not char.isprintable() for char in value)
    ):
        return value
    return None


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _includes(result: dict[str, Any]) -> tuple[dict[str, dict], dict[str, dict]]:
    """Authors and media by id from `includes`; an entry without a usable id resolves nothing."""
    includes = result.get("includes", {})
    users = includes.get("users", []) if isinstance(includes, dict) else None
    media = includes.get("media", []) if isinstance(includes, dict) else None
    if not isinstance(users, list) or not isinstance(media, list):
        raise IntegrationUpstreamError("X returned invalid authors or media")
    authors = {
        user["id"]: {
            "id": user["id"],
            "username": user.get("username")
            if isinstance(user.get("username"), str) and USERNAME.fullmatch(user["username"])
            else None,
            "name": bounded(user.get("name"), 100),
            "protected": user.get("protected") is True,
        }
        for user in users
        if isinstance(user, dict) and isinstance(user.get("id"), str) and ID.fullmatch(user["id"])
    }
    attached = {
        item["media_key"]: {
            "type": bounded(item.get("type"), 40),
            "url": _web_url(item.get("url"), secure=True),
            "preview_image_url": _web_url(item.get("preview_image_url"), secure=True),
            "width": _count(item.get("width")),
            "height": _count(item.get("height")),
        }
        for item in media
        if isinstance(item, dict)
        and isinstance(item.get("media_key"), str)
        and MEDIA_KEY.fullmatch(item["media_key"])
    }
    return authors, attached


def _post_urls(*sources: Any) -> list[dict[str, Any]]:
    """Links the post text carries, by expanded URL; attached media's own links are left out."""
    found: dict[str, dict[str, Any]] = {}
    for entities in sources:
        urls = entities.get("urls") if isinstance(entities, dict) else None
        for item in urls if isinstance(urls, list) else []:
            if not isinstance(item, dict) or item.get("media_key"):
                continue
            expanded = _web_url(item.get("expanded_url"))
            if expanded is None or expanded in found:
                continue
            if len(found) == MAX_POST_URLS:
                return list(found.values())
            found[expanded] = {
                "expanded_url": expanded,
                "unwound_url": _web_url(item.get("unwound_url")),
                "title": bounded(item.get("title"), 300),
                "description": bounded(item.get("description"), 500),
            }
    return list(found.values())


def _public_post(
    post: Any, authors: dict[str, dict], media: dict[str, dict], account: dict | None
) -> dict[str, Any] | None:
    """One cleaned post, or None for a post whose author protects their posts."""
    if not isinstance(post, dict) or not isinstance(post.get("id"), str):
        raise IntegrationUpstreamError("X returned an invalid post")
    post_id = post["id"]
    if not ID.fullmatch(post_id):
        raise IntegrationUpstreamError("X returned an invalid post")
    note = post.get("note_post") if isinstance(post.get("note_post"), dict) else None
    body = note.get("text") if note is not None else None
    if not isinstance(body, str):
        body = post.get("text")
    if not isinstance(body, str) or len(body) > MAX_POST_CHARACTERS:
        raise IntegrationUpstreamError("X returned an incomplete post")
    author_id = post.get("author_id")
    author = account or authors.get(author_id)
    if author is None and isinstance(author_id, str) and ID.fullmatch(author_id):
        # X left the author out of includes (a partial error): keep the post with its id.
        author = {"id": author_id, "username": None, "name": None, "protected": False}
    if author is not None and author.get("protected"):
        return None
    attachments = post.get("attachments")
    keys = attachments.get("media_keys") if isinstance(attachments, dict) else None
    metrics = post.get("public_metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    created = post.get("created_at")
    return {
        "id": post_id,
        "url": f"https://x.com/i/web/status/{post_id}",
        "text": body,
        "created_at": created if isinstance(created, str) and len(created) <= 40 else None,
        "author": {key: author[key] for key in ("id", "username", "name")} if author else None,
        "public_metrics": {
            key: metrics[key] for key in POST_METRICS if _count(metrics.get(key)) is not None
        },
        "urls": _post_urls(post.get("entities"), note.get("entities") if note else None),
        "media": [
            media[key]
            for key in (keys if isinstance(keys, list) else [])[:MAX_POST_MEDIA]
            if isinstance(key, str) and key in media
        ],
    }


def _public_page(
    result: dict[str, Any], *, limit: int, account: dict | None = None
) -> dict[str, Any]:
    posts = result.get("data", [])
    meta = result.get("meta", {})
    if not isinstance(posts, list) or not isinstance(meta, dict) or len(posts) > limit:
        raise IntegrationUpstreamError("X returned an invalid page of posts")
    authors, media = _includes(result)
    clean = [
        post
        for post in (_public_post(item, authors, media, account) for item in posts)
        if post is not None
    ]
    cursor = meta.get("next_token")
    if cursor is not None and (not isinstance(cursor, str) or not CURSOR.fullmatch(cursor)):
        raise IntegrationUpstreamError("X returned an invalid next_token")
    return {"posts": clean, "next_token": cursor}


def _fit(page: dict[str, Any], maximum: int | None) -> dict[str, Any]:
    """Keep the leading posts that fit the binding's bound, saying when some were left out.

    X's next_token continues after the whole page, so a left-out post is skipped; a smaller
    max_results reads every post. A single post too large on its own is a known outcome.
    """
    whole = {**page, "truncated": False}
    if maximum is None or json_size(whole) <= maximum:
        return whole
    posts = page["posts"]
    used = json_size({**page, "posts": [], "truncated": True})
    kept = 0
    for index, post in enumerate(posts):
        used += json_size(post) + (2 if index else 0)  # ", " between posts
        if used > maximum:
            break
        kept += 1
    if posts and not kept:
        raise ServiceResponseTooLarge("A single X post exceeds the response bound.")
    return {**page, "posts": posts[:kept], "truncated": True}


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
                    # Reconnecting asks again for what was granted (start_connect merges it).
                    "granted_capabilities": capabilities_for(
                        scopes, attempt.requested_capabilities
                    ),
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
        self,
        method: str,
        path: str,
        token: str,
        *,
        ambiguous_write: bool = False,
        problems: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        # Every caller supplies a fixed path, a validated numeric X id or a validated username;
        # never accept a URL. A refusal keeps what X said as provider_error, beside Tin's own
        # unchanged message. `problems` returns a read's errors-only body for the caller.
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
                        provider_error=await _read_problem(response, token),
                    )
                if status == 401:
                    raise _XUnauthorized("X access token expired")
                if status == 403:
                    raise _XForbidden(
                        "X rejected this access; reconnect or check your X plan",
                        provider_error=await _read_problem(response, token),
                    )
                if status >= 500 and ambiguous_write:
                    raise IntegrationDeliveryUnknownError(
                        "X may have completed the action; check its status before retrying"
                    )
                if 300 <= status < 400:
                    raise IntegrationUpstreamError("X could not complete the request")
                if status >= 500:
                    raise IntegrationProviderError(
                        "X could not complete the request",
                        provider_error=await _read_problem(response, token),
                    )
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
                    raise IntegrationProviderError(
                        f"X refused this request (HTTP {status})",
                        provider_error=_problem(status, value, token),
                    )
                # X reports a referenced post it can't return (deleted or protected) as a
                # partial error beside the data it did return. A read keeps that data; a write,
                # or errors with no data, is still incomplete unless the caller reads them.
                if not isinstance(value, dict) or (
                    value.get("errors")
                    and (method != "GET" or ("data" not in value and not problems))
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
        problems: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        current = await self.connection(connection.project_id, capability)
        if (
            current.id != connection.id
            or current.external_account_id != connection.external_account_id
        ):
            raise IntegrationAuthorizationError("X connection changed; start again")
        token = await self.access_token(current)
        options = {"ambiguous_write": ambiguous_write, "problems": problems, **kwargs}
        try:
            return await self._request_token(method, path, token, **options)
        except _XUnauthorized:
            token = await self.access_token(current, rejected=token)
            try:
                return await self._request_token(method, path, token, **options)
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

    async def search_recent(
        self,
        connection: Any,
        *,
        query: str,
        max_results: int = 10,
        start_time: str | None = None,
        end_time: str | None = None,
        next_token: str | None = None,
    ) -> dict[str, Any]:
        """Public posts from about the last seven days that match an X search query.

        Posts by accounts that protect their posts are left out.
        """
        params = _search_params(
            query=query,
            max_results=max_results,
            start_time=start_time,
            end_time=end_time,
            next_token=next_token,
        )
        result = await self._request(
            connection, PUBLIC_READ, "GET", "/2/tweets/search/recent", params=params
        )
        return _public_page(result, limit=max_results)

    async def user_posts(
        self,
        connection: Any,
        *,
        username: str,
        max_results: int = 10,
        start_time: str | None = None,
        end_time: str | None = None,
        pagination_token: str | None = None,
        exclude_replies: bool = True,
    ) -> dict[str, Any]:
        """One public account's own recent posts, newest first; reposts are left out."""
        username = _username(username)
        params = _user_posts_params(
            max_results=max_results,
            start_time=start_time,
            end_time=end_time,
            pagination_token=pagination_token,
            exclude_replies=exclude_replies,
        )
        account = await self._public_account(connection, username)
        result = await self._request(
            connection, PUBLIC_READ, "GET", f"/2/users/{account['id']}/tweets", params=params
        )
        page = _public_page(result, limit=max_results, account=account)
        return {"account": {key: account[key] for key in ("id", "username", "name")}, **page}

    async def _public_account(self, connection: Any, username: str) -> dict[str, Any]:
        result = await self._request(
            connection,
            PUBLIC_READ,
            "GET",
            f"/2/users/by/username/{username}",
            params={"user.fields": "protected"},
            problems=True,
        )
        user = result.get("data")
        if not isinstance(user, dict):
            errors = result.get("errors")
            first = errors[0] if isinstance(errors, list) and errors else None
            if not isinstance(first, dict):
                raise IntegrationUpstreamError("X returned an invalid account")
            found = _problem(200, first)
            if found.type == "resource-not-found":
                raise ServiceCallRefused(
                    f"X found no account @{username}.", code="not_found", provider_error=found
                )
            raise ServiceCallRefused(
                f"X did not return @{username}; the account may be suspended or unavailable.",
                code="account_unavailable",
                provider_error=found,
            )
        name = user.get("username")
        if (
            not isinstance(user.get("id"), str)
            or not ID.fullmatch(user["id"])
            or not isinstance(name, str)
            or not USERNAME.fullmatch(name)
            or type(user.get("protected")) is not bool
        ):
            raise IntegrationUpstreamError("X returned an invalid account")
        if user["protected"]:
            raise ServiceCallRefused(
                f"@{name} protects its posts; Tin reads only public posts.",
                code="protected_account",
            )
        return {"id": user["id"], "username": name, "name": bounded(user.get("name"), 100)}

    async def call(
        self,
        operation: str,
        arguments: Any,
        *,
        connection: Any,
        run_id: UUID | None,
        execution_key: str,
        max_response_bytes: int | None,
    ) -> dict[str, Any]:
        """One gateway read of public posts, fitted to the binding and receipted.

        X's refusals settle as a named error for the step, so a later step may try again:
        what X said rides along as provider_error. A lost or unreadable response stays
        uncertain, as for every gateway read.
        """
        check_arguments(operation, arguments)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"account": connection.external_account_id, "operation": operation, **arguments},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        receipt = {
            "connection": connection,
            "capability": PUBLIC_READ,
            "fingerprint": fingerprint,
            "execution_key": execution_key,
            "run_id": run_id,
        }
        read = self.search_recent if operation == "x.posts.search" else self.user_posts
        try:
            try:
                page = await read(connection, **arguments)
            except _XForbidden as exc:
                raise ServiceCallRefused(
                    "X refused this read. Check that Tin's X access covers public posts, "
                    "or reconnect X.",
                    code="permission_denied",
                    provider_error=exc.provider_error,
                ) from None
            except IntegrationAuthorizationError as exc:
                # Tin's own sentence (reconnect, more access, a changed account): settled.
                raise ServiceCallRefused(str(exc), code="reauthorization_required") from None
            result = _fit(page, max_response_bytes)
        except ServiceResponseTooLarge:
            # X answered; the outcome is known even though no post fits the bound.
            await record_call(
                self.db,
                **receipt,
                status="completed",
                response_summary={"post_count": len(page["posts"]), "returned_count": 0},
            )
            raise
        except IntegrationError as exc:
            await record_call(
                self.db,
                **receipt,
                status="failed",
                error_code=getattr(exc, "code", None) or "provider_request_failed",
            )
            raise
        await record_call(
            self.db,
            **receipt,
            status="completed",
            response_summary={
                "post_count": len(page["posts"]),
                "returned_count": len(result["posts"]),
                "truncated": result["truncated"],
                "has_more": result["next_token"] is not None,
            },
        )
        return result

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
