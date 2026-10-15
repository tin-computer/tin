"""Approved content.generate pages become pages on the site through website.change.

Approval with a GitHub repository starts one metered adaptation, never the Markdown publisher.
The page's approved copy is the authority: the source is pinned at its exact revision and
publication digest, and anything not approved in this project is refused.
test_website_change covers website.change's own merge rules.
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from test_private_workflows import ACTOR, app, mcp, structured
from test_private_workflows import fixture as private_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import approved_article, content_draft, website_change
from tin_lite import content_repository_delivery as delivery
from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.content_delivery import (
    DRAFT_WORKFLOW_ID,
    ContentDelivery,
    about_usd,
    adaptation_start_key,
    choice_key,
    delivery_key,
)
from tin_lite.content_delivery_api import publish_preview, retry_delivery
from tin_lite.integrations import GitHubPullRequestResult, GitHubRepositoryBinding
from tin_lite.organic_audit import canonical_json
from tin_lite.page_urls import PageUrls
from tin_lite.procedures import procedure_checkpoint_path
from tin_lite.publication import OutputCheckpoint

KEYS = ("content.generate", "website.change")
TITLE = "How to keep recurring AI work reliable"
LISTING = (
    '---\nmeta_title: "How to keep AI work reliable"\n'
    'meta_description: "Keep recurring AI work reliable with durable runs, receipts and '
    'a review before anything ships."\n---\n\n'
)
ANSWER_BODY = f"""# {TITLE}

Last updated: 2026-09-28

Recurring AI work stays reliable when every run is durable, every paid call leaves a receipt
and a person reviews the result before anything ships. A retry then reuses the receipt
instead of paying again, and the review catches a wrong answer before a customer reads it.

## Why does a retry repeat a paid call?

