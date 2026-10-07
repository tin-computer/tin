import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import yaml
from test_content_draft import article, fixture, start
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite.activities import TinActivities
from tin_lite.content_delivery import (
    ContentDelivery,
    DeliverySettings,
    article_body,
    choice_key,
    destination,
    display_title,
    github_key,
    render_file,
    settings_path,
)
from tin_lite.content_delivery_api import retry_delivery
from tin_lite.content_plan import item_kind
from tin_lite.domain import RunStatus
from tin_lite.integrations import (
    GitHubCommitResult,
    GitHubPullRequestResult,
    GitHubRepositoryBinding,
)
from tin_lite.organic_audit import canonical_json
from tin_lite.publication import OutputCheckpoint


def context():
    return {
        "program_id": str(uuid4()),
        "plan_revision": "a" * 40,
        "project_revision": "b" * 40,
        "due_date": "2026-09-14",
        "item": {
            "id": "one",
            "title": "Useful buyer task",
            "action": "new_page",
            "destination": "https://example.com/blog/useful-task",
            "verification": ["Check facts"],
        },
        "style": {"sha256": "c" * 64},
    }


def test_markdown_delivery_preserves_reviewed_article_and_separates_internal_material():
    ctx = context()
    raw = article(ctx)
    settings = DeliverySettings(
        mode="github_pr",
        repository="owner/site",
        frontmatter={
            "title": "{title}",
            "date": "{date}",
            "slug": "{slug}",
            "draft": True,
        },
    )
    path, content, title = render_file(raw, ctx, settings, None)
    assert path == "content/blog/useful-task.md"
    metadata, body = content[4:].split("\n---\n\n", 1)
    assert yaml.safe_load(metadata) == {
        "title": title,
        "date": "2026-09-14",
        "slug": "useful-task",
        "draft": True,
    }
    assert body == article_body(raw, ctx)[0]
    assert "Verification notes" not in content and "brief_sha256" not in content
    with pytest.raises(ValueError, match="already exists"):
        render_file(raw, ctx, settings, "Existing article")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_updates_require_exact_mapping_preserve_site_frontmatter(newline):
    ctx = context()
    ctx["item"]["action"] = "update_page"
    settings = DeliverySettings(mode="github_pr", repository="owner/site")
    with pytest.raises(ValueError, match="Map this existing-page"):
        destination(settings, ctx)
    settings = settings.model_copy(
        update={
            "item_paths": {"one": "docs/existing.md"},
            "frontmatter": {"title": "Not applied to updates"},
        }
    )
    header = newline.join(["---", "layout: post", "custom: preserved", "---", ""])
    _, rendered, _ = render_file(article(ctx), ctx, settings, header + "Old prose")
    assert rendered.startswith(header)
    assert rendered[len(header) :].strip() == article_body(article(ctx), ctx)[0].strip()
    with pytest.raises(ValueError, match="does not exist"):
        render_file(article(ctx), ctx, settings, None)
    with pytest.raises(ValueError, match="unsupported"):
        render_file(article(ctx), ctx, settings, "+++\nlayout = 'post'\n+++\nOld prose")


@pytest.mark.parametrize(
    "pattern",
    [
        "../{slug}.md",
        "content//{slug}.md",
        "{slug}.mdx",
        ".github/{slug}.md",
        "/{slug}.md",
        "content/{unknown}.md",
        "content/{slug}/{slug}.md",
    ],
)
def test_destination_validation(pattern):
    with pytest.raises(ValueError):
        DeliverySettings(path_pattern=pattern)


@pytest.fixture
def pinned_before_adaptation(monkeypatch):
    """A draft whose delivery was pinned before planned articles went through website.change:
    its intent names a Markdown file, so the exact Markdown publisher delivers it."""
    pin = ContentDelivery.pin

    async def legacy(self, *, project_id, selected):
        intent = await pin(self, project_id=project_id, selected=selected)
        if intent and intent.get("via") and item_kind(selected["item"]) == "article":
            settings = DeliverySettings.model_validate(intent["settings"])
            intent = {key: value for key, value in intent.items() if key not in {"adapter", "via"}}
            intent["path"] = destination(settings, selected)[0]
        return intent

    monkeypatch.setattr(ContentDelivery, "pin", legacy)


