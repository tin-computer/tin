"""The shared OAuth verifier remembers complete successes briefly, never rejections."""

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from tin_lite.auth import (
    OAUTH_VERIFY_CACHE_SECONDS,
    AuthContext,
    ClerkAuth,
    VerifiedOAuthTokenCache,
)
from tin_lite.mcp_server import ClerkOAuthTokenVerifier

RESOURCE = "https://tin.test/mcp"
ISSUER = "https://clerk.tin.test"
CLIENT = "registered_tin_client"
NOW = 1_800_000_000
USER = "user_Synthetic"


def payload(**changes):
    return {
        "subject": USER,
        "client_id": CLIENT,
        "scopes": ["openid"],
        "revoked": False,
        "expired": False,
        "aud": RESOURCE,
        "expiration": NOW + 3600,
        **changes,
    }


class Clock:
    def __init__(self) -> None:
        self.now = float(NOW)

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr("tin_lite.auth.time.time", value)
    return value


class Upstream:
    """Synthetic Clerk introspection; `responses` maps a token to (status, body)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.responses: dict[str, tuple[int, dict]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["access_token"]
        self.calls.append(token)
        status_code, body = self.responses.get(token, (401, {"error": "invalid"}))
        return httpx.Response(status_code, json=body)


@pytest.fixture
async def verifier():
    settings = SimpleNamespace(
        clerk_secret_key=SecretStr("synthetic-clerk-key"),
        clerk_jwt_key=None,
        clerk_authorized_parties=("https://tin.test",),
        mcp_oauth_client_ids=frozenset(),
        switchboard_public_url="https://tin.test/",
        clerk_frontend_api_url=ISSUER,
    )
    auth = ClerkAuth(settings)
    await auth.close()
    upstream = Upstream()
    auth._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))  # noqa: SLF001
    try:
        yield auth, upstream
    finally:
        await auth.close()


async def test_repeat_verification_within_ttl_skips_clerk(verifier, clock):
    auth, upstream = verifier
    upstream.responses["token-a"] = (200, payload())
    mcp = ClerkOAuthTokenVerifier(auth, resource=RESOURCE)

    first = await mcp.verify_token("token-a")
    clock.now += OAUTH_VERIFY_CACHE_SECONDS - 1
    second = await mcp.verify_token("token-a")
    setup = await auth.authenticate_oauth_token("token-a")

    assert upstream.calls == ["token-a"]
    assert first == second
    assert second is not None and second.subject == USER and second.token == "token-a"  # noqa: S105
    assert setup is not None and setup.raw_token == "token-a"  # noqa: S105
    assert setup.resource == RESOURCE and setup.client_id == CLIENT


async def test_a_different_token_is_verified_separately(verifier, clock):
    auth, upstream = verifier
    upstream.responses["token-a"] = (200, payload())
    upstream.responses["token-b"] = (200, payload(subject="user_Other"))

    a = await auth.authenticate_oauth_token("token-a")
    b = await auth.authenticate_oauth_token("token-b")

    assert upstream.calls == ["token-a", "token-b"]
    assert a is not None and a.clerk_user_id == USER
    assert b is not None and b.clerk_user_id == "user_Other"


@pytest.mark.parametrize(
    "rejection",
    [
        (401, {"error": "invalid"}),
        (500, {"error": "unavailable"}),
        (200, payload(revoked=True)),
        (200, payload(aud="https://another.test/mcp")),
        (200, payload(aud=None, resource=None)),
    ],
)
async def test_rejections_are_never_cached(verifier, clock, rejection):
    auth, upstream = verifier
    upstream.responses["token-a"] = rejection

    assert await auth.authenticate_oauth_token("token-a") is None
    assert await auth.authenticate_oauth_token("token-a") is None
    upstream.responses["token-a"] = (200, payload())
    assert await auth.authenticate_oauth_token("token-a") is not None

    assert upstream.calls == ["token-a"] * 3


async def test_entries_expire_after_the_ttl(verifier, clock):
    auth, upstream = verifier
    upstream.responses["token-a"] = (200, payload())

    assert await auth.authenticate_oauth_token("token-a") is not None
    # Revocation at Clerk is noticed no later than the TTL.
    upstream.responses["token-a"] = (200, payload(revoked=True))
    clock.now += OAUTH_VERIFY_CACHE_SECONDS - 0.5
    assert await auth.authenticate_oauth_token("token-a") is not None
    clock.now += 0.5
    assert await auth.authenticate_oauth_token("token-a") is None

    assert upstream.calls == ["token-a"] * 2


async def test_ttl_never_outlives_the_verified_token_expiry(verifier, clock):
    auth, upstream = verifier
    upstream.responses["token-a"] = (200, payload(expiration=NOW + 10))

    assert await auth.authenticate_oauth_token("token-a") is not None
    clock.now += 9
    assert await auth.authenticate_oauth_token("token-a") is not None
    clock.now += 1
    upstream.responses["token-a"] = (200, payload(expiration=NOW + 3600))
    assert await auth.authenticate_oauth_token("token-a") is not None

    assert upstream.calls == ["token-a"] * 2


async def test_the_raw_token_is_never_stored(verifier, clock):
    auth, upstream = verifier
    upstream.responses["token-a"] = (200, payload())

    assert await auth.authenticate_oauth_token("token-a") is not None

    entries = auth._oauth_cache._entries  # noqa: SLF001
    assert len(entries) == 1
    (key, (_, stored)), *_ = entries.items()
    assert isinstance(key, bytes) and len(key) == 32
    assert b"token-a" not in key
    assert stored.raw_token == ""


def context(expires_at=None):
    return AuthContext(USER, "oauth_token", client_id=CLIENT, expires_at=expires_at)  # noqa: S106


def test_cache_is_bounded_and_evicts_least_recently_used(clock):
    cache = VerifiedOAuthTokenCache(max_entries=3)
    for token in ("t1", "t2", "t3"):
        cache.put(token, context())
    assert cache.get("t1") is not None
    cache.put("t4", context())
    cache.put("t5", context())

    assert len(cache) == 3
    assert cache.get("t2") is None
    assert cache.get("t3") is None
    assert [cache.get(t) is not None for t in ("t1", "t4", "t5")] == [True, True, True]


def test_expired_entries_are_evicted_before_live_ones(clock):
    cache = VerifiedOAuthTokenCache(max_entries=2)
    cache.put("short", context(expires_at=NOW + 5))
    cache.put("long", context())
    clock.now += 5
    cache.put("new", context())

    assert len(cache) == 2
    assert cache.get("long") is not None
    assert cache.get("new") is not None
    assert cache.get("short") is None


def test_already_expired_results_are_not_stored(clock):
    cache = VerifiedOAuthTokenCache()
    cache.put("t1", context(expires_at=NOW))
    cache.put("t2", context(expires_at=NOW - 1))
    assert len(cache) == 0
