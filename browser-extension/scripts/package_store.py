#!/usr/bin/env python3
"""Build the deterministic, minimal Chrome Web Store upload archive."""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import tempfile
import zipfile
from pathlib import Path

EXTENSION_ROOT = Path(__file__).resolve().parent.parent
STORE_MANIFEST = EXTENSION_ROOT / "manifest.store.json"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
EXPECTED_NAME = "Tin Computer for LinkedIn BETA"
BETA_DESCRIPTION_PREFIX = "THIS EXTENSION IS FOR BETA TESTING."
PRODUCTION_HOSTS = {
    "https://api.tin.computer/*",
    "https://app.tin.computer/*",
    "https://tin.computer/*",
    "https://www.tin.computer/*",
    "https://*.linkedin.com/*",
}
ICON_PATHS = {
    "16": "icons/icon-16.png",
    "32": "icons/icon-32.png",
    "48": "icons/icon-48.png",
    "128": "icons/icon-128.png",
}

# Archive paths are an explicit allowlist. In particular, the dev manifest,
# tests, fixtures, package metadata, docs, and local filesystem metadata can
# never enter the Web Store upload by accident.
ARCHIVE_SOURCES = {
    "manifest.json": "manifest.store.json",
    "icons/icon-16.png": "icons/icon-16.png",
    "icons/icon-32.png": "icons/icon-32.png",
    "icons/icon-48.png": "icons/icon-48.png",
    "icons/icon-128.png": "icons/icon-128.png",
    "popup/popup.css": "popup/popup.css",
    "popup/popup.html": "popup/popup.html",
    "popup/popup.js": "popup/popup.js",
    "src/background.js": "src/background.js",
    "src/browser-context.js": "src/browser-context.js",
    "src/protocol.js": "src/protocol.js",
    "src/tin-bridge.js": "src/tin-bridge.js",
    "popup/collection.html": "popup/collection.html",
    "popup/collection.js": "popup/collection.js",
    "popup/collection.css": "popup/collection.css",
    "src/collection/core.js": "src/collection/core.js",
    "src/collection/page-evidence.js": "src/collection/page-evidence.js",
    "src/collection/linkedin.js": "src/collection/linkedin.js",
    "src/collection/worker.js": "src/collection/worker.js",
    "src/collection/bridge.js": "src/collection/bridge.js",
}


def fail(message: str) -> None:
    raise SystemExit(f"Chrome Web Store package validation failed: {message}")


def read_manifest() -> dict[str, object]:
    try:
        manifest = json.loads(STORE_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read {STORE_MANIFEST.name}: {exc}")
    if not isinstance(manifest, dict):
        fail("store manifest must be a JSON object")
    return manifest


def validate_png(path: Path, expected_size: int) -> None:
    try:
        header = path.read_bytes()[:24]
    except OSError as exc:
        fail(f"cannot read {path.relative_to(EXTENSION_ROOT)}: {exc}")
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        fail(f"{path.relative_to(EXTENSION_ROOT)} must be a PNG")
    width, height = struct.unpack(">II", header[16:24])
    if (width, height) != (expected_size, expected_size):
        fail(
            f"{path.relative_to(EXTENSION_ROOT)} is {width}x{height}; "
            f"expected {expected_size}x{expected_size}"
        )


def validate_manifest(manifest: dict[str, object]) -> None:
    if manifest.get("manifest_version") != 3:
        fail("store manifest must use Manifest V3")
    if manifest.get("name") != EXPECTED_NAME:
        fail(f"store name must be exactly {EXPECTED_NAME!r}")
    description = manifest.get("description")
    if (
        not isinstance(description, str)
        or not description.startswith(BETA_DESCRIPTION_PREFIX)
        or len(description) > 132
    ):
        fail(
            "description must start with the exact beta-testing disclosure "
            "and contain at most 132 characters"
        )
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(
        r"(?:0|[1-9]\d{0,4})(?:\.(?:0|[1-9]\d{0,4})){0,3}", version
    ):
        fail("version is not a valid Chrome extension version")
    if any(int(component) > 65535 for component in version.split(".")):
        fail("Chrome extension version components cannot exceed 65535")
    if set(manifest.get("permissions", [])) != {
        "alarms",
        "cookies",
        "storage",
        "webRequest",
        "scripting",
    }:
        fail("store permissions must be exactly alarms, cookies, storage, and webRequest")
    if set(manifest.get("host_permissions", [])) != PRODUCTION_HOSTS:
        fail("store host permissions must contain production origins only")
    if manifest.get("icons") != ICON_PATHS:
        fail("store manifest must declare the exact 16/32/48/128 icon set")
    action = manifest.get("action")
    if not isinstance(action, dict) or action.get("default_icon") != ICON_PATHS:
        fail("toolbar action must declare the exact icon set")
    content_scripts = manifest.get("content_scripts")
    if not isinstance(content_scripts, list) or len(content_scripts) != 2:
        fail("store manifest must declare only the narrow Tin page bridge")
    if set(content_scripts[0].get("matches", [])) != {
        "https://tin.computer/*",
        "https://www.tin.computer/*",
    }:
        fail("Tin page bridge must match production dashboards only")
    serialized = json.dumps(manifest, separators=(",", ":"))
    for forbidden in (
        "api.staging.tin.computer",
        "staging.tin.computer",
        "localhost",
        "127.0.0.1",
        "<all_urls>",
    ):
        if forbidden in serialized:
            fail(f"store manifest contains forbidden dev/broad capability {forbidden!r}")


def validate_sources() -> None:
    for archive_path, source_path in ARCHIVE_SOURCES.items():
        path = EXTENSION_ROOT / source_path
        if not path.is_file() or path.is_symlink():
            fail(f"archive source {source_path!r} must be a regular file")
        if archive_path.startswith("/") or ".." in Path(archive_path).parts:
            fail(f"unsafe archive path {archive_path!r}")
    for size, relative_path in ICON_PATHS.items():
        validate_png(EXTENSION_ROOT / relative_path, int(size))


def zip_info(archive_path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(archive_path, FIXED_ZIP_TIME)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def build(output: Path) -> None:
    manifest = read_manifest()
    validate_manifest(manifest)
    validate_sources()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for archive_path in sorted(ARCHIVE_SOURCES):
                source = EXTENSION_ROOT / ARCHIVE_SOURCES[archive_path]
                archive.writestr(zip_info(archive_path), source.read_bytes())
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(output)


def parse_args() -> argparse.Namespace:
    manifest = read_manifest()
    default_name = f"tin-computer-linkedin-beta-{manifest.get('version', 'invalid')}.zip"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=EXTENSION_ROOT / "dist" / default_name,
        help="destination ZIP path",
    )
    return parser.parse_args()


if __name__ == "__main__":
    build(parse_args().output)