async def configured(f):
    f.binding = GitHubRepositoryBinding(uuid4(), 123, 456, "owner/site", "main", "9" * 40)
    f.runtime.integrations.github_repository_binding = AsyncMock(return_value=f.binding)
    f.runtime.integrations.github_markdown_file = AsyncMock(return_value=None)
    f.runtime.integrations.github_create_pull_request = AsyncMock(
        return_value=GitHubPullRequestResult(
            "owner/site", "tin/test", 42, "https://github.com/owner/site/pull/42"
        )
    )
    f.delivery = ContentDelivery(
        database=f.db, storage=f.storage, integrations=f.runtime.integrations
    )
    settings = DeliverySettings(mode="github_pr", repository="owner/site")
    await f.delivery.save_settings(
        project_id=f.project.id,
        program_id=f.configured.id,
        settings=settings,
        request_id=uuid4(),
        expected_revision=f.storage.repo.head,
        actor=ACTOR,
    )
    f.inputs["plan_revision"] = ""
    return f


async def publish_draft(f, run):
    ctx = await f.service.prepare(run)
    activities = TinActivities(
        database=f.db,
        storage=f.storage,
        settings=f.settings,
        integrations=f.runtime.integrations,
        sandboxes=SimpleNamespace(create=AsyncMock(return_value="sandbox-test"), kill=AsyncMock()),
    )
    await activities.create_codex_procedure_sandbox(str(run.id))
    run = await f.db.get_run(run.id)
    raw, path = article(ctx), f"content/drafts/{run.id}.md"
    base = f.storage.repo.head
    revision = f.storage.repo.edit({path: raw}, parent=run.expected_head_sha)
    f.storage.branch_revision, f.storage.repo.head = revision, base
    checkpoint = OutputCheckpoint.create(
        run=run, revision=revision, path=path, media_type="text/markdown", content=raw
    )
    key = f"{run.id}:procedure_artifact_persist"
    async with f.db.effect_lock(key, "procedure_artifact_persist") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="procedure_artifact_persist")
        await f.db.complete_procedure_persist(
            conn,
            execution_key=key,
            run_id=run.id,
            result={
                "checkpoint": checkpoint.to_dict(),
                "summary": "Draft ready",
                "sandbox_killed": True,
            },
            sandbox_killed=True,
        )
    await activities.commit_codex_procedure_artifact(str(run.id))
    await activities.request_codex_procedure_review(str(run.id))
    f.activities = activities
    return await f.db.get_run(run.id), ctx, raw


async def approve(f, run):
    await f.db.set_review_actor(run_id=run.id, clerk_user_id=ACTOR)
    await f.activities.record_codex_procedure_approval(str(run.id))
    await f.activities.project_codex_procedure_result(str(run.id))
    return await f.db.get_run(run.id)


