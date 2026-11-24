from __future__ import annotations

import os
import zlib
from collections.abc import Callable
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """With TEST_SHARD=2/4, keep only the second of four fixed slices of the suite.

    CI runs the slices in parallel jobs. A test's slice comes from a hash of its node ID, so
    every xdist worker picks the same tests, and each test lands in exactly one slice.
    """
    shard = os.environ.get("TEST_SHARD")
    if not shard:
        return
    index, total = (int(part) for part in shard.split("/"))
    if not 1 <= index <= total:
        raise pytest.UsageError(f"TEST_SHARD must look like 2/4, got {shard!r}")
    kept, deselected = [], []
    for item in items:
        in_shard = zlib.crc32(item.nodeid.encode()) % total == index - 1
        (kept if in_shard else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drop the migrated template database this process made for its tests, if any."""
    from disposable_db import drop_template

    drop_template()


# Windows refuses to create a symlink without Developer Mode or an elevated shell.
_WINDOWS_SYMLINK_PRIVILEGE = 1314


@pytest.fixture
def make_symlink() -> Callable[[Path, Path], None]:
    """Create `link` pointing at `target`, or skip where the OS will not create one.

    A test about refusing symlinks needs a symlink to exist; without the privilege there is
    nothing to refuse, so the case is skipped rather than reported as a product failure.
    """

    def make(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as error:
            if getattr(error, "winerror", None) != _WINDOWS_SYMLINK_PRIVILEGE:
                raise
            pytest.skip("creating symlinks needs Developer Mode or an elevated shell on Windows")

    return make


@pytest.fixture(autouse=True)
def _settled_switchboard_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test process is always young; only tests about restarts pretend Tin just came up."""
    from tin_lite import activities

    monkeypatch.setattr(activities, "process_uptime_seconds", lambda: 3600.0)
