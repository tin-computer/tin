"""Exercise browser tool bounds without installing a browser or making network calls."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError


@pytest.fixture
def browser_tools(monkeypatch):
    for name in ("camoufox", "camoufox.addons", "camoufox.async_api"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["camoufox.addons"].DefaultAddons = SimpleNamespace(UBO="ubo")
    sys.modules["camoufox.async_api"].AsyncCamoufox = AsyncMock()
    spec = importlib.util.spec_from_file_location(
        "test_cfx_tools", Path(__file__).parents[1] / "sandbox" / "camoufox_mcp.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    page = SimpleNamespace(
        set_viewport_size=AsyncMock(),
        evaluate=AsyncMock(return_value={"width": 390, "height": 844}),
        screenshot=AsyncMock(return_value=b"jpeg bytes"),
    )
    module._STATE = module.BrowserState(page=page)
    return module, page


@pytest.mark.asyncio
async def test_resize_keeps_page_and_reports_measured_dimensions(browser_tools):
    tools, page = browser_tools
    assert json.loads(await tools.set_viewport(390, 844)) == {"width": 390, "height": 844}
    page.set_viewport_size.assert_awaited_once_with({"width": 390, "height": 844})
    assert tools._STATE.page is page
    page.evaluate.return_value = {"width": 1440, "height": 900}
    with pytest.raises(ToolError, match="did not resize"):
        await tools.set_viewport(390, 844)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(319, 844), (390, 319), (1921, 900), (390, 1201)])
async def test_resize_rejects_unbounded_dimensions_before_browser_access(browser_tools, size):
    tools, page = browser_tools
    with pytest.raises(ToolError, match="viewport must be"):
        await tools.set_viewport(*size)
    page.set_viewport_size.assert_not_awaited()


@pytest.mark.asyncio
async def test_screenshot_returns_image_in_memory_with_pixel_and_byte_bounds(browser_tools):
    tools, page = browser_tools
    image = await tools.screenshot()
    assert image.data == b"jpeg bytes" and image.path is None
    assert image.to_image_content().mime_type == "image/jpeg"
    page.screenshot.assert_awaited_once_with(
        type="jpeg", quality=80, full_page=False, scale="css", timeout=30_000
    )
    page.screenshot.return_value = b"x" * (tools.MAX_SCREENSHOT_BYTES + 1)
    with pytest.raises(ToolError, match="exceeds 2 MB"):
        await tools.screenshot()
    page.screenshot.reset_mock()
    page.evaluate.return_value = {"width": 390, "height": 5000}
    with pytest.raises(ToolError, match="set a viewport"):
        await tools.screenshot()
    page.screenshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_registers_image_output_and_resize_inputs(browser_tools):
    tools, _ = browser_tools
    registered = {t.name: t for t in await tools.server.list_tools()}
    assert registered["screenshot"].input_schema["properties"] == {}
    assert set(registered["set_viewport"].input_schema["required"]) == {"width", "height"}