@pytest.mark.usefixtures("pinned_before_adaptation")
async def test_review_then_exact_delivery_is_idempotent_and_pins_settings(
    publication_db, monkeypatch
):
    f = await configured(await fixture(publication_db, monkeypatch))
    run = await start(f, key=str(uuid4()))
    run, ctx, raw = await publish_draft(f, run)
    assert (await f.delivery.status(run))["status"] == "awaiting_review"
    explanation = await f.db.pool.fetchval(
        "SELECT explanation FROM run_decisions WHERE run_id=$1", run.id
    )
    assert "Approval opens an unmerged" in explanation
    # The decision and the run are labelled by the draft's heading, not its run-owned file name.
    title = article_body(raw, ctx)[1]
    items = json.loads(
        await f.db.pool.fetchval("SELECT items FROM run_decisions WHERE run_id=$1", run.id)
    )
    assert items[0]["title"] == title and items[0]["file"] == run.artifact_path
    assert run.artifact_title == title and str(run.id) not in title
    with pytest.raises(ValueError, match="Approve"):
        await f.delivery.deliver(run.id)
    f.runtime.integrations.github_create_pull_request.assert_not_called()
    run = await approve(f, run)
    # Later edits cannot replace the article or retarget an approved run.
    f.storage.repo.edit(
        {
            settings_path(f.configured.id): canonical_json(DeliverySettings().model_dump()),
            run.artifact_path: b"# A later user edit",
        }
    )
    before_calls = f.model.calls
    await asyncio.gather(f.delivery.deliver(run.id), f.delivery.deliver(run.id))
    f.runtime.integrations.github_create_pull_request.assert_awaited_once()
    call = f.runtime.integrations.github_create_pull_request.call_args.kwargs
    assert call["execution_key"] == github_key(run.id)
    assert call["files"][0].content == article_body(raw, ctx)[0]
    assert call["expected_binding"] == f.binding
    assert f.model.calls == before_calls
    assert (await f.db.get_run(run.id)).status == RunStatus.SUCCEEDED
    status = await f.delivery.status(run)
    assert status["status"] == "completed" and status["pull_request"]["number"] == 42
    # A saved workflow's "last result" carries the same label.
    await f.db.pool.execute(
        "UPDATE workflow_runs SET project_workflow_id=$2 WHERE id=$1", run.id, f.configured.id
    )
    saved = await f.db.list_project_workflows(project_id=f.project.id)
    assert [w.last_artifact_title for w in saved if w.last_run_id == run.id] == [title]
    assert (await f.delivery.statuses([run]))[run.id] == status
    facts = await f.service.programs.facts(f.configured.id)
    assert facts["drafts"][ctx["item"]["id"]]["delivery"]["pull_request"]["number"] == 42
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events WHERE run_id=$1 "
            "AND event_type='content_draft_pull_request_ready'",
            run.id,
        )
        == 1
    )


@pytest.mark.usefixtures("pinned_before_adaptation")
async def test_delivery_failure_retries_without_redrafting_or_retaking_snapshot(
    publication_db, monkeypatch
):
    f = await configured(await fixture(publication_db, monkeypatch))
    run, _, _ = await publish_draft(f, await start(f))
    run = await approve(f, run)
    f.runtime.integrations.github_create_pull_request.side_effect = httpx.ReadError(
        "lost provider response"
    )
    with pytest.raises(httpx.ReadError):
        await f.delivery.deliver(run.id)
    assert (await f.db.get_run(run.id)).status == RunStatus.SUCCEEDED
    assert (await f.delivery.status(run))["status"] == "failed"
    await retry_delivery(
        runtime=f.runtime, settings=f.settings, project_id=f.project.id, run_id=run.id
    )
    args = f.runtime.temporal.start_workflow.call_args
    assert args.args == ("tin.content_draft_delivery", str(run.id))
    f.runtime.integrations.github_create_pull_request.side_effect = None
    f.runtime.integrations.github_repository_binding.reset_mock()
    await f.delivery.deliver(run.id)
    f.runtime.integrations.github_repository_binding.assert_not_called()
    assert (await f.delivery.status(run))["status"] == "completed"


@pytest.mark.parametrize("historical", [False, True])
async def test_draft_only_or_old_definition_never_acquires_delivery(
    publication_db, monkeypatch, historical
):
    f = await configured(await fixture(publication_db, monkeypatch))
    if historical:
        definition = deepcopy(f.workflow.definition)
        del definition["input_schema"]["properties"]["delivery"]
        f.workflow = replace(f.workflow, definition=definition)
        inputs = f.inputs
    else:
        inputs = {**f.inputs, "delivery": "draft_only"}
    run = await start(f, inputs=inputs)
    assert await f.delivery.intent(run) is None
    await f.delivery.deliver(run.id)
    f.runtime.integrations.github_create_pull_request.assert_not_called()


