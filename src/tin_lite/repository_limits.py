"""Repository snapshot bounds shared by definition validation and the gateway."""

from pathlib import PurePosixPath

# Every repository workspace uses these bounds; definitions no longer pin their own. They
# are runaway guards, set well above real sites. GitHub's recursive tree API truncates at
# 100,000 entries (directories included), so no snapshot can hold more files than that.
REPOSITORY_MAX_FILES = 100_000
# Kept in a temporary file while the snapshot is built; only the compressed result is held
# in memory, then written to the sandbox (/home/user), where it is unpacked and committed.
# The byte bounds are set by the switchboard VM (an e2-small: about 1 GB of free RAM and
# under 5 GB of free disk), not by the sandbox: each run in flight holds up to the download,
# the snapshot and its compressed copy on disk, and the compressed copy in memory.
REPOSITORY_MAX_BYTES = 250_000_000
# The compressed tarball also carries files the snapshot filters out (over 10 MB, links).
# It is streamed to a temporary file, never held in memory.
REPOSITORY_DOWNLOAD_MAX_BYTES = 1_000_000_000
# Paths the tarball omits or rewrites (export-ignore, export-subst) are read one by one,
# one GitHub API call each.
REPOSITORY_BLOB_FALLBACKS = 500
# A snapshot leaves out every file larger than this.
REPOSITORY_FILE_MAX_BYTES = 10_000_000
FILE_LIMIT_TEXT = f"{REPOSITORY_FILE_MAX_BYTES // 1_000_000} MB"

# Large files a snapshot may leave out and still count as complete: they can't hold page
# copy, metadata or code that a fix would edit. Keyed by suffix, valued by why.
_BINARY_KINDS = {
    "image": (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".ico",
        ".bmp",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
        ".psd",
    ),
    "video": (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ogv", ".mpg", ".mpeg"),
    "audio": (".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus"),
    "font": (".woff", ".woff2", ".ttf", ".otf", ".eot"),
    "archive": (".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".rar", ".zst"),
    "pdf": (".pdf",),
    "compiled": (".wasm",),
}
SKIPPABLE_SUFFIXES = {
    suffix: kind for kind, suffixes in _BINARY_KINDS.items() for suffix in suffixes
}
# Directories whose scripts and stylesheets are served or built output, not edited source.
PUBLIC_ROOTS = frozenset(
    {"public", "static", "dist", "build", "out", "www", "wwwroot", "htdocs", "_next"}
)
SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs", ".css")
SOURCE_MAP_SUFFIXES = tuple(f"{suffix}.map" for suffix in SCRIPT_SUFFIXES)
MINIFIED_SUFFIXES = tuple(f".min{suffix}" for suffix in SCRIPT_SUFFIXES)

# Why a tree entry stayed out of a snapshot, in the words a refusal uses.
OMISSION_TEXT = {
    "image": "an image",
    "video": "a video",
    "audio": "an audio file",
    "font": "a font",
    "archive": "an archive",
    "pdf": "a PDF",
    "compiled": "a compiled binary",
    "source_map": "a source map",
    "minified": "a minified asset",
    "built_asset": "a built asset under a public or static folder",
    "too_large": f"over the {FILE_LIMIT_TEXT} limit for files Tin reads",
    "symlink": "a symbolic link",
    "submodule": "a Git submodule",
    "unsupported_path": "a path Tin can't read safely",
    "unsupported_entry": "an entry GitHub didn't describe fully",
}


def skippable_large_file(path: str) -> str | None:
    """Why a file over REPOSITORY_FILE_MAX_BYTES may stay out of a complete snapshot, or None
    when it could hold copy, metadata or code a fix needs (it then counts as missing)."""
    pure = PurePosixPath(path.lower())
    name = pure.name
    if pure.suffix in SKIPPABLE_SUFFIXES:
        return SKIPPABLE_SUFFIXES[pure.suffix]
    if name.endswith(SOURCE_MAP_SUFFIXES):
        return "source_map"
    if name.endswith(MINIFIED_SUFFIXES):
        return "minified"
    if name.endswith(SCRIPT_SUFFIXES) and PUBLIC_ROOTS.intersection(pure.parts[:-1]):
        return "built_asset"
    return None


def describe_omission(item: dict) -> str:
    """`public/data.json (12.4 MB, over the 10 MB limit for files Tin reads)`."""
    path = item.get("path") or "an unnamed entry"
    reason = OMISSION_TEXT.get(item.get("reason", ""), "left out")
    size = item.get("size")
    if isinstance(size, int) and size > REPOSITORY_FILE_MAX_BYTES:
        return f"{path} ({size / 1_000_000:.1f} MB, {reason})"
    return f"{path} ({reason})"


def describe_omissions(items, *, limit: int = 5, total: int | None = None) -> str:
    """The first few omissions in one sentence fragment, with how many more of `total`."""
    items = list(items)
    named = "; ".join(describe_omission(item) for item in items[:limit])
    more = (len(items) if total is None else total) - min(limit, len(items))
    return named + (f"; and {more} more" if more > 0 and named else "")
