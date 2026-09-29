"""MCP connection guidance and Clerk-owned OAuth consent on a Tin page.

Protected-resource discovery points directly to Clerk. The consent page is enabled
through Clerk's OAuth consent location, never by changing discovery or proxying tokens.
The earlier authorization page redirects old links to Clerk. Tin no longer serves
a rewritten authorization-server document: doing so fails issuer-origin validation.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

router = APIRouter()

STATIC_DIR = Path(__file__).with_name("static")
AUTHORIZE_PATH = "/mcp/authorize"
CONSENT_PATH = "/mcp/consent"
AGENT_PROMPT = "Use Tin to grow my project like a pro!"


def public_url(settings: Any) -> str:
    return str(settings.switchboard_public_url).rstrip("/")


def clerk_authorize_url(settings: Any, query: str) -> str:
    base = str(settings.clerk_frontend_api_url).rstrip("/")
    return f"{base}/oauth/authorize?{query}" if query else f"{base}/oauth/authorize"


def agent_hint(settings: Any) -> str:
    """What an agent reads when it probes /mcp from a shell instead of loading it."""
    mcp_url = f"{public_url(settings)}/mcp"
    return (
        "This is Tin's MCP server. Use your coding agent's native MCP connection, not "
        "a shell.\n"
        "\n"
        "Codex:\n"
        f"  codex mcp add tin --url {mcp_url}\n"
        "  then quit Codex, start it again in the project folder, and send the prompt again.\n"
        "\n"
        "Claude Code:\n"
        f"  claude mcp add -t http tin {mcp_url}\n"
        "  then /mcp to sign in, and send the prompt again.\n"
        "\n"
        "Both open a browser sign-in once. Do not read the saved token from the keychain or "
        "write your own client.\n"
        "\n"
        f'The prompt that starts the onboarding: "{AGENT_PROMPT}"\n'
    )


def bearer_challenge(settings: Any) -> str:
    resource_metadata = f"{public_url(settings)}/.well-known/oauth-protected-resource/mcp"
    return (
        'Bearer error="invalid_token", error_description="Authentication required", '
        f'resource_metadata="{resource_metadata}"'
    )


def is_bare_probe(request: Request) -> bool:
    """A GET on /mcp from something that is not an MCP client streaming events."""
    if request.method != "GET" or request.url.path != "/mcp":
        return False
    if request.headers.get("authorization"):
        return False
    return "text/event-stream" not in request.headers.get("accept", "")


def agent_hint_response(settings: Any) -> Response:
    return PlainTextResponse(
        agent_hint(settings),
        status_code=401,
        headers={"WWW-Authenticate": bearer_challenge(settings)},
    )


def install_shell_probe_hint(app: FastAPI, settings: Any) -> None:
    """Answer a shell's GET /mcp with the install lines instead of a bare 401."""

    @app.middleware("http")
    async def explain_mcp_to_shell_probes(request: Request, call_next):  # type: ignore[no-untyped-def]
        if is_bare_probe(request):
            return agent_hint_response(settings)
        return await call_next(request)


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
@router.get("/.well-known/openid-configuration", include_in_schema=False)
async def retired_authorization_server(request: Request) -> Response:
    # Metadata served from Tin with a Clerk issuer is invalid. Clients must discover
    # Clerk directly through Tin's protected-resource metadata, including old clients.
    return JSONResponse(
        {
            "error": "authorization_server_moved",
            "authorization_server": str(request.app.state.settings.clerk_frontend_api_url).rstrip(
                "/"
            ),
            "resource_metadata": (
                f"{public_url(request.app.state.settings)}/.well-known/oauth-protected-resource/mcp"
            ),
        },
        status_code=410,
        headers={"Cache-Control": "no-store"},
    )


@router.get(AUTHORIZE_PATH, include_in_schema=False)
async def legacy_mcp_authorize(request: Request) -> Response:
    if not request.query_params.get("client_id"):
        return PlainTextResponse(
            "Start the connection from your coding agent to get a fresh sign-in link.",
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    return RedirectResponse(
        clerk_authorize_url(request.app.state.settings, request.url.query),
        status_code=307,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get(CONSENT_PATH, response_class=HTMLResponse, include_in_schema=False)
async def mcp_consent_page(request: Request) -> Response:
    """Host Clerk's prebuilt consent component without interpreting OAuth parameters."""
    settings = request.app.state.settings
    if not request.query_params.get("client_id"):
        return PlainTextResponse(
            "Start the connection from your coding agent to get a fresh sign-in link.",
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    from tin_lite.api import ASSET_VERSION
    from tin_lite.fonts import private_font_stylesheet
    from tin_lite.traffic_sensor import TRAFFIC_SENSOR_MARKER, traffic_sensor_script

    # Clerk retains the original client, state, PKCE, scope and callback parameters.
    query = request.url.query
    return_url = f"{public_url(settings)}{CONSENT_PATH}?{query}"
    replacements = {
        "{{ASSET_VERSION}}": ASSET_VERSION,
        "{{CLERK_PUBLISHABLE_KEY}}": settings.clerk_publishable_key,
        "{{CLERK_FRONTEND_API_URL}}": settings.clerk_frontend_api_url,
        "{{SIGN_UP_URL}}": f"{public_url(settings)}/sign-up?"
        + urlencode({"redirect_url": return_url}),
        "{{SIGN_IN_URL}}": f"{public_url(settings)}/sign-in?"
        + urlencode({"redirect_url": return_url}),
        "{{AGENT_PROMPT}}": AGENT_PROMPT,
    }
    html = (STATIC_DIR / "mcp-consent.html").read_text()
    for marker, value in replacements.items():
        html = html.replace(marker, escape(str(value), quote=True))
    html = html.replace("<!--PRIVATE_FONTS_STYLESHEET-->", private_font_stylesheet(settings))
    html = html.replace(TRAFFIC_SENSOR_MARKER, traffic_sensor_script(settings))
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
        },
    )
