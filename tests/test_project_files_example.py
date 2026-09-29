"""File-first authoring: use current project data without a producer or revision input."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tin_lite.community import ContributedPackage, validate, validate_private_copy
from tin_lite.public_workflows import PUBLIC_WORKFLOWS
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/example.project_files"


async def test_file_example_accepts_public_and_private_ownership():
    package = ContributedPackage(key="example.project_files", path=PACKAGE)
    await validate(package, root=ROOT)
    await validate_private_copy(package, root=ROOT)
    assert all(item.key != package.key for item in PUBLIC_WORKFLOWS)


def context(content):
    def read_text(path):
        assert path == "BRAND.md"
        if isinstance(content, Exception):
            raise content
        return content

    return SimpleNamespace(files=SimpleNamespace(read_text=read_text))


@pytest.mark.parametrize(
    ("file", "inputs", "expected"),
    [
        (
            "# Brand\n\nClear, useful prose.",
            {"brand_notes": "Old fallback"},
            "Clear, useful prose.",
        ),
        (FileNotFoundError(), {"brand_notes": "Supplied notes"}, "Supplied notes"),
    ],
)
def test_example_prefers_file_and_accepts_missing_file_fallback(file, inputs, expected):
    result = runpy.run_path(str(PACKAGE / "main.py"))["run"](context(file), inputs)
    spec = validate_code_definition(
        json.loads((PACKAGE / "workflow.json").read_text())["definition"]
    )
    output = validate_code_result(json.dumps(result).encode(), spec).decode()
    assert output.endswith(expected)
    assert "Old fallback" not in output


@pytest.mark.parametrize("content", [FileNotFoundError(), "   ", "x" * 30001, ValueError("unsafe")])
def test_missing_blank_oversized_or_invalid_source_is_not_a_usable_report(content):
    run = runpy.run_path(str(PACKAGE / "main.py"))["run"]
    with pytest.raises(ValueError):
        run(context(content), {})


def test_invalid_file_does_not_silently_use_fallback():
    run = runpy.run_path(str(PACKAGE / "main.py"))["run"]
    with pytest.raises(ValueError, match="unsafe"):
        run(context(ValueError("unsafe")), {"brand_notes": "Plausible fallback"})
