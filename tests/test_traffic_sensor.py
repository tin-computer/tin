from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from tin_lite.api import router
from tin_lite.mcp_oauth import router as consent_router
from tin_lite.settings import Settings
from tin_lite.traffic_sensor import TRAFFIC_SENSOR_SRC, traffic_sensor_script


def test_sensor_markup_only_when_configured():
    assert traffic_sensor_script(SimpleNamespace(traffic_sensor_site=None)) == ""
    assert traffic_sensor_script(SimpleNamespace(traffic_sensor_site="organic-tin")) == (
        f'<script async src="{TRAFFIC_SENSOR_SRC}" data-site="organic-tin"></script>'
    )


@pytest.mark.parametrize("site", ["", "Organic", 'x"onload="y', "a b", "a" * 65, "-x"])
def test_reject_unsafe_site_id(site):
    with pytest.raises(ValueError):
        Settings.validate_traffic_sensor_site(site)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/sign-in",
        "/mcp/consent?client_id=test",
        "/documents/runs/00000000-0000-0000-0000-000000000001",
    ],
)
async def test_product_pages_load_sensor_from_operator_setting_only(path, enabled):
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        switchboard_public_url="https://tin.test",
        app_url=None,
        clerk_publishable_key="pk_test_placeholder",
        clerk_frontend_api_url="https://clerk.tin.test",
        private_fonts_stylesheet_url=None,
        traffic_sensor_site="organic-tin" if enabled else None,
    )
    app.include_router(router)
    app.include_router(consent_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert "<!--TRAFFIC_SENSOR_SCRIPT-->" not in response.text
    assert (TRAFFIC_SENSOR_SRC in response.text) == enabled
    if enabled:
        assert 'data-site="organic-tin"' in response.text
        assert response.text.index(TRAFFIC_SENSOR_SRC) < response.text.index("</head>")
