"""LinkedIn grouping reuses the ordinary workflow and document contracts."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from tin_lite.catalog import BUILTIN_WORKFLOWS, WORKFLOW_SYSTEMS


def test_linkedin_group_preserves_existing_social_group():
    assert next(s for s in WORKFLOW_SYSTEMS if s.id == "x").name == "Social"
    assert next(s for s in WORKFLOW_SYSTEMS if s.id == "linkedin").name == "LinkedIn"
    assert next(w for w in BUILTIN_WORKFLOWS if w.key == "connections.collect").system == "linkedin"


def test_private_group_and_advanced_fields_are_presentation_only():
    from tin_lite.private_workflows import authoring_guide, validate_private_definition
    from tin_lite.workflow_inputs import validate_input_schema
    from tin_lite.workflow_packages import decode_workflow_source

    path = "workflow_packages/custom.research_digest/workflow.json"
    guide = authoring_guide(settings=SimpleNamespace(), project_id=uuid4())
    definition = decode_workflow_source(
        guide["example_files"][path].encode(), definition_path=path
    ).definition
    definition["system"] = "linkedin"
    validate_private_definition(definition)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project_id": {"type": "string", "format": "uuid"},
            "notes": {
                "type": "string",
                "maxLength": 200,
                "x-tin-ui": {"advanced": True, "control": "textarea"},
            },
        },
        "required": ["project_id"],
    }
    validate_input_schema(schema)
    schema["properties"]["notes"]["x-tin-ui"]["advanced"] = "yes"
    with pytest.raises(ValueError, match="boolean"):
        validate_input_schema(schema)
