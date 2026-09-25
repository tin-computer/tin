import sys
from pathlib import Path

from e2b import Template
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

CODEX_TEMPLATE_ALIAS = "tin-lite-codex"
BROWSER_TEMPLATE_ALIAS = "tin-lite-codex-browser"
STUDIO_TEMPLATE_ALIAS = "tin-lite-codex-studio"
ISOLATED_TEMPLATE_ALIAS = "tin-lite-codex-isolated"
BROWSER_API_TEMPLATE_ALIAS = "tin-lite-codex-browser-api"
STUDIO_API_TEMPLATE_ALIAS = "tin-lite-codex-studio-api"

# The E2B `codex` base template ships whatever Codex release E2B last published, so Tin pins
# the CLI explicitly; the model every sandbox runs is pinned in codex_config.toml next to it.
CODEX_CLI_VERSION = "0.156.1"

# Pinned browser-profile dependencies. A template rebuild is the only way any of these move.
CAMOUFOX_PACKAGE_VERSION = "0.5.5"
CAMOUFOX_BROWSER_BUILD = "official/152.0.4-beta.29"
MCP_PACKAGE_VERSION = "2.1.1"
# Cloudflare's apt repository serves only the current WARP release, so the exact pin makes a
# rebuild fail loudly when the pin must move instead of drifting silently.
WARP_PACKAGE_VERSION = "2026.7.1377.0"
CFX_PYTHON = "/opt/tin-lite/cfx-venv/bin/python"

# Pinned studio-profile dependencies (the browser image plus the video toolkit). ffmpeg comes
# from Debian bookworm, resvg is a static binary whose archive digest is checked, and the
# Python renderer runs in its own venv beside the browser's.
FFMPEG_PACKAGE_VERSION = "7:5.1.9-0+deb12u1"
RESVG_VERSION = "0.48.1"
RESVG_SHA256 = "fa8c26495a187e592c501db15bf9e8a9fdc051d4b2b336b39703d5b59f912b9d"
PILLOW_PACKAGE_VERSION = "12.3.0"
NUMPY_PACKAGE_VERSION = "2.3.4"
STUDIO_PYTHON = "/opt/tin-lite/studio-venv/bin/python"
STUDIO_FILES = (
    "tin_studio.py",
    "capture.py",
    "render.py",
    "voice.py",
    "studio_contracts.py",
    "fonts/Montserrat-ExtraBold.ttf",
    "fonts/Montserrat-Bold.ttf",
)

# Cloudflare WARP for the browser profile. E2B egress is IPv4-only, while Cloudflare
# serves Turnstile challenge dependencies from IPv6-only hostnames, so the browser needs
# the local WARP SOCKS proxy (127.0.0.1:40000) for dual-stack reachability.
_WARP_INSTALL = (
    "install -d -m 0755 /usr/share/keyrings && "
    "curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg "
    "| gpg --dearmor -o /usr/share/keyrings/cloudflare-warp.gpg && "
    'echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp.gpg] '
    'https://pkg.cloudflareclient.com/ bookworm main" '
    "> /etc/apt/sources.list.d/cloudflare-warp.list && "
    "apt-get update && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
    f"cloudflare-warp={WARP_PACKAGE_VERSION} && "
    "warp-cli --version && "
    "rm -rf /var/lib/apt/lists/*"
)

# Camoufox, a fingerprint-hardened Firefox, is the only browser in the browser profile:
# headless Chrome cannot pass Cloudflare Turnstile's bot scoring. The venv also carries the
# MCP SDK for the Tin-owned camoufox-mcp server that drives it.
_CAMOUFOX_INSTALL = (
    "apt-get update && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
    "python3-venv fonts-liberation fonts-noto-color-emoji "
    # Xvfb backs Camoufox's headless="virtual" mode; Mesa supplies software WebGL (llvmpipe)
    # so the browser does not leak the native-headless "no WebGL" tell to risk checks.
    "xvfb libgl1-mesa-dri libglx-mesa0 libegl1 libgles2 && "
    "rm -rf /var/lib/apt/lists/* && "
    "python3 -m venv /opt/tin-lite/cfx-venv && "
    "/opt/tin-lite/cfx-venv/bin/pip install --quiet "
    f"'camoufox[geoip]=={CAMOUFOX_PACKAGE_VERSION}' 'mcp=={MCP_PACKAGE_VERSION}'"
)

