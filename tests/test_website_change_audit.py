"""website.change, phase 2: the audit finds; website.change plans, fixes and publishes.

Each fixable finding of the latest audit is one change row. Rows the founder approved, on no
protected page, publish once the required checks pass; the rest open a pull request the
founder merges. A declined finding never comes back, and a row already in an open pull
request is not put in a second one. organic.technical_fix stays registered, hidden, for its
pinned site-fix-v5 runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from test_adapted_page_delivery import ANSWER_PAGE_WORKFLOW_ID, PR_URL, clean
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db
from test_technical_batch import row
from test_technical_site_fixes import (
    BASE,
    HOST,
    NEXT_PACKAGE,
    PAGE,
    ROBOTS,
    SITEMAP,
    archive,
    served,
    served_page,
)
from test_website_change import fixture as website_fixture
from test_website_change import save_protected

from tin_lite import content_repository_delivery as delivery
from tin_lite import technical_batch as batch_rules
from tin_lite import technical_fix, website_change, website_change_audit
from tin_lite import technical_repair_plan as repair_plan
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.integrations import GitHubPullRequestResult
from tin_lite.organic_audit import canonical_json
from tin_lite.procedures import procedure_checkpoint_path
from tin_lite.run_service import start_workflow_run
from tin_lite.technical_fix_execution import TechnicalFixExecution
from tin_lite.technical_fix_live import LiveRecheck
from tin_lite.workflow_inputs import WorkflowInputError

SITEMAP_LINE = "oa_" + "b" * 20
LANG = "oa_" + "c" * 20
OLD = "oa_" + "a" * 20
SIGN_IN = "oa_" + "d" * 20
AI_SEARCH = "oa_" + "e" * 20
LANDING = "oa_" + "f" * 20
ABOUT = f"{BASE}/about"
SITEMAP_URL = f"{BASE}/sitemap.xml"


# --- Fixtures --------------------------------------------------------------------------------


async def audit_run(f, findings, *, revision):
    """A succeeded organic audit run; its findings are what TechnicalFixSources reads."""
    run, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=ANSWER_PAGE_WORKFLOW_ID,
        started_by_clerk_user_id=ACTOR,
        definition_commit_sha="d" * 40,
        pinned_definition=f.definitions["content.answer_page"],
    )
    await f.db.pool.execute(
        "UPDATE workflow_runs SET executor='organic.audit', status='succeeded', "
        "canonical_commit_sha=$2, input=$3::jsonb, created_at=now() + ($4 || ' seconds')::interval "
        "WHERE id=$1",
        run.id,
        revision,
        json.dumps({"site_url": f"{BASE}/", "market": "US"}),
        str(len(f.audits)),
    )
    f.audits[str(run.id)] = {
        "source": {"audit_run_id": str(run.id), "audit_revision": revision},
        "target": {"url": f"{BASE}/", "host": HOST, "site_hosts": [HOST]},
        "crawl_status": "completed",
        "findings": findings,
        "excluded_findings": [],
    }
    return run


async def fixture(db, monkeypatch, *, site=None, repo=None):
    f = await website_fixture(db, monkeypatch)
    f.audits = {}

    async def inspect(self, *, project_id, audit_run_id):
        return json.loads(json.dumps(f.audits[str(audit_run_id)]))

    monkeypatch.setattr(
        "tin_lite.technical_fix_sources.TechnicalFixSources.inspect", inspect, raising=True
    )
    f.site = site or {
        f"{BASE}/robots.txt": ROBOTS,
        SITEMAP_URL: SITEMAP,
        ABOUT: PAGE,
        f"{BASE}/sign-in": PAGE,
        f"{BASE}/lp/spring": PAGE,
    }

    async def fetch_file(url, *, host, kind):
        if url not in f.site:
            raise ValueError("not served")
        return served(url, f.site[url])

    async def fetch(url, *, host):
        return served_page(url, f.site[url])

    f.fetch, f.fetch_file = AsyncMock(side_effect=fetch), AsyncMock(side_effect=fetch_file)
    integrations = f.runtime.integrations
    integrations.github_repository_bundle = AsyncMock(
        return_value=SimpleNamespace(
            archive=archive(
                repo
                or {
                    "public/robots.txt": ROBOTS,
                    "app/layout.tsx": "export default function Layout() {}\n",
                    "package.json": NEXT_PACKAGE,
                }
            ),
            complete=True,
        )
    )
    integrations.github_open_pull_requests = AsyncMock(
        return_value=SimpleNamespace(truncated=False, changed_paths=(), document=b"{}")
    )
    integrations.github_pull_request_state = AsyncMock(
        return_value={"state": "open", "merged": False, "merged_at": None}
    )

    async def create_pull_request(**kwargs):
        # The receipt GitHub delivery records, which the live check reads after merge.
        result = GitHubPullRequestResult("owner/site", "tin/test", 42, PR_URL)
        await db.record_integration_call(
            execution_key=kwargs["execution_key"],
            project_id=kwargs["project_id"],
            run_id=kwargs["run_id"],
            connection_id=f.binding.connection_id,
            provider_key="infra.github",
            capability="pull_requests.write",
            request_fingerprint="f" * 64,
            status="completed",
            response_summary=asdict(result),
        )
        return result

    integrations.github_create_pull_request = AsyncMock(side_effect=create_pull_request)
    f.execution = TechnicalFixExecution(
        database=db,
        storage=f.storage,
        integrations=integrations,
        fetch=f.fetch,
        fetch_file=f.fetch_file,
    )
    monkeypatch.setattr(
        "tin_lite.technical_fix_execution.TechnicalFixExecution", lambda **kwargs: f.execution
    )
    f.live = LiveRecheck(
        database=db, integrations=integrations, fetch=f.fetch, fetch_file=f.fetch_file
    )
    monkeypatch.setattr(website_change_audit, "live_recheck", lambda *args: f.live)
    return f


def findings():
    return [
        row("robots.sitemap_reference_missing", SITEMAP_LINE, priority="high_impact"),
        row("onpage.lang_missing", LANG, urls=[ABOUT]),
    ]


INPUTS = {"expected_repository": "owner/site", "repository_serves_site": True}


async def preflight(f, **inputs):
    return await website_change_audit.plan_changes(
        database=f.db,
        storage=f.storage,
        integrations=f.runtime.integrations,
        project_id=f.project.id,
        inputs={**INPUTS, **inputs},
    )


async def start(f, **inputs):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.website,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={"source": "audit", **INPUTS, **inputs},
    )


async def approve(f, change_id, action="approve"):
    stored = await website_change.get_change(f.db, project_id=f.project.id, change_id=change_id)
    return await website_change.decide(
        f.db,
        project_id=f.project.id,
        change_id=change_id,
        action=action,
        actor=ACTOR,
        request_id=uuid4(),
        content_sha256=stored["content_sha256"],
    )


ROBOTS_FIX = {"path": "public/robots.txt", "content": ROBOTS + f"Sitemap: {SITEMAP_URL}\n"}
LAYOUT_FIX = {
    "path": "app/layout.tsx",
    "content": "export default function Layout() { return <html lang='en' /> }\n",
}


async def made(f, run, files=(ROBOTS_FIX,), body=None):
    """Run the change the way the procedure does, up to the opened pull request."""
    assert await f.activities.prepare_codex_procedure(str(run.id)) is False
    await f.activities.create_codex_procedure_sandbox(str(run.id))
    run = await f.db.get_run(run.id)
    source = await delivery.saved_source(f.db, run.id)
    manifest = {
        "repository": "owner/site",
        "default_branch": "main",
        "head_sha": f.binding.head_sha,
        "title": "Fix what the audit found",
        "body": body or f"Fixes the audit's findings. {batch_rules.FRAMEWORK_NOTE}",
        "files": list(files),
        "outcome": "patch",
        "reason": "",
        "verification": [delivery.CHECK_COMMAND],
    }
    path = procedure_checkpoint_path(run.id)
    head = f.storage.repo.head
    revision = f.storage.repo.edit({path: canonical_json(manifest)}, parent=run.expected_head_sha)
    f.storage.branch_revision, f.storage.repo.head = revision, head
    key = f"{run.id}:procedure_artifact_persist"
    async with f.db.effect_lock(key, "procedure_artifact_persist") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="procedure_artifact_persist")
        await f.db.complete_procedure_persist(
            conn,
            execution_key=key,
            run_id=run.id,
            result={"ephemeral_commit_sha": revision, "checkpoint_path": path},
            sandbox_killed=True,
        )
    await f.activities.commit_codex_procedure_artifact(str(run.id))
    await f.db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", run.id)
    return await f.db.get_run(run.id), source


def mergeable(f, state="clean"):
    integrations = f.runtime.integrations
    integrations.github_pull_request_merge_state.return_value = clean(mergeable_state=state)
    integrations.github_merge_pull_request.return_value = {
        "merged": True,
        "commit": "b" * 40,
        "url": f"https://github.com/owner/site/commit/{'b' * 40}",
    }
    return integrations


async def merge_outcome(f, run):
    await f.activities.deliver_content_draft(str(run.id))
    receipt = await f.db.get_effect(delivery.merge_key(run.id))
    return receipt.result if receipt else None


# --- Rows from the latest audit ----------------------------------------------------------


async def test_a_technical_run_records_rows_from_the_latest_audit(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    await audit_run(f, [row("onpage.viewport_missing", OLD, urls=[ABOUT])], revision="1" * 40)
    latest = await audit_run(f, findings(), revision="2" * 40)
    preview = await preflight(f)
    assert preview["source"]["audit_run_id"] == str(latest.id)
    rows = {change["change_id"]: change for change in preview["changes"]}
    # One row per fixable finding of the latest audit; the older audit's finding is not one.
    assert set(rows) == {SITEMAP_LINE, LANG}
    sitemap = rows[SITEMAP_LINE]
    assert (sitemap["source"], sitemap["kind"], sitemap["paths"]) == (
        "audit",
        "robots_sitemap_line",
        ["/robots.txt"],
    )
    assert rows[LANG]["paths"] == ["/about"] and rows[LANG]["status"] == "pending"
    assert sitemap["detail"]["audit_run_id"] == str(latest.id)
    stored = await website_change.list_changes(f.db, project_id=f.project.id)
    assert {item["change_id"] for item in stored} == {SITEMAP_LINE, LANG}
    # Nobody approved them: the next run opens one pull request for both.
    assert preview["next_run"] == {
        "mode": "pull_request",
        "reason": website_change_audit.UNAPPROVED_REASON,
        "change_ids": [SITEMAP_LINE, LANG],
    }
    assert preview["repository_binding"]["repository"] == "owner/site"
    # Without the member's confirmation that the repository serves the site, nothing is
    # recorded and nothing binds.
    from tin_lite.technical_fix_sources import TechnicalFixError

    await f.db.pool.execute("DELETE FROM website_changes WHERE project_id=$1", f.project.id)
    with pytest.raises(TechnicalFixError, match="serves the audited site"):
        await preflight(f, repository_serves_site=False)
    assert await website_change.list_changes(f.db, project_id=f.project.id) == []
    await preflight(f)
    # A second preview records nothing new.
    await preflight(f)
    assert len(await website_change.list_changes(f.db, project_id=f.project.id)) == 2
    run = await start(f)
    source = await delivery.saved_source(f.db, run.id)
    assert source["source"] == "audit" and source["audit"]["audit_run_id"] == str(latest.id)
    assert [change["change_id"] for change in source["changes"]] == [SITEMAP_LINE, LANG]
    assert source["publish"]["mode"] == "pull_request"
    assert not delivery.adapts(run) and delivery.repairs_site(run)
    # A second technical run waits for this one.
    with pytest.raises(WorkflowInputError, match="still working"):
        await start(f)
    # The same preview over HTTP and MCP.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        shown = await client.post(
            f"/api/projects/{f.project.id}/website-changes/preflight", json=INPUTS
        )
        assert shown.status_code == 200 and shown.json()["source"]["audit_run_id"] == str(latest.id)
    by_mcp = structured(
        await mcp(f, monkeypatch).call_tool(
            "preflight_website_change", {"project_id": str(f.project.id), **INPUTS}
        )
    )
    assert {change["change_id"] for change in by_mcp["changes"]} == {SITEMAP_LINE, LANG}
    assert any("approve or decline once" in item for item in by_mcp["relay"])


async def test_judgment_calls_wait_for_the_coding_agent_and_an_approved_answer_is_reused(
    publication_db, monkeypatch
):
    f = await fixture(
        publication_db,
        monkeypatch,
        site={f"{BASE}/robots.txt": "User-agent: OAI-SearchBot\nDisallow: /\n\n" + ROBOTS},
    )
    await audit_run(f, [row("robots.ai_search_crawlers_blocked", AI_SEARCH)], revision="2" * 40)
    preview = await preflight(f)
    [decision] = preview["decisions_needed"]
    assert decision["id"] == AI_SEARCH and preview["ask"] == repair_plan.ASK
    assert preview["changes"] == [] and preview["next_run"]["change_ids"] == []
    with pytest.raises(WorkflowInputError, match="judgment calls"):
        await start(f)
    answered = await preflight(f, decisions=[f"{AI_SEARCH}=allow"])
    [change] = answered["changes"]
    assert change["detail"]["decision"] == "allow"
    await approve(f, AI_SEARCH)
    # The approved row keeps its answer: a later run needs no decisions to publish it.
    again = await preflight(f)
    assert again["decisions_needed"] == []
    assert again["next_run"]["mode"] == "direct"
    assert again["plan"]["repairs"][0]["decision"] == "allow"
    # An explicit answer this run wins over the remembered one: keeping the crawlers blocked
    # leaves the site as it is, so there is nothing to make.
    other = await preflight(f, decisions=[f"{AI_SEARCH}=keep_blocked"])
    assert other["next_run"]["change_ids"] == []
    assert other["plan"]["left_out"]["decided_keep"][0]["id"] == AI_SEARCH


# --- Two modes, as in phase 1 -------------------------------------------------------------


async def test_approved_technical_rows_publish_once_the_required_checks_pass(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    await audit_run(f, findings(), revision="2" * 40)
    await preflight(f)
    approved = await approve(f, SITEMAP_LINE)
    preview = await preflight(f)
    assert preview["next_run"] == {
        "mode": "direct",
        "reason": website_change_audit.DIRECT_REASON,
        "change_ids": [SITEMAP_LINE],
    }
    # The unapproved row waits for the next run's pull request.
    assert [item["id"] for item in preview["plan"]["left_out"]["waiting"]] == [LANG]
    run = await start(f)
    source = await delivery.saved_source(f.db, run.id)
    [change] = source["changes"]
    assert change["approval"]["by"] == ACTOR and change["approval"]["via"] == "decision"
    assert change["approval"]["at"] == approved["decided_at"]
    run, _ = await made(f, run)
    integrations = f.runtime.integrations
    created = integrations.github_create_pull_request.await_args.kwargs
    assert [item.path for item in created["files"]] == ["public/robots.txt"]
    repo = f.storage.repo
    report = repo.trees[repo.head][f"website/changes/{run.id}.md"][1].decode()
    assert "## Website changes" in report and f"`{SITEMAP_LINE}`" in report
    # A check the repository does not require failed: Tin merges anyway.
    mergeable(f, "unstable")
    f.site[f"{BASE}/robots.txt"] = ROBOTS + f"Sitemap: {SITEMAP_URL}\n"
    integrations.github_pull_request_state.return_value = {
        "state": "closed",
        "merged": True,
        "merged_at": "2026-10-01T12:00:00+00:00",
    }
    merge = await merge_outcome(f, run)
    integrations.github_merge_pull_request.assert_awaited_once()
    assert merge["status"] == "merged" and merge["merge_rule"] == "approved_changes"
    assert merge["mergeable_state"] == "unstable" and merge["merged_by"] == "tin"
    assert merge["checks_rule"] == "required_checks"
    assert integrations.github_merge_pull_request.await_args.kwargs["new_paths"] == ()
    # The live check ran right after the merge: merged, and the problem is gone.
    live = await f.live.view(run)
    assert live["state"] == "fixed"
    assert live["findings"] == [
        {
            "finding_id": SITEMAP_LINE,
            "check_id": "robots.sitemap_reference_missing",
            "state": "fixed",
            "message": "fixed on the live site",
        }
    ]
    # MCP get_run shows the same live check for the run.
    monkeypatch.setattr("tin_lite.mcp_server.live_service", lambda runtime: f.live)
    shown = structured(await mcp(f, monkeypatch).call_tool("get_run", {"run_id": str(run.id)}))
    assert shown["live_check"]["state"] == "fixed" and shown["status"] == "succeeded"
    events = await f.db.pool.fetch(
        "SELECT event_type FROM activity_events WHERE run_id=$1 AND event_type LIKE 'website%'",
        run.id,
    )
    assert [event["event_type"] for event in events] == ["website_change_merged"]
    # Merged rows are not made again; the waiting row is the next run's pull request.
    nxt = await preflight(f)
    assert nxt["next_run"]["change_ids"] == [LANG]
    assert nxt["plan"]["left_out"]["in_pull_request"][0]["pull_request"]["state"] == "merged"


async def test_unapproved_rows_open_a_pull_request_and_are_not_opened_again(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    await audit_run(f, findings(), revision="2" * 40)
    run = await start(f)
    run, source = await made(f, run, files=(ROBOTS_FIX, LAYOUT_FIX))
    integrations = mergeable(f)
    merge = await merge_outcome(f, run)
    integrations.github_pull_request_merge_state.assert_not_called()
    integrations.github_merge_pull_request.assert_not_called()
    assert merge == {
        "pull_request": PR_URL,
        "number": 42,
        "status": "left_open",
        "reason": website_change_audit.UNAPPROVED_REASON,
    }
    # Next week the same rows still sit in that open pull request: no second one.
    preview = await preflight(f)
    assert preview["next_run"]["change_ids"] == []
    assert {
        item["pull_request"]["url"] for item in preview["plan"]["left_out"]["in_pull_request"]
    } == {PR_URL}
    with pytest.raises(
        WorkflowInputError, match=f"already sits in a website.change pull request: {PR_URL}"
    ):
        await start(f)
    # Once the founder closes it unmerged, the rows can go in a new pull request.
    integrations.github_pull_request_state.return_value = {
        "state": "closed",
        "merged": False,
        "merged_at": None,
    }
    assert (await preflight(f))["next_run"]["change_ids"] == [SITEMAP_LINE, LANG]


async def test_protected_rows_always_open_a_pull_request(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    await audit_run(
        f,
        [
            row("indexation.utility_pages_indexable", SIGN_IN, urls=[f"{BASE}/sign-in"]),
            row("indexation.ad_landing_pages_indexable", LANDING, urls=[f"{BASE}/lp/spring"]),
        ],
        revision="2" * 40,
    )
    await preflight(f)
    await approve(f, SIGN_IN)
    await approve(f, LANDING)
    # /sign-in is a default; the founder protects /lp in the project's settings.
    await save_protected(f, ["/lp"], 0)
    preview = await preflight(f)
    rows = {change["change_id"]: change for change in preview["changes"]}
    assert rows[SIGN_IN]["approved"] and rows[SIGN_IN]["protected"] == "/sign-in"
    assert rows[LANDING]["approved"] and rows[LANDING]["protected"] == "/lp"
    assert preview["next_run"]["mode"] == "pull_request"
    assert "a protected page" in preview["next_run"]["reason"]
    run = await start(f)
    page_fix = {"path": "app/sign-in/page.tsx", "content": "export const robots = 'noindex';\n"}
    run, _ = await made(f, run, files=(page_fix,))
    integrations = mergeable(f)
    merge = await merge_outcome(f, run)
    integrations.github_merge_pull_request.assert_not_called()
    assert merge["status"] == "left_open" and "protected page" in merge["reason"]


async def test_an_approved_patch_that_touches_a_protected_file_waits(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    await audit_run(f, findings(), revision="2" * 40)
    await preflight(f)
    await approve(f, LANG)
    run = await start(f)
    assert (await delivery.saved_source(f.db, run.id))["publish"]["mode"] == "direct"
    sign_in = {"path": "src/app/(auth)/sign-in/page.tsx", "content": "export default 1;\n"}
    run, _ = await made(f, run, files=(LAYOUT_FIX, sign_in))
    integrations = mergeable(f)
    merge = await merge_outcome(f, run)
    integrations.github_merge_pull_request.assert_not_called()
    assert merge["reason"] == "It touches /sign-in, a protected page, so it waits for your review."


@pytest.mark.parametrize("verdict", ["blocked", "dirty"])
async def test_an_approved_technical_pull_request_never_merges_blocked_or_dirty(
    publication_db, monkeypatch, verdict
):
    f = await fixture(publication_db, monkeypatch)
    monkeypatch.setattr(delivery, "MERGE_WAIT_SECONDS", 0)
    await audit_run(f, findings(), revision="2" * 40)
    await preflight(f)
    await approve(f, SITEMAP_LINE)
    run, _ = await made(f, await start(f))
    integrations = mergeable(f, verdict)
    integrations.github_pull_request_merge_state.return_value = clean(
        mergeable=verdict != "dirty", mergeable_state=verdict
    )
    merge = await merge_outcome(f, run)
    integrations.github_merge_pull_request.assert_not_called()
    assert merge["status"] == "left_open" and "mergeable_state" not in merge


# --- Decided rows stay decided --------------------------------------------------------------


async def test_declined_rows_never_return(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    await audit_run(f, findings(), revision="2" * 40)
    first = await preflight(f)
    sha = {change["change_id"]: change["content_sha256"] for change in first["changes"]}
    declined = await approve(f, LANG, action="decline")
    # A newer audit finds the same problem on more pages: the finding stays declined.
    await audit_run(
        f,
        [
            row("robots.sitemap_reference_missing", SITEMAP_LINE),
            row("onpage.lang_missing", LANG, urls=[ABOUT, f"{BASE}/pricing"]),
        ],
        revision="3" * 40,
    )
    preview = await preflight(f)
    assert [change["change_id"] for change in preview["changes"]] == [SITEMAP_LINE]
    assert [item["id"] for item in preview["plan"]["left_out"]["declined"]] == [LANG]
    assert preview["next_run"]["change_ids"] == [SITEMAP_LINE]
    stored = await website_change.get_change(f.db, project_id=f.project.id, change_id=LANG)
    assert stored == declined and stored["content_sha256"] == sha[LANG]
    run = await start(f)
    source = await delivery.saved_source(f.db, run.id)
    assert [change["change_id"] for change in source["changes"]] == [SITEMAP_LINE]
    # Asking for it by ID does not bring it back either.
    only = await preflight(f, finding_ids=[LANG])
    assert only["changes"] == [] and only["plan"]["left_out"]["declined"][0]["id"] == LANG


# --- Caps -----------------------------------------------------------------------------------


async def test_caps_are_enforced(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    many = [
        row("onpage.image_alt_missing", f"oa_{index:020x}", urls=[f"{BASE}/p{index}"])
        for index in range(35)
    ]
    await audit_run(f, many, revision="2" * 40)
    preview = await preflight(f)
    assert len(preview["changes"]) == repair_plan.MAX_FINDINGS == 30
    assert len(preview["plan"]["left_out"]["over_cap"]) == 5
    assert preview["caps"] == {"findings": 30, "files": 20, "changed_lines": 800}
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == website_change.KEY)
    output = spec.definition["procedure"]["output"]
    assert output["max_files"] == 20 and output["site_repair_policy"] == "site-fix-v5"
    # A patch over 20 files or 800 changed lines is refused before any pull request.
    wide = [{"path": f"app/p{index}/page.tsx", "content": "x\n"} for index in range(21)]
    with pytest.raises(ValueError, match="one to 20 files"):
        batch_rules.check_bounds(wide, {})
    with pytest.raises(ValueError, match="at most 800"):
        batch_rules.check_bounds(
            [{"path": "app/page.tsx", "content": "y\n" * 801}], {"app/page.tsx": "x\n"}
        )
    # A page change keeps content.deliver's five files.
    proof = {"article_path": "content/blog/a.md"}
    six = {"files": [{"path": f"content/blog/{i}.md", "content": "x"} for i in range(6)]}
    with pytest.raises(ValueError, match="at most 5 files"):
        website_change.check_patch(six, {}, proof)


# --- The technical fix is hidden; its pinned runs are unchanged ----------------------------


def test_the_technical_fix_is_hidden_but_pinned_v5_runs_are_unchanged():
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == technical_fix.KEY)
    definition, files = spec.definition_and_resource_files()
    assert spec.version_label == "0.6.1" and definition["public_discovery"] is False
    assert technical_fix.definition_policy(definition) == "site-fix-v5"
    # Everything a pinned run reads besides the version label and the catalog flag is main's
    # 0.6.0: inputs, procedure, prompt and skills, byte for byte.
    pinned = {k: v for k, v in definition.items() if k not in {"version", "public_discovery"}}
    digest = hashlib.sha256(canonical_json(pinned))
    for path in sorted(files):
        digest.update(path.encode())
        digest.update(files[path])
    assert digest.hexdigest() == (
        "48a35d53736228bf5f5cf6529521395d20a97187c3b414109060a992d498ae47"
    )
    from tin_lite.growth_plan import PROGRAMS

    listed = {w for program in PROGRAMS["programs"] for w in program["tin"]["workflows"]}
    assert technical_fix.KEY not in listed and website_change.KEY in listed


def test_planned_url_changes_fit_the_same_row_contract():
    """Phase 3: #239's planned redirects and noindex changes become rows the same way."""
    entry = {
        "finding_id": "oa_" + "9" * 20,
        "check_id": "planned.redirect",
        "kind": "merge_redirect",
        "group": "redirects",
        "change": "redirects a page the plan merged",
        "issue": "Page decisions proposes redirecting /old to /new.",
        "urls": [f"{BASE}/old"],
        "redirects": [{"from": f"{BASE}/old", "to": f"{BASE}/new"}],
    }
    change = website_change_audit.change_row(
        entry, {"audit_run_id": str(uuid4()), "audit_revision": "2" * 40}
    )
    assert (change.source, change.kind, change.paths) == (
        "planned",
        "redirect",
        ("/old", "/new"),
    )
    assert set(website_change.SOURCES["audit"]) == {r.kind for r in repair_plan.REPAIRS.values()}
    assert all(
        website_change.CHANGE_ID.fullmatch(entry["finding_id"])
        for entry in [entry, {"finding_id": SITEMAP_LINE}]
    )


