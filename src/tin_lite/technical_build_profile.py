"""Bounded, data-only reads of a repository snapshot. Never import or execute repository code."""

import tarfile
from pathlib import PurePosixPath

from tin_lite.repository_limits import snapshot_reader


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
    with tarfile.open(fileobj=snapshot_reader(archive), mode="r:gz") as source:
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
