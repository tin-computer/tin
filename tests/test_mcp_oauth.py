from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from tin_lite.mcp_oauth import (
    AGENT_PROMPT,
    install_shell_probe_hint,
)
from tin_lite.mcp_oauth import router as mcp_oauth_router


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.tin.test",
        clerk_publishable_key="pk_test_Y2xlcmsudGluLnRlc3Qk",
    )


def _app(settings: SimpleNamespace) -> FastAPI:
    app = FastAPI()
    app.include_router(mcp_oauth_router)
    install_shell_probe_hint(app, settings)
    app.state.settings = settings
    return app


@pytest.mark.asyncio
async def test_retired_discovery_does_not_serve_a_mismatched_issuer() -> None:
    app = _app(_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        for route in [
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
        ]:
            response = await client.get(route)
            assert response.status_code == 410
            assert response.json()["authorization_server"] == "https://clerk.tin.test"
            assert "issuer" not in response.json()
            assert "authorization_endpoint" not in response.json()


@pytest.mark.asyncio
async def test_legacy_authorize_returns_to_clerk_with_the_exact_request() -> None:
    app = _app(_settings())
    query = (
        "client_id=client_codex&redirect_uri=http%3A%2F%2F127.0.0.1%3A4321%2Fcallback"
        "&response_type=code&scope=openid&state=abc%2Bdef%26ghi&code_challenge=xyz"
        "&code_challenge_method=S256&resource=https%3A%2F%2Ftin.test%2Fmcp"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        page = await client.get(f"/mcp/authorize?{query}")
        bare = await client.get("/mcp/authorize")
    assert page.status_code == 307
    assert page.headers["location"] == f"https://clerk.tin.test/oauth/authorize?{query}"
    assert page.headers["Cache-Control"] == "no-store"
    assert bare.status_code == 400


@pytest.mark.asyncio
async def test_shell_probe_of_mcp_gets_install_lines_and_the_bearer_challenge() -> None:
    app = _app(_settings())

    @app.get("/mcp")
    async def real_mcp_get() -> dict[str, str]:  # stands in for the mounted MCP app
        return {"stream": "mcp"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        curl = await client.get("/mcp")
        browser = await client.get("/mcp", headers={"Accept": "text/html"})
        mcp_client = await client.get(
            "/mcp", headers={"Accept": "application/json, text/event-stream"}
        )
        authenticated = await client.get("/mcp", headers={"Authorization": "Bearer t"})

    assert curl.status_code == 401
    assert curl.headers["content-type"].startswith("text/plain")
    assert curl.headers["www-authenticate"] == (
        'Bearer error="invalid_token", error_description="Authentication required", '
        'resource_metadata="https://tin.test/.well-known/oauth-protected-resource/mcp"'
    )
    assert "codex mcp add tin --url https://tin.test/mcp" in curl.text
    assert "claude mcp add -t http tin https://tin.test/mcp" in curl.text
    assert "Do not read the saved token from the keychain" in curl.text
    assert AGENT_PROMPT in curl.text
    assert browser.status_code == 401
    assert mcp_client.status_code == 200 and mcp_client.json() == {"stream": "mcp"}
    assert authenticated.status_code == 200


@pytest.mark.asyncio
async def test_consent_page_preserves_oauth_request_and_uses_clerk_component() -> None:
    app = _app(_settings())
    query = (
        "client_id=client_codex&redirect_uri=http%3A%2F%2F127.0.0.1%3A4321%2Fcallback"
        "&response_type=code&scope=openid&state=opaque%2Bstate%26value"
        "&code_challenge=challenge&code_challenge_method=S256"
        "&resource=https%3A%2F%2Ftin.test%2Fmcp"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        page = await client.get(f"/mcp/consent?{query}")
        bare = await client.get("/mcp/consent")

    assert page.status_code == 200
    assert bare.status_code == 400
    assert page.headers["cache-control"] == "no-store, max-age=0"
    assert page.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert 'id="clerk-oauth-consent"' in page.text
    assert "https://clerk.tin.test/npm/@clerk/clerk-js@6/" in page.text
    assert "Let your coding agent grow your project." in page.text
    assert "Then, back in your project" not in page.text  # no bottom section (Emre)
    assert AGENT_PROMPT not in page.text
    assert "<form" not in page.text  # Clerk, not Tin HTML, owns the consent decision.

    from html.parser import HTMLParser

    class Links(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.hrefs: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.hrefs.append(dict(attrs)["href"])

    links = Links()
    links.feed(page.text)
    for link in links.hrefs[:2]:
        parsed = urlsplit(link)
        assert parsed.netloc == "tin.test"
        returned = parse_qs(parsed.query)["redirect_url"][0]
        assert returned == f"https://tin.test/mcp/consent?{query}"


CURATOR_TITLE = "Let your coding agent use Curator."
TIN_TITLE = "Let your coding agent grow your project."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource", "title"),
    [
        ("&resource=https%3A%2F%2Fcurator.tin.computer%2Fmcp", CURATOR_TITLE),
        ("&resource=https%3A%2F%2Ftin.test%2Fmcp", TIN_TITLE),
        ("", TIN_TITLE),
        (
            "&resource=https%3A%2F%2Fcurator.tin.computer%2Fmcp"
            "&resource=https%3A%2F%2Fcurator.tin.computer%2Fmcp",
            CURATOR_TITLE,
        ),
        (
            "&resource=https%3A%2F%2Fcurator.tin.computer%2Fmcp&resource=https%3A%2F%2Ftin.test%2Fmcp",
            TIN_TITLE,
        ),
    ],
)
async def test_consent_page_names_the_app_the_agent_asked_for(resource, title) -> None:
    app = _app(_settings())
    query = f"client_id=client_claude&scope=openid&state=opaque{resource}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        page = await client.get(f"/mcp/consent?{query}")
    assert page.status_code == 200
    assert f"<h1>{title}</h1>" in page.text
    curator = title == CURATOR_TITLE
    assert ("plan your marketing" in page.text) is not curator
    assert ("read what you mark on them" in page.text) is curator


@pytest.mark.asyncio
async def test_consent_page_does_not_render_client_supplied_html_or_redirect_off_site() -> None:
    app = _app(_settings())
    query = (
        "client_id=%22%3E%3Cscript%3Ealert(1)%3C%2Fscript%3E"
        "&redirect_uri=https%3A%2F%2Fevil.test%2Fcallback&state=a%26b"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        page = await client.get(f"/mcp/consent?{query}")
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert 'href="https://evil.test' not in page.text
    assert 'src="https://evil.test' not in page.text