# --- A run that can't read the repository fails ----------------------------------------------


async def test_a_run_that_cannot_read_the_repository_ends_failed(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    await audit_run(f, findings(), revision="2" * 40)
    f.runtime.integrations.github_repository_bundle.return_value = SimpleNamespace(
        archive=b"", complete=False
    )
    run = await start(f)
    assert await f.activities.prepare_codex_procedure(str(run.id)) is True
    run = await f.db.get_run(run.id)
    # Before, this ended succeeded with "No change proposed"; Tin did not do the job.
    assert run.status.value == "failed"
    assert run.error_message == (
        "Tin couldn't read every file in the repository. No change proposed."
    )
    assert run.artifact_path == f"website/changes/{run.id}.md"
    events = await f.db.pool.fetch(
        "SELECT event_type FROM activity_events WHERE run_id=$1 AND event_type LIKE 'website%'",
        run.id,
    )
    assert [event["event_type"] for event in events] == ["website_change_failed"]
    f.runtime.integrations.github_create_pull_request.assert_not_awaited()


async def test_nothing_left_to_change_still_succeeds(publication_db, monkeypatch):
    f = await fixture(
        publication_db,
        monkeypatch,
        site={f"{BASE}/robots.txt": ROBOTS + f"Sitemap: {SITEMAP_URL}\n", SITEMAP_URL: SITEMAP},
    )
    await audit_run(f, [row("robots.sitemap_reference_missing", SITEMAP_LINE)], revision="2" * 40)
    run = await start(f)
    assert await f.activities.prepare_codex_procedure(str(run.id)) is True
    run = await f.db.get_run(run.id)
    assert run.status.value == "succeeded" and run.error_message is None
    assert run.result_summary == "Website changes checked. No change proposed."


def test_which_preparation_outcomes_fail():
    for reason in ("repository_incomplete", "unsupported_source", "plan_too_large"):
        assert website_change_audit.preparation_failed({"reason": reason})
    for reason in ("nothing_to_fix", "already_resolved", "incomplete_pr_evidence", None):
        assert not website_change_audit.preparation_failed({"reason": reason})
        assert website_change_audit.preparation_summary({"reason": reason}) == (
            "Website changes checked. No change proposed."
        )
    # When preparation recorded the files it couldn't read (#276 does), the error names them.
    missing = {
        "reason": "repository_incomplete",
        "repository_missing": [{"path": "public/hero.mp4"}, {"path": "dist/app.js"}],
        "repository_missing_count": 5,
    }
    assert website_change_audit.preparation_summary(missing) == (
        "Tin couldn't read every file in the repository: public/hero.mp4; dist/app.js; and 3 "
        "more. No change proposed."
    )
