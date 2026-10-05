"""Agent-led style guidance shares project authorization and creates no model work."""

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_private_workflows import app, fixture, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.style_capture import parse_packet
from tin_lite.writing_style import STYLE_PATH, writing_style_guide


def test_discovery_contract_collects_evidence_instead_of_inventing_a_default():
    guide = writing_style_guide()
    assert guide["schema"] == "writing-style-guide-v2"
    sources = {source["kind"]: source for source in guide["source_discovery"]["sources"]}
    for invitation in (
        guide["caller_instruction"],
        guide["opening_question"],
        sources["authored_writing"]["ask"],
    ):
        assert "blog posts" in invitation and "links or files" in invitation
    assert guide["opening_question"].count("?") == 1
    pacing = str(guide["source_discovery"]["conversation_pacing"])
    assert "menu, not a checklist" in pacing
    assert "skip it when the user already supplied sources" in pacing
    assert "Do not also request an Obsidian vault and agent histories" in pacing
    assert "one focused follow-up" in pacing
    assert set(sources) == {
        "authored_writing",
        "obsidian",
        "agent_sessions",
        "other_selected_sources",
    }
    sessions = str(sources["agent_sessions"])
    assert all(name in sessions for name in ("Codex", "Claude Code", "other harness"))
    assert "deduplicate" in sessions and "compaction summary" in sessions
    assert "Do not silently" in guide["source_discovery"]["unavailable_or_declined"]
    assert "not permission to skip discovery" in guide["selection"]["limited_basis"]
    assert "before committing" in guide["selection"]["confirm"]
    assert "six independent writing samples" in str(guide["selection"]["quality"])
    # A copied blank template cannot buy a preferences-only run using our example voice.
    with pytest.raises(ValueError):
        parse_packet(guide["source_template"].encode())


async def test_style_guide_http_mcp_parity_and_no_run(publication_db, monkeypatch):
    f = await fixture(publication_db)
    server = mcp(f, monkeypatch)
    guide = structured(
        await server.call_tool("get_writing_style_guide", {"project_id": str(f.project.id)})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get(f"/api/projects/{f.project.id}/writing-style/guide")
    assert response.status_code == 200 and response.json() == guide == writing_style_guide()
    assert response.headers["Cache-Control"] == "no-store"
    assert guide["path"] == STYLE_PATH
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
    f.runtime.temporal.start_workflow.assert_not_called()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, "user_outsider")), base_url="https://tin.test"
    ) as client:
        assert (
            await client.get(f"/api/projects/{f.project.id}/writing-style/guide")
        ).status_code == 404
    denied = mcp(f, monkeypatch, actor="user_outsider")
    with pytest.raises(ToolError, match="project not found"):
        await denied.call_tool("get_writing_style_guide", {"project_id": str(f.project.id)})


def test_new_public_article_prompt_reads_optional_project_guide():
    article = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.public_article")
    assert article.version_label == "1.8.0"
    assert article.system == "organic-traffic"
    assert article.prerequisites[0].producer == "style.capture"
    assert article.procedure.project_skills[0].path == STYLE_PATH
    assert article.procedure.project_skills[0].required is False
    prompt = (article.procedure.root / "PROMPT.md").read_text()
    assert STYLE_PATH in prompt
    assert "voice_notes" in prompt and "not factual evidence" in prompt
