"""Bounded, data-only source matching. Never import or execute repository code."""

import hashlib
import io
import json
import re
import tarfile
import tomllib
from pathlib import PurePosixPath

try:
    from tin_lite.technical_metadata_rules import MAX_HTML_BYTES, MetadataParser, has_metadata
except ModuleNotFoundError:
    from technical_metadata_rules import MAX_HTML_BYTES, MetadataParser, has_metadata

STATIC = "static-html-v1"
WHEEL = "hatchling-wheel-html-v1"
TOKENS = frozenset(
    {
        "ASSET_VERSION",
        "CLERK_PUBLISHABLE_KEY",
        "CLERK_FRONTEND_API_URL",
        "BILLING_ENABLED",
        "AUTH_RETURN_URL",
        "AUTH_FLOW",
    }
)
MARKER = re.compile(r"\{\{([A-Z_]+)\}\}")


def wheel_package(raw):
    """Only the plain, static Python 3.12 wheel contract. No hooks or plugins."""
    config = tomllib.loads(raw.decode())
    if config.get("build-system") != {
        "requires": ["hatchling"],
        "build-backend": "hatchling.build",
    }:
        raise ValueError("Unsupported Python build backend.")
    project = config["project"]
    if project.get("dynamic") or project.get("requires-python") != ">=3.12,<3.13":
        raise ValueError("Only static Python 3.12 package metadata is supported.")
    hatch = config.get("tool", {}).get("hatch", {})
    packages = hatch.get("build", {}).get("targets", {}).get("wheel", {}).get("packages", [])
    if (
        len(packages) != 1
        or not re.fullmatch(r"src/[A-Za-z_][A-Za-z_0-9]*", packages[0])
        or hatch != {"build": {"targets": {"wheel": {"packages": packages}}}}
    ):
        raise ValueError("Custom build configuration, hooks and multiple packages are unsupported.")
    # Hatch metadata may read these files. Never allow an escape from the snapshot.
    for field in ("readme", "license"):
        value = project.get(field)
        name = (
            value.get("file") if isinstance(value, dict) else value if field == "readme" else None
        )
        if name and (not isinstance(name, str) or not safe_path(name)):
            raise ValueError("Unsafe package metadata path.")
    if project.get("license-files"):
        raise ValueError("Custom license globs are not supported by this profile.")
    return packages[0]


def safe_path(name):
    path = PurePosixPath(name)
    return not path.is_absolute() and all(p not in {"", ".", ".."} for p in name.split("/"))


# The most a caller reads out of one snapshot into memory.
ARCHIVE_MAX_BYTES = 100_000_000
ARCHIVE_MAX_FILES = 20_000


def archive_files(archive, *, select=None):
    """The snapshot's files by path. `select(path, size)` keeps only the files a caller reads,
    and the read bounds apply to those; every member is still checked."""
    files, total, seen = {}, 0, set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        for member in source:
            if member.isdir():
                continue
            if not member.isfile() or not safe_path(member.name) or member.name in seen:
                raise ValueError("Unsupported repository snapshot.")
            seen.add(member.name)
            if select is not None and not select(member.name, member.size):
                continue
            total += member.size
            if total > ARCHIVE_MAX_BYTES or len(files) >= ARCHIVE_MAX_FILES:
                raise ValueError("Unsupported repository snapshot.")
            files[member.name] = source.extractfile(member).read()
    return files


def snapshot_digest(files):
    # GitHub rematerialization may change gzip headers, never source identity.
    entries = [(path, hashlib.sha256(raw).hexdigest()) for path, raw in sorted(files.items())]
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def render(html, values):
    for key, value in values.items():
        html = html.replace("{{" + key + "}}", value)
    return html


def match_render(source, served):
    """Capture only known, bounded public literal substitutions; match the entire page."""
    pieces, keys, position = [], [], 0
    for match in MARKER.finditer(source):
        key = match[1]
        if key not in TOKENS:
            return None
        pieces.append(re.escape(source[position : match.start()]))
        # Match only the ordinary public product page. OAuth return parameters
        # must never enter an SEO repair's evidence or project-state snapshot.
        value_pattern = {
            "BILLING_ENABLED": "true|false",
            "AUTH_RETURN_URL": "",
            "AUTH_FLOW": "product",
        }.get(key, "[^<>\"'\\r\\n]{0,500}")
        pieces.append(f"(?P={key})" if key in keys else f"(?P<{key}>{value_pattern})")
        keys.append(key)
        position = match.end()
    pieces.append(re.escape(source[position:]))
    matched = re.fullmatch("".join(pieces), served)
    return matched.groupdict() if matched else None


def match_profile(archive, pages, check, *, allow_partial=False):
    try:
        files = archive_files(archive)
        if "pyproject.toml" in files:
            package = wheel_package(files["pyproject.toml"])
            if "hatch.toml" in files:
                return None
            profile = {"kind": WHEEL, "package": package}
        else:
            manifests = {
                "package.json",
                "requirements.txt",
                "setup.py",
                "Cargo.toml",
                "go.mod",
                "Gemfile",
                "composer.json",
                "pom.xml",
                "build.gradle",
                "deno.json",
                "deno.jsonc",
            }
            if any(
                PurePosixPath(p).name in manifests or p.endswith((".csproj", ".fsproj"))
                for p in files
            ):
                return None
            package, profile = "", {"kind": STATIC}
        candidates = {}
        for path, raw in files.items():
            if path.endswith((".html", ".htm")) and len(raw) <= MAX_HTML_BYTES:
                if package and not path.startswith(package + "/"):
                    continue
                try:
                    candidates[path] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
        originals, observations, unsupported = {}, [], []
        for page in pages:
            if has_metadata(page["html"], check):
                continue
            matches = []
            for path, html in candidates.items():
                values = (
                    match_render(html, page["html"])
                    if package
                    else {}
                    if html == page["html"]
                    else None
                )
                if values is not None and MetadataParser(html).heads == 1:
                    matches.append((path, values))
            if len(matches) != 1:
                if not allow_partial:
                    return None
                unsupported.append(
                    {
                        "url": page["url"],
                        "reason": "Ambiguous source match"
                        if matches
                        else "No supported source HTML match",
                    }
                )
                continue
            path, values = matches[0]
            originals[path] = candidates[path]
            observations.append(
                {
                    "path": path,
                    "url": page.get("url", ""),
                    "values": values,
                    "sha256": page["sha256"],
                }
            )
        if (
            not 1 <= len(originals) <= 3
            or sum(len(x.encode()) for x in originals.values()) > 60_000
        ):
            return None
        return {
            "originals": originals,
            "unsupported_pages": unsupported,
            "verification_profile": {
                **profile,
                "snapshot_sha256": snapshot_digest(files),
                "observations": observations,
            },
        }
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, tarfile.TarError):
        return None
