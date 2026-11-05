"""The files a document carries next to it: figures, embeds, videos and the data behind them.

A document at `content/articles/2026-10-05-1a2b3c4d.md` may write files to
`content/articles/2026-10-05-1a2b3c4d.assets/` and refer to them from the Markdown, as
`./2026-10-05-1a2b3c4d.assets/flock-paths.svg` in a figure, as the `src:` of a `tin-embed` or
`tin-video` block, or in a link. The document declares its files: Tin keeps exactly the files it
refers to, of the kinds its workflow allows, records their checksums with the checkpoint, and
approval binds them with the document. Files nothing refers to are never published. Article
drafts carry SVG figures and HTML embeds; another workflow may declare more kinds.

Projects are untrusted. A file that fails its kind's check is left out with a reason rather
than failing the run; the reader shows SVG only as an image and embeds only in a sandboxed frame.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from tin_lite.studio_contracts import inspect_mp4


@dataclass(frozen=True)
class Kind:
    """A kind of file a document may carry: its media type, whether its bytes are binary, the
    most one file may hold, and what the agent and the founder are told it is."""

    media_type: str
    binary: bool
    max_bytes: int
    label: str
    extensions: tuple[str, ...]


KINDS = {
    "svg": Kind("image/svg+xml", False, 512_000, "SVG images", ("svg",)),
    "html": Kind("text/html", False, 512_000, "self-contained HTML files", ("html",)),
    "png": Kind("image/png", True, 8_000_000, "PNG images", ("png",)),
    "jpeg": Kind("image/jpeg", True, 8_000_000, "JPEG images", ("jpg", "jpeg")),
    "mp4": Kind("video/mp4", True, 16_000_000, "MP4 videos", ("mp4",)),
    "vtt": Kind("text/vtt", False, 512_000, "WebVTT captions", ("vtt",)),
    "json": Kind("application/json", False, 1_000_000, "JSON files", ("json",)),
}
# What article drafts carry, and what a definition that names no kinds means.
DEFAULT_KINDS = ("svg", "html")
_KIND_OF = {extension: name for name, kind in KINDS.items() for extension in kind.extensions}
MEDIA_TYPES = {f".{extension}": KINDS[name].media_type for extension, name in _KIND_OF.items()}
BINARY_MEDIA_TYPES = frozenset(kind.media_type for kind in KINDS.values() if kind.binary)
MAX_FILES_CEILING = 24
MAX_BYTES_CEILING = 64_000_000
NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(?:"
    + "|".join(sorted(_KIND_OF, key=len, reverse=True))
    + ")"
)
# Script, event handlers, embedded HTML, and references that leave the file.
UNSAFE_SVG = re.compile(
    r"<\s*script\b|<\s*foreignObject\b|\son[a-z]+\s*=|javascript:"
    r"|(?:xlink:)?href\s*=\s*[\"']\s*(?!#|data:image/)[^\"'\s]",
    re.I,
)


def folder(article_path: str) -> str:
    """The files folder next to a document: its path without `.md`, plus `.assets`."""
    if not article_path.endswith(".md"):
        raise ValueError("Only a Markdown document has an assets folder.")
    return article_path[: -len(".md")] + ".assets"


def referenced(article: str, article_path: str) -> list[str]:
    """The files the document refers to, as project paths, in the order they first appear."""
    name = folder(article_path).rsplit("/", 1)[-1]
    pattern = re.compile(rf"(?<![\w.-])(?:\./)?{re.escape(name)}/({NAME.pattern})(?![\w.-])")
    found: list[str] = []
    for match in pattern.finditer(article):
        path = f"{folder(article_path)}/{match.group(1)}"
        if path not in found:
            found.append(path)
    return found


def kind(path: str) -> str:
    return _KIND_OF[path[path.rindex(".") + 1 :]]


def media_type(path: str) -> str:
    return KINDS[kind(path)].media_type


def is_binary(path: str) -> bool:
    return KINDS[kind(path)].binary


def _size(limit: int) -> str:
    return f"{limit // 1_000_000} MB" if limit >= 1_000_000 else f"{limit // 1000} KB"


def problem(path: str, content: bytes, kinds: tuple[str, ...] = DEFAULT_KINDS) -> str | None:
    """Why a file can't be kept, or None. A problem drops the file; it never fails a run."""
    name = kind(path)
    spec = KINDS[name]
    if name not in kinds:
        return f"this workflow doesn't keep {spec.label}"
    if not content or len(content) > spec.max_bytes:
        return f"it is empty or larger than {_size(spec.max_bytes)}"
    if name == "png" and (content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR"):
        return "it is not a PNG image"
    if name == "jpeg" and content[:3] != b"\xff\xd8\xff":
        return "it is not a JPEG image"
    if name == "mp4":
        try:
            inspect_mp4(content, name="the video")
        except ValueError as exc:
            return str(exc)
    if spec.binary:
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return "it is not UTF-8 text"
    if name == "svg" and UNSAFE_SVG.search(text):
        return "the SVG contains script, event handlers or outside references"
    if name == "vtt" and not text.removeprefix("\ufeff").startswith("WEBVTT"):
        return "the captions don't start with WEBVTT"
    if name == "json":
        try:
            json.loads(text)
        except ValueError:
            return "it is not valid JSON"
    return None


@dataclass(frozen=True)
class AssetPolicy:
    """How many files a document may carry, how many bytes they may use together, and of
    which kinds."""

    max_files: int
    max_bytes: int
    kinds: tuple[str, ...] = DEFAULT_KINDS

    def definition(self) -> dict:
        value: dict = {"max_files": self.max_files, "max_bytes": self.max_bytes}
        # Article drafts' definitions name no kinds, so their pinned shape never changes.
        if self.kinds != DEFAULT_KINDS:
            value["kinds"] = list(self.kinds)
        return value

    @classmethod
    def load(cls, value: object) -> AssetPolicy:
        if not isinstance(value, dict) or not (
            {"max_files", "max_bytes"} <= set(value) <= {"max_files", "max_bytes", "kinds"}
        ):
            raise ValueError("Codex procedure assets contract is invalid")
        files, size = value["max_files"], value["max_bytes"]
        if (
            type(files) is not int
            or type(size) is not int
            or not 1 <= files <= MAX_FILES_CEILING
            or not 1 <= size <= MAX_BYTES_CEILING
        ):
            raise ValueError("Codex procedure assets limits are out of range")
        kinds = value.get("kinds", list(DEFAULT_KINDS))
        if (
            not isinstance(kinds, list)
            or not kinds
            or len(set(kinds)) != len(kinds)
            or any(item not in KINDS for item in kinds)
        ):
            raise ValueError("Codex procedure assets kinds are invalid")
        return cls(max_files=files, max_bytes=size, kinds=tuple(kinds))


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


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def attachments_line(checkpoint: dict | None, article: str) -> str | None:
    """What a reviewer will see besides the words, for the Decisions card, or None."""
    files = (checkpoint or {}).get("assets") or []
    kept = [item["media_type"] for item in files]
    # A raster image is a figure when the document shows it as one, not when it is a poster.
    pictures = sum(
        1
        for item in files
        if item["media_type"] in {"image/png", "image/jpeg"}
        and re.search(
            rf"!\[[^\]]*\]\([^)\s]*/{re.escape(item['artifact_path'].rsplit('/', 1)[-1])}", article
        )
    )
    figures = (
        kept.count("image/svg+xml")
        + pictures
        + len(re.findall(r"(?m)^(?:`{3,}|~{3,})\s*mermaid\b", article))
    )
    embeds = kept.count("text/html")
    videos = len(re.findall(r"(?m)^(?:`{3,}|~{3,})\s*tin-video\b", article))
    parts = [
        _count(number, noun)
        for number, noun in ((figures, "figure"), (embeds, "interactive piece"), (videos, "video"))
        if number
    ]
    if not parts:
        return None
    listed = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
    return f"With {listed}."
