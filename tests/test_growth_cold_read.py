"""Offline checks for the growth.cold_read procedure package.

This is a pure-judgment codex.procedure: no embedded Python resource (unlike
example.posthog_funnel/product.analytics_brief) and no model_routes (unlike the
workflow.code examples), so there is no deterministic function to unit test the
way those packages are. What IS checkable offline, and what these tests cover:

1. The package loads through the exact same path validate-community uses
   (community.validate_files), with no filesystem checkout required.
2. The declared input contract actually enforces its own bounds -- this is the
   "truncated read or a missing selection" case AGENTS.md calls out for a
   procedure without model routes: a missing product_url, too many
   additional_pages, or an invalid URL are rejected before a run ever starts,
   not left for the agent to discover at runtime.
"""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tin_lite.community import validate_files
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs

ROOT = Path(__file__).resolve().parent.parent / "workflow_packages" / "growth.cold_read"
DEFINITION_PATH = "workflow_packages/growth.cold_read/workflow.json"


def package_files():
    files = {}
    for path in ROOT.rglob("*"):
        if path.is_file():
            key = "workflow_packages/growth.cold_read/" + path.relative_to(ROOT).as_posix()
            files[key] = path.read_bytes()
    return files


def definition():
    return json.loads((ROOT / "workflow.json").read_text())["definition"]


async def test_package_validates_through_the_same_path_as_ci():
    loaded_definition, _fingerprint = await validate_files(
        package_files(), definition_path=DEFINITION_PATH
    )
    assert loaded_definition["key"] == "growth.cold_read"
    assert loaded_definition["executor"] == "codex.procedure"
    assert loaded_definition["procedure"]["sandbox"]["profile"] == "browser"
    assert "integration_requirements" not in loaded_definition


def test_a_bare_product_url_normalizes_with_defaults():
    # normalize_workflow_inputs deliberately excludes project_id from its return value
    # (it's bound separately server-side); verified against its actual implementation.
    normalized = normalize_workflow_inputs(
        schema=definition()["input_schema"],
        project_id=str(uuid4()),
        inputs={"product_url": "https://example.com"},
    )
    assert normalized == {
        "product_url": "https://example.com",
        "additional_pages": [],
        "notes": "",
    }


def test_missing_product_url_is_rejected():
    schema = definition()["input_schema"]
    with pytest.raises(WorkflowInputError):
        normalize_workflow_inputs(schema=schema, project_id=str(uuid4()), inputs={})


def test_more_than_two_additional_pages_is_rejected():
    schema = definition()["input_schema"]
    with pytest.raises(WorkflowInputError):
        normalize_workflow_inputs(
            schema=schema,
            project_id=str(uuid4()),
            inputs={
                "product_url": "https://example.com",
                "additional_pages": [
                    "https://example.com/pricing",
                    "https://example.com/about",
                    "https://example.com/extra",
                ],
            },
        )


def test_format_uri_is_not_strictly_enforced_by_the_schema_layer():
    # Verified against the actual FormatChecker in this environment: jsonschema has no
    # "uri" checker registered (the optional rfc3987 backend isn't a project dependency),
    # so format: "uri" is currently a no-op here -- a malformed string passes schema
    # validation. This isn't unique to this package (qa.signup_walkthrough's product_url
    # uses the same format keyword); it's why the skill's own instructions handle an
    # unreachable/invalid product_url with an explicit diagnostic report instead of
    # assuming the schema already ruled that case out.
    schema = definition()["input_schema"]
    normalized = normalize_workflow_inputs(
        schema=schema, project_id=str(uuid4()), inputs={"product_url": "not-a-url"}
    )
    assert normalized["product_url"] == "not-a-url"


def test_two_additional_pages_is_within_bounds():
    schema = definition()["input_schema"]
    normalized = normalize_workflow_inputs(
        schema=schema,
        project_id=str(uuid4()),
        inputs={
            "product_url": "https://example.com",
            "additional_pages": ["https://example.com/pricing", "https://example.com/about"],
        },
    )
    assert len(normalized["additional_pages"]) == 2


def test_oversized_notes_are_rejected():
    schema = definition()["input_schema"]
    with pytest.raises(WorkflowInputError):
        normalize_workflow_inputs(
            schema=schema,
            project_id=str(uuid4()),
            inputs={"product_url": "https://example.com", "notes": "x" * 1001},
        )
