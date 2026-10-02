"""Approved answer pages and public articles become pages on the site through content.deliver.

Approval with a GitHub repository (and Codex API execution) starts one metered adaptation,
never the Markdown publisher. The founder's commit-to-main setting merges a page-only PR once
GitHub calls it clean; anything else stays an open PR that says why.
"""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from test_private_workflows import ACTOR, app, mcp, structured
from test_private_workflows import fixture as private_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import approved_document
from tin_lite import content_repository_delivery as delivery
from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.content_delivery import (
    ANSWER_PAGE_WORKFLOW_ID,
    PUBLIC_ARTICLE_WORKFLOW_ID,
    ContentDelivery,
    about_usd,
    adaptation_start_key,
    choice_key,
    delivery_key,
)
from tin_lite.content_delivery_api import publish_preview, retry_delivery
from tin_lite.integrations import GitHubPullRequestResult, GitHubRepositoryBinding
from tin_lite.organic_audit import canonical_json
from tin_lite.page_urls import PageUrls, present
from tin_lite.procedures import procedure_checkpoint_path
from tin_lite.workflow_reviews import WorkflowReviews

KEYS = ("content.answer_page", "content.public_article", "content.deliver")
LISTING = (
    "---\nmeta_title: How to keep AI work reliable\n"
    "meta_description: Keep recurring AI work reliable with durable runs, receipts and "
    "a review before anything ships.\n---\n\n"
)
ANSWER_BODY = (
    "# How to keep recurring AI work reliable\n\nLast updated: 2026-09-28\n\n"
    + " ".join(["Durable runs keep receipts, so a retry never repeats a paid call."] * 6)
    + "\n"
)
ARTICLE_BODY = (
    "# A useful public article\n\n"
    + " ".join(["Keep this useful explanation and its source."] * 12)
    + "\n"
)
PR_URL = "https://github.com/owner/site/pull/42"


async def fixture(db, monkeypatch, *, api=True):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda details: None)
    f = await private_fixture(db)
    if not api:
        f.settings.codex_api_projects = set()
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


async def answer_page(f, *, body=ANSWER_BODY, project=None):
    """A reviewed answer page waiting for approval, saved the way the workflow saves it."""
    project = project or f.project
    run, _ = await f.db.create_run(
        project_id=project.id,
        workflow_id=ANSWER_PAGE_WORKFLOW_ID,
        started_by_clerk_user_id=ACTOR,
        definition_commit_sha="d" * 40,
        pinned_definition=f.definitions["content.answer_page"],
    )
    path = f"content/answers/2026-09-28-reliable-ai-work-{run.id.hex[:6]}.md"
    revision = f.storage.repo.edit({path: (LISTING + body).encode()})
    key = f"{run.id}:answer_page_commit"
    async with f.db.effect_lock(key, "answer_page_commit") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="answer_page_commit")
        await f.db.complete_effect(
            conn,
            execution_key=key,
            result={"canonical_commit_sha": revision, "artifact_path": path, "title": "Reliable"},
        )
    await f.db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=revision,
        artifact_path=path,
        artifact_ref=f"code.storage://{project.state_repo_id}@{revision}/{path}",
        artifact_title="How to keep recurring AI work reliable",
    )
    return await f.db.get_run(run.id)


async def approve_answer_page(f, run):
    await f.db.set_review_actor(run_id=run.id, clerk_user_id=ACTOR)
    await f.db.record_human_review(run_id=run.id, decision="approved")
    await f.db.project_success(
        run_id=run.id,
        canonical_commit_sha=run.canonical_commit_sha,
        artifact_ref=run.artifact_ref,
        artifact_path=run.artifact_path,
    )
    return await f.db.get_run(run.id)