Without a [receipt](https://example.com/docs/receipts), a retried step cannot tell that the
call already happened, so it pays for it twice.

## How does a review help?

A [review](https://example.com/docs/reviews) shows the exact result before it ships, and
approval binds that version.

## FAQ

### Does a retry cost twice?

No. The receipt records the call, so the retry reuses it.

### Can I edit the result before it ships?

Yes. Feedback revises the same document before approval.

## Sources

- [Receipts](https://example.com/docs/receipts)
- [Reviews](https://example.com/docs/reviews)
- [Durable runs](https://example.com/docs/runs)
"""
ARTICLE_BODY = (
    "# A useful planned article\n\n"
    + " ".join(["Keep this useful explanation and its source."] * 12)
    + "\n"
)
PR_URL = "https://github.com/owner/site/pull/42"
# The address the page's plan gave it, which its review shows.
PAGE_URL = "https://example.com/blog/reliable-ai-work"
PAGE_PATH = "/blog/reliable-ai-work"


async def fixture(db, monkeypatch):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda details: None)
    f = await private_fixture(db)
    f.definitions, files = {}, {}
    for spec in (w for w in BUILTIN_WORKFLOWS if w.key in KEYS):
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
        f.definitions[spec.key] = definition
        files.update({spec.definition_path: canonical_json(definition), **resources})
    read = f.storage.read_canonical_artifact

    async def read_resource(**kw):
        if kw["repo_id"] == "registry/workflows":
            return files[kw["path"]]
        return await read(**kw)

    f.storage.read_canonical_artifact = read_resource

    async def publish(**kwargs):
        return f.storage.repo.edit({kwargs["path"]: kwargs["content"]}), True

    f.storage.publish_state_document = publish
    await db.upsert_integration_connection(
        project_id=f.project.id,
        provider_key="infra.github",
        external_account_id="456",
        external_account_label="owner/site",
        configuration={
            "selected_repository": "owner/site",
            "write_opted_in": True,
            "permissions": {"contents": "write", "pull_requests": "write"},
        },
        credential_ciphertext=None,
        credential_key_version=None,
        connected_by_clerk_user_id=ACTOR,
    )
    connection = await db.get_integration_connection(
        project_id=f.project.id, provider_key="infra.github"
    )
    f.binding = GitHubRepositoryBinding(connection.id, 456, 123, "owner/site", "main", "9" * 40)
    f.runtime.integrations = SimpleNamespace(
        ensure_requirements=AsyncMock(),
        github_repository_binding=AsyncMock(return_value=f.binding),
        github_markdown_file=AsyncMock(return_value=None),
        github_create_pull_request=AsyncMock(
            return_value=GitHubPullRequestResult("owner/site", "tin/test", 42, PR_URL)
        ),
        github_commit_files=AsyncMock(),
        github_pull_request_merge_state=AsyncMock(),
        github_merge_pull_request=AsyncMock(),
        # The site's main branch requires a build check, and its build passed.
        github_required_status_checks=AsyncMock(
            return_value={"readable": True, "contexts": ["build"]}
        ),
        github_commit_checks=AsyncMock(
            return_value={"readable": True, "checks": [{"name": "build", "state": "success"}]}
        ),
    )
    f.runtime.temporal.get_workflow_handle = MagicMock(
        return_value=SimpleNamespace(signal=AsyncMock())
    )
    f.activities = TinActivities(
        database=db,
        storage=f.storage,
        settings=f.settings,
        integrations=f.runtime.integrations,
        sandboxes=SimpleNamespace(create=AsyncMock(return_value="sandbox-test"), kill=AsyncMock()),
        temporal=f.runtime.temporal,
    )
    f.delivery = ContentDelivery(
        database=db, storage=f.storage, integrations=f.runtime.integrations
    )
    return f


async def save(db, key, operation, result):
    async with db.effect_lock(key, operation) as (conn, _):
        await db.start_effect(conn, execution_key=key, operation=operation)
        await db.complete_effect(conn, execution_key=key, result=result)


async def draft(
    f, *, kind="answer", body=None, project=None, title=TITLE, page_url=PAGE_URL, action=None
):
    """A content.generate draft waiting for review, saved the way the procedure saves it: its
    plan selection, its prepared context and its published page. `page_url` is the address
    its plan gave it; None is a draft selected before pages had planned addresses."""
    project = project or f.project
    item = {"id": f"item-{uuid4().hex[:8]}", "title": title, "kind": kind}
    if action:
        item["action"] = action
    program_id = str(uuid4())
    run, _ = await f.db.create_run(
        project_id=project.id,
        workflow_id=DRAFT_WORKFLOW_ID,
        started_by_clerk_user_id=ACTOR,
        definition_commit_sha="d" * 40,
        pinned_definition=f.definitions["content.generate"],
        input_payload={"program_id": program_id, "item_id": item["id"]},
    )
    await save(
        f.db,
        content_draft.selection_key(run.id),
        content_draft.SELECTION_OPERATION,
        {
            "program_id": program_id,
            "item": item,
            "host": "example.com",
            "mode": "selected",
            **({"page_url": page_url} if page_url else {}),
        },
    )
    await save(
        f.db,
        content_draft.receipt_key(run.id),
        content_draft.KEY,
        {
            "output_validator": content_draft.EDITORIAL_VALIDATOR,
            "program_id": program_id,
            "item": item,
            "due_date": "2026-09-28",
            "style": {"path": None, "revision": None, "sha256": None},
            **({"kind": content_draft.ANSWER} if kind == "answer" else {}),
            **({"page_url": page_url} if page_url else {}),
        },
    )
    if body is None:
        body = LISTING + ANSWER_BODY if kind == "answer" else ARTICLE_BODY
    raw = body.encode()
    await f.db.pool.execute(
        "UPDATE workflow_runs SET expected_head_sha=$2 WHERE id=$1", run.id, "e" * 40
    )
    run = await f.db.get_run(run.id)
    path = content_draft.PATH_TEMPLATE.format(run_id=run.id)
    revision = f.storage.repo.edit({path: raw})
    await save(
        f.db,
        f"{run.id}:procedure_canonical_commit",
        "procedure_canonical_commit",
        {
            "canonical_commit_sha": revision,
            "artifact_path": path,
            "checkpoint": OutputCheckpoint.create(
                run=run, revision=revision, path=path, media_type="text/markdown", content=raw
            ).to_dict(),
        },
    )
    await f.db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=revision,
        artifact_path=path,
        artifact_ref=f"code.storage://{project.state_repo_id}@{revision}/{path}",
        artifact_title=title,
    )
    return await f.db.get_run(run.id)


async def answer_page(f, *, body=None, project=None, page_url=PAGE_URL):
    """A content.generate answer page waiting for review."""
    return await draft(f, kind="answer", body=body, project=project, page_url=page_url)


async def approve_answer_page(f, run):
    """The founder's recorded approval: who approved it, and the exact revision."""
    await f.db.set_review_actor(run_id=run.id, clerk_user_id=ACTOR)
    await f.db.record_human_review(run_id=run.id, decision="approved")
    await f.db.project_success(
        run_id=run.id,
        canonical_commit_sha=run.canonical_commit_sha,
        artifact_ref=run.artifact_ref,
        artifact_path=run.artifact_path,
    )
    return await f.db.get_run(run.id)


async def children(f, source):
    """The page's adaptations by website.change."""
    return await f.db.pool.fetch(
        "SELECT r.id, r.status, r.workflow_id FROM workflow_runs r JOIN effect_receipts s "
        "ON s.execution_key='content-delivery:' || r.id::text || ':source' "
        "WHERE r.workflow_id = ANY($1::uuid[]) AND s.result->>'source_run_id'=$2",
        list(delivery.ADAPTER_WORKFLOW_IDS),
        str(source.id),
    )


async def select(f, run, **inputs):
    return await website_change.select_source(
        database=f.db,
        storage=f.storage,
        integrations=f.runtime.integrations,
        project_id=f.project.id,
        inputs={
            "source": "content_draft",
            "source_run_id": str(run.id),
            "expected_repository": "owner/site",
            **inputs,
        },
    )


# Source checks: the approved document is the authority, not a caller's claim.


async def test_answer_page_source_splits_listing_from_the_exact_copy(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await approve_answer_page(f, await answer_page(f))
    source = await select(f, run)
    assert source["source_kind"] == "answer_page"
    assert source["article"] == ANSWER_BODY  # Frontmatter split off; the copy is unchanged.
    assert source["article_sha256"] == hashlib.sha256(ANSWER_BODY.encode()).hexdigest()
    assert source["page_metadata"] == {
        "meta_title": "How to keep AI work reliable",
        "meta_description": "Keep recurring AI work reliable with durable runs, receipts and "
        "a review before anything ships.",
    }
    assert source["title"] == TITLE
    assert source["source_revision"] == run.canonical_commit_sha
    assert source["binding"]["repository"] == "owner/site"
    assert source["page_url"] == PAGE_URL and source["route"] == PAGE_PATH
    assert source["change"]["paths"] == [PAGE_PATH]


async def test_changed_bytes_or_revision_drift_refuse_the_page(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await approve_answer_page(f, await draft(f, kind="article"))
    read = f.storage.read_canonical_artifact

    async def tampered(**kw):
        raw = await read(**kw)
        return raw + b"\nAn edit nobody reviewed.\n" if kw["path"] == run.artifact_path else raw

    f.storage.read_canonical_artifact = tampered
    with pytest.raises(ValueError, match="publication digest"):
        await select(f, run)
    f.storage.read_canonical_artifact = read
    page = await approve_answer_page(f, await answer_page(f))
    source = await select(f, page)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET canonical_commit_sha=$2 WHERE id=$1", page.id, "e" * 40
    )
    with pytest.raises(ValueError, match="publication proof"):
        await select(f, page)
    async with f.db.pool.acquire() as conn:
        with pytest.raises(ValueError, match="not approved"):
            await approved_article.guard(conn, project_id=f.project.id, source=source)


async def test_unapproved_foreign_or_oversized_pages_are_refused(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    waiting = await answer_page(f)
    with pytest.raises(ValueError, match="approve"):
        await select(f, waiting)
    other = await f.db.create_project(name="Another site", state_repo_id="projects/another")
    foreign = await approve_answer_page(f, await answer_page(f, project=other))
    with pytest.raises(ValueError, match="from this project"):
        await select(f, foreign)
    huge = await approve_answer_page(
        f, await draft(f, kind="article", body=ARTICLE_BODY + "x" * 80_000)
    )
    with pytest.raises(ValueError, match="size limit"):
        await select(f, huge)


# Approval starts exactly one adaptation and never the Markdown publisher.


@pytest.mark.parametrize("surface", ["http", "mcp"])
async def test_approval_starts_one_adaptation_on_both_transports(
    publication_db, monkeypatch, surface
):
    f = await fixture(publication_db, monkeypatch)
    run = await answer_page(f)
    if surface == "http":
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
        ) as client:
            response = await client.post(
                f"/api/workflows/runs/{run.id}/approve", json={"delivery": "github_commit"}
            )
        assert response.status_code == 202, response.text
        shown = response.json()["content_delivery"]
        assert shown["adapter"] == "repository" and shown["mode"] == "github_commit"
        assert shown["approval_label"] == "Publish" and "cost_preview" in shown
    else:
        result = structured(
            await mcp(f, monkeypatch).call_tool(
                "approve_workflow_run", {"run_id": str(run.id), "delivery": "github_commit"}
            )
        )
        assert any("separate run" in item for item in result["relay"])
        assert any("merges its pull request into main" in item for item in result["relay"])
    choice = (await f.db.get_effect(choice_key(run.id))).result
    assert choice["adapter"] == "repository" and choice["path"] is None
    assert choice["settings"]["mode"] == "github_commit"
    # Approval pins no route: the page's address is the one its plan gave it.
    assert "route" not in choice
    assert choice["trigger_source"] == ("mcp" if surface == "mcp" else "manual")
    run = await approve_answer_page(f, run)
    for _ in range(2):
        await f.activities.deliver_content_draft(str(run.id))
    started = await children(f, run)
    assert len(started) == 1 and started[0]["workflow_id"] == delivery.WEBSITE_CHANGE_ID
    child = await f.db.get_run(started[0]["id"])
    assert child.started_by_clerk_user_id == ACTOR
    assert child.input["source_run_id"] == str(run.id)
    assert child.input["source"] == "content_draft"
    assert await f.db.run_start_idempotency_key(child.id) == adaptation_start_key(run.id)
    source = await delivery.saved_source(f.db, child.id)
    assert source["change"]["approval"]["by"] == ACTOR
    assert source["route"] == PAGE_PATH and source["publish"]["mode"] == "direct"
    # The Markdown publisher never ran, and its receipt key belongs to the adaptation.
    f.runtime.integrations.github_commit_files.assert_not_called()
    f.runtime.integrations.github_create_pull_request.assert_not_called()
    with pytest.raises(Exception, match="content_draft_adaptation_v1"):
        async with f.db.effect_lock(delivery_key(run.id), "content_draft_delivery_v1"):
            pass
    status = await f.delivery.status(run)
    assert status["adapter"] == "repository" and status["run_id"] == str(child.id)
    assert (await f.delivery.statuses([run]))[run.id] == status


async def test_an_approved_article_goes_to_its_planned_address(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await draft(f, kind="article", page_url="https://example.com/guides/useful-article")
    await f.delivery.choose(run=run, mode="github_pr", actor=ACTOR)
    run = await approve_answer_page(f, run)
    await f.activities.deliver_content_draft(str(run.id))
    started = await children(f, run)
    assert len(started) == 1 and started[0]["workflow_id"] == delivery.WEBSITE_CHANGE_ID
    source = await delivery.saved_source(f.db, started[0]["id"])
    # Every page goes to the address its plan gave it, whatever kind of page it is.
    assert source["source_kind"] == "article" and source["route"] == "/guides/useful-article"
    assert source["article"] == ARTICLE_BODY
    assert source["publish"]["mode"] == "pull_request"
    f.runtime.integrations.github_create_pull_request.assert_not_called()


async def test_a_draft_from_before_planned_addresses_names_none(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await approve_answer_page(f, await draft(f, kind="article", page_url=None))
    source = await select(f, run)
    # Its pull request names the address, and Tin merges nothing but the page itself.
    assert source["route"] is None and source["change"]["paths"] == []


async def test_a_page_already_committed_as_markdown_is_not_adapted(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await answer_page(f)
    await f.delivery.record_choice(
        run, {"settings": {"mode": "github_commit", "repository": "owner/site"}, "path": "x.md"}
    )
    run = await approve_answer_page(f, run)
    with pytest.raises(ValueError, match="already ships through its Markdown delivery"):
        await select(f, run)


# Billing: an ordinary metered session, and a refused admission the founder can retry.


async def billed(f, balance):
    from tin_lite.billing import BillingService
    from tin_lite.billing_contracts import ProjectSpendingPolicy

    f.settings.billing_test_enabled = True
    f.db.billing = BillingService(database=f.db, settings=f.settings)
    await f.db.pool.execute(
        "UPDATE workspaces SET created_by_clerk_user_id=$2 WHERE id=$1",
        f.project.workspace_id,
        ACTOR,
    )
    await f.db.pool.execute(
        "INSERT INTO workspace_memberships(workspace_id,clerk_user_id) "
        "VALUES($1,$2) ON CONFLICT DO NOTHING",
        f.project.workspace_id,
        ACTOR,
    )
    await f.db.billing.enroll_test(f.project.workspace_id, ACTOR)
    await f.db.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=10_000_000_000,
            monthly_nanos=20_000_000_000,
            expected_revision=0,
        ),
    )
    # Synthetic test funds; no Stripe or provider network calls.
    await f.db.pool.execute(
        "UPDATE billing_accounts SET balance_nanos=$2 WHERE workspace_id=$1",
        f.project.workspace_id,
        balance,
    )


async def test_adaptation_is_one_metered_session_with_a_cost_preview(publication_db, monkeypatch):
    from tin_lite.billing_contracts import object_value

    f = await fixture(publication_db, monkeypatch)
    await billed(f, 20_000_000_000)
    run = await answer_page(f)
    preview = await publish_preview(runtime=f.runtime, settings=f.settings, run=run, actor=ACTOR)
    assert preview["adapt"] is True and preview["mode"] == "github_pr"
    # website.change's own $4 ceiling and its content_draft p90 (2026-10-08), kept apart.
    assert (preview["cost"]["maximum_usd"], preview["cost"]["estimated_usd"]) == ("4.00", "0.60")
    assert preview["footer"] == (
        "Tin adapts it to your site and opens a pull request · about $0.60, at most $4"
    )
    await f.delivery.choose(run=run, mode="github_pr", actor=ACTOR)
    run = await approve_answer_page(f, run)
    await f.activities.deliver_content_draft(str(run.id))
    child = (await children(f, run))[0]["id"]
    terms = object_value(
        await f.db.pool.fetchval("SELECT terms FROM billing_run_budgets WHERE run_id=$1", child)
    )
    assert terms["kind"] == "codex_api" and terms["funding"] == "procedure_session_v1"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_quotes") == 0


async def test_insufficient_credits_record_a_failed_delivery_to_retry(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await answer_page(f)
    await billed(f, 0)
    await f.delivery.choose(run=run, mode="github_commit", actor=ACTOR)
    run = await approve_answer_page(f, run)
    with pytest.raises(ApplicationError) as refused:
        await f.activities.deliver_content_draft(str(run.id))
    assert refused.value.non_retryable
    assert not await children(f, run)
    status = await f.delivery.status(run)
    assert status["status"] == "failed" and "Add credits" in status["error"]
    assert "Prepare PR" in status["error"] and "stays in Tin" in status["error"]
    f.runtime.integrations.github_commit_files.assert_not_called()
    # After adding credits, retrying delivery tries the same start again.
    result = await retry_delivery(
        runtime=f.runtime, settings=f.settings, project_id=f.project.id, run_id=run.id
    )
    assert result["status"] == "pending"
    assert f.runtime.temporal.start_workflow.call_args.args == (
        "tin.content_draft_delivery",
        str(run.id),
    )
    await f.db.pool.execute(
        "UPDATE billing_accounts SET balance_nanos=$2 WHERE workspace_id=$1",
        f.project.workspace_id,
        20_000_000_000,
    )
    await f.activities.deliver_content_draft(str(run.id))
    assert len(await children(f, run)) == 1
    assert (await f.delivery.status(run))["status"] in {"pending", "started"}


# After the approval: the website.change run that opened the page's pull request.


async def adapted_pull_request(f, *, mode="github_commit", extra_files=(), route=None):
    """An approved answer page adapted by the website.change run its approval started, up to
    the opened pull request. `route` is the public URL the pull request names."""
    run = await answer_page(f)
    await f.delivery.choose(run=run, mode=mode, actor=ACTOR)
    run = await approve_answer_page(f, run)
    await f.activities.deliver_content_draft(str(run.id))
    child = await f.db.get_run((await children(f, run))[0]["id"])
    await f.activities.prepare_codex_procedure(str(child.id))
    await f.activities.create_codex_procedure_sandbox(str(child.id))
    child = await f.db.get_run(child.id)
    source = await delivery.saved_source(f.db, child.id)
    page = "---\ntitle: How to keep AI work reliable\n---\n\n" + source["article"]
    manifest = {
        "repository": "owner/site",
        "default_branch": "main",
        "head_sha": f.binding.head_sha,
        "title": "Add the reliable AI work page",
        "body": "Adds the approved page. Build not run.\n\n"
        + (f"Public URL: {route}\n" if route else "Public URL: unknown\n"),
        "files": [
            {"path": "content/pages/reliable-ai-work.md", "content": page},
            *extra_files,
        ],
        "verification": [delivery.CHECK_COMMAND],
    }
    path = procedure_checkpoint_path(child.id)
    head = f.storage.repo.head
    revision = f.storage.repo.edit({path: canonical_json(manifest)}, parent=child.expected_head_sha)
    f.storage.branch_revision, f.storage.repo.head = revision, head
    key = f"{child.id}:procedure_artifact_persist"
    async with f.db.effect_lock(key, "procedure_artifact_persist") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="procedure_artifact_persist")
        await f.db.complete_procedure_persist(
            conn,
            execution_key=key,
            run_id=child.id,
            result={"ephemeral_commit_sha": revision, "checkpoint_path": path},
            sandbox_killed=True,
        )
    await f.activities.commit_codex_procedure_artifact(str(child.id))
    await f.db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", child.id)
    return run, await f.db.get_run(child.id)


def clean(**extra):
    return {
        "state": "open",
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
        "url": None,
        "mergeable": True,
        "mergeable_state": "clean",
        "head_sha": "a" * 40,
        "head_ref": "tin/test",
        "base_ref": "main",
        "same_repository": True,
        **extra,
    }


async def test_retargeted_page_stays_open(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    _, child = await adapted_pull_request(f, route=PAGE_URL)
    f.runtime.integrations.github_pull_request_merge_state.return_value = clean(
        base_ref="release/elsewhere"
    )
    f.runtime.integrations.github_merge_pull_request.return_value = {"merged": False}
    await f.activities.deliver_content_draft(str(child.id))
    f.runtime.integrations.github_merge_pull_request.assert_not_called()
    result = (await f.db.get_effect(delivery.merge_key(child.id))).result
    assert result["status"] == "left_open" and "destination branch" in result["reason"]


# Page URLs and the card line.


async def test_page_url_is_the_planned_address_and_says_what_approval_does(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    run = await answer_page(f)
    pages = PageUrls(database=f.db, storage=f.storage, settings=f.settings)
    before = await pages.view(run, None)
    # The review shows where the page will be published: the address its plan gave it.
    assert before["url"] == PAGE_URL and before["source"] == "plan_destination"
    assert before["label"] == "Will be published at" and before["file_path"] is None
    assert before["note"] == (
        "After you approve, Tin adapts the page to your site and opens a pull request."
    )
    assert (await pages.views([run], {}))[run.id]["note"] == before["note"]
    legacy = await draft(f, kind="article", page_url=None)
    proposed = await pages.view(legacy, None)
    assert proposed["source"] == "title_slug" and proposed["label"] == "Proposed URL"
    run, child = await adapted_pull_request(f, mode="github_pr", route=PAGE_URL)
    status = await f.delivery.status(run)
    after = await pages.view(run, status)
    assert after["source"] == "delivery_route" and after["final"] is True
    assert after["url"] == PAGE_URL
    assert after["pull_request"]["number"] == 42
    assert after["note"].startswith("Pull request #42 is open.")


def test_card_cost_is_rounded_to_a_dollar_or_cents():
    assert about_usd("5.00") == "$5" and about_usd("1.50") == "$2"
    assert about_usd("0.45") == "$0.45" and about_usd("0") is None and about_usd(None) is None


async def test_publish_preview_follows_the_saved_delivery_setting(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await answer_page(f)
    preview = await publish_preview(runtime=f.runtime, settings=f.settings, run=run, actor=ACTOR)
    sentence = "Tin adapts it to your site and opens a pull request"
    assert preview == {
        "adapt": True,
        "label": "Publish",
        "mode": "github_pr",
        "repository": "owner/site",
        # The address the page's plan gave it, where website.change publishes it.
        "url": PAGE_URL,
        "sentence": sentence,
        "cost": None,
        "footer": sentence,
    }
    monkeypatch.setattr(
        "tin_lite.content_delivery.ContentDelivery.saved_mode",
        AsyncMock(return_value="github_commit"),
    )
    committed = await publish_preview(runtime=f.runtime, settings=f.settings, run=run, actor=ACTOR)
    assert committed["footer"] == "Tin adapts it to your site and commits it to main"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get(
            f"/api/projects/{f.project.id}/content-drafts/{run.id}/publish-preview"
        )
        assert response.status_code == 200 and response.json()["mode"] == "github_commit"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, "stranger")), base_url="https://tin.test"
    ) as client:
        denied = await client.get(
            f"/api/projects/{f.project.id}/content-drafts/{run.id}/publish-preview"
        )
    assert denied.status_code == 404


def test_publish_says_what_the_delivery_setting_does():
    from tin_lite.content_delivery import publish_sentence

    assert publish_sentence("github_commit") == "Tin adapts it to your site and commits it to main"
    assert publish_sentence("github_pr") == "Tin adapts it to your site and opens a pull request"
