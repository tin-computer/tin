"""Setup shows existing project documents without weakening run admission."""

import json
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_billing import billed as billed
from test_private_workflows import app, structured
from test_procedure_publication import publication_db as publication_db
from test_workflow_code import PATH, activate_code, example_files, setup

from tin_lite.workflow_inputs import validate_input_schema


async def test_incomplete_setup_still_checks_files_and_matches_mcp(billed, monkeypatch):
    f = billed
    server, _, _ = await setup(f, monkeypatch)
    definition = json.loads(example_files()[PATH])
    schema = definition["definition"]["input_schema"]
    schema["properties"]["minimum_cents"].pop("default")
    schema["properties"]["guide"] = {
        "type": "string",
        "title": "Audience",
        "minLength": 1,
        "maxLength": 512,
        "default": "context/AUDIENCE.md",
        "x-tin-ui": {"control": "project_file"},
    }
    schema["required"].append("guide")
    definition["definition"]["prerequisites"] = [
        {
            "kind": "artifact",
            "path": "{guide}",
            "level": "required",
            "reason": "Drafts use the project's saved audience.",
        }
    ]
    revision = f.storage.repo.edit(
        {PATH: json.dumps(definition).encode(), "context/AUDIENCE.md": b"A saved audience."}
    )
    active = await activate_code(f, server, revision=revision)
    selection = {
        "project_id": str(f.project.id),
        "workflow_id": active["workflow_id"],
        "inputs": {},
    }
    result = structured(await server.call_tool("get_code_workflow_setup", selection))
    assert not result["can_run"] and not result["can_schedule"]
    assert result["missing_inputs"] == ["minimum_cents"]
    assert result["inputs"]["guide"] == "context/AUDIENCE.md"
    assert result["prerequisites"][0]["satisfied"]
    assert result["prerequisites"][0]["resolved_path"] == "context/AUDIENCE.md"
    assert result["prerequisites"][0]["revision"] == revision
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://test"
    ) as client:
        response = await client.post(
            f"/api/projects/{f.project.id}/workflow-setup",
            json={k: v for k, v in selection.items() if k != "project_id"},
        )
        assert response.status_code == 200 and response.json() == result
    with pytest.raises(ToolError, match="minimum_cents"):
        await server.call_tool("start_workflow", {**selection, "request_id": str(uuid4())})
    missing = structured(
        await server.call_tool(
            "get_code_workflow_setup",
            {**selection, "inputs": {"minimum_cents": 1, "guide": "missing.md"}},
        )
    )
    assert not missing["can_run"]
    assert missing["prerequisites"][0]["resolved_path"] == "missing.md"
    assert not missing["prerequisites"][0]["satisfied"]
    assert "missing.md" in " ".join(missing["issues"])
    complete = structured(
        await server.call_tool(
            "get_code_workflow_setup", {**selection, "inputs": {"minimum_cents": 1}}
        )
    )
    assert complete["can_run"] and complete["missing_inputs"] == []
    cleared = structured(
        await server.call_tool(
            "get_code_workflow_setup", {**selection, "inputs": {"minimum_cents": 1, "guide": ""}}
        )
    )
    assert not cleared["can_run"] and cleared["missing_inputs"] == ["guide"]
    assert "guide" not in cleared["inputs"], "a cleared choice must not silently restore a default"
    with pytest.raises(ToolError, match="minimum_cents"):
        await server.call_tool(
            "get_code_workflow_setup", {**selection, "inputs": {"minimum_cents": "wrong type"}}
        )


def test_project_file_hint_only_accepts_string_inputs():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project_id": {"type": "string"},
            "document": {"type": "string", "x-tin-ui": {"control": "project_file"}},
        },
    }
    validate_input_schema(schema)
    schema["properties"]["document"]["type"] = "integer"
    with pytest.raises(ValueError, match="cannot use"):
        validate_input_schema(schema)