async def public_article(f, *, body=ARTICLE_BODY):
    run, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=PUBLIC_ARTICLE_WORKFLOW_ID,
        started_by_clerk_user_id=ACTOR,
        definition_commit_sha="d" * 40,
        pinned_definition=f.definitions["content.public_article"],
        input_payload={"brief": "Explain a practical buyer problem."},
    )
    path = f"content/articles/{run.id}.md"
    raw = (LISTING + body).encode()
    revision = f.storage.repo.edit({path: raw})
    key = f"{run.id}:procedure_canonical_commit"
    async with f.db.effect_lock(key, "procedure_canonical_commit") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="procedure_canonical_commit")
        await f.db.complete_effect(
            conn,
            execution_key=key,
            result={
                "canonical_commit_sha": revision,
                "artifact_path": path,
                "checkpoint": {"sha256": hashlib.sha256(raw).hexdigest()},
            },
        )
    await f.db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=revision,
        artifact_path=path,
        artifact_ref=f"code.storage://{f.project.state_repo_id}@{revision}/{path}",
        artifact_title="A useful public article",
    )
    return await f.db.get_run(run.id)


async def approve_article(f, run):
    reviews = WorkflowReviews(runtime=f.runtime, settings=f.settings)
    view = await reviews.view(run.id, ACTOR)
    await reviews.approve(run_id=run.id, actor=ACTOR, token=view["review_token"])
    await f.db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", run.id)
    return await f.db.get_run(run.id)


async def children(f, source):
    return await f.db.pool.fetch(
        "SELECT r.id, r.status FROM workflow_runs r JOIN effect_receipts s "
        "ON s.execution_key='content-delivery:' || r.id::text || ':source' "
        "WHERE r.workflow_id=$1 AND s.result->>'source_run_id'=$2",
        delivery.WORKFLOW_ID,
        str(source.id),
    )


