"""Every produced document tells the founder's agent where it is, how to revise it and where
the founder reviews it; no route is offered that would not reach what gets approved."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_private_workflows import fixture, mcp, structured
from test_procedure_publication import publication_db as publication_db
from test_service_billing import install

from tin_lite.document_handoff import document_handoff
from tin_lite.domain import RunStatus
from tin_lite.onboarding_experience import result_links
from tin_lite.workflow_reviews import SUPPORTED_IDS

SETTINGS = SimpleNamespace(switchboard_public_url="https://tin.test")
REVISION = "a" * 40


def run(**overrides):
    values = {
        "id": uuid4(),
        "project_id": uuid4(),
        "workflow_id": uuid4(),
        "project_workflow_id": None,
        "executor": "codex.procedure",
        "status": RunStatus.SUCCEEDED,
        "review_required": False,
        "review_decision": None,
        "artifact_path": "reports/research/brief.md",
        "canonical_commit_sha": REVISION,
    }
    return SimpleNamespace(**(values | overrides))


def test_a_report_in_files_is_revised_by_committing_the_file():
    report = run()
    [link] = result_links(SETTINGS, report)
    route = link["revise"]

    assert link["artifact_path"] == "reports/research/brief.md"
    assert link["revision"] == REVISION
    assert link["review_url"] == link["url"]
    assert link["review_url"] == (
        f"https://tin.test/document/{report.id}?project={report.project_id}"
    )
    assert link["review_pending"] is False
    assert route["direct_edit"] is True
    assert [step["tool"] for step in route["before"]] == ["list_project_files", "read_project_file"]
    assert route["tool"] == "commit_project_changes"
    assert route["arguments"]["project_id"] == str(report.project_id)
    assert route["arguments"]["changes"] == [
        {
            "operation": "upsert",
            "path": "reports/research/brief.md",
            "content": "<the complete revised text>",
        }
    ]
    # The founder's link to a revised copy is the Files reader at the commit's new revision.
    assert route["revised_url"] == (
        f"https://tin.test/file?project={report.project_id}"
        "&path=reports%2Fresearch%2Fbrief.md&revision={revision}"
    )
    assert "note" not in route


def test_a_waiting_article_is_revised_through_its_review_not_the_file():
    draft = run(
        workflow_id=next(iter(SUPPORTED_IDS)),
        status=RunStatus.NEEDS_INPUT,
        review_required=True,
        artifact_path="reports/content/next-article.md",
    )
    [link] = result_links(SETTINGS, draft)
    route = link["revise"]

    assert link["kind"] == "review" and link["review_pending"] is True
    assert route["direct_edit"] is False
    assert "would not reach them" in route["reason"]
    assert route["before"][0]["tool"] == "get_workflow_review"
    assert route["tool"] == "request_workflow_changes"
    assert set(route["arguments"]) == {"run_id", "feedback", "review_token", "request_id"}
    assert route["keeps_review"] is True and route["metered"] is True


def test_a_waiting_draft_without_a_revision_route_says_so():
    draft = run(
        executor="content.answer_page",
        status=RunStatus.NEEDS_INPUT,
        review_required=True,
        artifact_path="content/answers/2026-09-28-which-tools.md",
    )
    route = document_handoff(SETTINGS, draft)["revise"]

    assert route["direct_edit"] is False and route["tool"] is None
    assert "No revision route exists while this review is open" in route["reason"]


def test_an_approved_draft_can_be_edited_but_the_decided_copy_stays():
    approved = run(
        workflow_id=next(iter(SUPPORTED_IDS)),
        review_required=True,
        review_decision="approved",
        artifact_path="reports/content/next-article.md",
    )
    handoff = document_handoff(SETTINGS, approved)

    assert handoff["review_pending"] is False
    assert handoff["review_decision"] == "approved"
    assert handoff["revise"]["tool"] == "commit_project_changes"
    assert "stay as they were" in handoff["revise"]["note"]
    assert "content_delivery" in handoff["revise"]["note"]


def test_documents_with_their_own_revision_tool_point_to_it():
    program = uuid4()
    plan = document_handoff(SETTINGS, run(executor="content.plan", project_workflow_id=program))[
        "revise"
    ]
    assert plan["direct_edit"] is False and plan["tool"] == "edit_content_plan"
    assert plan["arguments"]["project_workflow_id"] == str(program)
    assert plan["before"][0]["tool"] == "read_content_plan"

    campaign = document_handoff(SETTINGS, run(executor="outreach.email_campaign"))["revise"]
    assert campaign["tool"] == "revise_email_campaign"

    picks = document_handoff(
        SETTINGS,
        run(executor="growth.onboarding", status=RunStatus.NEEDS_INPUT, review_required=True),
    )["revise"]
    assert picks == {
        "direct_edit": False,
        "tool": "record_onboarding_picks",
        "reason": "The plan changes through the founder's picks, not through file edits.",
    }


def test_a_file_mcp_cannot_edit_has_no_route():
    video = document_handoff(SETTINGS, run(artifact_path="creative/demo.mp4"))["revise"]
    assert video["direct_edit"] is False and video["tool"] is None
    assert "UTF-8 text files only" in video["reason"]


def test_a_run_without_a_document_has_no_handoff():
    assert result_links(SETTINGS, run(artifact_path=None)) == []
    assert result_links(SETTINGS, run(canonical_commit_sha=None)) == []


async def test_mcp_run_reads_carry_the_handoff_and_membership_still_holds(
    publication_db, monkeypatch
):
    f = await fixture(publication_db)
    workflow = await install(f, "research.deep_dive")
    revision = f.storage.repo.edit({"reports/RESEARCH_DEEP_DIVE.md": b"# Findings\n"})
    report, _ = await f.db.create_run(project_id=f.project.id, workflow_id=workflow.id)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', artifact_path=$2, canonical_commit_sha=$3 "
        "WHERE id=$1",
        report.id,
        "reports/RESEARCH_DEEP_DIVE.md",
        revision,
    )
    server = mcp(f, monkeypatch)

    viewed = structured(await server.call_tool("get_run", {"run_id": str(report.id)}))
    [link] = viewed["result_links"]
    assert link["revise"]["tool"] == "commit_project_changes"
    assert link["revise"]["arguments"]["changes"][0]["path"] == "reports/RESEARCH_DEEP_DIVE.md"
    assert link["review_url"].endswith(f"/document/{report.id}?project={f.project.id}")

    output = structured(await server.call_tool("read_run_output", {"run_id": str(report.id)}))
    assert output["content"] == "# Findings\n"
    assert output["revise"] == link["revise"]
    assert output["review_pending"] is False

    waiting, _ = await f.db.create_run(project_id=f.project.id, workflow_id=workflow.id)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='running', review_required=true WHERE id=$1", waiting.id
    )
    await f.db.request_human_review(
        run_id=waiting.id,
        canonical_commit_sha=revision,
        artifact_ref=f"code.storage://repo@{revision}/reports/RESEARCH_DEEP_DIVE.md",
        artifact_path="reports/RESEARCH_DEEP_DIVE.md",
        summary="A draft waits for review.",
    )
    held = structured(await server.call_tool("get_run", {"run_id": str(waiting.id)}))
    assert held["result_links"][0]["review_pending"] is True
    assert held["result_links"][0]["revise"]["direct_edit"] is False

    stranger = mcp(f, monkeypatch, actor="user_stranger")
    for tool in ("get_run", "read_run_output"):
        with pytest.raises(ToolError):
            await stranger.call_tool(tool, {"run_id": str(report.id)})
