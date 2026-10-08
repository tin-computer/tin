"""website.change, source feedback: the copy fix qa.feedback_to_fix planned.

The feedback run plans the wording change and opens nothing. Its plan becomes one `feedback`
row in Decisions; approving it starts website.change, which applies the plan's files as they
are under the same rules as the other planned patches: approved and unprotected merges once the
required checks pass, anything else opens a pull request for the founder.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
from test_feedback_to_fix import PAGE, patch, plan
from test_procedure_publication import publication_db as publication_db
from test_website_change import save_protected
from test_website_change_audit import ACTOR, INPUTS, approve, mergeable
from test_website_change_phase3 import apply, fixture, preview

from tin_lite import content_repository_delivery as delivery
from tin_lite import feedback_fix_plan, website_change
from tin_lite import website_change_feedback as feedback

FB_INPUTS = {"source": "feedback"}


async def feedback_run(f, text=None, *, succeeded=True):
    """A qa.feedback_to_fix run whose PLAN.md is saved in project files."""
    definition = dict(f.definitions["content.answer_page"], key=feedback_fix_plan.WORKFLOW_KEY)
    if getattr(f, "feedback_workflow", None) is None:
        f.feedback_workflow = await f.db.upsert_registry_workflow(
            workflow_id=uuid4(),
            key=feedback_fix_plan.WORKFLOW_KEY,
            title="Turn what people say about your product into a copy fix",
            description="Plans a copy fix.",
            executor=definition["executor"],
            definition_repo_id="registry/workflows",
            definition_path=f"workflows/{feedback_fix_plan.WORKFLOW_KEY}.json",
            current_commit_sha="e" * 40,
            version_label="1.0.0",
            definition=definition,
        )
    workflow = f.feedback_workflow
    run, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=workflow.id,
        started_by_clerk_user_id=ACTOR,
        definition_commit_sha="e" * 40,
        pinned_definition=definition,
    )
    revision = f.storage.repo.edit({feedback.plan_path(run.id): (text or plan(patch())).encode()})
    if succeeded:
        await f.db.pool.execute(
            "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2 WHERE id=$1",
            run.id,
            revision,
        )
    return run, revision


async def start(f, **inputs):
    from tin_lite.run_service import start_workflow_run

    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.website,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={**INPUTS, **FB_INPUTS, **inputs},
    )


async def test_without_a_feedback_run_the_source_says_so(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    found = await preview(f, "feedback")
    assert found["changes"] == [] and found["note"] == feedback.NONE_YET


async def test_the_copy_fix_waits_in_decisions_with_its_wording_and_quotes(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    plan_run, revision = await feedback_run(f, succeeded=False)
    # The run records its row as it publishes, before it is marked succeeded.
    await feedback.record(
        database=f.db,
        storage=f.storage,
        integrations=f.runtime.integrations,
        run=plan_run,
        revision=revision,
    )
    [row] = await website_change.list_changes(f.db, project_id=f.project.id, status="pending")
    expected = "fb_" + feedback_fix_plan.quotes_digest(patch())[:20]
    assert (row["change_id"], row["source"], row["kind"]) == (expected, "feedback", "copy")
    assert row["paths"] == ["/"] and row["protected"] is None
    assert row["title"] == "Copy fix: The hero says you type the book you are reading."
    detail = row["detail"]
    assert detail["edits"] == patch()["edits"] and detail["quotes"] == patch()["quotes"]
    assert (detail["people"], detail["theme_kind"]) == (2, "unclear_what_it_is")
    assert detail["plan_run_id"] == str(plan_run.id)
    # Without an approval, a start would open a pull request for the founder.
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2 WHERE id=$1",
        plan_run.id,
        revision,
    )
    found = await preview(f, "feedback")
    assert found["next_run"]["mode"] == "pull_request"
    assert any("newest copy fix is one change" in line for line in found["relay"])


async def test_approving_the_copy_fix_starts_website_change_which_publishes_it(
    publication_db, monkeypatch
):
    from test_private_workflows import app

    f = await fixture(publication_db, monkeypatch)
    await feedback_run(f)
    [row] = (await preview(f, "feedback"))["changes"]
    base = f"/api/projects/{f.project.id}/website-changes"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        decided = await client.post(
            f"{base}/{row['change_id']}/approve",
            json={"request_id": str(uuid4()), "content_sha256": row["content_sha256"]},
        )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["status"] == "approved" and body["next"]["reason"] is None
    run = await f.db.get_run(body["next"]["run_id"])
    assert run.input["source"] == "feedback"
    assert run.input["expected_repository"] == "owner/site"
    integrations = mergeable(f)
    merge = await apply(f, run)
    # No Codex session: Tin opened the PR with exactly the planned file and merged it.
    created = integrations.github_create_pull_request.await_args.kwargs
    assert [(item.path, item.content) for item in created["files"]] == [("src/app/page.tsx", PAGE)]
    assert created["title"].startswith("Copy fix: ")
    assert "2 different people said it" in created["body"]
    assert "“A reading typing gym.” → “Type the books you read.”" in created["body"]
    assert patch()["quotes"][0] in created["body"]
    integrations.github_merge_pull_request.assert_awaited_once()
    assert merge["status"] == "merged" and merge["merge_rule"] == "approved_changes"
    run = await f.db.get_run(run.id)
    assert run.status.value == "succeeded" and run.artifact_path == f"website/changes/{run.id}.md"
    # The change sits in that PR now: nothing opens it again.
    again = await preview(f, "feedback")
    assert (
        again["next_run"]["change_ids"] == []
        and "already sits in PR" in (again["next_run"]["reason"])
    )


async def test_an_approved_copy_fix_on_a_protected_page_waits_for_the_founder(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    await feedback_run(f, plan(patch(route="/pricing")))
    [row] = (await preview(f, "feedback"))["changes"]
    await approve(f, row["change_id"])
    await save_protected(f, ["/pricing"], 0)
    found = await preview(f, "feedback")
    assert found["next_run"]["mode"] == "pull_request"
    assert "/pricing, a protected page" in found["next_run"]["reason"]
    run = await start(f)
    integrations = mergeable(f)
    merge = await apply(f, run)
    integrations.github_merge_pull_request.assert_not_called()
    assert merge["status"] == "left_open"


async def test_a_newer_run_that_plans_nothing_withdraws_the_pending_fix(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    await feedback_run(f)
    assert len((await preview(f, "feedback"))["changes"]) == 1
    await feedback_run(f, plan(outcome="insufficient_data", reason="Only one person said it."))
    found = await preview(f, "feedback")
    assert found["changes"] == []
    assert found["note"].startswith("The newest feedback run planned no copy change")
    assert "Only one person said it" in found["note"]
    assert await website_change.list_changes(f.db, project_id=f.project.id) == []


async def test_a_declined_fix_is_not_proposed_again_in_new_words(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    await feedback_run(f)
    [row] = (await preview(f, "feedback"))["changes"]
    await approve(f, row["change_id"], action="decline")
    page = PAGE.replace("Type the books you read.", "Type any book.")
    reworded = patch(
        edits=[{**patch()["edits"][0], "after": "Type any book."}],
        files=[{"path": "src/app/page.tsx", "action": "update", "content": page}],
    )
    await feedback_run(f, plan(reworded))
    found = await preview(f, "feedback")
    [again] = found["changes"]
    assert again["change_id"] == row["change_id"] and again["status"] == "declined"
    assert found["next_run"]["change_ids"] == []
    assert "You declined this copy fix" in found["next_run"]["reason"]
    # New voices make it a new change.
    joined = patch(quotes=[*patch()["quotes"], "https://www.reddit.com/r/x/comments/d/y/"])
    await feedback_run(f, plan({**joined, "people": 3}))
    [fresh] = (await preview(f, "feedback"))["changes"]
    assert fresh["change_id"] != row["change_id"] and fresh["status"] == "pending"


async def test_a_copy_fix_whose_file_moved_upstream_is_left_and_says_why(
    publication_db, monkeypatch
):
    from unittest.mock import AsyncMock

    f = await fixture(publication_db, monkeypatch)
    await feedback_run(f, plan(patch(base_sha="1" * 40)))
    f.runtime.integrations.github_changed_paths = AsyncMock(
        return_value={"paths": ["src/app/page.tsx"], "complete": True}
    )
    found = await preview(f, "feedback")
    assert found["next_run"]["change_ids"] == []
    assert (
        "src/app/page.tsx changed on main since the plan read it" in (found["next_run"]["reason"])
    )
    assert feedback.RERUN in found["next_run"]["reason"]


async def test_the_approval_over_mcp_starts_website_change_too(publication_db, monkeypatch):
    from test_private_workflows import mcp, structured

    f = await fixture(publication_db, monkeypatch)
    await feedback_run(f)
    server = mcp(f, monkeypatch)
    shown = structured(
        await server.call_tool(
            "preflight_website_change",
            {"project_id": str(f.project.id), "source": "feedback", **INPUTS},
        )
    )
    [row] = shown["changes"]
    approved = structured(
        await server.call_tool(
            "approve_website_change",
            {
                "project_id": str(f.project.id),
                "change_id": row["change_id"],
                "content_sha256": row["content_sha256"],
                "request_id": str(uuid4()),
            },
        )
    )
    assert approved["change"]["next"]["run_id"]
    assert "Tin started website.change" in json.dumps(approved)
    assert delivery.applies_plan(await f.db.get_run(approved["change"]["next"]["run_id"]))