async def select(f, run, **inputs):
    return await delivery.select_source(
        database=f.db,
        storage=f.storage,
        integrations=f.runtime.integrations,
        project_id=f.project.id,
        inputs={"source_run_id": str(run.id), "expected_repository": "owner/site", **inputs},
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
    assert source["title"] == "How to keep recurring AI work reliable"
    assert source["source_revision"] == run.canonical_commit_sha
    assert source["binding"]["repository"] == "owner/site"
    assert "approval" not in source  # Only the approval's own start carries a delivery.


async def test_public_article_source_needs_its_exact_review_token(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await approve_article(f, await public_article(f))
    source = await select(f, run)
    assert source["source_kind"] == "public_article"
    assert source["article"] == ARTICLE_BODY and source["page_metadata"]["meta_title"]
    async with f.db.pool.acquire() as conn:
        await approved_document.guard(conn, project_id=f.project.id, source=source)
    await f.db.pool.execute(
        "UPDATE workflow_review_commands SET review_token=$2 WHERE source_run_id=$1",
        run.id,
        "0" * 64,
    )
    with pytest.raises(ValueError, match="review proof"):
        await select(f, run)
    async with f.db.pool.acquire() as conn:
        with pytest.raises(ValueError, match="review proof"):
            await approved_document.guard(conn, project_id=f.project.id, source=source)


async def test_changed_bytes_or_revision_drift_refuse_the_page(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await approve_article(f, await public_article(f))
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
            await approved_document.guard(conn, project_id=f.project.id, source=source)


async def test_unapproved_foreign_or_oversized_pages_are_refused(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    waiting = await answer_page(f)
    with pytest.raises(ValueError, match="approve"):
        await select(f, waiting)
    other = await f.db.create_project(name="Another site", state_repo_id="projects/another")
    foreign = await approve_answer_page(f, await answer_page(f, project=other))
    with pytest.raises(ValueError, match="from this project"):
        await select(f, foreign)
    huge = await approve_answer_page(f, await answer_page(f, body=ANSWER_BODY + "x" * 150_000))
    with pytest.raises(ValueError, match="150 KB"):
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
    assert choice["trigger_source"] == ("mcp" if surface == "mcp" else "manual")
    run = await approve_answer_page(f, run)
    for _ in range(2):
        await f.activities.deliver_content_draft(str(run.id))
    started = await children(f, run)
    assert len(started) == 1
    child = await f.db.get_run(started[0]["id"])
    assert child.started_by_clerk_user_id == ACTOR
    assert child.input["source_run_id"] == str(run.id)
    assert await f.db.run_start_idempotency_key(child.id) == adaptation_start_key(run.id)
    source = await delivery.saved_source(f.db, child.id)
    assert source["approval"] == {"mode": "github_commit", "requested_by": ACTOR}
    # The Markdown publisher never ran, and its receipt key belongs to the adaptation.
    f.runtime.integrations.github_commit_files.assert_not_called()
    f.runtime.integrations.github_create_pull_request.assert_not_called()
    with pytest.raises(Exception, match="content_draft_adaptation_v1"):
        async with f.db.effect_lock(delivery_key(run.id), "content_draft_delivery_v1"):
            pass
    status = await f.delivery.status(run)
    assert status["adapter"] == "repository" and status["run_id"] == str(child.id)
    assert (await f.delivery.statuses([run]))[run.id] == status


async def test_public_article_approval_adapts_and_keeps_its_review(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await public_article(f)
    await f.delivery.choose(run=run, mode="github_pr", actor=ACTOR, adapt=True)
    run = await approve_article(f, run)
    await f.activities.deliver_content_draft(str(run.id))
    started = await children(f, run)
    assert len(started) == 1
    source = await delivery.saved_source(f.db, started[0]["id"])
    assert source["source_kind"] == "public_article"
    assert source["approval"]["mode"] == "github_pr"
    f.runtime.integrations.github_create_pull_request.assert_not_called()


async def test_without_api_execution_publish_keeps_the_markdown_publisher(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch, api=False)
    run = await answer_page(f)
    assert (await publish_preview(runtime=f.runtime, settings=f.settings, run=run, actor=ACTOR))[
        "adapt"
    ] is False
    # Not adapted: the generic pick needs a saved workflow, exactly as before.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.post(
            f"/api/workflows/runs/{run.id}/approve", json={"delivery": "github_commit"}
        )
    assert response.status_code == 409 and "Save this workflow" in response.text
    assert not await children(f, run)


async def test_prepare_pr_is_refused_for_a_page_already_committed_as_markdown(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    run = await answer_page(f)
    await f.delivery.record_choice(
        run, {"settings": {"mode": "github_commit", "repository": "owner/site"}, "path": "x.md"}
    )
    run = await approve_answer_page(f, run)
    with pytest.raises(ValueError, match="already has automatic delivery"):
        await select(f, run)
    # An approval start with no adaptation choice cannot carry a delivery setting.
    unchosen = await approve_answer_page(f, await answer_page(f))
    with pytest.raises(ValueError, match="did not ask"):
        await delivery.select_source(
            database=f.db,
            storage=f.storage,
            integrations=f.runtime.integrations,
            project_id=f.project.id,
            inputs={"source_run_id": str(unchosen.id), "expected_repository": "owner/site"},
            approval=True,
        )


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
    assert preview["cost"]["estimated_usd"] == "10.00"
    assert preview["footer"] == "Tin adapts it to your site and opens a pull request · up to $10"
    await f.delivery.choose(run=run, mode="github_pr", actor=ACTOR, adapt=True)
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
    await f.delivery.choose(run=run, mode="github_commit", actor=ACTOR, adapt=True)
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


# After the PR: merge only a page-only PR that GitHub calls clean.


async def adapted_pull_request(
    f, *, mode="github_commit", extra_files=(), route=None, chosen_route=None
):
    if chosen_route:
        # The founder's saved choice, as save_page_route writes it.
        from tin_lite.page_routes import PATH

        f.storage.repo.edit({PATH: canonical_json({"routes": {"answer_page": chosen_route}})})
    run = await answer_page(f)
    await f.delivery.choose(run=run, mode=mode, actor=ACTOR, adapt=True)
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
            {"path": "content/answers/reliable-ai-work.md", "content": page},
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


async def test_commit_setting_merges_a_clean_page_only_pull_request(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run, child = await adapted_pull_request(f, route="https://example.com/answers/reliable")
    integrations = f.runtime.integrations
    integrations.github_pull_request_merge_state.side_effect = [
        clean(mergeable=None, mergeable_state="unknown"),
        clean(),
    ]
    integrations.github_merge_pull_request.return_value = {
        "merged": True,
        "commit": "b" * 40,
        "url": f"https://github.com/owner/site/commit/{'b' * 40}",
    }
    naps = []

    async def nap(seconds):
        naps.append(seconds)

    monkeypatch.setattr(delivery.asyncio, "sleep", nap)
    for _ in range(2):  # The recorded outcome is reused; GitHub is asked to merge once.
        await f.activities.deliver_content_draft(str(child.id))
    integrations.github_merge_pull_request.assert_awaited_once()
    call = integrations.github_merge_pull_request.call_args.kwargs
    assert call["number"] == 42 and call["expected_head_sha"] == "a" * 40
    assert call["execution_key"] == f"{child.id}:procedure_pull_request_merge"
    assert [item.path for item in call["files"]] == ["content/answers/reliable-ai-work.md"]
    assert call["new_paths"] == ("content/answers/reliable-ai-work.md",)
    assert naps == [delivery.MERGE_POLL_SECONDS]
    merge = (await f.db.get_effect(delivery.merge_key(child.id))).result
    assert merge["status"] == "merged" and merge["commit"] == "b" * 40
    status = await f.delivery.status(run)
    assert status["merged"] is True and status["pull_request"]["number"] == 42
    assert status["public_route"] == "https://example.com/answers/reliable"
    events = await f.db.pool.fetch(
        "SELECT run_id FROM activity_events WHERE event_type='content_delivery_merged'"
    )
    assert {row["run_id"] for row in events} == {run.id, child.id}
    page = present(
        {
            "base": {
                "url": "https://example.com/x",
                "host": "example.com",
                "title": "T",
                "source": "title_slug",
                "final": False,
            }
        },
        status,
    )
    assert page["source"] == "delivery_route" and page["state"] == "merged"
    assert page["url"] == "https://example.com/answers/reliable"
    assert page["note"] == "Merged into owner/site. Waiting for your site to deploy it."


async def test_retargeted_page_stays_open(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    _, child = await adapted_pull_request(f)
    f.runtime.integrations.github_pull_request_merge_state.return_value = clean(
        base_ref="release/elsewhere"
    )
    f.runtime.integrations.github_merge_pull_request.return_value = {"merged": False}
    await f.activities.deliver_content_draft(str(child.id))
    f.runtime.integrations.github_merge_pull_request.assert_not_called()
    result = (await f.db.get_effect(delivery.merge_key(child.id))).result
    assert result["status"] == "left_open" and "destination branch" in result["reason"]


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        ("dirty", "It conflicts with the default branch."),
        ("blocked", "GitHub needs a review or a required check before it can merge"),
        ("unstable", "Its checks had not all passed after a few minutes"),
    ],
)
async def test_an_unmergeable_pull_request_stays_open_and_says_why(
    publication_db, monkeypatch, verdict, reason
):
    f = await fixture(publication_db, monkeypatch)
    run, child = await adapted_pull_request(f)
    monkeypatch.setattr(delivery, "MERGE_WAIT_SECONDS", 0)
    monkeypatch.setattr(delivery.asyncio, "sleep", AsyncMock())
    f.runtime.integrations.github_pull_request_merge_state.return_value = clean(
        mergeable=verdict != "dirty", mergeable_state=verdict
    )
    await f.activities.deliver_content_draft(str(child.id))
    f.runtime.integrations.github_merge_pull_request.assert_not_called()
    merge = (await f.db.get_effect(delivery.merge_key(child.id))).result
    assert merge["status"] == "left_open" and merge["reason"].startswith(reason)
    status = await f.delivery.status(run)
    assert status["merged"] is False and status["status"] == "completed"
    page = present(
        {
            "base": {
                "url": "https://example.com/x",
                "host": "example.com",
                "title": "T",
                "source": "title_slug",
                "final": False,
            }
        },
        status,
    )
    assert page["state"] == "proposed" and reason in page["note"]


async def test_a_pull_request_with_site_code_is_never_merged(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    route = {"path": "src/app/answers/[slug]/page.tsx", "content": "export default 1;\n"}
    _, child = await adapted_pull_request(f, extra_files=(route,))
    await f.activities.deliver_content_draft(str(child.id))
    f.runtime.integrations.github_pull_request_merge_state.assert_not_called()
    f.runtime.integrations.github_merge_pull_request.assert_not_called()
    merge = (await f.db.get_effect(delivery.merge_key(child.id))).result
    assert merge["status"] == "left_open" and "site files besides the page" in merge["reason"]


async def test_pull_request_setting_never_merges(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run, child = await adapted_pull_request(f, mode="github_pr")
    await f.activities.deliver_content_draft(str(child.id))
    f.runtime.integrations.github_pull_request_merge_state.assert_not_called()
    assert await f.db.get_effect(delivery.merge_key(child.id)) is None
    status = await f.delivery.status(run)
    assert status["pull_request"]["url"] == PR_URL and status["merge"] is None


# Page URLs and the card line.


async def test_page_url_uses_the_adaptation_route_and_says_what_approval_does(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    run = await answer_page(f)
    pages = PageUrls(database=f.db, storage=f.storage, settings=f.settings)
    base = {
        "url": "https://example.com/how-to-keep-recurring-ai-work-reliable",
        "host": "example.com",
        "title": "How to keep recurring AI work reliable",
        "source": "title_slug",
        "final": False,
    }
    await pages._save(run.id, {"base": base})
    before = await pages.view(run, None)
    assert before["label"] == "Proposed URL" and before["file_path"] is None
    assert before["note"] == (
        "After you approve, Tin adapts the page to your site and opens a pull request."
    )
    assert (await pages.views([run], {}))[run.id]["note"] == before["note"]
    run, child = await adapted_pull_request(f, route="https://example.com/answers/reliable-ai-work")
    await pages._save(run.id, {"base": base})
    status = await f.delivery.status(run)
    after = await pages.view(run, status)
    assert after["source"] == "delivery_route" and after["final"] is True
    assert after["url"] == "https://example.com/answers/reliable-ai-work"
    assert after["pull_request"]["number"] == 42
    assert after["note"].startswith("Pull request #42 is open.")


def test_card_cost_is_rounded_to_a_dollar_or_cents():
    assert about_usd("5.00") == "$5" and about_usd("1.50") == "$2"
    assert about_usd("0.45") == "$0.45" and about_usd("0") is None and about_usd(None) is None


async def test_publish_preview_follows_the_saved_delivery_setting(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await answer_page(f)
    preview = await publish_preview(runtime=f.runtime, settings=f.settings, run=run, actor=ACTOR)
    ask = preview.pop("ask_the_founder")
    assert ask["question"] == "Where on your site should pages that answer buyer questions go?"
    assert preview == {
        "adapt": True,
        "label": "Publish",
        "mode": "github_pr",
        "repository": "owner/site",
        "route": None,
        "sentence": "Tin adapts it to your site and opens a pull request",
        "cost": None,
        "footer": "Tin adapts it to your site and opens a pull request",
    }
    f.delivery.saved_mode = AsyncMock(return_value="github_commit")
    monkeypatch.setattr(
        "tin_lite.content_delivery.ContentDelivery.saved_mode",
        AsyncMock(return_value="github_commit"),
    )
    committed = await publish_preview(runtime=f.runtime, settings=f.settings, run=run, actor=ACTOR)
    # With no chosen route, a commit-to-main setting still leaves the first pull request open.
    assert committed["footer"] == (
        "Tin adapts it to your site and opens a pull request, "
        "since you have not chosen where these pages live yet"
    )
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
    shown = structured(await mcp(f, monkeypatch).call_tool("get_run", {"run_id": str(run.id)}))
    assert shown["delivery_preview"]["footer"] == committed["footer"]


async def test_discovery_lists_approved_pages_by_title(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    page = await approve_answer_page(f, await answer_page(f))
    article = await approve_article(f, await public_article(f))
    found = await delivery.discover(f.db, f.project.id)
    assert {"run_id": str(page.id), "title": "How to keep recurring AI work reliable"} in found[
        "articles"
    ]
    assert {"run_id": str(article.id), "title": "A useful public article"} in found["articles"]
    assert found["repository"] == "owner/site"
    assert json.dumps(found)  # Plain data for MCP preparation.


def test_publish_says_pull_request_until_the_founder_chooses_a_route():
    from tin_lite.content_delivery import publish_sentence

    # Until the founder chooses where these pages live, a commit-to-main setting still leaves
    # the first pull request open; a pull-request setting reads the same either way.
    assert publish_sentence("github_commit") == "Tin adapts it to your site and commits it to main"
    assert publish_sentence("github_commit", route_missing=True) == (
        "Tin adapts it to your site and opens a pull request, "
        "since you have not chosen where these pages live yet"
    )
    assert publish_sentence("github_pr", route_missing=True) == publish_sentence("github_pr")
