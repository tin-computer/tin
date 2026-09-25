"""Live acceptance probe for the browser sandbox profile.

Creates one `tin-lite-codex-browser` sandbox with open egress and proves, as the unprivileged
user, that the shared `warp-up` script registers and connects Cloudflare WARP, that the WARP
SOCKS proxy reaches an IPv6-only host, that the Tin-owned `camoufox-mcp` server renders a
public page through WARP, and that the same server completes a Cloudflare Turnstile form:
the widget loads, issues a token, and the demo backend validates it. The sandbox is always
killed. Run from the switchboard's configured runtime environment:

    uv run python scripts/verify_browser_sandbox.py

The default Turnstile page is Cloudflare's own demo, which uses the always-pass test sitekey.
It proves the widget script, the challenge iframe, and its IPv6-only dependencies load and that
a token round-trips, not that Cloudflare's bot scoring accepts the browser. Point
`--turnstile-url` at a page with a real sitekey for that; the probe stops after the widget
reports a token when `--turnstile-marker` is empty.
"""

from __future__ import annotations

import argparse
import asyncio
from textwrap import dedent

from e2b import AsyncSandbox

from tin_lite.settings import get_settings

# Runs inside the sandbox with the cfx-venv python. It drives /opt/tin-lite/camoufox-mcp over
# stdio exactly as Codex does, so the acceptance exercises the real tool surface.
PROBE_CLIENT = dedent(
    r"""
    import asyncio
    import base64
    import json
    import os
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    def jpeg_dimensions(data):
        if data[:2] != b'\xff\xd8':
            fail('screenshot was not JPEG')
        offset = 2
        while offset + 9 <= len(data):
            marker = data[offset:offset+2]
            length = int.from_bytes(data[offset+2:offset+4], 'big')
            if marker in (b'\xff\xc0', b'\xff\xc1', b'\xff\xc2'):
                return (int.from_bytes(data[offset+7:offset+9], 'big'),
                        int.from_bytes(data[offset+5:offset+7], 'big'))
            if marker[0] != 255 or length < 2:
                break
            offset += 2 + length
        fail('screenshot had no JPEG dimensions')

    def fail(message):
        print(message, file=sys.stderr)
        raise SystemExit(72)

    async def call(session, tool, **arguments):
        result = await session.call_tool(tool, arguments)
        text = "\n".join(
            block.text for block in result.content if getattr(block, "type", "") == "text"
        )
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            fail(f"{tool} failed: {text[:2000]}")
        return text

    async def evidence(session):
        for name in ("console_messages", "network_failures"):
            print(f"--- {name}\n{await call(session, name)}", file=sys.stderr)

    class FontPage(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/font.woff2':
                body = Path('/home/user/probe-font.woff2').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'font/woff2')
            elif self.path == '/':
                body = b'''<meta name="viewport" content="width=device-width,initial-scale=1">
                <style>@font-face{font-family:TinProbe;src:url(/font.woff2)}
                @font-face{font-family:BrokenProbe;src:url(/missing.woff2)}
                #sample{font:40px TinProbe,monospace}#missing{font-family:BrokenProbe}
                #layout{display:grid;grid-template-columns:1fr 1fr}
                @media(max-width:600px){#layout{grid-template-columns:1fr}}</style>
                <div id="layout"><span id="sample">WWW iii 012345</span>
                <span id="missing">Expected missing font</span></div>'''
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
            else:
                body = b'Expected missing font'
                self.send_response(404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    async def visual_probe(session):
        fixture = HTTPServer(('127.0.0.1', 0), FontPage)
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        try:
            await call(session, 'navigate', url=f'http://127.0.0.1:{fixture.server_port}/')
            await call(session, 'evaluate', expression='''() => new Promise(resolve => {
              const deadline=Date.now()+5000;
              const check=()=>performance.getEntriesByType('resource').some(r=>
                r.name.endsWith('/font.woff2') && r.decodedBodySize>0) || Date.now()>deadline
                ? resolve(true) : setTimeout(check,100); check(); })''')
            for width, height in ((1440, 900), (390, 844)):
                dimensions = json.loads(await call(
                    session, 'set_viewport', width=width, height=height
                ))
                if dimensions != {'width': width, 'height': height}:
                    fail('viewport dimensions were not applied')
                state = json.loads(await call(session, 'evaluate', expression='''() => ({
                  narrow: matchMedia('(max-width:600px)').matches,
                  columns: getComputedStyle(document.querySelector('#layout'))
                    .gridTemplateColumns.split(' ').length,
                  downloaded: performance.getEntriesByType('resource').some(r=>
                    r.name.endsWith('/font.woff2') && r.decodedBodySize>0)
                })'''))
                expected = {'narrow': width < 600, 'columns': 1 if width < 600 else 2,
                            'downloaded': True}
                if state != expected:
                    fail(f'responsive/font evidence did not match: {state}')
                result = await session.call_tool('screenshot', {})
                images = [b for b in result.content if getattr(b, 'type', '') == 'image']
                if len(images) != 1 or not 0 < len(base64.b64decode(images[0].data)) <= 2_000_000:
                    fail('bounded screenshot was not returned through MCP')
                if jpeg_dimensions(base64.b64decode(images[0].data)) != (width, height):
                    fail('screenshot dimensions did not match the requested viewport')
            failures = await call(session, 'network_failures')
            if '404 GET' not in failures or '/missing.woff2' not in failures:
                fail('a real missing font was not reported')
            print('VISUAL_EVIDENCE_OK=true', flush=True)
        finally:
            fixture.shutdown()
            fixture.server_close()

    async def main():
        env = {
            "TIN_BROWSER_PROFILE_DIR": os.environ["TIN_PROBE_PROFILE_DIR"],
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        if "TIN_BROWSER_PROXY" in os.environ:
            env["TIN_BROWSER_PROXY"] = os.environ["TIN_BROWSER_PROXY"]
        params = StdioServerParameters(
            command=os.environ["TIN_PROBE_MCP_PYTHON"],
            args=[os.environ["TIN_PROBE_MCP_SCRIPT"]],
            env=env,
            cwd=os.environ["TIN_PROBE_PROFILE_DIR"],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await visual_probe(session)
                text = await call(session, "navigate", url=os.environ["TIN_PROBE_URL"])
                if os.environ["TIN_PROBE_MARKER"] not in text:
                    await evidence(session)
                    fail("camoufox-mcp did not render the probe page")
                print("CAMOUFOX_OK=true", flush=True)

                await call(session, "navigate", url=os.environ["TIN_TURNSTILE_URL"])
                if os.environ["TIN_TURNSTILE_MARKER"]:
                    # Cloudflare's demo form: a free-text user field and a read-only password.
                    await call(session, "fill", selector="#user", value="tin-probe")
                state = ""
                for attempt in range(6):
                    state = await call(session, "turnstile_state")
                    if '"token_present": true' in state:
                        break
                    if attempt == 2 and '"widget_present": true' in state:
                        state = await call(session, "click_turnstile", timeout_seconds=30)
                        if '"token_present": true' in state:
                            break
                    await asyncio.sleep(5)
                if '"token_present": true' not in state:
                    await evidence(session)
                    fail(f"Turnstile issued no token: {state}")
                print("TURNSTILE_TOKEN_OK=true", flush=True)

                if os.environ["TIN_TURNSTILE_MARKER"]:
                    await call(session, "click_role", role="button", name="Sign in")
                    text = await call(session, "wait_for", text=os.environ["TIN_TURNSTILE_MARKER"])
                    if os.environ["TIN_TURNSTILE_MARKER"] not in text:
                        await evidence(session)
                        fail("the Turnstile demo backend did not validate the token")
                print("TURNSTILE_OK=true", flush=True)

    asyncio.run(main())
    """
).strip()

