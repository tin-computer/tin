"""Starting an unsaved content program tells the agent the exact calls that save and start it."""

import json
from uuid import UUID, uuid4

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_procedure_publication import publication_db as publication_db
from test_workflow_prerequisites import ACTOR, A, project_fixture, server, succeeded_run


def diagnostic(error: ToolError) -> dict:
    text = str(error)
    return json.loads(text[text.index("{") :])


async def test_unsaved_content_program_names_the_next_calls(publication_db, monkeypatch):
    f = await project_fixture(
        publication_db, extra=("organic.audit", "organic.keyword_plan", "content.plan")
    )
    await publication_db.pool.execute(
        "UPDATE projects SET timezone='Europe/Istanbul' WHERE id=$1", f.project.id
    )
    site = {"site_url": "https://a.example/", "market": "US"}
    audit = await succeeded_run(f, "organic.audit", site)
    keywords = await succeeded_run(f, "organic.keyword_plan", site)
    inputs = {
        "audit_run_id": str(audit.id),
        "keyword_run_id": str(keywords.id),
        "start_date": "2026-10-07",  # a Wednesday
    }
    tools = server(f, monkeypatch)
    start = {"project_id": str(f.project.id), "workflow_id": "content.plan", "inputs": inputs}
    with pytest.raises(ToolError) as caught:
        await tools.call_tool("start_workflow", start)
    found = diagnostic(caught.value)
    assert found["code"] == "content_program_not_saved"
    assert "create_project_workflow" in found["message"]
    create = found["next_tool"]
    assert create["name"] == "create_project_workflow"
    arguments = create["arguments"]
    assert arguments["project_id"] == str(f.project.id)
    assert arguments["workflow_id"] == "content.plan"
    assert {key: arguments["inputs"][key] for key in inputs} == inputs
    assert arguments["inputs"]["duration"] == "6_months"  # defaults are filled in
    assert arguments["schedule"] == {
        "cadence": "weekly",
        "weekdays": ["wednesday"],
        "local_time": "09:00",
        "timezone": "Europe/Istanbul",
    }
    UUID(arguments["request_id"])
    # Saving the schedule starts the program's first run, so no second start is suggested.
    assert "starts the program's first run itself" in found["then"]
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 2

    # Once the program is saved, the answer is the start call with its id filled in.
    saved = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=f.workflows["content.plan"].id,
        definition_commit_sha=A,
        name=arguments["name"],
        inputs=arguments["inputs"],
        input_schema=f.workflows["content.plan"].definition["input_schema"],
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
    )
    with pytest.raises(ToolError) as caught:
        await tools.call_tool("start_workflow", start)
    found = diagnostic(caught.value)
    assert found["next_tool"]["name"] == "start_project_workflow"
    assert found["next_tool"]["arguments"]["project_workflow_id"] == str(saved.id)
    assert str(saved.id) in found["message"] and "then" not in found
    f.runtime.temporal.start_workflow.assert_not_awaited()


async def test_unknown_saved_workflow_says_where_to_find_the_id(publication_db, monkeypatch):
    f = await project_fixture(publication_db)
    with pytest.raises(ToolError, match="call list_project_workflows to get its id"):
        await server(f, monkeypatch).call_tool(
            "start_project_workflow",
            {
                "project_id": str(f.project.id),
                "project_workflow_id": str(uuid4()),
                "request_id": str(uuid4()),
            },
        )
    with pytest.raises(ToolError, match="call list_project_runs to get it"):
        await server(f, monkeypatch).call_tool("get_run", {"run_id": "not-a-run"})