# The browser binary and GeoIP database are fetched into the runtime user's cache so launches
# as `user` find them and no run downloads anything at start.
_CAMOUFOX_FETCH = (
    f"/opt/tin-lite/cfx-venv/bin/camoufox fetch {CAMOUFOX_BROWSER_BUILD} && "
    f"{CFX_PYTHON} -c 'from camoufox.geolocation import download_mmdb; download_mmdb()' && "
    "/opt/tin-lite/cfx-venv/bin/camoufox version && "
    f"{CFX_PYTHON} /opt/tin-lite/camoufox-mcp --check"
)

# The studio toolkit: ffmpeg encodes, resvg rasterizes character SVGs (Pillow cannot), and the
# renderer's venv shares the already-fetched Camoufox browser through the user cache.
_STUDIO_INSTALL = (
    "apt-get update && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
    f"ffmpeg={FFMPEG_PACKAGE_VERSION} && "
    "rm -rf /var/lib/apt/lists/* && "
    "ffmpeg -version | head -n 1 && "
    "curl -fsSL -o /tmp/resvg.tar.gz "
    "https://github.com/linebender/resvg/releases/download/"
    f"v{RESVG_VERSION}/resvg-linux-x86_64.tar.gz && "
    f'echo "{RESVG_SHA256}  /tmp/resvg.tar.gz" | sha256sum -c - && '
    "tar -xzf /tmp/resvg.tar.gz -C /opt/tin-lite/studio resvg && rm -f /tmp/resvg.tar.gz && "
    "chmod 0755 /opt/tin-lite/studio/resvg && /opt/tin-lite/studio/resvg --version && "
    "python3 -m venv /opt/tin-lite/studio-venv && "
    "/opt/tin-lite/studio-venv/bin/pip install --quiet "
    f"'camoufox[geoip]=={CAMOUFOX_PACKAGE_VERSION}' 'pillow=={PILLOW_PACKAGE_VERSION}' "
    f"'numpy=={NUMPY_PACKAGE_VERSION}' && "
    "chown -R user:user /opt/tin-lite/studio /opt/tin-lite/studio-venv"
)
_STUDIO_CHECK = f"{STUDIO_PYTHON} /opt/tin-lite/studio/tin_studio.py help >/dev/null"


class TemplateSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    e2b_api_key: SecretStr = Field(alias="E2B_API_KEY")