VERIFY_SCRIPT = dedent(
    r"""
    #!/usr/bin/env bash
    set -euo pipefail
    umask 077

    if [[ "$(id -u)" -eq 0 ]]; then
      echo 'browser probe must run as the unprivileged sandbox user' >&2
      exit 70
    fi

    # warp-up is the same script the procedure runner uses.
    /opt/tin-lite/warp-up /tmp/warp
    code="$(curl --socks5-hostname 127.0.0.1:40000 -sS -o /dev/null \
      -w '%{http_code}' --max-time 20 https://ipv6.google.com/)" || true
    if [[ "${code}" == "200" ]]; then
      echo 'WARP_IPV6_OK=true'
    else
      echo "WARP SOCKS proxy did not reach an IPv6-only host (status ${code:-none})" >&2
      exit 71
    fi

    mkdir -p /tmp/cfx-profile
    TIN_PROBE_PROFILE_DIR=/tmp/cfx-profile \
    TIN_PROBE_MCP_PYTHON=/opt/tin-lite/cfx-venv/bin/python \
    TIN_PROBE_MCP_SCRIPT=/opt/tin-lite/camoufox-mcp \
      /opt/tin-lite/cfx-venv/bin/python /home/user/cfx-probe.py
    """
).strip()

REQUIRED_MARKERS = {
    "WARP_OK=true",
    "WARP_IPV6_OK=true",
    "CAMOUFOX_OK=true",
    "TURNSTILE_OK=true",
    "VISUAL_EVIDENCE_OK=true",
}


