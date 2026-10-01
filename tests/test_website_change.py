"""website.change, phase 1: approved pages on a founder's website.

A change the founder approved with commit to main publishes: Tin opens the pull request and
merges it once GitHub reports it clean. Anything else opens a pull request for the founder to
merge, and so does anything that touches a protected page, approved or not. Approval is
recorded in Postgres with who, when and the exact revision; a project file never counts as one.
"""

import hashlib
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_adapted_page_delivery import (
    PR_URL,
    answer_page,
    approve_answer_page,
    clean,
)
from test_adapted_page_delivery import fixture as page_fixture
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_repository_delivery as delivery
from tin_lite import website_change
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_audit import canonical_json
from tin_lite.page_routes import PATH as ROUTES_PATH
from tin_lite.procedures import procedure_checkpoint_path
from tin_lite.run_service import start_workflow_run
from tin_lite.website_change import ChangeRow, WebsiteChangeConflict
from tin_lite.workflow_inputs import WorkflowInputError

ROUTE = "/blog/{slug}"
PAGE_URL = "https://example.com/blog/reliable-ai-work"


async def fixture(db, monkeypatch):
    f = await page_fixture(db, monkeypatch)
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == website_change.KEY)
    definition, resources = spec.definition_and_resource_files()
    await db.upsert_registry_workflow(
        workflow_id=spec.id,
        key=spec.key,
        title=spec.title,
        description=spec.description,
        executor=spec.executor,
        definition_repo_id="registry/workflows",
        definition_path=spec.definition_path,
        current_commit_sha="d" * 40,
        version_label=spec.version_label,
        definition=definition,
    )
    files = {spec.definition_path: canonical_json(definition), **resources}
    read = f.storage.read_canonical_artifact

    async def read_resource(**kw):
        if kw["repo_id"] == "registry/workflows" and kw["path"] in files:
            return files[kw["path"]]
        return await read(**kw)

    f.storage.read_canonical_artifact = read_resource
    f.website = await db.get_workflow(spec.id)
    f.content_deliver = await db.get_workflow(delivery.WORKFLOW_ID)
    monkeypatch.setattr(delivery.asyncio, "sleep", AsyncMock())
    return f


def choose_route(f, route=ROUTE, kind="answer_page"):
    """The founder's saved choice, as save_page_route writes it."""
    f.storage.repo.edit({ROUTES_PATH: canonical_json({"routes": {kind: route}})})


async def approved_for_main(f, page=None):
    """An answer page approved with the founder's delivery: commit to main."""
    page = page or await answer_page(f)
    await f.delivery.choose(run=page, mode="github_commit", actor=ACTOR, adapt=True)
    return await approve_answer_page(f, page)


async def approve_without_approver(f, run):
    """An approval that does not record who approved it, like rows from before reviewers."""
    await f.db.record_human_review(run_id=run.id, decision="approved")
    await f.db.project_success(
        run_id=run.id,
        canonical_commit_sha=run.canonical_commit_sha,
        artifact_ref=run.artifact_ref,
        artifact_path=run.artifact_path,
    )
    return await f.db.get_run(run.id)


async def start(f, page, workflow=None, **inputs):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=workflow or f.website,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={
            "source_run_id": str(page.id),
            "expected_repository": "owner/site",
            **inputs,
        },
    )


def manifest_for(f, source, *, page_path, public_url, content=None, extra_files=()):
    page = content or ("---\ntitle: How to keep AI work reliable\n---\n\n" + source["article"])
    return {
        "repository": "owner/site",
        "default_branch": "main",
        "head_sha": f.binding.head_sha,
        "title": "Add the reliable AI work page",
        "body": f"Adds the approved page. Build not run.\n\nPublic URL: {public_url}\n",
        "files": [{"path": page_path, "content": page}, *extra_files],
        "verification": [delivery.CHECK_COMMAND],
    }


