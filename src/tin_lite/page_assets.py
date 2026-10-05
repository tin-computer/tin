"""The files an article draft carries next to it: figures and embeds in its assets folder.

A draft at `content/articles/2026-10-05-1a2b3c4d.md` may write files to
`content/articles/2026-10-05-1a2b3c4d.assets/` and refer to them from the Markdown, as
`./2026-10-05-1a2b3c4d.assets/flock-paths.svg` in a figure or as the `src:` of a `tin-embed`
block. The article declares its assets: Tin keeps exactly the files the Markdown refers to,
records their checksums with the checkpoint, and approval binds them with the article. Files
nothing refers to are never published.

Projects are untrusted. An SVG that could run script is left out with a reason rather than
failing the run; the reader shows SVG only as an image and embeds only in a sandboxed frame.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Text assets for now; raster images come with binary checkpoint support.
MEDIA_TYPES = {".svg": "image/svg+xml", ".html": "text/html"}
FILE_MAX_BYTES = 512_000
MAX_FILES_CEILING = 24
MAX_BYTES_CEILING = 4_000_000
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(?:svg|html)")
# Script, event handlers, embedded HTML, and references that leave the file.
UNSAFE_SVG = re.compile(
    r"<\s*script\b|<\s*foreignObject\b|\son[a-z]+\s*=|javascript:"
    r"|(?:xlink:)?href\s*=\s*[\"']\s*(?!#|data:image/)[^\"'\s]",
    re.I,
)


def folder(article_path: str) -> str:
    """The assets folder next to an article: its path without `.md`, plus `.assets`."""
    if not article_path.endswith(".md"):
        raise ValueError("Only a Markdown article has an assets folder.")
    return article_path[: -len(".md")] + ".assets"


def referenced(article: str, article_path: str) -> list[str]:
    """The assets the article refers to, as project paths, in the order they first appear."""
    name = folder(article_path).rsplit("/", 1)[-1]
    pattern = re.compile(rf"(?<![\w.-])(?:\./)?{re.escape(name)}/({NAME.pattern})(?![\w.-])")
    found: list[str] = []
    for match in pattern.finditer(article):
        path = f"{folder(article_path)}/{match.group(1)}"
        if path not in found:
            found.append(path)
    return found


def media_type(path: str) -> str:
    return MEDIA_TYPES[path[path.rindex(".") :]]


def problem(path: str, content: bytes) -> str | None:
    """Why an asset can't be kept, or None. A problem drops the asset; it never fails a run."""
    if not content or len(content) > FILE_MAX_BYTES:
        return f"it is empty or larger than {FILE_MAX_BYTES // 1000} KB"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return "it is not UTF-8 text"
    if path.endswith(".svg") and UNSAFE_SVG.search(text):
        return "the SVG contains script, event handlers or outside references"
    return None


@dataclass(frozen=True)
class AssetPolicy:
    """How many assets a draft may keep, and how many bytes they may use together."""

    max_files: int
    max_bytes: int

    def definition(self) -> dict[str, int]:
        return {"max_files": self.max_files, "max_bytes": self.max_bytes}

    @classmethod
    def load(cls, value: object) -> AssetPolicy:
        if not isinstance(value, dict) or set(value) != {"max_files", "max_bytes"}:
            raise ValueError("Codex procedure assets contract is invalid")
        files, size = value["max_files"], value["max_bytes"]
        if (
            type(files) is not int
            or type(size) is not int
            or not 1 <= files <= MAX_FILES_CEILING
            or not 1 <= size <= MAX_BYTES_CEILING
        ):
            raise ValueError("Codex procedure assets limits are out of range")
        return cls(max_files=files, max_bytes=size)


def review_binding(checkpoint: dict | None) -> list[dict[str, str]]:
    """The assets an approval binds: each kept file's path and checksum, from the checkpoint."""
    return [
        {"path": item["artifact_path"], "sha256": item["sha256"]}
        for item in (checkpoint or {}).get("assets") or []
    ]


def source_binding(source: dict) -> dict:
    """The review record's assets, rebuilt from a pinned delivery source; empty without any."""
    assets = [
        {"path": item["path"], "sha256": item["sha256"]} for item in source.get("assets") or []
    ]
    return {"assets": assets} if assets else {}


async def verified(storage, *, repo_id: str, revision: str, binding: list[dict]) -> list[dict]:
    """The approved assets as delivery receives them, each re-read and checked at `revision`."""
    assets = []
    for item in binding:
        raw = await storage.read_canonical_artifact(
            repo_id=repo_id, commit_sha=revision, path=item["path"]
        )
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("An approved figure or embed differs from what was approved.")
        assets.append(
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "media_type": media_type(item["path"]),
                "bytes": len(raw),
            }
        )
    return assets
