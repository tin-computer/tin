"""The root helper's intel actions: trusted settings, the read-only day file, check-intel."""

from __future__ import annotations

import gzip
import json
import os
import pwd
import sqlite3
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from intel_cli_loader import PATH as CLI_PATH
from test_codex_isolation import load_sandbox_module

from tin_lite.e2b_runtime import INTEL_CLI_VERSION, INTEL_INPUT_PATH

GOOD = {
    "INTEL_DB": "/opt/tin-intel/day.db",
    "INTEL_URL": "https://tin.test/internal/run-tools/intel",
    "INTEL_GRANT": "g" * 43,
    "INTEL_SCOPE": "world",
    "INTEL_DAY": "2026-11-24",
}


def helper():
    return load_sandbox_module("isolated_procedure")


def test_the_worker_gets_the_intel_settings_and_nothing_else(monkeypatch):
    module = helper()
    monkeypatch.setattr(module, "trusted_config", lambda path, label: dict(GOOD))
    monkeypatch.setattr(module, "proxy_environment", list)
    monkeypatch.setattr(module, "studio_environment", list)
    assert module.intel_environment() == [f"{k}={GOOD[k]}" for k in sorted(GOOD)]
    command = module.worker_command("/bin/true")
    assert f"INTEL_GRANT={GOOD['INTEL_GRANT']}" in command
    assert command[-1] == "/bin/true" and command[0] == "/usr/bin/setpriv"


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "INTEL_DB": "/tmp/mine.db"},  # noqa: S108 — the attack being refused
        {**GOOD, "INTEL_URL": "http://tin.test/internal/run-tools/intel"},
        {**GOOD, "INTEL_URL": "https://attacker.test/collect"},
        {**GOOD, "INTEL_SCOPE": "everything"},
        {**GOOD, "INTEL_DAY": "yesterday!"},
        {**GOOD, "OPENAI_API_KEY": "x"},
        {k: v for k, v in GOOD.items() if k != "INTEL_GRANT"},
    ],
)
def test_untrusted_intel_settings_are_refused(monkeypatch, bad):
    module = helper()
    monkeypatch.setattr(module, "trusted_config", lambda path, label: bad)
    with pytest.raises(RuntimeError):
        module.intel_environment()


def test_no_settings_means_no_intel_environment(monkeypatch):
    module = helper()
    monkeypatch.setattr(module, "trusted_config", lambda path, label: None)
    assert module.intel_environment() == []


def snapshot(path: Path) -> Path:
    lines = [
        {"kind": "meta", "format": "intel-snapshot/1", "scope": "world", "day": "2026-11-24"},
        {
            "kind": "item",
            "item_key": "abcde",
            "source": "BBC News",
            "source_type": "world",
            "url": "https://www.bbc.com/news/1",
            "title": "A story",
            "text": "Body",
            "engagement": {},
            "rank": 1,
        },
    ]
    path.write_bytes(gzip.compress("".join(json.dumps(x) + "\n" for x in lines).encode()))
    return path


def test_intel_load_installs_a_read_only_day_file(monkeypatch, tmp_path):
    """As the test user (not root): ownership changes are no-ops, setpriv is stripped. What is
    checked is the flow, the input checks and the file's final mode."""
    module = helper()
    me = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: me)
    monkeypatch.setattr(module.os, "chown", lambda *args: None)
    monkeypatch.setattr(module, "INTEL_INPUT", snapshot(tmp_path / "input.jsonl.gz"))
    monkeypatch.setattr(module, "INTEL_BINARY", CLI_PATH)
    monkeypatch.setattr(module, "INTEL_DIR", tmp_path / "opt")
    monkeypatch.setattr(module, "INTEL_DB", tmp_path / "opt" / "day.db")
    real_run = subprocess.run
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        env_at = command.index("/usr/bin/env")
        assert command[:3] == ["/usr/bin/setpriv", "--reuid=tin-work", "--regid=tin-work"]
        assert not any("PROXY" in part or "INTEL_GRANT" in part for part in command)
        return real_run(["python3", *command[env_at + 5 :]], **kwargs)  # noqa: S603, S607

    monkeypatch.setattr(module.subprocess, "run", run)
    module.intel_load()
    day = tmp_path / "opt" / "day.db"
    assert stat.S_IMODE(day.stat().st_mode) == 0o444
    assert sqlite3.connect(day).execute("SELECT id FROM items").fetchall() == [("abcde",)]
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            day.open("r+b")
    assert len(commands) == 1 and not list(tmp_path.glob("tin-intel-*"))


def test_intel_load_refuses_a_linked_or_oversized_input(monkeypatch, tmp_path):
    module = helper()
    me = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: me)
    source = snapshot(tmp_path / "input.jsonl.gz")
    link = tmp_path / "link.gz"
    link.symlink_to(source)
    monkeypatch.setattr(module, "INTEL_INPUT", link)
    with pytest.raises(RuntimeError, match="invalid intel input"):
        module.intel_load()
    monkeypatch.setattr(module, "INTEL_INPUT", source)
    monkeypatch.setattr(module, "INTEL_MAX_INPUT", 10)
    with pytest.raises(RuntimeError, match="invalid intel input"):
        module.intel_load()


def test_check_intel_matches_the_pinned_cli_version(monkeypatch, tmp_path):
    module = helper()
    root_owned = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755)

    class Binary(type(CLI_PATH)):
        def lstat(self):
            return root_owned

    monkeypatch.setattr(module, "INTEL_BINARY", Binary(CLI_PATH))
    monkeypatch.setattr(module, "SQLITE_SHELL", tmp_path / "absent")
    assert module.check_intel() == f"TIN_INTEL_READY_V1 {INTEL_CLI_VERSION}"
    shell = tmp_path / "sqlite3"
    shell.write_text("")
    monkeypatch.setattr(module, "SQLITE_SHELL", shell)
    with pytest.raises(RuntimeError, match="sqlite3 shell"):
        module.check_intel()
    root_owned.st_mode = stat.S_IFREG | 0o775
    with pytest.raises(RuntimeError, match="not protected"):
        module.check_intel()


def test_the_controller_and_the_runtime_agree_on_paths():
    controller = load_sandbox_module("agent_procedure")
    module = helper()
    assert str(module.INTEL_INPUT) == INTEL_INPUT_PATH
    assert controller.INTEL_DB == str(module.INTEL_DB)
    assert str(controller.INTEL_CONFIG) == str(module.INTEL_CONFIG)
    assert module.INTEL_VERSION == INTEL_CLI_VERSION


def test_the_brief_names_the_intel_command_only_for_judges():
    controller = load_sandbox_module("agent_procedure")
    base = {
        "workflow_key": "intel.world_judge",
        "prompt": "Write today's world stories.",
        "entry_skill": "world-stories",
        "skill_files": {},
        "workspace": {"kind": "project.state"},
        "output": {
            "kind": "project.artifact",
            "path": "intel/world/runs/x/NOTES.md",
            "max_bytes": 9,
        },
        "verification": {"commands": []},
        "project_skills": [],
        "inputs": {"day": "2026-11-24"},
    }
    assert "`intel` command" in controller.brief(
        {**base, "intel": {"scope": "world"}}, "project.artifact", "p"
    )
    assert "`intel` command" not in controller.brief(base, "project.artifact", "p")
