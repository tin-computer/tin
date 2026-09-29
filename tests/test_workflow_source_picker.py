"""HTTP and MCP source selection expose one project-bound, proof-filtered contract."""

import json
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_code_evidence import fixture, start
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db


async def test_generic_candidates_and_selected_run_metadata_match_http_and_mcp(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch, dynamic=True)
    server = mcp(f, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get(
            f"/api/projects/{f.project.id}/workflow-sources/{f.evidence_workflow.id}"
        )
        assert response.status_code == 200, response.text
        candidate = response.json()["slots"][0]["candidates"][0]
        assert candidate["run_id"] == str(f.posts.id)
        assert candidate["revision"] == f.posts.canonical_commit_sha
        assert candidate["artifact_path"] == f.posts.artifact_path
        contract = structured(
            await server.call_tool(
                "get_workflow",
                {
                    "project_id": str(f.project.id),
                    "workflow_id": str(f.evidence_workflow.id),
                },
            )
        )
        assert contract["preparation"]["sources"][0]["candidates"] == [candidate]
        assert contract["readiness"]["state"] == "ready"
        run = await start(f)
        run_view = await client.get(f"/api/workflows/runs/{run.id}")
        selected = run_view.json()["selected_sources"]["posts"]
        assert selected["run_id"] == str(f.posts.id)
        assert selected["read_url"] == candidate["read_url"]
        assert "content" not in selected and "publication_checkpoint" not in selected
        mcp_run = structured(await server.call_tool("get_run", {"run_id": str(run.id)}))
        assert mcp_run["selected_sources"]["posts"] == selected


async def test_saved_source_picker_uses_pinned_definition_and_project_boundary(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    configured = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=f.evidence_workflow.id,
        definition_commit_sha=f.evidence_workflow.current_commit_sha,
        name="Saved source consumer",
        inputs=f.evidence_inputs,
        input_schema=f.evidence_definition["input_schema"],
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
        pinned_definition=f.evidence_definition,
    )
    revised = deepcopy(f.evidence_definition)
    del revised["code"]["evidence"]
    del revised["input_schema"]["properties"]["posts_run_id"]
    revised["input_schema"]["required"].remove("posts_run_id")
    revision = f.storage.repo.edit(
        {
            f.evidence_workflow.definition_path: json.dumps(
                {
                    "package_format": "tin-workflow-package-v1",
                    "definition": revised,
                }
            ).encode()
        }
    )
    await f.db.upsert_registry_workflow(
        workflow_id=f.evidence_workflow.id,
        key=f.evidence_workflow.key,
        title=f.evidence_workflow.title,
        description=f.evidence_workflow.description,
        executor="workflow.code",
        definition_repo_id=f.project.state_repo_id,
        definition_path=f.evidence_workflow.definition_path,
        current_commit_sha=revision,
        version_label="2.0.0",
        definition=revised,
    )
    server = mcp(f, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        base = f"/api/projects/{f.project.id}/workflow-sources/{f.evidence_workflow.id}"
        latest = await client.get(base)
        pinned = await client.get(base, params={"project_workflow_id": str(configured.id)})
        assert latest.status_code == pinned.status_code == 200
        assert latest.json()["slots"] == []
        assert pinned.json()["definition_revision"] == configured.definition_commit_sha
        assert pinned.json()["slots"][0]["candidates"][0]["run_id"] == str(f.posts.id)
    contract = structured(
        await server.call_tool(
            "get_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": str(f.evidence_workflow.id),
                "project_workflow_id": str(configured.id),
            },
        )
    )
    assert contract["preparation"]["sources"] == pinned.json()["slots"]
    other = await f.db.create_project(name="Other", state_repo_id="projects/other-picker")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, "outsider")), base_url="https://tin.test"
    ) as client:
        forbidden = await client.get(base, params={"project_workflow_id": str(configured.id)})
    assert forbidden.status_code == 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        misplaced = await client.get(
            f"/api/projects/{other.id}/workflow-sources/{f.evidence_workflow.id}",
            params={"project_workflow_id": str(configured.id)},
        )
    assert misplaced.status_code == 404


async def test_invalid_generic_proof_yields_empty_picker_and_blocked_exact_readiness(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    receipt_key = f"{f.posts.id}:procedure_canonical_commit"
    receipt = deepcopy((await f.db.get_effect(receipt_key)).result)
    receipt["checkpoint"]["sha256"] = "0" * 64
    await f.db.pool.execute(
        "UPDATE effect_receipts SET result=$2::jsonb WHERE execution_key=$1",
        receipt_key,
        json.dumps(receipt),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        picker = await client.get(
            f"/api/projects/{f.project.id}/workflow-sources/{f.evidence_workflow.id}"
        )
        exact = await client.get(
            f"/api/workflows/{f.evidence_workflow.id}",
            params={"project_id": str(f.project.id)},
        )
        listing = await client.get("/api/workflows", params={"project_id": str(f.project.id)})
    assert picker.json()["slots"][0]["candidates"] == []
    assert exact.json()["readiness"]["state"] == "blocked"
    listed = next(row for row in listing.json() if row["id"] == str(f.evidence_workflow.id))
    assert listed["readiness"]["state"] == "advisory"
    contract = structured(
        await mcp(f, monkeypatch).call_tool(
            "get_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": str(f.evidence_workflow.id),
            },
        )
    )
    assert contract["preparation"]["sources"][0]["candidates"] == []
    assert contract["readiness"]["state"] == "blocked"


async def test_bounded_discovery_error_is_explicit_on_http_and_mcp(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)

    async def exhausted(**_kwargs):
        raise ValueError("Recent approved-source search reached its 200-run limit")

    monkeypatch.setattr("tin_lite.api.discover_slots", exhausted)
    monkeypatch.setattr("tin_lite.mcp_server.discover_slots", exhausted)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        picker = await client.get(
            f"/api/projects/{f.project.id}/workflow-sources/{f.evidence_workflow.id}"
        )
        workflow = await client.get(
            f"/api/workflows/{f.evidence_workflow.id}",
            params={"project_id": str(f.project.id)},
        )
    assert picker.status_code == workflow.status_code == 409
    with pytest.raises(ToolError, match="200-run limit"):
        await mcp(f, monkeypatch).call_tool(
            "get_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": str(f.evidence_workflow.id),
            },
        )