@pytest.mark.usefixtures("pinned_before_adaptation")
async def test_http_mcp_settings_scope_and_reviewed_retry(publication_db, monkeypatch):
    f = await configured(await fixture(publication_db, monkeypatch))
    server = mcp(f, monkeypatch)
    args = {"project_id": str(f.project.id), "program_id": str(f.configured.id)}
    saved = structured(await server.call_tool("get_content_delivery_settings", args))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        path = f"/api/projects/{f.project.id}/content-programs/{f.configured.id}/delivery"
        assert (await client.get(path)).json() == saved
        payload = {
            "request_id": str(uuid4()),
            "expected_revision": saved["revision"],
            "settings": {**saved["settings"], "frontmatter": {"title": "{title}"}},
        }
        first = await client.put(path, json=payload)
        assert first.status_code == 200
        second = await client.put(path, json=payload)
        assert second.status_code == 200 and second.json()["replayed"]
        run = await start(f)
        assert (
            await client.post(
                f"/api/projects/{f.project.id}/content-drafts/{run.id}/delivery/retry"
            )
        ).status_code == 409
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, actor="stranger")), base_url="https://tin.test"
    ) as client:
        assert (await client.get(path)).status_code == 404
        assert (await client.put(path, json=payload)).status_code == 404
    result = structured(await server.call_tool("get_run", {"run_id": str(run.id)}))
    assert result["content_delivery"]["approval_label"] == "Approve & open PR"


@pytest.mark.usefixtures("pinned_before_adaptation")
async def test_changed_github_connection_cannot_redirect_reviewed_draft(
    publication_db, monkeypatch
):
    f = await configured(await fixture(publication_db, monkeypatch))
    run, _, _ = await publish_draft(f, await start(f))
    await approve(f, run)
    f.runtime.integrations.github_repository_binding.return_value = replace(
        f.binding, repository_id=999
    )
    with pytest.raises(ValueError, match="connection changed"):
        await f.delivery.deliver(run.id)
    f.runtime.integrations.github_create_pull_request.assert_not_called()


COMMIT_SHA = "f" * 40


async def commit_configured(f, mode="github_commit", repository="owner/site"):
    f.runtime.integrations.github_commit_files = AsyncMock(
        return_value=GitHubCommitResult(
            "owner/site", "main", COMMIT_SHA, f"https://github.com/owner/site/commit/{COMMIT_SHA}"
        )
    )
    await f.delivery.save_settings(
        project_id=f.project.id,
        program_id=f.configured.id,
        settings=DeliverySettings(mode=mode, repository=repository),
        request_id=uuid4(),
        expected_revision=f.storage.repo.head,
        actor=ACTOR,
    )
    return f


@pytest.mark.usefixtures("pinned_before_adaptation")
async def test_github_commit_mode_publishes_the_same_file_without_a_pull_request(
    publication_db, monkeypatch
):
    f = await commit_configured(await configured(await fixture(publication_db, monkeypatch)))
    run, ctx, raw = await publish_draft(f, await start(f))
    status = await f.delivery.status(run)
    assert status["approval_label"] == "Approve & publish" and status["mode"] == "github_commit"
    run = await approve(f, run)
    await asyncio.gather(f.delivery.deliver(run.id), f.delivery.deliver(run.id))
    f.runtime.integrations.github_create_pull_request.assert_not_called()
    f.runtime.integrations.github_commit_files.assert_awaited_once()
    call = f.runtime.integrations.github_commit_files.call_args.kwargs
    assert call["execution_key"] == github_key(run.id)
    assert call["files"][0].content == article_body(raw, ctx)[0]
    assert call["base_branch"] == "main" and call["expected_binding"] == f.binding
    assert "expected_base_sha" not in call  # The default branch may move; no branch is cut.
    status = await f.delivery.status(run)
    assert status["status"] == "completed" and status["pull_request"] is None
    assert status["commit"]["commit"] == COMMIT_SHA
    assert status["commit"]["path"] == call["files"][0].path
    assert (await f.delivery.statuses([run]))[run.id] == status
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events WHERE run_id=$1 "
            "AND event_type='content_draft_commit_ready'",
            run.id,
        )
        == 1
    )


