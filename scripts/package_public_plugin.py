"""Check the public plugin source; build only after review metadata and endpoint are ready.

This is a narrow inventory/metadata check, not a replacement for OpenAI's portal scans.
No credentials or review-account instructions belong in the package.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "tin-mcp"
INVENTORY = ("plugin.json", "mcp.json", "assets/icon.png")


def check() -> tuple[dict, dict, list[str]]:
    for relative in INVENTORY:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"missing or unsafe package file: {relative}")
    manifest = json.loads((ROOT / "plugin.json").read_text())
    mcp = json.loads((ROOT / "mcp.json").read_text())
    if manifest["name"] != ROOT.name or not re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]):
        raise ValueError("expected tin-mcp and an explicit semantic version")
    extension = manifest["extensions"]["com.openai"]
    if manifest.get("apps") is not None or extension.get("apps") is not None:
        raise ValueError("public uploads must not declare app bindings")
    interface = extension["interface"]
    for field, limit in {
        "displayName": 30,
        "shortDescription": 30,
        "longDescription": 4000,
        "developerName": 80,
    }.items():
        if not 0 < len(interface[field]) <= limit:
            raise ValueError(f"invalid listing field: {field}")
    for field in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        url = urlsplit(interface[field])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError(f"invalid public HTTPS URL: {field}")
    for field in ("logo", "composerIcon"):
        if interface[field] != "./assets/icon.png":
            raise ValueError(f"update the reviewed inventory before changing {field}")
    prompts = interface["defaultPrompt"]
    if not 1 <= len(prompts) <= 3 or len(set(prompts)) != len(prompts):
        raise ValueError("expected one to three distinct default prompts")
    if any(not p.strip() or len(p) > 128 or "\n" in p for p in prompts):
        raise ValueError("invalid default prompt")
    review = extension["review"]
    cases = review["test_cases"]
    if len(cases["positive"]) != 5 or len(cases["negative"]) != 3:
        raise ValueError("review requires five positive and three negative cases")
    for kind, values in cases.items():
        required = ["description", "prompt"]
        if kind == "positive":
            required += ["tools_triggered", "expected_behavior"]
        if any(not isinstance(c.get(k), str) or not c[k].strip() for c in values for k in required):
            raise ValueError(f"incomplete {kind} review case")
    servers = mcp["mcpServers"]
    if set(servers) != {"tin"} or servers["tin"]["type"] != "streamable-http":
        raise ValueError("expected the single Tin streamable HTTP server")
    gaps = []
    if not review.get("demo_recording_url"):
        gaps.append("review.demo_recording_url: record, host and verify a real walkthrough")
    if not isinstance(review.get("commerce"), bool):
        gaps.append("review.commerce: declare whether the plugin supports commerce")
    return manifest, mcp, gaps


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Build a ZIP after live discovery verification")
    args = parser.parse_args()
    manifest, mcp, gaps = check()
    print("Package source inventory and metadata shape passed.")
    for gap in gaps:
        print(f"Pending: {gap}")
    if args.output is None:
        print("Live endpoint, support access, demo playback and portal review are separate checks.")
        return
    if gaps:
        raise SystemExit("Cannot build the submission ZIP until the pending metadata is completed.")
    endpoint = mcp["mcpServers"]["tin"]["url"]
    url = urlsplit(endpoint)
    if url.scheme != "https" or url.username or url.password or url.path != "/mcp/plugins":
        raise ValueError("expected the configured public HTTPS /mcp/plugins endpoint")
    discovery = f"{url.scheme}://{url.netloc}/.well-known/oauth-protected-resource{url.path}"
    response = httpx.get(discovery, timeout=15, follow_redirects=False)
    response.raise_for_status()
    if response.json().get("resource") != endpoint:
        raise ValueError("live discovery does not advertise the packaged resource")
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("write the ZIP outside the plugin source directory")
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for relative in INVENTORY:
            info = ZipInfo(f"{manifest['name']}/{relative}")
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, (ROOT / relative).read_bytes())
    with ZipFile(output) as archive:
        assert archive.namelist() == [f"{manifest['name']}/{p}" for p in INVENTORY]
        assert archive.testzip() is None
    print(f"Built and inspected {output}. Portal connection and review remain required.")


if __name__ == "__main__":
    main()
