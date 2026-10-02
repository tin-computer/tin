"""Offline fakes for the loop workflow packages: project files, services, models and a clock.

The packages run as ordinary modules here, with their package directory on the import path
as the code runner sets it, so `from channels import ...` resolves to the package's own copy.
"""

import datetime as dt
import fnmatch
import importlib.util
import json
import sys
from types import SimpleNamespace

from tin_lite.community import REPOSITORY_ROOT

TODAY = dt.datetime(2026, 9, 29, 15, 0, tzinfo=dt.UTC)


# Package-local helper modules; each package imports its own copy by these names.
HELPERS = ("channels", "readout", "panel_check", "architecture")


def load(key, monkeypatch=None, *, now=TODAY):
    root = REPOSITORY_ROOT / "workflow_packages" / key
    for name in HELPERS:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(root))
    # No __pycache__ in the package: the validator refuses files a package can't carry.
    writes, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec = importlib.util.spec_from_file_location(
            "pkg_" + key.replace(".", "_"), root / "main.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = writes
        sys.path.remove(str(root))
        for name in HELPERS:
            sys.modules.pop(name, None)
    if monkeypatch is not None:
        monkeypatch.setattr(module, "dt", clock(now))
    return module


def definition(key):
    path = REPOSITORY_ROOT / "workflow_packages" / key / "workflow.json"
    return json.loads(path.read_text())["definition"]


def clock(now):
    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    return SimpleNamespace(
        date=dt.date, timedelta=dt.timedelta, UTC=dt.UTC, timezone=dt.timezone, datetime=Frozen
    )


class Files:
    def __init__(self, files):
        self.files = {
            path: value if isinstance(value, str) else json.dumps(value)
            for path, value in (files or {}).items()
        }
        self.reads = []

    def read_text(self, path):
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        text = self.files[path]
        if len(text.encode()) > 64_000:
            raise ValueError("project file exceeds 64000 bytes")
        return text

    def glob(self, pattern):
        return sorted(path for path in self.files if fnmatch.fnmatchcase(path, pattern))


class Services:
    """Answers each call by step from `responses`; an Exception value is raised."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call(self, *, service, step, operation, arguments=None):
        self.calls.append(
            {"service": service, "step": step, "operation": operation, "arguments": arguments}
        )
        answer = self.responses.get(step)
        if callable(answer):
            answer = answer(arguments)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise ValueError("Service response unavailable or invalid")
        return answer


class Models:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def generate(self, **payload):
        self.calls.append(payload)
        answer = self.answers.get(payload["step"])
        if isinstance(answer, Exception):
            raise answer
        return {"parsed": answer, "text": json.dumps(answer)}


def context(files=None, services=None, models=None):
    return SimpleNamespace(
        files=Files(files),
        services=Services(services or {}),
        models=Models(models or {}),
        run_id="00000000-0000-4000-8000-000000000001",
    )


def gsc(rows, **extra):
    return {"rows": rows, **extra}


def hogql(columns, rows, **extra):
    return {"columns": columns, "rows": rows, "has_more": False, "truncated": False, **extra}
