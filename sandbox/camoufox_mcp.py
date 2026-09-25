#!/usr/bin/env python3
"""Tin-owned MCP server that drives one persistent Camoufox browser for a procedure run.

The server launches a single fingerprint-hardened Firefox on the first tool call and keeps one
page open for the rest of the run, so cookies, login state, and the page itself survive between
tool calls. Tools return bounded text or a viewport screenshot; no downloads or file writes.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import functools
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from camoufox.addons import DefaultAddons
from camoufox.async_api import AsyncCamoufox
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_PROXY = "socks5://127.0.0.1:40000"
DEFAULT_EGRESS = "split"
HCAPTCHA_FRAME_MARKER = "hcaptcha.com/captcha/"
MAX_TEXT_CHARS = 20_000
MAX_ERROR_CHARS = 1_500
EVENT_BUFFER = 200
NAVIGATION_TIMEOUT_MS = 60_000
ACTION_TIMEOUT_MS = 30_000
MAX_SCREENSHOT_BYTES = 2_000_000
MAX_VIEWPORT_WIDTH = 1920
MAX_VIEWPORT_HEIGHT = 1200
TURNSTILE_HOST = "challenges.cloudflare.com"
TURNSTILE_RESPONSE_JS = (
    "() => { const el = document.querySelector('input[name=\"cf-turnstile-response\"]');"
    " return Boolean(el && el.value); }"
)
TURNSTILE_WIDGET_JS = (
    "() => { const el = document.querySelector('.cf-turnstile, [data-sitekey]');"
    " if (!el) return null; const r = el.getBoundingClientRect();"
    " return {x: r.x, y: r.y, width: r.width, height: r.height}; }"
)
TOOL_NAMES = (
    "navigate",
    "current_page",
    "page_text",
    "snapshot",
    "set_viewport",
    "screenshot",
    "click",
    "click_role",
    "fill",
    "press",
    "wait_for",
    "evaluate",
    "console_messages",
    "network_failures",
    "turnstile_state",
    "click_turnstile",
    "hcaptcha_state",
    "click_hcaptcha",
)


@dataclass
class BrowserState:
    page: Any
    console: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=EVENT_BUFFER)
    )
    failures: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=EVENT_BUFFER)
    )


_STATE: BrowserState | None = None
_LAUNCH_LOCK = asyncio.Lock()
_CONTEXT_MANAGER: Any = None


def _watch(state: BrowserState, page: Any) -> None:
    page.on("console", lambda msg: state.console.append(f"[{msg.type}] {msg.text}"))
    page.on(
        "requestfailed",
        lambda req: state.failures.append(f"{req.method} {req.url} failed: {req.failure}"),
    )
    page.on(
        "response",
        lambda resp: (
            state.failures.append(f"{resp.status} {resp.request.method} {resp.url}")
            if resp.status >= 400
            else None
        ),
    )


def _adopt(state: BrowserState, page: Any) -> None:
    """A link that opens a new tab becomes the active page; the old one is closed."""
    previous = state.page
    state.page = page
    _watch(state, page)
    if previous is not page and not previous.is_closed():
        asyncio.get_running_loop().create_task(previous.close())


def _split_pac(proxy: str) -> str:
    """A PAC script that sends only Cloudflare's challenge infrastructure through the WARP
    SOCKS proxy (its dependencies are IPv6-only and E2B egress is IPv4-only) and everything
    else out directly, so payment processors and their risk checks see the sandbox's own
    cloud IP rather than a shared Cloudflare VPN exit."""
    parts = urlsplit(proxy)
    hop = f"SOCKS5 {parts.hostname}:{parts.port or 1080}"
    script = (
        "function FindProxyForURL(url, host) {"
        ' if (dnsDomainIs(host, ".cloudflare.com") || dnsDomainIs(host, ".cloudflareinsights.com"))'
        f' return "{hop}";'
        ' return "DIRECT"; }'
    )
    return (
        "data:application/x-ns-proxy-autoconfig;base64,"
        + base64.b64encode(script.encode()).decode()
    )


def _launch_options() -> dict[str, Any]:
    profile_dir = os.environ["TIN_BROWSER_PROFILE_DIR"]
    # An explicitly empty TIN_BROWSER_PROXY runs without a proxy for local smoke tests only.
    proxy = os.environ.get("TIN_BROWSER_PROXY", DEFAULT_PROXY)
    # TIN_BROWSER_EGRESS: "warp" sends everything through the WARP proxy, "direct" uses the
    # sandbox's own egress, "split" (default) uses WARP only for Cloudflare challenge hosts.
    egress = os.environ.get("TIN_BROWSER_EGRESS", DEFAULT_EGRESS) if proxy else "direct"
    options: dict[str, Any] = {
        # "virtual" runs Firefox on an Xvfb display instead of native headless mode: native
        # headless has no WebGL and leaks headless tells, which Stripe Checkout's hCaptcha and
        # similar risk checks score against. Xvfb and Mesa are installed in the browser image.
        "headless": "virtual",
        "humanize": True,
        # Timezone and locale follow the IP the sites see: the proxy exit in "warp" mode, the
        # sandbox's own public address otherwise.
        "geoip": True,
        "exclude_addons": [DefaultAddons.UBO],
        "persistent_context": True,
        "user_data_dir": profile_dir,
    }
    if egress == "warp":
        options["proxy"] = {"server": proxy}
    elif egress == "split":
        options["firefox_user_prefs"] = {
            "network.proxy.type": 2,
            "network.proxy.autoconfig_url": _split_pac(proxy),
            "network.proxy.socks_remote_dns": True,
        }
    return options


async def _launch() -> BrowserState:
    """Start the browser on first use. Codex gives an MCP server only seconds to answer its
    handshake, so the browser must not launch before the server is listening."""
    global _STATE, _CONTEXT_MANAGER
    async with _LAUNCH_LOCK:
        if _STATE is None:
            manager = AsyncCamoufox(**_launch_options())
            context = await manager.__aenter__()
            _CONTEXT_MANAGER = manager
            page = context.pages[0] if context.pages else await context.new_page()
            state = BrowserState(page=page)
            _watch(state, page)
            context.on("page", lambda opened: _adopt(state, opened))
            _STATE = state
        return _STATE


@asynccontextmanager
async def _browser(_server: MCPServer):
    global _STATE, _CONTEXT_MANAGER
    try:
        yield None
    finally:
        manager, _CONTEXT_MANAGER, _STATE = _CONTEXT_MANAGER, None, None
        if manager is not None:
            await manager.__aexit__(None, None, None)


server = MCPServer(
    "camoufox",
    instructions=(
        "One persistent, fingerprint-hardened Firefox for this run. Every tool acts on the "
        "same page, so state survives between calls; never expect a fresh browser."
    ),
    lifespan=_browser,
)


async def _page() -> Any:
    return (await _launch()).page


def _reported(tool):
    """Surface the browser's own error text (a timeout, a missing selector, a navigation
    failure) to the agent; the MCP SDK hides the message of any other exception."""

    @functools.wraps(tool)
    async def wrapper(*args, **kwargs):
        try:
            return await tool(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            detail = " ".join(str(exc).split())[:MAX_ERROR_CHARS]
            raise ToolError(f"{tool.__name__} failed: {type(exc).__name__}: {detail}") from exc

    return wrapper


def _bounded(text: str, max_chars: int) -> str:
    limit = max(1, min(int(max_chars), MAX_TEXT_CHARS))
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("only http and https URLs can be opened")


async def _settle(page: Any) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - a settled page is best effort after an action
        return


async def _describe(page: Any, max_chars: int) -> str:
    # An action that starts a navigation can tear down the document while it is being read;
    # wait for the new document instead of failing the whole tool call.
    for attempt in range(3):
        await _settle(page)
        try:
            title = await page.title()
            text = await page.inner_text("body", timeout=ACTION_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - retried below; the last failure propagates
            if attempt == 2:
                raise
            await asyncio.sleep(1)
            continue
        return f"url: {page.url}\ntitle: {title}\n\n{_bounded(text, max_chars)}"
    raise RuntimeError("the page did not settle")


async def _turnstile_box(page: Any) -> dict[str, float] | None:
    for frame in page.frames:
        if TURNSTILE_HOST not in frame.url:
            continue
        element = await frame.frame_element()
        box = await element.bounding_box()
        if box:
            return box
    return await page.evaluate(TURNSTILE_WIDGET_JS)


async def _turnstile(page: Any) -> dict[str, Any]:
    box = await _turnstile_box(page)
    token = bool(await page.evaluate(TURNSTILE_RESPONSE_JS))
    return {"widget_present": box is not None, "token_present": token, "widget_box": box}


@server.tool()
@_reported
async def navigate(url: str) -> str:
    """Open an http(s) URL in the one persistent page and return its URL, title, and text."""
    _check_url(url)
    page = await _page()
    await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    return await _describe(page, 4000)


@server.tool()
@_reported
async def current_page() -> str:
    """Return the current URL and title without touching the page."""
    page = await _page()
    return f"url: {page.url}\ntitle: {await page.title()}"


@server.tool()
@_reported
async def page_text(max_chars: int = 8000) -> str:
    """Return the visible text of the current page, bounded to max_chars."""
    page = await _page()
    return _bounded(await page.inner_text("body"), max_chars)


@server.tool()
@_reported
async def snapshot(max_chars: int = 12000) -> str:
    """Return the accessibility tree of the current page as text, bounded to max_chars."""
    page = await _page()
    return _bounded(await page.locator("body").aria_snapshot(), max_chars)


@server.tool()
@_reported
async def set_viewport(width: int, height: int) -> str:
    """Set a 320–1920 by 320–1200 CSS-pixel viewport in the same page.

    This tests responsive layout, not a mobile OS, touch device or mobile browser.
    Returns measured dimensions so a failed resize cannot be mistaken for mobile evidence.
    """
    if not 320 <= width <= MAX_VIEWPORT_WIDTH or not 320 <= height <= MAX_VIEWPORT_HEIGHT:
        raise ValueError("viewport must be 320–1920 pixels wide and 320–1200 pixels high")
    page = await _page()
    await page.set_viewport_size({"width": width, "height": height})
    actual = await page.evaluate("() => ({width: innerWidth, height: innerHeight})")
    if actual != {"width": width, "height": height}:
        raise RuntimeError(f"viewport did not resize: {actual}")
    return json.dumps(actual)


@server.tool()
@_reported
async def screenshot() -> Image:
    """Return a bounded JPEG of the visible viewport, without writing a file.

    Scroll the same page to inspect another region. No full-page capture or arbitrary paths.
    """
    page = await _page()
    size = await page.evaluate("() => ({width: innerWidth, height: innerHeight})")
    if not (0 < size["width"] <= MAX_VIEWPORT_WIDTH and 0 < size["height"] <= MAX_VIEWPORT_HEIGHT):
        raise ValueError("set a viewport of at most 1920 by 1200 before taking a screenshot")
    data = await page.screenshot(
        type="jpeg", quality=80, full_page=False, scale="css", timeout=ACTION_TIMEOUT_MS
    )
    if len(data) > MAX_SCREENSHOT_BYTES:
        raise ValueError("viewport screenshot exceeds 2 MB; use a smaller viewport")
    return Image(data=data, format="jpeg")


@server.tool()
@_reported
async def click(selector: str) -> str:
    """Click the first element matching a CSS or text selector, then describe the page."""
    page = await _page()
    await page.click(selector, timeout=ACTION_TIMEOUT_MS)
    return await _describe(page, 2000)


@server.tool()
@_reported
async def click_role(role: str, name: str) -> str:
    """Click the first element with an ARIA role and accessible name, then describe the page."""
    page = await _page()
    await page.get_by_role(role, name=name).first.click(timeout=ACTION_TIMEOUT_MS)
    return await _describe(page, 2000)


@server.tool()
@_reported
async def fill(selector: str, value: str) -> str:
    """Type a value into the first element matching a selector. The value is never echoed."""
    page = await _page()
    await page.fill(selector, value, timeout=ACTION_TIMEOUT_MS)
    return f"filled {selector}"


@server.tool()
@_reported
async def press(key: str) -> str:
    """Press a keyboard key such as Enter or Tab on the focused element."""
    page = await _page()
    await page.keyboard.press(key)
    return await _describe(page, 2000)


@server.tool()
@_reported
async def wait_for(
    selector: str | None = None, text: str | None = None, timeout_seconds: int = 30
) -> str:
    """Wait for a selector or visible text to appear, then describe the page."""
    page = await _page()
    timeout_ms = max(1, min(int(timeout_seconds), 180)) * 1000
    if selector:
        await page.wait_for_selector(selector, timeout=timeout_ms)
    elif text:
        await page.get_by_text(text).first.wait_for(timeout=timeout_ms)
    else:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    return await _describe(page, 2000)


@server.tool()
@_reported
async def evaluate(expression: str) -> str:
    """Evaluate a JavaScript expression or arrow function in the page and return JSON."""
    page = await _page()
    result = await page.evaluate(expression)
    return _bounded(json.dumps(result, default=str), 8000)


@server.tool()
@_reported
async def console_messages(limit: int = 50) -> str:
    """Return the most recent browser console messages."""
    state = await _launch()
    lines = list(state.console)[-max(1, min(int(limit), EVENT_BUFFER)) :]
    return _bounded("\n".join(lines) or "none", 8000)


@server.tool()
@_reported
async def network_failures(limit: int = 50) -> str:
    """Return recent failed requests and responses with status 400 or higher."""
    state = await _launch()
    lines = list(state.failures)[-max(1, min(int(limit), EVENT_BUFFER)) :]
    return _bounded("\n".join(lines) or "none", 8000)


@server.tool()
@_reported
async def turnstile_state() -> str:
    """Report whether a Cloudflare Turnstile widget is on the page and whether it holds a token."""
    return json.dumps(await _turnstile(await _page()))


@server.tool()
@_reported
async def click_turnstile(timeout_seconds: int = 30) -> str:
    """Click the Turnstile checkbox like a user and wait for the widget to issue its token."""
    page = await _page()
    state = await _turnstile(page)
    if state["token_present"]:
        return json.dumps(state)
    box = state["widget_box"]
    if not box:
        raise RuntimeError("no Turnstile widget is on the page")
    await page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
    deadline = asyncio.get_running_loop().time() + max(1, min(int(timeout_seconds), 180))
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1)
        state = await _turnstile(page)
        if state["token_present"]:
            break
    return json.dumps(state)


async def _frame_geometry(frame: Any) -> tuple[dict[str, float] | None, bool]:
    """The frame element's bounding box and visibility; (None, False) once it detaches."""
    try:
        element = await frame.frame_element()
        return await element.bounding_box(), await element.is_visible()
    except Exception:  # noqa: BLE001 - a detached frame has no geometry
        return None, False


