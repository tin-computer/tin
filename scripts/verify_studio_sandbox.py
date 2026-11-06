"""Live acceptance probe for the studio sandbox profile.

Creates one `tin-lite-codex-studio` sandbox with open egress and proves, as the unprivileged
user, that the `tin-studio` toolkit is installed with ffmpeg and resvg, that it can inspect and
capture a public page through its own Camoufox at the 9:16 phone viewport, rasterize the example
character, and render a short silent preview that passes the demo-video contract. Voice is not
exercised because it needs a run-bound grant; the switchboard route is covered by unit tests
and by a real workflow run. The sandbox is always killed.

    uv run python scripts/verify_studio_sandbox.py [--url https://example.com/]
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from textwrap import dedent

from e2b import AsyncSandbox

from tin_lite.settings import get_settings

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "src" / "tin_lite" / "example_character.svg"

VERIFY_SCRIPT = dedent(
    r"""
    set -euo pipefail
    work=/home/user/.tin-lite/studio/verify
    mkdir -p "$work"
    tin-studio help >/dev/null && echo "TOOLKIT_OK=true"
    ffmpeg -version | head -n 1
    /opt/tin-lite/studio/resvg --version
    tin-studio check "$work/larry.svg" | grep -q '^OK' && echo "CHARACTER_OK=true"
    tin-studio rasterize "$work/larry.svg" "$work/larry.png" \
      --mouth mouth-open --expression expr-happy
    tin-studio inspect "$TIN_PROBE_URL" "$work/inspect" > "$work/inspect.json"
    python3 - "$work/inspect.json" <<'PY'
    import json, sys
    d = json.load(open(sys.argv[1]))
    print("INSPECT_OK=true" if d["title"] and d["headings"] is not None else "INSPECT_OK=false")
    PY
    cat > "$work/script.json" <<JSON
    {"hook": "one *quick* look", "steps": [
      {"goto": "$TIN_PROBE_URL", "label": "home", "caption": "the *home* page", "hold": 4000},
      {"scroll": {"by": 500}, "label": "down", "caption": "*scroll* down", "hold": 4000}
    ]}
    JSON
    tin-studio capture "$work/script.json" "$work/out" | tail -n 1
    tin-studio render "$work/out" "$work/preview.mp4" \
      --character "$work/larry.svg" --fps 24 | tail -n 1
    tin-studio check "$work/preview.mp4" | grep -q '^OK' && echo "RENDER_OK=true"
    """
)
REQUIRED_MARKERS = {"TOOLKIT_OK=true", "CHARACTER_OK=true", "INSPECT_OK=true", "RENDER_OK=true"}


async def verify(*, url: str, api: bool = False) -> None:
    settings = get_settings()
    sandbox = await AsyncSandbox.create(
        settings.e2b_studio_api_template if api else settings.e2b_studio_template,
        timeout=900,
        api_key=settings.e2b_api_key.get_secret_value(),
    )
    try:
        work = "/home/tin-work/studio" if api else "/home/user/.tin-lite/studio"
        if api:
            await sandbox.commands.run(
                "mkdir -p /home/user/project && "
                "/usr/bin/sudo -n /opt/tin-lite/isolated-procedure prepare",
                timeout=45,
            )
            boundary = await sandbox.commands.run(
                "test ! -r /home/user/.codex/config.toml && "
                "test ! -r /home/user/.tin-lite && "
                "test ! -w /opt/tin-lite/isolated-procedure",
                user="tin-work",
                timeout=10,
            )
            assert boundary.exit_code == 0
            print("STUDIO_ISOLATION_OK=true")
        await sandbox.files.write(
            f"{work}/verify/larry.svg",
            EXAMPLE.read_bytes(),
            user="tin-work" if api else "user",
        )
        await sandbox.files.write(
            f"{work}/verify.sh",
            VERIFY_SCRIPT.replace("/home/user/.tin-lite/studio", work),
            user="tin-work" if api else "user",
        )
        lines: list[str] = []

        async def show(chunk: str) -> None:
            lines.append(chunk)
            print(chunk, end="", flush=True)

        handle = await sandbox.commands.run(
            f"bash {work}/verify.sh 2>&1",
            envs={"TIN_PROBE_URL": url, "HOME": "/home/tin-work" if api else "/home/user"},
            user="tin-work" if api else "user",
            timeout=840,
            background=True,
            on_stdout=show,
        )
        result = await handle.wait()
        output = "".join(lines)
        missing = REQUIRED_MARKERS - set(output.split())
        if result.exit_code != 0 or missing:
            raise SystemExit(
                f"studio verification missed {sorted(missing)} (exit {result.exit_code})"
            )
    finally:
        await sandbox.kill()
    print("STUDIO_SANDBOX_OK=true")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--api", action="store_true", help="Verify the isolated Studio API image")
    args = parser.parse_args()
    asyncio.run(verify(url=args.url, api=args.api))


if __name__ == "__main__":
    main()
