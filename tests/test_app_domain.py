from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from tin_lite.api import router
from tin_lite.auth_redirects import auth_return_url
from tin_lite.mcp_oauth import bearer_challenge
from tin_lite.mcp_server import _project_links
from tin_lite.product_urls import dashboard_url
from tin_lite.settings import Settings

SERVICE = "https://lite.tin.computer"
APP = "https://app.tin.computer"
SETTINGS = SimpleNamespace(
    switchboard_public_url=SERVICE,
    app_url=APP,
    clerk_publishable_key="pk_test_placeholder",
    clerk_frontend_api_url="https://clerk.tin.computer",
)


def test_app_fallback_and_stable_mcp_identity():
    assert dashboard_url(SimpleNamespace(switchboard_public_url=SERVICE)) == SERVICE
    assert dashboard_url(SETTINGS) == APP
    assert f"{SERVICE}/.well-known/" in bearer_challenge(SETTINGS)
    assert all(
        link.startswith(APP + "/") for link in _project_links(SETTINGS, UUID(int=1)).values()
    )


@pytest.mark.parametrize("origin", [SERVICE, APP])
def test_both_exact_auth_return_origins(origin):
    target = f"{origin}/?project=existing"
    assert auth_return_url(SETTINGS, target) == (target, False)


@pytest.mark.parametrize(
    "target",
    [
        "https://app.tin.computer.evil.test/",
        "https://evil.test/",
        "https://app.tin.computer:444/",
        "https://app.tin.computer/sign-in",
        "https://app.tin.computer/sign-up/verify-email-address",
        "https://app.tin.computer/#document/anything",
    ],
)
def test_auth_does_not_widen_returns(target):
    with pytest.raises(ValueError):
        auth_return_url(SETTINGS, target)


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.tin.computer/path",
        "https://app.tin.computer?next=evil",
        "https://app.tin.computer/#anything",
        "https://user:password@app.tin.computer",
        "//app.tin.computer",
        "http://app.tin.computer",
        "https://app.tin.computer:bad",
        "https://app.tin.computer\\evil",
        "https://app.tin.computer\n",
        "",
    ],
)
def test_reject_unsafe_app_setting(origin):
    with pytest.raises(ValueError):
        Settings.validate_app_url(origin)


@pytest.mark.parametrize("origin", [APP, APP + "/", "http://127.0.0.1:8000"])
def test_valid_app_setting(origin):
    assert Settings.validate_app_url(origin) == origin.rstrip("/")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "path",
    [
        "/?project=existing&billing_payment=payment",
        "/billing?project=existing",
        "/system?project=existing",
        "/document/00000000-0000-0000-0000-000000000001?return=decisions",
        "/connect?project=existing&providers=infra.github,analytics.gsc",
        "/documents/runs/00000000-0000-0000-0000-000000000001?source=retained",
    ],
)
async def test_legacy_browser_routes_redirect_without_dropping_query(path, method):
    app = FastAPI()
    app.state.settings = SETTINGS
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=SERVICE
    ) as client:
        result = await client.request(method, path)
    assert result.status_code == 302
    assert result.headers["location"] == APP + path
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/sign-in",
        "/sign-up",
        "/integrations/callback/google?state=x&code=y",
        "/integrations/callback/github?state=x&code=y",
        "/?redirect_url=https%3A%2F%2Fclerk.tin.computer%2Foauth%2Fauthorize%3Fstate%3Dx",
    ],
)
async def test_legacy_auth_and_callback_pages_finish_on_original_origin(path):
    app = FastAPI()
    app.state.settings = SETTINGS
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=SERVICE
    ) as client:
        result = await client.get(path)
    assert result.status_code == 200
    assert f'data-app-url="{APP}"' in result.text
    assert f'data-mcp-url="{SERVICE}/mcp"' in result.text


@pytest.mark.asyncio
async def test_new_origin_shell_and_no_host_header_redirect_target():
    app = FastAPI()
    app.state.settings = SETTINGS
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP) as client:
        result = await client.get("/")
        foreign = await client.get("/", headers={"Host": "evil.test"})
    assert result.status_code == 200
    assert f'<link rel="canonical" href="{APP}/"' in result.text
    assert f'data-mcp-url="{SERVICE}/mcp"' in result.text
    assert "location" not in foreign.headers
    assert "evil.test" not in foreign.text


@pytest.mark.parametrize(
    "path",
    [
        "/system",
        "/chat",
        "/activity",
        "/decisions",
        "/people",
        "/analytics",
        "/files",
        "/integrations",
        "/billing",
        "/file",
        "/document/00000000-0000-0000-0000-000000000001",
        "/task/00000000-0000-0000-0000-000000000001",
        "/compare/00000000-0000-0000-0000-000000000001",
        "/sign-in/factor-one",
        "/sign-up/verify-email-address",
    ],
)
async def test_dashboard_and_clerk_paths_support_direct_load(path):
    app = FastAPI()
    app.state.settings = SETTINGS
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP) as client:
        result = await client.get(path + "?project=existing")
    assert result.status_code == 200
    assert 'id="app-shell"' in result.text
    assert "no-store" in result.headers["cache-control"]


@pytest.mark.parametrize(
    "path",
    [
        "/not-a-page",
        "/api/not-a-page",
        "/document/not-a-run",
        "/integrations/callback/not-a-provider",
    ],
)
async def test_dashboard_routes_do_not_swallow_unknown_or_service_paths(path):
    app = FastAPI()
    app.state.settings = SETTINGS
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP) as client:
        result = await client.get(path)
    assert result.status_code == 404


def test_local_self_host_auth_return_requires_exact_configured_origin():
    settings = SimpleNamespace(
        **{**vars(SETTINGS), "switchboard_public_url": "http://127.0.0.1:8000", "app_url": None}
    )
    target = "http://127.0.0.1:8000/billing?project=existing"
    assert auth_return_url(settings, target) == (target, False)
    for target in [
        "http://127.0.0.1:9000/billing",
        "http://evil.test/billing",
        "http://app.tin.computer/billing",
    ]:
        with pytest.raises(ValueError):
            auth_return_url(settings, target)


@pytest.mark.asyncio
async def test_full_domain_switch_advertises_app_and_keeps_explicit_legacy_returns():
    settings = SimpleNamespace(**vars(SETTINGS))
    settings.switchboard_public_url = APP
    settings.legacy_public_url = SERVICE
    assert f"{APP}/.well-known/" in bearer_challenge(settings)
    target = f"{SERVICE}/mcp/consent?client_id=existing&state=opaque"
    assert auth_return_url(settings, target) == (target, True)
    app = FastAPI()
    app.state.settings = settings
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=SERVICE
    ) as client:
        old = await client.get("/?project=existing")
        callback = await client.get("/integrations/callback/github?state=x&code=y")
        new = await client.get(APP + "/")
    assert old.status_code == 302
    assert old.headers["location"] == APP + "/?project=existing"
    assert callback.status_code == 200
    assert new.status_code == 200
    assert f'data-mcp-url="{APP}/mcp"' in new.text
    assert f'data-mcp-url="{SERVICE}/mcp"' not in new.text
    settings.legacy_public_url = None
    with pytest.raises(ValueError):
        auth_return_url(settings, target)