async def _frame_eval(frame: Any, expression: str) -> Any:
    """Evaluate in a frame, returning None where a cross-origin frame refuses."""
    try:
        return await frame.evaluate(expression)
    except Exception:  # noqa: BLE001 - best-effort state read
        return None


async def _hcaptcha(page: Any) -> dict[str, Any]:
    """Locate hCaptcha's checkbox and challenge frames wherever they are nested (Stripe
    Checkout wraps them in its own iframe) and read the widget's state."""
    checkbox_box = None
    checkbox_checked = False
    challenge_present = False
    for frame in page.frames:
        url = frame.url
        if HCAPTCHA_FRAME_MARKER not in url:
            continue
        box, visible = await _frame_geometry(frame)
        if "frame=checkbox" in url:
            checkbox_box = box or checkbox_box
            checked = await _frame_eval(
                frame, "() => document.querySelector('#checkbox')?.getAttribute('aria-checked')"
            )
            checkbox_checked = checkbox_checked or checked == "true"
        elif "frame=challenge" in url and visible and box and box["width"] > 50:
            # hCaptcha keeps a hidden challenge frame mounted from the start; only a visible
            # one means the widget escalated to a picture puzzle.
            challenge_present = True
    token_present = False
    for frame in page.frames:
        value = await _frame_eval(
            frame, "() => document.querySelector('textarea[name=h-captcha-response]')?.value || ''"
        )
        if value:
            token_present = True
            break
    return {
        "widget_present": checkbox_box is not None,
        "checkbox_checked": checkbox_checked,
        "challenge_present": challenge_present,
        "token_present": token_present,
        "checkbox_box": checkbox_box,
    }