def _tin_runtime(template):
    return (
        template.from_template("codex")
        .pip_install("uv==0.5.21")
        .run_cmd(
            f"npm install -g @openai/codex@{CODEX_CLI_VERSION} && codex --version",
            user="root",
        )
        .run_cmd(
            "UV_PYTHON_INSTALL_DIR=/opt/tin-lite/python "
            "uv venv --python 3.12.8 /opt/tin-lite/metadata-venv && "
            "uv pip install --python /opt/tin-lite/metadata-venv/bin/python "
            "hatchling==1.32.0 packaging==26.3 pathspec==1.1.1 pluggy==1.6.0 "
            "tomlkit==0.15.1 trove-classifiers==2026.6.1.19 && "
            "chown root:root /opt/tin-lite && chmod 0755 /opt/tin-lite",
            user="root",
        )
        .copy(
            "sandbox/verify_technical_metadata.py",
            "/opt/tin-lite/verify-technical-metadata.py",
            user="root",
            mode=0o644,
        )
        .copy(
            "src/tin_lite/technical_metadata_rules.py",
            "/opt/tin-lite/technical_metadata_rules.py",
            user="root",
            mode=0o644,
        )
        .copy(
            "src/tin_lite/technical_build_profile.py",
            "/opt/tin-lite/technical_build_profile.py",
            user="root",
            mode=0o644,
        )
        .copy("sandbox/codex_config.toml", "/home/user/.codex/config.toml", user="user", mode=0o644)
        .copy("sandbox/run_design.sh", "/opt/tin-lite/run-design", user="user", mode=0o755)
        .copy("sandbox/run_task.sh", "/opt/tin-lite/run-task", user="user", mode=0o755)
        .copy(
            "sandbox/run_procedure.sh",
            "/opt/tin-lite/run-procedure",
            user="user",
            mode=0o755,
        )
        .copy(
            "sandbox/task_app_server.py",
            "/opt/tin-lite/task-app-server",
            user="user",
            mode=0o755,
        )
        .copy(
            "src/tin_lite/payment_card_guard.py",
            "/opt/tin-lite/payment_card_guard.py",
            user="root",
            mode=0o644,
        )
        .copy(
            "sandbox/procedure_app_server.py",
            "/opt/tin-lite/procedure-app-server",
            user="user",
            mode=0o755,
        )
        .copy(
            "sandbox/verify_technical_title.py",
            "/opt/tin-lite/verify-technical-title.py",
            user="user",
            mode=0o755,
        )
        .copy(
            "src/tin_lite/technical_title_rules.py",
            "/opt/tin-lite/technical_title_rules.py",
            user="root",
            mode=0o644,
        )
    )


def task_template():
    return _diagram_layer(
        _tin_runtime(Template(file_context_path=Path(__file__).resolve().parents[1]))
    )


def _diagram_layer(template):
    # Chromium is an offline renderer here, not the open-egress browser profile.
    # package-lock pins Playwright and its exact bundled Chromium revision. The
    # runtime cannot download dependencies and no project code is loaded by it.
    root = "/opt/tin-lite/diagram"
    # Install the expensive pinned browser layer before frequently edited assets.
    for filename in ("package.json", "package-lock.json"):
        template = template.copy(filename, f"{root}/{filename}", user="root", mode=0o644)
    template = template.run_cmd(
        f"cd {root} && npm ci --ignore-scripts && "
        f"PLAYWRIGHT_BROWSERS_PATH={root}/browsers "
        "npx playwright install --with-deps chromium && "
        f"chmod -R a+rX,go-w {root}",
        user="root",
    )
    for filename in (
        "scripts/check_diagram.mjs",
        "scripts/diagram-review.mjs",
        "web/diagram-contract.js",
        "web/diagram-brand.js",
        "web/diagram-audit.js",
        "web/diagram-quality.js",
        "src/tin_lite/static/diagram-renderer.js",
        "src/tin_lite/static/diagram-routing.wasm",
        "src/tin_lite/static/app.css",
        "src/tin_lite/static/fonts/geist-sans-regular.woff2",
        "src/tin_lite/static/fonts/geist-sans-bold.woff2",
        "src/tin_lite/static/fonts/geist-mono-regular.woff2",
        "src/tin_lite/static/fonts/geist-mono-bold.woff2",
        "THIRD_PARTY_NOTICES.md",
        "third_party/licenses/geist-sans-OFL.txt",
        "src/tin_lite/static/fonts/Geist-OFL.txt",
        "third_party/licenses/libavoid-js-LICENSE.txt",
        "third_party/licenses/elkjs-LICENSE.md",
    ):
        template = template.copy(filename, f"{root}/{filename}", user="root", mode=0o644)
    return template.copy(
        "sandbox/diagram_review.py", "/opt/tin-lite/diagram_review.py", user="root", mode=0o644
    ).run_cmd(
        f"cd {root} && node scripts/check_diagram.mjs --version",
        user="root",
    )


