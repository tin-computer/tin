"""Legacy source receipts remain readable without source-choice presentation."""

import httpx
from test_code_evidence import fixture, start
from test_private_workflows import app, mcp, structured
from test_procedure_publication import publication_db as publication_db


async def test_legacy_code_source_still_runs_and_projects_provenance(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, dynamic=True)
    run = await start(f)
    server = mcp(f, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        run_view = await client.get(f"/api/workflows/runs/{run.id}")
        selected = run_view.json()["selected_sources"]["posts"]
        assert selected["run_id"] == str(f.posts.id)
        assert selected["revision"] == f.posts.canonical_commit_sha
        assert "content" not in selected and "publication_checkpoint" not in selected
        assert selected["read_url"].endswith(f"/document/{f.posts.id}?project={f.project.id}")
        retired = await client.get(
            f"/api/projects/{f.project.id}/workflow-sources/{f.evidence_workflow.id}"
        )
        assert retired.status_code == 404
    mcp_run = structured(await server.call_tool("get_run", {"run_id": str(run.id)}))
    assert mcp_run["selected_sources"]["posts"] == selected
    contract = structured(
        await server.call_tool(
            "get_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": str(f.evidence_workflow.id),
            },
        )
    )
    assert "sources" not in contract.get("preparation", {})
