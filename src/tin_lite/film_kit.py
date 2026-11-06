"""Tin's film kit, `tin-film`, as the package a coding agent runs with npx.

A release is the repository's archive (infra/deploy.sh), so the kit's own files (web/film) sit
beside the service. Tin packs them once per process into an npm tarball (`package/…`) named by
its digest, so an agent's npx never runs a stale copy: `npx -y <url> <command>`. Tin's own films
(their specs, recordings and takes) stay out of the package.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2] / "web" / "film"
# Tin's own films and what a take leaves behind; the package carries the kit and its examples.
_SKIPPED_DIRECTORIES = frozenset({"films", "worlds", "takes", "world", "node_modules"})
_EXECUTABLE = frozenset({"tin-film.js"})
MAX_PACKAGE_BYTES = 8_000_000


def _files() -> list[Path]:
    if not (ROOT / "package.json").is_file():
        raise LookupError("The film kit isn't part of this installation.")
    files = []
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part.startswith(".") or part in _SKIPPED_DIRECTORIES for part in relative.parts):
            continue
        if path.is_file() and not path.is_symlink() and not path.name.endswith(".test.js"):
            files.append(path)
    return files


@lru_cache(maxsize=1)
def package() -> tuple[bytes, str]:
    """The kit as an npm tarball and its SHA-256: the same bytes for the same files."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path in _files():
            data = path.read_bytes()
            info = tarfile.TarInfo(f"package/{path.relative_to(ROOT).as_posix()}")
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o755 if path.name in _EXECUTABLE else 0o644
            archive.addfile(info, io.BytesIO(data))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as stream:
        stream.write(raw.getvalue())
    content = packed.getvalue()
    if len(content) > MAX_PACKAGE_BYTES:
        raise RuntimeError("the film kit grew past its package limit")
    return content, hashlib.sha256(content).hexdigest()


def file_name() -> str:
    return f"tin-film-{package()[1][:12]}.tgz"


def url(settings: Any) -> str:
    """Where an agent fetches this release's kit; npx caches it by this address."""
    return f"{settings.switchboard_public_url.rstrip('/')}/kit/{file_name()}"


def command(settings: Any) -> str:
    return f"npx -y {url(settings)}"


@lru_cache(maxsize=1)
def guide() -> str:
    """How a film is made to Tin's standard: the kit's guide, as the agent reads it first."""
    path = ROOT / "guide" / "README.md"
    if not path.is_file():
        raise LookupError("The film kit isn't part of this installation.")
    return path.read_text(encoding="utf-8")