@server.tool()
@_reported
async def hcaptcha_state() -> str:
    """Report whether an hCaptcha widget is on the page, whether its checkbox is checked or a
    token issued, and whether it escalated to a visual challenge."""
    return json.dumps(await _hcaptcha(await _page()))


@server.tool()
@_reported
async def click_hcaptcha(timeout_seconds: int = 30) -> str:
    """Click the hCaptcha checkbox like a user and wait until it is checked, a token is issued,
    or the widget escalates to a visual challenge, which this browser cannot complete."""
    page = await _page()
    state = await _hcaptcha(page)
    if state["checkbox_checked"] or state["token_present"]:
        return json.dumps(state)
    box = state["checkbox_box"]
    if not box:
        raise RuntimeError("no hCaptcha checkbox is on the page")
    await page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
    deadline = asyncio.get_running_loop().time() + max(1, min(int(timeout_seconds), 180))
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1)
        state = await _hcaptcha(page)
        if state["checkbox_checked"] or state["token_present"] or state["challenge_present"]:
            break
    return json.dumps(state)


async def _registered_tool_names() -> list[str]:
    return sorted(tool.name for tool in await server.list_tools())


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        names = asyncio.run(_registered_tool_names())
        if names != sorted(TOOL_NAMES):
            print(f"registered tools {names} differ from TOOL_NAMES", file=sys.stderr)
            return 1
        print("camoufox-mcp tools: " + ", ".join(names))
        return 0
    if argv:
        print("usage: camoufox-mcp [--check]", file=sys.stderr)
        return 64
    if not os.environ.get("TIN_BROWSER_PROFILE_DIR"):
        print("TIN_BROWSER_PROFILE_DIR is required", file=sys.stderr)
        return 64
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
