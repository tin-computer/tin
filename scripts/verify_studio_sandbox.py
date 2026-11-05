"""Live acceptance probe for the studio sandbox profile.

Creates one `tin-lite-codex-studio` sandbox with open egress and proves, as the unprivileged
user, that the `tin-studio` toolkit is installed with ffmpeg and resvg, that it can inspect and
capture a public page through its own Camoufox at the 9:16 phone viewport, rasterize the example
character, and render a short silent preview that passes the demo-video contract. It also films
the page with `tin-film` (inspect, record, render, deliver) into a note that passes the
feature-film contract, and rehearses the staged worked example (`tin-film new`) without an
error. Voice is not exercised because it needs a run-bound grant; the switchboard route is
covered by unit tests and by a real workflow run. The sandbox is always killed.

    uv run python scripts/verify_studio_sandbox.py [--url https://example.com/] [--api]
        [--template <alias>]   # a freshly built alias, before the configured one is replaced
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from textwrap import dedent

from e2b import AsyncSandbox
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    tin-film inspect "$TIN_PROBE_URL" --dir "$work/film" | tail -n 1
    python3 - "$work/film/film.json" <<'PY'
    import json, sys
    film = json.load(open(sys.argv[1]))
    film["beats"] = [
        {"camera": "body", "cut": True, "pad": 0},
        {"caption": "A page, filmed as it is."},
        {"begin": True}, {"hold": 4}, {"mark": "poster"},
        {"caption": "Every frame measured and checked."}, {"hold": 4}, {"end": True},
    ]
    json.dump(film, open(sys.argv[1], "w"))
    PY
    tin-film record "$work/film/film.json" | head -n 1
    tin-film render "$work/film/film.json" | head -n 1
    mkdir -p "$work/project/films"
    tin-film deliver "$work/film/film.json" "$work/project/films/probe.md" | tail -n 1 \
      | grep -q '^OK' && echo "FILM_OK=true"
    tin-film new "$work/staged" > /dev/null
    tin-film rehearse "$work/staged/film.json" > "$work/staged.log" \
      && ! grep -q '^  error:' "$work/staged.log" && echo "STAGED_OK=true"
    """
)
REQUIRED_MARKERS = {
    "TOOLKIT_OK=true",
    "CHARACTER_OK=true",
    "INSPECT_OK=true",
    "RENDER_OK=true",
    "FILM_OK=true",
    "STAGED_OK=true",
}


class ProbeSettings(BaseSettings):
    """Only what the probe needs, so it runs wherever an E2B key is configured."""

    model_config = SettingsConfigDict(env_file=(".env",), extra="ignore", case_sensitive=True)

    e2b_api_key: SecretStr = Field(alias="E2B_API_KEY")
    e2b_studio_template: str = Field(
        default="tin-lite-codex-studio", alias="TIN_LITE_E2B_STUDIO_TEMPLATE"
    )
    e2b_studio_api_template: str = Field(
        default="tin-lite-codex-studio-api", alias="TIN_LITE_E2B_STUDIO_API_TEMPLATE"
    )


async def verify(*, url: str, api: bool = False, template: str | None = None) -> None:
    settings = ProbeSettings()  # type: ignore[call-arg]
    sandbox = await AsyncSandbox.create(
        template or (settings.e2b_studio_api_template if api else settings.e2b_studio_template),
        timeout=1500,
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
            timeout=1440,
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
    parser.add_argument("--template", help="A template alias other than the configured one")
    args = parser.parse_args()
    asyncio.run(verify(url=args.url, api=args.api, template=args.template))


if __name__ == "__main__":
    main()
