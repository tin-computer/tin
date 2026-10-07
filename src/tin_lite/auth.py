from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Annotated, Any

import httpx
import jwt
from clerk_backend_api import AuthenticateRequestOptions, authenticate_request
from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from tin_lite.settings import Settings

SESSION_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="ClerkSession",
    description="A Clerk session token minted for the signed-in Tin user.",
)


@dataclass(frozen=True)
class AuthContext:
    clerk_user_id: str
    token_type: str
    scopes: frozenset[str] = frozenset()
    session_id: str | None = None
    client_id: str | None = None
    expires_at: int | None = None
    raw_token: str = field(default="", repr=False)
    resource: str | None = None


OAUTH_VERIFY_CACHE_SECONDS = 60
OAUTH_VERIFY_CACHE_ENTRIES = 4096


class VerifiedOAuthTokenCache:
    """Bounded memory of completely verified OAuth results for the identical token.

    MCP runs stateless HTTP, so every request would otherwise repeat Clerk
    introspection. Only successful results of the full verification are kept, keyed
    by a SHA-256 digest; the raw token is never stored. An entry lives at most
    OAUTH_VERIFY_CACHE_SECONDS and never past the verified token expiry, which bounds
    how long a revocation at Clerk can go unnoticed. Rejections are never cached.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = OAUTH_VERIFY_CACHE_SECONDS,
        max_entries: int = OAUTH_VERIFY_CACHE_ENTRIES,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[bytes, tuple[float, AuthContext]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _key(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()

    def get(self, token: str) -> AuthContext | None:
        key = self._key(token)
        entry = self._entries.get(key)
        if entry is None:
            return None
        deadline, context = entry
        if time.time() >= deadline:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return replace(context, raw_token=token)

    def put(self, token: str, context: AuthContext) -> None:
        now = time.time()
        deadline = now + self._ttl_seconds
        if context.expires_at is not None:
            deadline = min(deadline, context.expires_at)
        if deadline <= now or self._max_entries <= 0:
            return
        key = self._key(token)
        self._entries[key] = (deadline, replace(context, raw_token=""))
        self._entries.move_to_end(key)
        if len(self._entries) > self._max_entries:
            for expired in [k for k, (until, _) in self._entries.items() if until <= now]:
                del self._entries[expired]
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


class ClerkAuth:
    """One verifier for browser sessions and Clerk-issued MCP OAuth tokens."""

    def __init__(
        self,
        settings: Settings,
        *,
        oauth_resource_path: str = "/mcp",
        allow_legacy_clients: bool = True,
    ) -> None:
        self._secret_key = settings.clerk_secret_key.get_secret_value()
        self._jwt_key = (
            settings.clerk_jwt_key.get_secret_value() if settings.clerk_jwt_key else None
        )
        self._authorized_parties = list(settings.clerk_authorized_parties)
        if oauth_resource_path not in {"/mcp", "/mcp/plugins"}:
            raise ValueError("unknown MCP resource")
        self._oauth_client_ids = (
            settings.mcp_oauth_client_ids if allow_legacy_clients else frozenset()
        )
        self._oauth_resource = f"{settings.switchboard_public_url.rstrip('/')}{oauth_resource_path}"
        self._oauth_issuer = settings.clerk_frontend_api_url
        # Fixed, operator-configured issuer only; never follow a token's jku/x5u.
        self._oauth_jwks = jwt.PyJWKClient(
            f"{self._oauth_issuer}/.well-known/jwks.json", timeout=5, lifespan=300
        )
        self._client = httpx.AsyncClient(
            base_url="https://api.clerk.com/v1",
            timeout=10,
            headers={"Authorization": f"Bearer {self._secret_key}"},
        )
        # Per verifier instance: results also depend on this instance's fixed
        # issuer, resource and legacy client policy, so entries never cross them.
        self._oauth_cache = VerifiedOAuthTokenCache()

    async def close(self) -> None:
        await self._client.aclose()

    async def primary_email(self, clerk_user_id: str) -> str | None:
        """The person's primary email from Clerk's Backend API, or None when unavailable."""
        try:
            response = await self._client.get(f"/users/{clerk_user_id}")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        addresses = payload.get("email_addresses") or []
        primary_id = payload.get("primary_email_address_id")
        for item in addresses:
            if isinstance(item, dict) and item.get("id") == primary_id:
                return item.get("email_address") or None
        for item in addresses:
            if isinstance(item, dict) and item.get("email_address"):
                return item["email_address"]
        return None

    async def authenticate_session(self, request: Request) -> AuthContext:
        try:
            state = await self._authenticate_session_request(
                request,
                authorized_parties=self._authorized_parties,
            )
            return _context_from_state(state, credential_kind="session_token")  # noqa: S106
        except HTTPException:
            if not self._authorized_parties or not _has_bearer_token_without_azp(request):
                raise

        # Clerk's Backend API mints short-lived session continuations without an
        # `azp` claim. Clerk's verification guidance says the authorized-party
        # comparison is skipped when that claim is absent. The second verification
        # still requires a valid Clerk signature and session; it is never used for
        # a token that names a foreign browser origin.
        state = await self._authenticate_session_request(request, authorized_parties=None)
        return _context_from_state(state, credential_kind="session_token")  # noqa: S106

    async def _authenticate_session_request(
        self,
        request: Request,
        *,
        authorized_parties: list[str] | None,
    ) -> Any:
        return await asyncio.to_thread(
            authenticate_request,
            request,
            AuthenticateRequestOptions(
                secret_key=self._secret_key,
                jwt_key=self._jwt_key,
                authorized_parties=authorized_parties,
                accepts_token=["session_token"],
            ),
        )

    async def authenticate_oauth_token(self, token: str) -> AuthContext | None:
        """The shared MCP and connection-setup verifier, with a short success cache."""
        cached = self._oauth_cache.get(token)
        if cached is not None:
            return cached
        context = await self._verify_oauth_token(token)
        if context is not None:
            self._oauth_cache.put(token, context)
        return context

    async def _verify_oauth_token(self, token: str) -> AuthContext | None:
        try:
            response = await self._client.post(
                "https://api.clerk.com/oauth_applications/access_tokens/verify",
                json={"access_token": token},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("revoked") is not False
            or payload.get("expired") is not False
            or ("active" in payload and payload["active"] is not True)
        ):
            return None
        client_id = payload.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            return None
        if "azp" in payload and payload["azp"] != client_id:
            return None
        if any(payload[key] != self._oauth_issuer for key in ("iss", "issuer") if key in payload):
            return None
        # Introspection establishes current validity, but may omit the audience
        # even when the signed access token contains it. Validate both sources;
        # neither an allowlist nor the other source can override a conflict.
        if any(
            not _includes_resource(payload[key], self._oauth_resource)
            for key in ("aud", "audience", "resource")
            if key in payload
        ):
            return None
        scopes = _token_scopes(payload)
        if "openid" not in scopes:
            return None
        try:
            expires_at = _oauth_expiration(payload)
        except ValueError:
            return None
        subject = _first_string(payload, "subject", "user_id", "sub")
        if subject is None or not subject.startswith("user_"):
            return None
        resource_bound = any(key in payload for key in ("aud", "audience", "resource"))
        if token.count(".") == 2:
            claims = await asyncio.to_thread(self._verified_oauth_jwt, token)
            if (
                claims is None
                or claims.get("sub") != subject
                or claims.get("client_id") != client_id
                or "sid" in claims
                or ("azp" in claims and claims["azp"] != client_id)
                or ("issuer" in claims and claims["issuer"] != self._oauth_issuer)
                or any(
                    not _includes_resource(claims[key], self._oauth_resource)
                    for key in ("aud", "audience", "resource")
                    if key in claims
                )
            ):
                return None
            try:
                jwt_expiration = _oauth_expiration(claims)
            except ValueError:
                return None
            if jwt_expiration is None:
                return None
            expires_at = min(expires_at, jwt_expiration) if expires_at else jwt_expiration
            resource_bound = resource_bound or any(
                key in claims for key in ("aud", "audience", "resource")
            )
        # New DCR/CIMD clients need no per-client operator step when Clerk has
        # issued a token for Tin. Explicit IDs remain an opt-in legacy policy
        # for tokens without resource binding, never automatic registration trust.
        if not resource_bound and client_id not in self._oauth_client_ids:
            return None
        return AuthContext(
            clerk_user_id=subject,
            token_type="oauth_token",  # noqa: S106
            scopes=scopes,
            client_id=client_id,
            expires_at=expires_at,
            raw_token=token,
            resource=self._oauth_resource,
        )

    def _verified_oauth_jwt(self, token: str) -> dict[str, Any] | None:
        try:
            if jwt.get_unverified_header(token).get("typ") not in (
                "JWT",
                "at+jwt",
                "application/at+jwt",
            ):
                return None
            key = self._jwt_key or self._oauth_jwks.get_signing_key_from_jwt(token).key
            return jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self._oauth_issuer,
                # Resource claims are checked above, including legacy tokens
                # with no aud. Signature, issuer and lifetime are always checked.
                options={"verify_aud": False, "require": ["iss", "sub", "exp", "client_id"]},
            )
        except (jwt.PyJWTError, ValueError, TypeError):
            return None

    async def verified_email_addresses(self, clerk_user_id: str) -> frozenset[str]:
        try:
            response = await self._client.get(f"/users/{clerk_user_id}")
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="identity service is unavailable",
            ) from exc
        addresses: set[str] = set()
        for item in body.get("email_addresses", []):
            if not isinstance(item, dict):
                continue
            verification = item.get("verification")
            if not isinstance(verification, dict) or verification.get("status") != "verified":
                continue
            email = item.get("email_address")
            if isinstance(email, str):
                addresses.add(email.strip().casefold())
        return frozenset(addresses)

    async def fresh_session_token(self, session_id: str) -> str:
        """Mint a short session continuation for Tin's authenticated internal API calls."""
        try:
            response = await self._client.post(
                f"/sessions/{session_id}/tokens",
                json={"expires_in_seconds": 180},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="identity service is unavailable",
            ) from exc
        token = body.get("jwt") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="identity service returned an invalid session token",
            )
        return token


