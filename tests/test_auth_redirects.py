from html.parser import HTMLParser
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from tin_lite.api import router

SETTINGS = SimpleNamespace(
    clerk_publishable_key="pk_test_placeholder",
    clerk_frontend_api_url="https://clerk.tin.computer",
    switchboard_public_url="https://lite.tin.computer",
)


class AuthRoot(HTMLParser):
    attrs: dict[str, str]

    def handle_starttag(self, tag, attrs):
        if tag == "main" and dict(attrs).get("id") == "auth-root":
            self.attrs = dict(attrs)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/", "/sign-in", "/sign-up"])
@pytest.mark.parametrize(
    ("continuation", "flow"),
    [
        (None, "product"),
        ("https://clerk.tin.computer/oauth/authorize/continue?state={{AUTH_FLOW}}", "mcp"),
        ("https://accounts.tin.computer/oauth-consent?state=opaque&scope=openid", "mcp"),
        ("https://clerk.tin.computer/oauth/authorize/continue?state=a%2Bb%26c&scope=openid", "mcp"),
        ("https://clerk.tin.computer/oauth/authorize?state=a%2Bb%26c&scope=openid", "mcp"),
        ("https://lite.tin.computer/mcp/consent?client_id=a&state=opaque%2Bstate", "mcp"),
        ("https://lite.tin.computer/?project=existing", "product"),
        ("https://tin.computer/dashboard?from=signin", "product"),
    ],
)
async def test_auth_pages_preserve_only_validated_continuations(route, continuation, flow):
    app = FastAPI()
    app.state.settings = SETTINGS
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://lite.tin.computer"
    ) as client:
        response = await client.get(
            route, params={"redirect_url": continuation} if continuation else {}
        )
    assert response.status_code == 200
    parsed = AuthRoot()
    parsed.feed(response.text)
    assert parsed.attrs["data-auth-return"] == (continuation or "")
    assert parsed.attrs["data-auth-flow"] == flow
    assert response.headers["cache-control"] == "no-store, max-age=0"


CURATOR = "resource=https%3A%2F%2Fcurator.tin.computer%2Fmcp"
AUTHORIZE = "https://clerk.tin.computer/oauth/authorize"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/sign-in", "/sign-up"])
@pytest.mark.parametrize(
    ("continuation", "other_app"),
    [
        (f"{AUTHORIZE}?client_id=a&{CURATOR}", "Curator"),
        (f"https://lite.tin.computer/mcp/consent?client_id=a&{CURATOR}", "Curator"),
        (f"{AUTHORIZE}?{CURATOR}&{CURATOR}", "Curator"),
        (f"{AUTHORIZE}?client_id=a&resource=https%3A%2F%2Flite.tin.computer%2Fmcp", ""),
        (f"{AUTHORIZE}?{CURATOR}&resource=https%3A%2F%2Fevil.test%2Fmcp", ""),
        (f"https://tin.computer/dashboard?{CURATOR}", ""),
    ],
)
async def test_an_agent_signing_in_to_another_app_is_told_which(route, continuation, other_app):
    app = FastAPI()
    app.state.settings = SETTINGS
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://lite.tin.computer"
    ) as client:
        response = await client.get(route, params={"redirect_url": continuation})
    assert response.status_code == 200
    parsed = AuthRoot()
    parsed.feed(response.text)
    assert parsed.attrs["data-auth-return"] == continuation
    assert parsed.attrs["data-auth-app"] == other_app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test/",
        "//evil.test/",
        "javascript:alert(1)",
        "https://clerk.tin.computer.evil.test/oauth/authorize",
        "https://clerk.tin.computer@evil.test/oauth/authorize",
        "https://user@clerk.tin.computer/oauth/authorize",
        "https://clerk.tin.computer/other",
        "https://accounts.tin.computer/other",
        "https://clerk.tin.computer:444/oauth/authorize",
        "http://lite.tin.computer/",
        "https://lite.tin.computer/sign-in/",
        "https://lite.tin.computer/sign-up",
        "https://lite.tin.computer/#elsewhere",
        "https://lite.tin.computer\\@evil.test/",
        "https://lite.tin.computer/\n",
        "https://lite.tin.computer/\x7f",
        "",
        "a" * 16_385,
    ],
)
async def test_auth_pages_reject_open_redirects_and_loops(target):
    app = FastAPI()
    app.state.settings = SETTINGS
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://lite.tin.computer"
    ) as client:
        response = await client.get("/sign-in", params={"redirect_url": target})
    assert response.status_code == 400
    assert target not in response.text or not target


@pytest.mark.asyncio
async def test_auth_page_rejects_duplicate_return_addresses_and_escapes_attributes():
    app = FastAPI()
    app.state.settings = SETTINGS
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://lite.tin.computer"
    ) as client:
        duplicate = await client.get(
            "/sign-in",
            params=[
                ("redirect_url", "https://tin.computer/"),
                ("redirect_url", "https://evil.test/"),
            ],
        )
        continuation = (
            'https://clerk.tin.computer/oauth/authorize?state="><script>alert(1)</script>'
        )
        escaped = await client.get("/sign-in", params={"redirect_url": continuation})
    assert duplicate.status_code == 400
    assert "<script>alert(1)</script>" not in escaped.text
    parsed = AuthRoot()
    parsed.feed(escaped.text)
    assert parsed.attrs["data-auth-return"] == continuation