async def verify(*, url: str, marker: str, turnstile_url: str, turnstile_marker: str) -> None:
    from pathlib import Path

    settings = get_settings()
    sandbox = await AsyncSandbox.create(
        settings.e2b_browser_template,
        timeout=420,
        metadata={"purpose": "tin-lite-browser-acceptance", "profile": "browser"},
        lifecycle={"on_timeout": "pause", "auto_resume": False},
        network=None,
        api_key=settings.e2b_api_key.get_secret_value(),
    )
    try:
        script_path = "/home/user/verify-browser"
        await sandbox.files.write(script_path, VERIFY_SCRIPT)
        await sandbox.files.write("/home/user/cfx-probe.py", PROBE_CLIENT)
        await sandbox.files.write(
            "/home/user/probe-font.woff2",
            (
                Path(__file__).parents[1] / "src/tin_lite/static/fonts/geist-sans-regular.woff2"
            ).read_bytes(),
        )
        await sandbox.commands.run(f"chmod 700 {script_path}", timeout=20)
        result = await sandbox.commands.run(
            script_path,
            envs={
                "TIN_PROBE_URL": url,
                "TIN_PROBE_MARKER": marker,
                "TIN_TURNSTILE_URL": turnstile_url,
                "TIN_TURNSTILE_MARKER": turnstile_marker,
            },
            timeout=360,
        )
        observed = set(result.stdout.splitlines())
        if not REQUIRED_MARKERS <= observed:
            raise RuntimeError("browser acceptance markers were not produced")
    finally:
        await sandbox.kill()

    print(
        "live browser acceptance: PASS "
        "(warp-up registration and connect as user, WARP SOCKS egress with IPv6 reachability, "
        "camoufox-mcp rendered a public page through WARP, Turnstile widget loaded and its "
        "token validated through the same MCP tools, desktop/narrow layout and screenshots, "
        "successful font transfer and missing-font diagnostics, sandbox kill)"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--marker", default="Example Domain")
    parser.add_argument("--turnstile-url", default="https://demo.turnstile.workers.dev/")
    parser.add_argument(
        "--turnstile-marker",
        default="Turnstile token successfuly validated",
        help="text expected after submit; empty stops after the widget issues a token",
    )
    args = parser.parse_args()
    asyncio.run(
        verify(
            url=args.url,
            marker=args.marker,
            turnstile_url=args.turnstile_url,
            turnstile_marker=args.turnstile_marker,
        )
    )


if __name__ == "__main__":
    main()
