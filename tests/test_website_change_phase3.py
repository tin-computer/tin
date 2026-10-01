"""website.change, phase 3: planned URL changes and the blog index plan.

Planned redirects and noindex changes become `planned` rows, written and merged the way the
audit's fixes are. The blog index plan is one `blog_index` row whose files Tin applies as
they are. Both follow the phase 1 and 2 rules: an approved row on no protected page publishes
once the required checks pass, anything else opens a pull request, decided rows stay decided,
and a row already in a pull request is not opened again.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_adapted_page_delivery import PR_URL
from test_planned_url_changes import architecture, efficacy
from test_procedure_publication import publication_db as publication_db
from test_technical_site_fixes import BASE, PAGE, served_page
from test_website_change import save_protected
from test_website_change_audit import (
    INPUTS,
    approve,
    audit_run,
    made,
    merge_outcome,
    mergeable,
    start,
)
from test_website_change_audit import fixture as audit_fixture

from tin_lite import content_repository_delivery as delivery
from tin_lite import planned_url_changes as planned
from tin_lite import (
    website_change,
    website_change_audit,
    website_change_blog_index,
    website_change_planned,
)
from tin_lite.organic_audit import canonical_json
from tin_lite.workflow_inputs import WorkflowInputError

TODAY = date(2026, 10, 1)
REDIRECT = planned.finding_id(
    {
        "source": planned.EFFICACY_SOURCE,
        "kind": "redirect",
        "from": "/compare/x-alternatives",
        "to": "/alternatives/x",
    }
)
NOINDEX = planned.finding_id(
    {"source": planned.EFFICACY_SOURCE, "kind": "noindex", "from": "/sign-in"}
)
VERCEL = {
    "path": "vercel.json",
    "content": json.dumps(
        {
            "redirects": [
                {
                    "source": "/compare/x-alternatives",
                    "destination": "/alternatives/x",
                    "permanent": True,
                }
            ]
        }
    ),
}


async def fixture(db, monkeypatch, **kwargs):
    f = await audit_fixture(db, monkeypatch, **kwargs)
    monkeypatch.setattr(website_change_planned, "current_date", lambda: TODAY)
    await audit_run(f, [], revision="2" * 40)
    return f


def plan_files(f, *, efficacy_text=None, architecture_text=None):
    """The planner files as content_efficacy and site_architecture save them."""
    f.storage.repo.edit(
        {
            path: text.encode()
            for path, text in (
                (planned.EFFICACY_PATH, efficacy_text),
                (planned.ARCHITECTURE_PATH, architecture_text),
            )
            if text is not None
        }
    )


async def preview(f, source, **inputs):
    return await website_change_audit.preview(
        database=f.db,
        storage=f.storage,
        integrations=f.runtime.integrations,
        project_id=f.project.id,
        source=source,
        inputs={**INPUTS, **inputs},
    )


# --- Planned URL changes ----------------------------------------------------------------


async def test_planned_changes_become_rows_and_deletions_stay_with_the_founder(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    # Before any planner ran there is nothing to plan, and that is said plainly.
    empty = await preview(f, "planned")
    assert empty["changes"] == [] and "nothing is planned" in empty["note"]
    with pytest.raises(WorkflowInputError, match="Nothing in the planned URL changes"):
        await start(f, source="planned")
    plan_files(f, efficacy_text=efficacy(generated="2026-09-30"))
    found = await preview(f, "planned")
    rows = {change["change_id"]: change for change in found["changes"]}
    assert set(rows) == {REDIRECT, NOINDEX}
    assert (rows[REDIRECT]["source"], rows[REDIRECT]["kind"], rows[REDIRECT]["paths"]) == (
        "planned",
        "redirect",
        ["/compare/x-alternatives", "/alternatives/x"],
    )
    # /sign-in is protected: the change is still a row, but Tin asks and opens a PR.
    assert rows[NOINDEX]["protected"] == "/sign-in" and rows[NOINDEX]["suggestion"] == "ask"
    assert rows[REDIRECT]["suggestion"] == "apply"
    # Deleting a page is listed for the founder, never planned.
    [deletion] = found["plan"]["left_out"]["manual"]
    assert deletion["check_id"] == "planned.delete" and "/blog/dead" in deletion["issue"]
    assert "stays with you" in deletion["reason"]
    stored = await website_change.list_changes(f.db, project_id=f.project.id)
    assert {row["source"] for row in stored} == {"planned"}
    # A change the plan dropped is retired while still pending; decided ones stay.
    await approve(f, REDIRECT)
    plan_files(
        f,
        efficacy_text=efficacy(
            generated="2026-09-30",
            changes=[{"from": "/blog/a", "to": "/blog/b", "kind": "301", "reason": "merge"}],
        ),
    )
    later = await preview(f, "planned")
    assert {
        c["change_id"] for c in await website_change.list_changes(f.db, project_id=f.project.id)
    } == {
        REDIRECT,
        *[c["change_id"] for c in later["changes"]],
    }


async def test_an_approved_planned_redirect_publishes_and_is_checked_live(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    plan_files(f, architecture_text=architecture(generated="2026-09-20"))
    first = await preview(f, "planned")
    move = next(c for c in first["changes"] if c["paths"][0] == "/compare/x-alternatives")
    await approve(f, move["change_id"])
    found = await preview(f, "planned")
    assert found["next_run"]["mode"] == "direct"
    assert found["next_run"]["change_ids"] == [move["change_id"]]
    run = await start(f, source="planned")
    source = await delivery.saved_source(f.db, run.id)
    assert source["source"] == "planned" and source["planned"]["plan_files"] == [
        planned.ARCHITECTURE_PATH
    ]
    run, _ = await made(f, run, files=(VERCEL,))
    integrations = mergeable(f, "unstable")
    integrations.github_pull_request_state.return_value = {
        "state": "closed",
        "merged": True,
        "merged_at": "2026-10-01T12:00:00+00:00",
    }
    old, new = f"{BASE}/compare/x-alternatives", f"{BASE}/alternatives/x"

    async def fetch(url, *, host):
        read = served_page(new if url == old else url, PAGE)
        return {**read, "redirects": [old]} if url == old else read

    f.live.fetch = fetch
    merge = await merge_outcome(f, run)
    assert merge["status"] == "merged" and merge["checks_rule"] == "required_checks"
    live = await f.live.view(run)
    assert [row["state"] for row in live["findings"]] == ["fixed"]
    # Merged rows are not made again.
    again = await preview(f, "planned")
    assert move["change_id"] not in again["next_run"]["change_ids"]


async def test_unapproved_and_protected_planned_rows_open_a_pull_request(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    plan_files(f, efficacy_text=efficacy(generated="2026-09-30"))
    await preview(f, "planned")
    # The founder approves the /sign-in noindex; it is protected, so it still waits.
    await approve(f, NOINDEX)
    found = await preview(f, "planned")
    assert found["next_run"]["mode"] == "pull_request"
    assert found["next_run"]["reason"] == website_change_audit.UNAPPROVED_REASON
    run = await start(f, source="planned")
    run, _ = await made(f, run, files=(VERCEL,))
    integrations = mergeable(f)
    merge = await merge_outcome(f, run)
    integrations.github_merge_pull_request.assert_not_called()
    assert merge["status"] == "left_open"
    # Both rows sit in that open PR now: no second PR.
    with pytest.raises(WorkflowInputError, match=f"pull request: {PR_URL}"):
        await start(f, source="planned")


async def test_a_declined_planned_change_never_returns(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    plan_files(f, efficacy_text=efficacy(generated="2026-09-30"))
    await preview(f, "planned")
    await approve(f, REDIRECT, action="decline")
    found = await preview(f, "planned")
    assert REDIRECT not in {c["change_id"] for c in found["changes"]}
    assert found["plan"]["left_out"]["declined"][0]["id"] == REDIRECT
    stored = await website_change.get_change(f.db, project_id=f.project.id, change_id=REDIRECT)
    assert stored["status"] == "declined"


# --- The blog index plan -----------------------------------------------------------------

FILES = [
    {"path": "src/app/blog/page.tsx", "action": "create", "content": "export default 1;\n"},
    {"path": "src/lib/posts.ts", "action": "update", "content": "export const posts = [];\n"},
]


def plan_text(files=FILES, *, base_sha=None, route="/blog", **extra):
    patch = {
        "schema": "blog-index-patch/1",
        "repository": "owner/site",
        "base_ref": "main",
        "base_sha": base_sha or "9" * 40,
        "route": route,
        "summary": "A blog index that lists every post with its date.",
        "files": files,
        "caps": {"max_files": 5},
        **extra,
    }
    return (
        "# Blog index plan\n\nWhat changes and why.\n\n<!-- blog-index-patch.json:start -->\n"
        f"```json\n{json.dumps(patch)}\n```\n<!-- blog-index-patch.json:end -->\n"
    )


async def blog_index_run(f, text=None):
    """A succeeded content.blog_index run whose PLAN.md is saved in project files."""
    definition = dict(f.definitions["content.answer_page"], key="content.blog_index")
    workflow = await f.db.upsert_registry_workflow(
        workflow_id=uuid4(),
        key="content.blog_index",
        title="Build or fix the blog index",
        description="Plans the blog index.",
        executor=definition["executor"],
        definition_repo_id="registry/workflows",
        definition_path="workflows/content.blog_index.json",
        current_commit_sha="d" * 40,
        version_label="2.0.0",
        definition=definition,
    )
    run, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=workflow.id,
        started_by_clerk_user_id="user_privateauthor",
        definition_commit_sha="d" * 40,
        pinned_definition=definition,
    )
    revision = f.storage.repo.edit(
        {website_change_blog_index.plan_path(run.id): (text or plan_text()).encode()}
    )
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2 WHERE id=$1",
        run.id,
        revision,
    )
    return run


BI_INPUTS = {"source": "blog_index"}


async def apply(f, run):
    assert await f.activities.prepare_codex_procedure(str(run.id)) is True
    receipt = await f.db.get_effect(delivery.merge_key(run.id))
    return receipt.result


async def test_without_a_blog_index_run_the_source_says_so(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    found = await preview(f, "blog_index")
    assert found["changes"] == [] and found["note"] == website_change_blog_index.NONE_YET
    with pytest.raises(WorkflowInputError, match="No blog index plan yet"):
        await start(f, **BI_INPUTS)


async def test_the_blog_index_plan_is_one_row_applied_as_it_is(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    plan_run = await blog_index_run(f)
    found = await preview(f, "blog_index")
    [row] = found["changes"]
    files_json = canonical_json(FILES)
    assert row["change_id"] == "bi_" + hashlib.sha256(files_json).hexdigest()[:20]
    assert row["content_sha256"] == hashlib.sha256(files_json).hexdigest()
    assert (row["source"], row["kind"], row["paths"]) == ("blog_index", "index", ["/blog"])
    assert row["detail"]["plan_run_id"] == str(plan_run.id)
    assert found["next_run"]["mode"] == "pull_request"
    run = await start(f, **BI_INPUTS)
    integrations = mergeable(f)
    merge = await apply(f, run)
    # No Codex session: Tin opened the PR with exactly the plan's files.
    created = integrations.github_create_pull_request.await_args.kwargs
    assert [(item.path, item.content) for item in created["files"]] == [
        (item["path"], item["content"]) for item in FILES
    ]
    assert created["expected_base_sha"] == f.binding.head_sha
    integrations.github_merge_pull_request.assert_not_called()
    assert merge["status"] == "left_open"
    assert merge["reason"] == website_change_audit.UNAPPROVED_REASON
    run = await f.db.get_run(run.id)
    assert run.status.value == "succeeded" and run.artifact_path == f"website/changes/{run.id}.md"
    # The row sits in that open PR: a second start opens nothing.
    again = await preview(f, "blog_index")
    assert again["next_run"]["change_ids"] == [] and PR_URL in again["next_run"]["reason"]


async def test_an_approved_blog_index_publishes_once_required_checks_pass(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    await blog_index_run(f)
    [row] = (await preview(f, "blog_index"))["changes"]
    await approve(f, row["change_id"])
    found = await preview(f, "blog_index")
    assert found["next_run"]["mode"] == "direct"
    run = await start(f, **BI_INPUTS)
    integrations = mergeable(f, "unstable")
    merge = await apply(f, run)
    integrations.github_merge_pull_request.assert_awaited_once()
    merged = integrations.github_merge_pull_request.await_args.kwargs
    assert merged["new_paths"] == ("src/app/blog/page.tsx",)
    assert merge["status"] == "merged" and merge["merge_rule"] == "approved_changes"
    assert merge["checks_rule"] == "required_checks" and merge["mergeable_state"] == "unstable"


async def test_a_stale_blog_index_plan_is_left_and_says_why(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    await blog_index_run(f, plan_text(base_sha="1" * 40))
    integrations = f.runtime.integrations
    integrations.github_changed_paths = AsyncMock(
        return_value={"paths": ["src/lib/posts.ts", "README.md"], "complete": True}
    )
    found = await preview(f, "blog_index")
    assert found["next_run"]["change_ids"] == []
    assert "src/lib/posts.ts changed on main since the plan read it" in found["next_run"]["reason"]
    assert found["changes"][0]["status"] == "pending"
    with pytest.raises(WorkflowInputError, match="changed on main since the plan read it"):
        await start(f, **BI_INPUTS)
    # When only other files moved upstream, the plan still applies.
    integrations.github_changed_paths.return_value = {"paths": ["README.md"], "complete": True}
    assert (await preview(f, "blog_index"))["next_run"]["mode"] == "pull_request"


@pytest.mark.parametrize(
    ("files", "match"),
    [
        ([{**FILES[0], "path": f"src/p{i}.ts"} for i in range(6)], "one to 5 files"),
        ([{"path": "package.json", "action": "update", "content": "{}"}], "dependencies"),
        ([{"path": "pnpm-lock.yaml", "action": "update", "content": "x"}], "dependencies"),
        ([{"path": ".github/workflows/x.yml", "action": "create", "content": "x"}], "CI"),
        ([{"path": "../etc/passwd", "action": "create", "content": "x"}], "not a repository"),
    ],
)
def test_a_blog_index_plan_never_exceeds_its_caps(files, match):
    with pytest.raises(ValueError, match=match):
        website_change_blog_index.parse_plan(plan_text(files))
    with pytest.raises(ValueError, match="blog-index-patch.json block"):
        website_change_blog_index.parse_plan("# A plan without a block\n")


async def test_a_protected_blog_index_route_opens_a_pull_request(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    await blog_index_run(f)
    [row] = (await preview(f, "blog_index"))["changes"]
    await approve(f, row["change_id"])
    await save_protected(f, ["/blog"], 0)
    found = await preview(f, "blog_index")
    assert found["next_run"]["mode"] == "pull_request"
    assert "/blog, a protected page" in found["next_run"]["reason"]


# --- What the Decisions page reads -------------------------------------------------------


async def test_decisions_read_pending_rows_with_protection_and_open_judgment_calls(
    publication_db, monkeypatch
):
    import httpx
    from test_private_workflows import app, mcp, structured
    from test_technical_batch import row as finding
    from test_technical_site_fixes import ROBOTS

    f = await fixture(publication_db, monkeypatch)
    f.site[f"{BASE}/robots.txt"] = "User-agent: OAI-SearchBot\nDisallow: /\n\n" + ROBOTS
    ai_search = "oa_" + "e" * 20
    await audit_run(f, [finding("robots.ai_search_crawlers_blocked", ai_search)], revision="3" * 40)
    plan_files(f, efficacy_text=efficacy(generated="2026-09-30"))
    # The coding agent previews over MCP; the founder reads Decisions over HTTP.
    server = mcp(f, monkeypatch)
    by_mcp = structured(
        await server.call_tool(
            "preflight_website_change",
            {"project_id": str(f.project.id), "source": "planned", **INPUTS},
        )
    )
    assert {c["change_id"] for c in by_mcp["changes"]} == {REDIRECT, NOINDEX}
    assert any("planned URL changes hold 2 changes" in line for line in by_mcp["relay"])
    structured(
        await server.call_tool(
            "preflight_website_change", {"project_id": str(f.project.id), **INPUTS}
        )
    )
    base = f"/api/projects/{f.project.id}/website-changes"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        pending = (await client.get(base, params={"status": "pending"})).json()
        protected = {row["change_id"]: row["protected"] for row in pending}
        assert protected == {REDIRECT: None, NOINDEX: "/sign-in"}
        [call] = (await client.get(f"{base}/questions")).json()["questions"]
        assert call["id"] == ai_search and call["source"] == "audit"
        assert call["suggestion"] == "allow" and len(call["options"]) == 2
        shown = await client.post(f"{base}/preflight", json={"source": "blog_index", **INPUTS})
        assert (
            shown.status_code == 200 and shown.json()["note"] == website_change_blog_index.NONE_YET
        )
    # Once the coding agent answers, the question leaves Decisions.
    await preview(f, "audit", decisions=[f"{ai_search}=allow"])
    assert await website_change_audit.judgment_calls(f.db, f.project.id) == []