def isolated_template():
    # Separate alias: never silently change the existing default/browser/Studio images.
    return (
        task_template()
        .run_cmd(
            "useradd -m -s /bin/bash tin-work && "
            "git config --system --add safe.directory /home/user/project && "
            "git config --system --add safe.directory /home/user/state",
            user="root",
        )
        .copy(
            "sandbox/isolated_procedure.py",
            "/opt/tin-lite/isolated-procedure",
            user="root",
            mode=0o755,
        )
        .copy("sandbox/codex_usage.py", "/opt/tin-lite/codex_usage.py", user="root", mode=0o644)
        .copy(
            "sandbox/codex_api_config.py",
            "/opt/tin-lite/codex_api_config.py",
            user="root",
            mode=0o644,
        )
    )


def _browser_layer(template):
    return (
        template.copy("sandbox/warp_up.sh", "/opt/tin-lite/warp-up", user="user", mode=0o755)
        .copy("sandbox/camoufox_mcp.py", "/opt/tin-lite/camoufox-mcp", user="user", mode=0o755)
        .apt_install(["curl", "gnupg", "ca-certificates"])
        .run_cmd(_WARP_INSTALL, user="root")
        .run_cmd(_CAMOUFOX_INSTALL, user="root")
        .run_cmd(_CAMOUFOX_FETCH, user="user")
    )


def browser_template():
    return _browser_layer(task_template())


def browser_api_template():
    # Keep the historical OAuth browser image intact. Browser tools stay in the
    # controller; author commands use the credential-free exec-server UID.
    return _browser_layer(isolated_template())


def _studio_layer(template):
    template = template.copy(
        "sandbox/tin_studio.sh", "/opt/tin-lite/studio/tin-studio", user="user", mode=0o755
    )
    for name in STUDIO_FILES:
        template = template.copy(
            f"sandbox/studio/{name}", f"/opt/tin-lite/studio/{name}", user="user", mode=0o644
        )
    return (
        template.run_cmd(_STUDIO_INSTALL, user="root")
        .run_cmd(_STUDIO_CHECK, user="user")
        .make_symlink("/opt/tin-lite/studio/tin-studio", "/usr/local/bin/tin-studio", user="root")
    )


def studio_template():
    return _studio_layer(_browser_layer(task_template()))


def studio_api_template():
    # Capture/render run as the unprivileged worker, with their own browser cache.
    # The Codex controller and its model relay grant remain inaccessible to it.
    return _studio_layer(_browser_layer(isolated_template())).run_cmd(
        "mkdir -p /home/tin-work/.cache /home/tin-work/studio && "
        "cp -a /home/user/.cache/camoufox /home/tin-work/.cache/ && "
        "chown -R tin-work:tin-work /home/tin-work/.cache /home/tin-work/studio",
        user="root",
    )


TEMPLATES = {
    CODEX_TEMPLATE_ALIAS: (task_template, 2, 4096),
    BROWSER_TEMPLATE_ALIAS: (browser_template, 2, 8192),
    STUDIO_TEMPLATE_ALIAS: (studio_template, 4, 8192),
    ISOLATED_TEMPLATE_ALIAS: (isolated_template, 2, 4096),
    BROWSER_API_TEMPLATE_ALIAS: (browser_api_template, 2, 8192),
    STUDIO_API_TEMPLATE_ALIAS: (studio_api_template, 4, 8192),
}


def show_build_log(entry) -> None:
    print(entry.message, flush=True)


def main(argv: list[str]) -> int:
    settings = TemplateSettings()
    selected = list(TEMPLATES)
    if len(argv) == 2 and argv[0] == "--only":
        if argv[1] not in TEMPLATES:
            print(f"unknown template alias: {argv[1]}", file=sys.stderr)
            return 64
        selected = [argv[1]]
    elif argv:
        print("usage: template.py [--only <alias>]", file=sys.stderr)
        return 64
    for alias in selected:
        build, cpu_count, memory_mb = TEMPLATES[alias]
        Template.build(
            build(),
            alias=alias,
            cpu_count=cpu_count,
            memory_mb=memory_mb,
            on_build_logs=show_build_log,
            api_key=settings.e2b_api_key.get_secret_value(),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