async def made(
    f,
    run,
    *,
    page_path="content/blog/reliable-ai-work.md",
    public_url=PAGE_URL,
    content=None,
    extra_files=(),
):
    """Run the change the way the procedure does, up to the opened pull request."""
    await f.activities.prepare_codex_procedure(str(run.id))
    await f.activities.create_codex_procedure_sandbox(str(run.id))
    run = await f.db.get_run(run.id)
    source = await delivery.saved_source(f.db, run.id)
    manifest = manifest_for(
        f,
        source,
        page_path=page_path,
        public_url=public_url,
        content=content,
        extra_files=extra_files,
    )
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
    return await f.db.get_run(run.id)


def mergeable(f):
    integrations = f.runtime.integrations
    integrations.github_pull_request_merge_state.return_value = clean()
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


# The change row: one stable ID, its kind, its paths and its recorded approval.


async def test_an_approved_page_is_one_change_row_approved_by_its_reviewer(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    choose_route(f)
    page = await approved_for_main(f)
    run = await start(f, page)
    source = await delivery.saved_source(f.db, run.id)
    change = source["change"]
    assert change["change_id"] == website_change.page_change_id(page.id)
    assert change["change_id"].startswith("pg_") and len(change["change_id"]) == 23
    assert (change["source"], change["kind"], change["paths"]) == ("content_draft", "page", [ROUTE])
    assert change["content_revision"] == page.canonical_commit_sha
    assert change["content_sha256"] == source["source_sha256"]
    approval = change["approval"]
    assert approval["by"] == ACTOR and approval["via"] == "review" and approval["at"]
    assert approval["revision"] == page.canonical_commit_sha
    assert source["publish"]["mode"] == "direct" and source["route"] == ROUTE
    assert source["protected_paths"] == ["/sign-in", "/sign-up", "/auth-complete"]
    receipt = await f.db.get_effect(delivery.source_key(run.id))
    assert receipt.operation == website_change.OPERATION
    # The same page and repository cannot buy a second change while this one works.
    with pytest.raises(WorkflowInputError, match="still working"):
        await start(f, page)


def test_a_change_row_is_defined_once_for_every_source():
    sha = "a" * 64
    row = ChangeRow("oa_" + "1" * 20, "planned_url_change", "redirect", "Move", ("/a", "/b"), sha)
    assert row.as_dict()["paths"] == ["/a", "/b"]
    assert set(website_change.SOURCES) == {
        "content_draft",
        "planned_url_change",
        "technical_fix",
        "blog_index",
    }
    for bad, match in (
        ({"change_id": "oa_short"}, "change ID"),
        ({"kind": "page"}, "cannot be of kind"),
        ({"paths": ("no-slash",)}, "not a site path"),
        ({"content_sha256": "x"}, "SHA-256"),
        ({"title": "  "}, "title"),
    ):
        values = {
            "change_id": "oa_" + "1" * 20,
            "source": "planned_url_change",
            "kind": "redirect",
            "title": "Move",
            "paths": ("/a",),
            "content_sha256": sha,
            **bad,
        }
        with pytest.raises(ValueError, match=match):
            ChangeRow(**values)


# Two modes: a pre-approved change publishes; anything else is a pull request.


async def test_a_pre_approved_page_publishes_directly(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    choose_route(f)
    page = await approved_for_main(f)
    run = await made(f, await start(f, page))
    integrations = mergeable(f)
    for _ in range(2):  # The recorded outcome is reused; GitHub is asked to merge once.
        merge = await merge_outcome(f, run)
    integrations.github_merge_pull_request.assert_awaited_once()
    assert merge["status"] == "merged" and merge["merge_rule"] == "page_only"
    assert merge["commit"] == "b" * 40
    events = await f.db.pool.fetch(
        "SELECT run_id FROM activity_events WHERE event_type='website_change_merged'"
    )
    assert {row["run_id"] for row in events} == {run.id, page.id}
    status = await f.delivery.status(run)
    assert status["merged"] is True and status["pull_request"]["url"] == PR_URL
    assert status["change_id"] == website_change.page_change_id(page.id)
    assert status["publish"] == "direct" and status["public_route"] == PAGE_URL


@pytest.mark.parametrize(
    "why", ["no_recorded_approver", "approval_asked_for_a_pr", "no_delivery_chosen"]
)
async def test_a_change_that_was_not_pre_approved_opens_a_pull_request(
    publication_db, monkeypatch, why
):
    f = await fixture(publication_db, monkeypatch)
    choose_route(f)
    page = await answer_page(f)
    if why == "no_recorded_approver":
        page = await approve_without_approver(f, page)
    elif why == "approval_asked_for_a_pr":
        await f.delivery.choose(run=page, mode="github_pr", actor=ACTOR, adapt=True)
        page = await approve_answer_page(f, page)
    else:
        # Approved, but nobody chose commit to main: approval is not website publication.
        page = await approve_answer_page(f, page)
    run = await start(f, page)
    source = await delivery.saved_source(f.db, run.id)
    assert source["publish"]["mode"] == "pull_request"
    if why == "no_recorded_approver":
        assert source["change"]["approval"] is None
        assert "No one approved this change" in source["publish"]["reason"]
    elif why == "approval_asked_for_a_pr":
        assert source["change"]["approval"]["by"] == ACTOR
        assert "asked for a pull request" in source["publish"]["reason"]
    else:
        assert source["change"]["approval"]["by"] == ACTOR
        assert "did not ask Tin to commit" in source["publish"]["reason"]
    run = await made(f, run)
    integrations = mergeable(f)
    merge = await merge_outcome(f, run)
    integrations.github_pull_request_merge_state.assert_not_called()
    integrations.github_merge_pull_request.assert_not_called()
    assert merge["status"] == "left_open" and merge["reason"] == source["publish"]["reason"]
    status = await f.delivery.status(run)
    assert status["merged"] is False and status["pull_request"]["url"] == PR_URL
    assert await f.db.pool.fetchval(
        "SELECT count(*) FROM activity_events WHERE event_type='website_change_left_open'"
    )


async def test_protected_paths_open_a_pull_request_even_when_approved(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    choose_route(f)
    # The founder lists /blog as shared with another app: the approved page still waits.
    shared = await approved_for_main(f)
    run = await start(f, shared, protected_paths=["/blog"])
    source = await delivery.saved_source(f.db, run.id)
    assert source["change"]["approval"]["by"] == ACTOR
    assert source["publish"]["mode"] == "pull_request"
    assert "It touches /blog, a protected page" in source["publish"]["reason"]
    assert "/blog" in source["protected_paths"]
    merge = await merge_outcome(f, await made(f, run))
    assert merge["status"] == "left_open" and "/blog" in merge["reason"]
    # An approved page whose patch also edits the shared sign-in page stays a pull request.
    page = await approved_for_main(f)
    sign_in = {"path": "src/app/(auth)/sign-in/[[...sign-in]]/page.tsx", "content": "x\n"}
    run = await made(f, await start(f, page), extra_files=(sign_in,))
    integrations = mergeable(f)
    merge = await merge_outcome(f, run)
    integrations.github_merge_pull_request.assert_not_called()
    assert merge["status"] == "left_open"
    assert merge["reason"] == "It touches /sign-in, a protected page, so it waits for your review."


def test_protected_paths_cover_the_shared_auth_pages_and_what_serves_them():
    roots = website_change.protected_paths(["/partners/", "/", "not-a-path", "/sign-in"])
    assert roots == ["/sign-in", "/sign-up", "/auth-complete", "/partners"]
    assert website_change.protected("/sign-in", roots) == "/sign-in"
    assert website_change.protected("https://example.com/sign-up/sso", roots) == "/sign-up"
    assert website_change.protected("/partners/{slug}", roots) == "/partners"
    assert website_change.protected("/sign-inside", roots) is None
    assert website_change.protected("/blog/{slug}", roots) is None
    served = website_change.file_protected
    assert served("pages/sign-in.tsx", roots) == "/sign-in"
    assert served("src/app/(auth)/sign-up/page.tsx", roots) == "/sign-up"
    assert served("app/auth-complete/route.ts", roots) == "/auth-complete"
    assert served("content/blog/sign-in-help.md", roots) is None


async def test_a_file_edit_cannot_fake_approval(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    choose_route(f)
    page = await approve_without_approver(f, await answer_page(f))
    change_id = website_change.page_change_id(page.id)
    # Someone hand-edits project files to claim approval; no file is read as approval.
    f.storage.repo.edit(
        {
            "content/website-changes.json": canonical_json(
                {"changes": {change_id: {"status": "approved", "approved_by": ACTOR}}}
            ),
            "website/approvals.md": f"- [x] {change_id} approved\n".encode(),
        }
    )
    run = await start(f, page)
    source = await delivery.saved_source(f.db, run.id)
    assert source["change"]["approval"] is None
    assert source["publish"]["mode"] == "pull_request"
    # The same holds for a proposed change row: a pending row is not approved by a file.
    sha = hashlib.sha256(b"redirect /old to /new").hexdigest()
    row = ChangeRow("oa_" + "2" * 20, "planned_url_change", "redirect", "Move", ("/old",), sha)
    await website_change.propose(f.db, project_id=f.project.id, rows=[row])
    f.storage.repo.edit(
        {"content/website-changes.json": canonical_json({row.change_id: "approved"})}
    )
    assert (
        await website_change.approval_for(f.db.pool, project_id=f.project.id, change=row.as_dict())
        is None
    )
    await website_change.decide(
        f.db,
        project_id=f.project.id,
        change_id=row.change_id,
        action="approve",
        actor=ACTOR,
        request_id=uuid4(),
        content_sha256=sha,
    )
    approval = await website_change.approval_for(
        f.db.pool, project_id=f.project.id, change=row.as_dict()
    )
    assert approval["by"] == ACTOR and approval["via"] == "decision"
    # Other content under the same ID is not what the founder approved.
    changed = ChangeRow(row.change_id, row.source, row.kind, row.title, row.paths, "c" * 64)
    assert (
        await website_change.approval_for(
            f.db.pool, project_id=f.project.id, change=changed.as_dict()
        )
        is None
    )


# One decision per stable change ID.


async def test_decided_rows_stay_decided(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    project = f.project.id

    def row(n, content, kind="redirect"):
        return ChangeRow(
            f"oa_{n * 20}",
            "planned_url_change",
            kind,
            f"Change {n}",
            (f"/old-{n}",),
            hashlib.sha256(content.encode()).hexdigest(),
        )

    first = await website_change.propose(
        f.db, project_id=project, rows=[row("1", "a"), row("2", "b", "noindex"), row("3", "c")]
    )
    assert [item["status"] for item in first] == ["pending"] * 3
    approve_request = uuid4()
    approved = await website_change.decide(
        f.db,
        project_id=project,
        change_id=row("1", "a").change_id,
        action="approve",
        actor=ACTOR,
        request_id=approve_request,
        content_sha256=row("1", "a").content_sha256,
    )
    assert approved["status"] == "approved" and approved["decided_by"] == ACTOR
    await website_change.decide(
        f.db,
        project_id=project,
        change_id=row("2", "b").change_id,
        action="decline",
        actor=ACTOR,
        request_id=uuid4(),
        content_sha256=row("2", "b").content_sha256,
    )
    # Next week's run proposes the same stable IDs with new content: decided rows keep theirs.
    again = await website_change.propose(
        f.db, project_id=project, rows=[row("1", "z"), row("2", "y", "noindex"), row("3", "x")]
    )
    by_id = {item["change_id"]: item for item in again}
    assert by_id[row("1", "a").change_id]["status"] == "approved"
    assert by_id[row("1", "a").change_id]["content_sha256"] == row("1", "a").content_sha256
    assert by_id[row("2", "b").change_id]["status"] == "declined"
    assert by_id[row("3", "c").change_id]["content_sha256"] == row("3", "x").content_sha256
    assert await website_change.decided(
        f.db, project_id=project, change_ids=[r.change_id for r in (row("1", ""), row("3", ""))]
    ) == {row("1", "").change_id: "approved"}
    # A replay returns the recorded decision; a different decision is refused.
    replay = await website_change.decide(
        f.db,
        project_id=project,
        change_id=row("1", "a").change_id,
        action="approve",
        actor=ACTOR,
        request_id=approve_request,
        content_sha256=row("1", "a").content_sha256,
    )
    assert replay == approved
    with pytest.raises(WebsiteChangeConflict, match="already approved"):
        await website_change.decide(
            f.db,
            project_id=project,
            change_id=row("1", "a").change_id,
            action="decline",
            actor=ACTOR,
            request_id=uuid4(),
            content_sha256=row("1", "a").content_sha256,
        )
    # The founder decides on the content they read: stale content is refused.
    with pytest.raises(WebsiteChangeConflict, match="updated after you read it"):
        await website_change.decide(
            f.db,
            project_id=project,
            change_id=row("3", "c").change_id,
            action="approve",
            actor=ACTOR,
            request_id=uuid4(),
            content_sha256=row("3", "c").content_sha256,
        )
    with pytest.raises(WebsiteChangeConflict, match="request ID"):
        await website_change.decide(
            f.db,
            project_id=project,
            change_id=row("3", "c").change_id,
            action="approve",
            actor=ACTOR,
            request_id=approve_request,
            content_sha256=row("3", "x").content_sha256,
        )
    with pytest.raises(LookupError):
        await website_change.decide(
            f.db,
            project_id=project,
            change_id=row("3", "c").change_id,
            action="approve",
            actor="user_stranger",
            request_id=uuid4(),
            content_sha256=row("3", "x").content_sha256,
        )
    # Postgres itself refuses to reopen a decided row.
    with pytest.raises(Exception, match="already approved"):
        await f.db.pool.execute(
            "UPDATE website_changes SET status='pending', decided_at=NULL, "
            "decided_by_clerk_user_id=NULL, decision_request_id=NULL WHERE change_id=$1",
            row("1", "a").change_id,
        )
    with pytest.raises(WebsiteChangeConflict, match="declined"):
        await website_change.approval_for(
            f.db.pool, project_id=project, change=row("2", "b", "noindex").as_dict()
        )
    # A page is approved through its own review, never through a proposed row.
    page_row = ChangeRow("pg_" + "0" * 20, "content_draft", "page", "Page", ("/blog/x",), "a" * 64)
    with pytest.raises(ValueError, match="review"):
        await website_change.propose(f.db, project_id=project, rows=[page_row])


# Answer pages land at the founder's chosen route in the site's own page registry.


async def test_an_answer_page_without_a_chosen_route_asks_the_founder(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    page = await approved_for_main(f)
    with pytest.raises(WorkflowInputError) as refused:
        await start(f, page)
    message = str(refused.value)
    assert "Where on your site should pages that answer buyer questions go?" in message
    assert "save_page_route(page_type=answer_page)" in message and "will not guess" in message
    assert not await f.db.pool.fetchval(
        "SELECT count(*) FROM workflow_runs WHERE workflow_id=$1", website_change.WORKFLOW_ID
    )
    shown = structured(
        await mcp(f, monkeypatch).call_tool(
            "get_workflow", {"project_id": str(f.project.id), "workflow_key": "website.change"}
        )
    )
    preparation = shown["preparation"]
    assert preparation["page_routes"] == {}
    assert preparation["ask_the_founder"]["then"]["name"] == "save_page_route"
    assert {"run_id": str(page.id), "title": "How to keep recurring AI work reliable"} in (
        preparation["articles"]
    )


async def test_answer_pages_go_to_the_registry_route_never_content_answers(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    choose_route(f)
    page = await approved_for_main(f)
    run = await start(f, page)
    source = await delivery.saved_source(f.db, run.id)
    # Tin's own draft folder is never a page on the site.
    answers = manifest_for(
        f, source, page_path="content/answers/reliable-ai-work.md", public_url=PAGE_URL
    )
    with pytest.raises(ValueError, match=r"page registry at /blog/\{slug\}"):
        delivery.validate_patch(answers, source)
    # Nor may a website change touch dependencies.
    lockfile = manifest_for(
        f,
        source,
        page_path="content/blog/reliable-ai-work.md",
        public_url=PAGE_URL,
        extra_files=({"path": "bun.lock", "content": "x\n"},),
    )
    with pytest.raises(ValueError, match="cannot change dependencies"):
        delivery.validate_patch(lockfile, source)
    # A typed page registry keeps the copy as one JSON string, served at the chosen route.
    registry = (
        "export const blogPages = [\n  {\n    slug: 'reliable-ai-work',\n"
        f"    body: {json.dumps(source['article'])},\n  }},\n];\n"
    )
    route_file = {
        "path": "src/app/blog/[slug]/page.tsx",
        "content": "import { blogPages } from '@/content/blog-pages';\nexport default 1;\n",
    }
    run = await made(
        f,
        run,
        page_path="src/content/blog-pages.ts",
        content=registry,
        extra_files=(route_file,),
    )
    integrations = mergeable(f)
    merge = await merge_outcome(f, run)
    assert merge["status"] == "merged" and merge["merge_rule"] == "chosen_route"
    assert [
        item.path for item in integrations.github_merge_pull_request.call_args.kwargs["files"]
    ] == [
        "src/content/blog-pages.ts",
        "src/app/blog/[slug]/page.tsx",
    ]
    # A page placed anywhere else than the chosen route waits for the founder.
    other = await approved_for_main(f)
    run = await made(
        f, await start(f, other), public_url="https://example.com/answers/reliable-ai-work"
    )
    merge = await merge_outcome(f, run)
    assert merge["status"] == "left_open"
    assert merge["reason"] == (
        "It does not put the page at your chosen route /blog/{slug}, so it waits for your review."
    )


# content.deliver keeps working as it did; the two never adapt one page twice.


async def test_content_deliver_pinned_runs_are_unchanged(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == delivery.KEY)
    definition, files = spec.definition_and_resource_files()
    digest = hashlib.sha256(canonical_json(definition))
    for path in sorted(files):
        digest.update(path.encode())
        digest.update(files[path])
    # The definition and procedure files pinned runs point to are byte-for-byte main's 1.3.0.
    assert spec.version_label == "1.3.0"
    assert digest.hexdigest() == (
        "078e681833686005fec18b779b83d6ed7cd621b078af512699962d597050b555"
    )
    # An approval-started content.deliver run pins no change row and keeps its own rules:
    # a pull-request setting never merges, and nothing records a merge for it.
    page = await answer_page(f)
    await f.delivery.choose(run=page, mode="github_pr", actor=ACTOR, adapt=True)
    page = await approve_answer_page(f, page)
    await f.activities.deliver_content_draft(str(page.id))
    child = await f.db.pool.fetchval(
        "SELECT id FROM workflow_runs WHERE workflow_id=$1", delivery.WORKFLOW_ID
    )
    source = await delivery.saved_source(f.db, child)
    assert "change" not in source and "publish" not in source
    assert (await f.db.get_effect(delivery.source_key(child))).operation == delivery.OPERATION
    child = await made(f, await f.db.get_run(child), page_path="content/answers/reliable.md")
    manifest = await delivery.saved_manifest(f.db, f.storage, child)
    assert delivery.validate_patch(manifest, source) == delivery.validate_copy(manifest, source)
    assert await merge_outcome(f, child) is None
    status = await f.delivery.status(child)
    assert "change_id" not in status and status["merge"] is None


async def test_one_page_is_never_adapted_by_both_workflows(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    choose_route(f)
    # The approval started content.deliver: website.change refuses the same page.
    page = await answer_page(f)
    await f.delivery.choose(run=page, mode="github_commit", actor=ACTOR, adapt=True)
    page = await approve_answer_page(f, page)
    await f.activities.deliver_content_draft(str(page.id))
    with pytest.raises(WorkflowInputError, match="still working"):
        await start(f, page)
    # website.change started first: content.deliver refuses the same page.
    other = await approved_for_main(f)
    await start(f, other)
    with pytest.raises(WorkflowInputError, match="still working"):
        await start(f, other, workflow=f.content_deliver)


# The founder decides one change row over HTTP or MCP.


async def test_http_and_mcp_approve_or_decline_one_change_row(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    sha = "d" * 64
    rows = [
        ChangeRow(f"oa_{n * 20}", "planned_url_change", "noindex", f"Hide {n}", (f"/{n}",), sha)
        for n in ("4", "5")
    ]
    await website_change.propose(f.db, project_id=f.project.id, rows=rows)
    base = f"/api/projects/{f.project.id}/website-changes"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        listed = await client.get(base, params={"status": "pending"})
        assert [item["change_id"] for item in listed.json()] == [r.change_id for r in rows]
        body = {"request_id": str(uuid4()), "content_sha256": sha}
        approved = await client.post(f"{base}/{rows[0].change_id}/approve", json=body)
        assert approved.status_code == 200 and approved.json()["status"] == "approved"
        again = await client.post(
            f"{base}/{rows[0].change_id}/decline", json={**body, "request_id": str(uuid4())}
        )
        assert again.status_code == 409 and "stays decided" in again.text
        missing = await client.get(f"{base}/oa_{'9' * 20}")
        assert missing.status_code == 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, "user_stranger")), base_url="https://tin.test"
    ) as client:
        assert (await client.get(base)).status_code == 404
        denied = await client.post(
            f"{base}/{rows[1].change_id}/approve",
            json={"request_id": str(uuid4()), "content_sha256": sha},
        )
        assert denied.status_code == 404
    server = mcp(f, monkeypatch)
    declined = structured(
        await server.call_tool(
            "decline_website_change",
            {
                "project_id": str(f.project.id),
                "change_id": rows[1].change_id,
                "content_sha256": sha,
                "request_id": str(uuid4()),
            },
        )
    )
    assert declined["change"]["status"] == "declined"
    assert any("will not propose it" in item for item in declined["relay"])
    listed = structured(
        await server.call_tool("list_website_changes", {"project_id": str(f.project.id)})
    )
    assert {item["change_id"]: item["status"] for item in listed["changes"]} == {
        rows[0].change_id: "approved",
        rows[1].change_id: "declined",
    }
    with pytest.raises(ToolError, match="conflict"):
        await server.call_tool(
            "approve_website_change",
            {
                "project_id": str(f.project.id),
                "change_id": rows[1].change_id,
                "content_sha256": sha,
                "request_id": str(uuid4()),
            },
        )


# Merge once the repository's required checks pass (Emre, 10/1).


async def test_an_unstable_pull_request_merges_when_pre_approved(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    choose_route(f)
    page = await approved_for_main(f)
    run = await made(f, await start(f, page))
    integrations = mergeable(f)
    # A check the repository does not require failed: GitHub says unstable, not clean.
    integrations.github_pull_request_merge_state.return_value = clean(mergeable_state="unstable")
    merge = await merge_outcome(f, run)
    integrations.github_merge_pull_request.assert_awaited_once()
    assert merge["status"] == "merged" and merge["merged_by"] == "tin"
    # The receipt names the state that allowed the merge.
    assert merge["mergeable_state"] == "unstable"
    other = await approved_for_main(f)
    run = await made(f, await start(f, other))
    integrations = mergeable(f)
    merge = await merge_outcome(f, run)
    assert merge["status"] == "merged" and merge["mergeable_state"] == "clean"


NEVER_MERGE = {
    "dirty": "It conflicts with the default branch.",
    "blocked": "GitHub needs a review or a required check before it can merge, "
    "so Tin left it open.",
    "behind": "Your repository requires it to be up to date with the default branch first.",
    "draft": "It is a draft pull request.",
    "unknown": "Its required checks had not passed after a few minutes, so Tin left it open.",
}


@pytest.mark.parametrize("verdict", list(NEVER_MERGE))
async def test_blocked_or_dirty_pull_requests_never_merge(publication_db, monkeypatch, verdict):
    f = await fixture(publication_db, monkeypatch)
    choose_route(f)
    monkeypatch.setattr(delivery, "MERGE_WAIT_SECONDS", 0)
    page = await approved_for_main(f)
    run = await made(f, await start(f, page))
    integrations = mergeable(f)
    integrations.github_pull_request_merge_state.return_value = clean(
        mergeable=verdict not in {"dirty", "unknown"}, mergeable_state=verdict
    )
    merge = await merge_outcome(f, run)
    integrations.github_merge_pull_request.assert_not_called()
    assert merge["status"] == "left_open" and "mergeable_state" not in merge
    assert merge["reason"] == NEVER_MERGE[verdict]