async def test_approval_picks_delivery_for_one_draft_and_can_remember_it(
    publication_db, monkeypatch
):
    f = await commit_configured(
        await configured(await fixture(publication_db, monkeypatch)), mode="draft_only"
    )
    run, ctx, raw = await publish_draft(f, await start(f))
    assert await f.delivery.intent(run) is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        refused = await client.post(
            f"/api/workflows/runs/{run.id}/approve", json={"delivery": "elsewhere"}
        )
        assert refused.status_code == 422
        response = await client.post(
            f"/api/workflows/runs/{run.id}/approve",
            json={"delivery": "github_commit", "remember": True},
        )
    assert response.status_code == 202, response.text
    run = await f.db.get_run(run.id)
    assert run.review_decision == "approved"
    # The article goes to the site through website.change, not as a Markdown file: the pick
    # pins the repository and asks Tin to merge once the repository's checks pass.
    intent = await f.delivery.intent(run)
    assert intent["adapter"] == "repository" and intent["via"] == "website.change"
    assert intent["settings"]["mode"] == "github_commit"
    assert intent["settings"]["repository"] == "owner/site"
    assert intent["path"] is None and intent["repository_id"] == f.binding.repository_id
    remembered = await f.delivery.settings(project_id=f.project.id, program_id=f.configured.id)
    assert remembered["settings"]["mode"] == "github_commit"
    # The approval already happened; a later pick cannot retarget this draft.
    assert (await f.delivery.choose(run=run, mode="github_pr", actor=ACTOR)) == intent
    await f.activities.project_codex_procedure_result(str(run.id))
    await f.delivery.deliver(run.id)
    f.runtime.integrations.github_commit_files.assert_not_called()
    f.runtime.integrations.github_create_pull_request.assert_not_called()


async def test_a_new_draft_pins_website_change_for_a_repository_program(
    publication_db, monkeypatch
):
    f = await configured(await fixture(publication_db, monkeypatch))
    run, _, _ = await publish_draft(f, await start(f))
    intent = await f.delivery.intent(run)
    assert intent["adapter"] == "repository" and intent["via"] == "website.change"
    assert intent["path"] is None and intent["settings"]["mode"] == "github_pr"
    status = await f.delivery.status(run)
    assert status["adapter"] == "repository" and status["approval_label"] == "Publish"
    run = await approve(f, run)
    await f.delivery.deliver(run.id)  # The Markdown publisher leaves it to website.change.
    f.runtime.integrations.github_create_pull_request.assert_not_called()


async def test_approval_can_keep_a_draft_in_tin_or_open_a_pull_request(publication_db, monkeypatch):
    f = await configured(await fixture(publication_db, monkeypatch))
    f.runtime.integrations.github_commit_files = AsyncMock()
    run, _, _ = await publish_draft(f, await start(f))
    assert (await f.delivery.intent(run))["settings"]["mode"] == "github_pr"
    # A pick before approval may still change; "none" keeps the approved copy in Tin.
    await f.delivery.choose(run=run, mode="github_pr", actor=ACTOR)
    kept = await f.delivery.choose(run=run, mode="none", actor=ACTOR)
    assert kept["settings"]["mode"] == "draft_only" and kept["path"] is None
    assert (await f.db.get_effect(choice_key(run.id))).result == kept
    assert await f.delivery.intent(run) is None
    assert await f.delivery.status(run) is None
    program = await f.delivery.settings(project_id=f.project.id, program_id=f.configured.id)
    assert program["settings"]["mode"] == "github_pr"  # Not remembered: the program stays.
    run = await approve(f, run)
    await f.delivery.deliver(run.id)
    f.runtime.integrations.github_create_pull_request.assert_not_called()
    f.runtime.integrations.github_commit_files.assert_not_called()


def test_display_title_is_a_bounded_single_line_label_or_nothing():
    assert display_title(b"---\nx: 1\n---\n# A **useful** `guide`\n\nBody") == "A useful guide"
    assert len(display_title(b"# " + b"long " * 100)) <= 160
    assert display_title(b"No heading here") is None
    assert display_title(b"# \x00\x07") is None
    assert display_title(b"\xff\xfe") is None