async def require_user(
    request: Request,
    _credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(SESSION_BEARER),
    ],
) -> AuthContext:
    existing = getattr(request.state, "tin_auth", None)
    if isinstance(existing, AuthContext):
        return existing
    try:
        context = await request.app.state.auth.authenticate_session(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    await request.app.state.runtime.database.record_tin_user(context.clerk_user_id)
    request.state.tin_auth = context
    return context


def _context_from_state(state: Any, *, credential_kind: str) -> AuthContext:
    if not state.is_signed_in or not isinstance(state.payload, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    subject = state.payload.get("sub")
    if not isinstance(subject, str) or not subject.startswith("user_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # The pinned SDK classifies unprefixed JWTs as sessions. Require the verified
    # session claim and reject access-token JWT types before using that identity.
    session_id = state.payload.get("sid")
    try:
        header_type = jwt.get_unverified_header(state.token).get("typ")
    except (jwt.PyJWTError, TypeError):
        header_type = "invalid"
    if (
        not isinstance(session_id, str)
        or not session_id.startswith("sess_")
        or header_type not in (None, "JWT")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return AuthContext(
        clerk_user_id=subject,
        token_type=credential_kind,
        scopes=_token_scopes(state.payload),
        session_id=session_id,
        client_id=_first_string(state.payload, "client_id", "azp"),
        expires_at=(
            state.payload.get("exp") if isinstance(state.payload.get("exp"), int) else None
        ),
        raw_token=state.token or "",
    )


def _has_bearer_token_without_azp(request: Request) -> bool:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return False
    token = authorization.removeprefix("Bearer ")
    try:
        encoded_payload = token.split(".")[1]
        padding = "=" * (-len(encoded_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + padding))
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and "azp" not in payload


def _token_scopes(payload: dict[str, Any]) -> frozenset[str]:
    raw = payload.get("scope", payload.get("scopes", []))
    if isinstance(raw, str):
        return frozenset(part for part in raw.split() if part)
    if isinstance(raw, list):
        return frozenset(part for part in raw if isinstance(part, str) and part)
    return frozenset()


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _includes_resource(value: Any, resource: str) -> bool:
    if isinstance(value, str):
        return value == resource
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item for item in value)
        and resource in value
    )


def _oauth_expiration(payload: dict[str, Any]) -> int | None:
    # The OAuth verification endpoint's expiration is in seconds (unlike several
    # other Clerk endpoints). Null is its documented non-expiring token shape.
    if not any(key in payload for key in ("expiration", "expires_at", "exp")):
        raise ValueError("missing OAuth expiration")
    expirations = []
    for key in ("expiration", "expires_at", "exp"):
        if key not in payload:
            continue
        value = payload[key]
        if key == "expiration" and value is None:
            continue
        if (
            type(value) not in (int, float)
            or not 0 < value < 2**63
            or not math.isfinite(value)
            or value <= time.time()
        ):
            raise ValueError("invalid OAuth expiration")
        expirations.append(int(value))
    return min(expirations) if expirations else None
