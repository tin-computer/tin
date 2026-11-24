"""The sandbox's intel CLI, loaded as a module for tests."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "sandbox" / "intel" / "intel.py"


def load():
    loader = importlib.machinery.SourceFileLoader("intel_cli", str(PATH))
    spec = importlib.util.spec_from_loader("intel_cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module
