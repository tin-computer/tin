"""One content.generate drafts every plan item: an article, an answer page or a page refresh.

Emre's v2 organic map. content.generate 1.9.0 absorbs content.answer_page and content.refresh:
an answer keeps the answer-page quality rules and reaches the site through website.change at
the founder's route; a refresh proposes content.refresh's exact replacements, which the refresh
applier changes in the site's source after approval. Articles draft exactly as before.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from test_content_draft import fixture as draft_fixture
from test_content_draft import start
from test_content_editorial_judgment import judgment, judgment_notes
from test_content_plan import item
from test_content_refresh import EVIDENCE, GOOD, PAGE, PAGE_TSX, audit, document, finding
from test_private_workflows import ACTOR as PRIVATE_ACTOR
from test_private_workflows import app, mcp, structured
from test_procedure_publication import publication_db as publication_db
from test_technical_title_repair import archive

from tin_lite import approved_article, content_draft, website_change
from tin_lite import content_refresh as refresh
from tin_lite import content_repository_delivery as repository_delivery
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.content_delivery import ContentDelivery, delivery_key
from tin_lite.content_draft_sources import ContentDraftSources
from tin_lite.content_editorial_judgment import assessment_document, validate_pair
from tin_lite.content_plan import KINDS, plan_path
from tin_lite.content_refresh_sources import ContentRefreshSources
from tin_lite.integrations import (
    GitHubCommitResult,
    GitHubPullRequestResult,
    GitHubRepositoryBinding,
)
from tin_lite.organic_audit import audit_paths, canonical_json
from tin_lite.page_routes import PATH as ROUTES_PATH
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_inputs import WorkflowInputError

ACTOR = "user_test"
NOW = datetime(2026, 9, 29, 16, tzinfo=UTC)
ROUTE = "/blog/{slug}"
PAGE_URL = "https://example.com/pricing"
QUESTION = "Which API sends iMessages from an app?"
DESCRIPTION = (
    "Example sends iMessages from your app with one API call, reports delivery and replies, "
    "and needs no Mac."
)
ANSWER = f"""---
meta_title: "{QUESTION}"
meta_description: "{DESCRIPTION}"
---

# {QUESTION}

Last updated: 2026-09-29

Example sends iMessages from your app through one API call. You create a line, send a
message with a single request, and receive delivery status and replies by webhook. It runs on
hardware Example operates, so you need no Mac of your own. Pricing is per line, with messages
included up to the plan's limit.

## How does Example send an iMessage?

Your server calls the [send endpoint](https://example.com/docs/send) with the recipient and
the text. Example queues it on your line and reports delivery by webhook.

## What does it cost?

Each line is billed monthly, as the [pricing page](https://example.com/pricing) lists.

## FAQ

### Do I need a Mac?

No. Example runs the hardware, so your app only calls the API.

### Can my app read replies?

Yes. Replies arrive at your webhook with the sender and the text.

## Sources

- [Send endpoint](https://example.com/docs/send)
- [Pricing](https://example.com/pricing)
- [Apple iMessage support](https://support.apple.com/messages)
"""


def spec(key):
    return next(w for w in BUILTIN_WORKFLOWS if w.key == key)


class Reader:
    def __init__(self, hosts):
        self.hosts, self.urls = hosts, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, *, max_bytes):
        self.urls.append(url)
        return {"status": "observed", "status_code": 200, "body": PAGE.encode(), "charset": "utf-8"}


async def fixture(db, monkeypatch, *, route=True, findings=None, first="answer"):
    """A content program whose plan holds an answer, a refresh and an article, beside a
    finished audit. content.generate is the current 1.9.0 definition."""
    f = await draft_fixture(db, monkeypatch, judgment=True)
    for key in ("organic.audit", refresh.KEY, website_change.KEY):
        workflow = spec(key)
        await db.upsert_registry_workflow(
            workflow_id=workflow.id,
            key=workflow.key,
            title=workflow.title,
            description=workflow.description,
            executor=workflow.executor,
            definition_repo_id="registry/workflows",
            definition_path=workflow.definition_path,
            current_commit_sha="e" * 40,
            version_label=workflow.version_label,
            definition=workflow.definition,
        )
    audit_run, _ = await db.create_run(
        project_id=f.project.id, workflow_id=spec("organic.audit").id, input_payload={}
    )
    paths = audit_paths(str(audit_run.id))
    findings = findings or audit(finding("search.low_ctr", "/pricing"))
    revision = f.storage.repo.edit(
        {
            paths["findings.json"]: canonical_json(findings),
            paths["evidence.json"]: canonical_json(EVIDENCE),
        }
    )
    await db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, "
        "finished_at=now(), lease_active=false WHERE id=$1",
        audit_run.id,
        revision,
    )
    plan = (await f.service.programs.read(project_id=f.project.id, program_id=f.configured.id))[
        "plan"
    ]
    article = plan["batches"][0]["items"][0]
    answer = {**item("answer_gap"), "title": QUESTION, "kind": "answer"}
    page = {
        **item("refresh_pricing"),
        "kind": "refresh",
        "action": "update_page",
        "destination": PAGE_URL,
    }
    plan["batches"][0]["items"] = (
        [answer, page, article] if first == "answer" else [page, answer, article]
    )
    edits = {plan_path(f.configured.id): canonical_json(plan)}
    if route:
        edits[ROUTES_PATH] = canonical_json({"routes": {"answer_page": ROUTE}})
    f.storage.repo.edit(edits)
    f.console = AsyncMock(return_value={"rows": []})
    f.integrations = SimpleNamespace(
        search_console_analytics=f.console,
        github_pull_request_state=AsyncMock(
            return_value={"state": "open", "merged": False, "merged_at": None}
        ),
    )
    f.refreshes = ContentRefreshSources(
        database=db,
        storage=f.storage,
        integrations=f.integrations,
        reader=Reader,
        clock=lambda: NOW,
    )
    f.sources = ContentDraftSources(database=db, storage=f.storage, refreshes=f.refreshes)
    f.ids = {"answer": answer["id"], "refresh": page["id"], "article": article["id"]}
    return f


def inputs(f, kind=None):
    values = {"program_id": str(f.configured.id)}
    return {**values, "item_id": f.ids[kind]} if kind else values


async def prepared(f, kind, *, kinds=KINDS):
    run = await start(f, inputs=inputs(f, kind))
    context = await f.sources.prepare(
        run,
        output_validator=content_draft.EDITORIAL_VALIDATOR,
        positioning=True,
        kinds=kinds,
    )
    return run, context


async def save(db, key, operation, result):
    async with db.effect_lock(key, operation) as (conn, _):
        await db.start_effect(conn, execution_key=key, operation=operation)
        await db.complete_effect(conn, execution_key=key, result=result)


async def publish(f, run, primary):
    """Save the run's reviewed page the way the procedure publishes it, for review."""
    path = content_draft.PATH_TEMPLATE.format(run_id=run.id)
    revision = f.storage.repo.edit({path: primary})
    await save(
        f.db,
        f"{run.id}:procedure_canonical_commit",
        "procedure_canonical_commit",
        {
            "canonical_commit_sha": revision,
            "artifact_path": path,
            "checkpoint": {"sha256": hashlib.sha256(primary).hexdigest()},
        },
    )
    await f.db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=revision,
        artifact_path=path,
        artifact_ref=f"code.storage://{f.project.state_repo_id}@{revision}/{path}",
    )
    return await f.db.get_run(run.id)


async def approve(f, run):
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


def binding():
    return GitHubRepositoryBinding(
        UUID("00000000-0000-4000-8000-0000000000aa"), 22, 11, "acme/site", "main", "b" * 40
    )


def connected(f, monkeypatch):
    monkeypatch.setattr(
        f.db,
        "get_integration_connection",
        AsyncMock(
            return_value=SimpleNamespace(
                status="connected", configuration={"selected_repository": "acme/site"}
            )
        ),
    )
    f.integrations.github_repository_binding = AsyncMock(return_value=binding())


# The definition: three kinds, one review policy each, and older pins draft articles only.


def test_the_definition_drafts_three_kinds_with_a_review_policy_each():
    definition = spec(content_draft.KEY).definition
    assert definition["version"] == "1.9.0"
    assert content_draft.supported_kinds(definition) == ("article", "answer", "refresh")
    assert definition["human_review"]["review_label"] == "Review article"
    kinds = definition["human_review_kinds"]
    assert kinds["answer"]["review_label"] == "Review answer page"
    assert kinds["refresh"]["review_label"] == "Review refresh"
    assert all(policy["revision_adapter"] == "content-revision.v1" for policy in kinds.values())
    # The output contract is the editorial pair it was; the sandbox image needs no change.
    assert definition["procedure"]["output"]["validator"] == content_draft.EDITORIAL_VALIDATOR
    # A definition pinned before kinds existed (1.8.0) drafts articles only.
    assert content_draft.supported_kinds({"version": "1.8.0"}) == ("article",)


# Each kind drafts and validates.


async def test_each_kind_is_selected_and_prepared_with_its_own_sources(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    discovery = await f.sources.discover(project_id=f.project.id, program_id=f.configured.id)
    assert [i["kind"] for i in discovery["items"]] == ["answer", "refresh", "article"]
    assert discovery["next"]["item_id"] == f.ids["answer"]

    answer_run, answer = await prepared(f, "answer")
    selection = await f.sources.selection(answer_run.id)
    assert selection["page_route"] == ROUTE
    assert answer["kind"] == "answer" and answer["answer"]["question"] == QUESTION
    assert answer["answer"]["route"] == ROUTE

    page_run, page = await prepared(f, "refresh")
    assert page["kind"] == "refresh"
    assert page["refresh"]["page"]["url"] == PAGE_URL
    assert page["refresh"]["page"]["checks"] == ["search.low_ctr"]
    assert page["refresh"]["current"]["title"] == "Setup & configuration | Example"
    # The refresh receipt is content.refresh's, so the wait, results and applier see it.
    assert await f.refreshes.saved(page_run.id) == page["refresh"]

    _, article = await prepared(f, "article")
    # An article's context is exactly what earlier versions prepared.
    assert not {"kind", "answer", "refresh"} & article.keys()


def test_an_answer_draft_keeps_the_answer_page_quality_rules():
    context = {
        "output_validator": content_draft.EDITORIAL_VALIDATOR,
        "kind": "answer",
        "program_id": "p",
        "plan_revision": "a" * 40,
        "project_revision": "b" * 40,
        "style": {"sha256": "c" * 64},
        "item": {**item("answer_gap"), "title": QUESTION, "kind": "answer"},
    }
    page = ANSWER.encode()
    proof = validate_pair(page, judgment_notes(context, judgment(context, "draft")), context)
    assert proof["outcome"] == "draft"
    covered = judgment(context)
    assert validate_pair(assessment_document(covered), judgment_notes(context, covered), context)
    short = ANSWER.replace(
        "Example sends iMessages from your app through one API call. You create a line, send a\n"
        "message with a single request, and receive delivery status and replies by webhook. "
        "It runs on\nhardware Example operates, so you need no Mac of your own. Pricing is per "
        "line, with messages\nincluded up to the plan's limit.",
        "Example sends iMessages through one API call.",
    )
    two_sources = ANSWER.replace(
        "- [Apple iMessage support](https://support.apple.com/messages)\n", ""
    )
    for bad, message in (
        (short, "40-60 words"),
        (two_sources, "at least 3 sources"),
        (
            ANSWER.replace('meta_description: "', 'meta_summary: "'),
            "meta_title and meta_description",
        ),
        (ANSWER.replace("Example sends iMessages from your app with one", "Short"), "70 to 160"),
        (ANSWER.split("---\n\n", 1)[1], "starts with its meta_title"),
        (ANSWER.replace("## FAQ", "## Generation notes\n\n## FAQ"), "companion"),
    ):
        with pytest.raises(ValueError, match=message):
            content_draft.validate_artifact(bad.encode(), context)


async def test_a_refresh_draft_is_content_refreshs_exact_proposal(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    _, context = await prepared(f, "refresh")
    proposal = document(GOOD, context["refresh"])
    value = judgment(context, "draft")
    assert value["compared_pages"][0]["url"] == PAGE_URL
    assert validate_pair(proposal, judgment_notes(context, value), context)["outcome"] == "draft"
    # A plausible but unusable result: the model paraphrased the page's current title.
    paraphrased = [{**GOOD[0], "old": "Setup and configuration | Example"}, GOOD[1]]
    with pytest.raises(ValueError, match="current title, exactly"):
        content_draft.validate_artifact(document(paraphrased, context["refresh"]), context)
    # The page already meets its searches: an assessment, with no review or delivery.
    covered = judgment(context)
    assert validate_pair(assessment_document(covered), judgment_notes(context, covered), context)


# Old plans without kinds, and the versions pinned before them.


async def test_plans_without_kinds_still_draft_articles(publication_db, monkeypatch):
    f = await draft_fixture(publication_db, monkeypatch, judgment=True)
    run = await start(f)
    context = await f.service.prepare(
        run, output_validator=content_draft.EDITORIAL_VALIDATOR, kinds=KINDS
    )
    assert "kind" not in context["item"] and "kind" not in context
    assert content_draft.context_kind(context) == "article"
    discovery = await f.service.discover(project_id=f.project.id, program_id=f.configured.id)
    assert discovery["items"][0]["kind"] == "article"
    assert discovery["items"][0]["passed_over"] is None


async def test_an_older_definition_drafts_only_articles_and_passes_the_rest_over(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    older = ("article",)
    discovery = await f.sources.discover(
        project_id=f.project.id, program_id=f.configured.id, kinds=older
    )
    assert discovery["next"]["item_id"] == f.ids["article"]
    assert [i["passed_over"] for i in discovery["items"]][:2] == ["unsupported_kind"] * 2
    with pytest.raises(ValueError, match="drafts article items only"):
        await f.sources.choose(project_id=f.project.id, inputs=inputs(f, "answer"), kinds=older)
    chosen = await f.sources.choose(project_id=f.project.id, inputs=inputs(f), kinds=older)
    assert chosen["item"]["id"] == f.ids["article"]
    # Preparation refuses a typed item for a definition that cannot draft it, before compute.
    run = await start(f, inputs=inputs(f, "answer"))
    with pytest.raises(ValueError, match="drafts articles only"):
        await f.sources.prepare(
            run, output_validator=content_draft.EDITORIAL_VALIDATOR, kinds=older
        )


# Answer pages need the founder's route, and reach the site only through website.change.


async def test_an_answer_draft_is_refused_without_a_route(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, route=False)
    with pytest.raises(WorkflowInputError, match=r"save_page_route\(page_type=answer_page\)"):
        await start(f, inputs=inputs(f, "answer"))
    with pytest.raises(WorkflowInputError, match="Where on your site should pages that answer"):
        await start(f, inputs=inputs(f))  # The next item is the answer.
    assert not await f.db.pool.fetchval(
        "SELECT count(*) FROM workflow_runs WHERE workflow_id=$1", f.workflow.id
    )
    f.storage.repo.edit({ROUTES_PATH: canonical_json({"routes": {"answer_page": ROUTE}})})
    run = await start(f, inputs=inputs(f, "answer"))
    assert (await f.sources.selection(run.id))["page_route"] == ROUTE


async def test_an_approved_answer_goes_through_website_change_at_its_route(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    connected(f, monkeypatch)
    run, context = await prepared(f, "answer")
    run = await publish(f, run, ANSWER.encode())
    delivery = ContentDelivery(database=f.db, storage=f.storage, integrations=f.integrations)
    chosen = await delivery.choose(run=run, mode="github_commit", actor=ACTOR)
    assert chosen["adapter"] == "repository" and chosen["via"] == "website.change"
    assert chosen["route"] == ROUTE and chosen["path"] is None
    run = await approve(f, run)
    # The page travels without its search listing, which goes beside it, never into content/answers.
    source = await repository_delivery.page_source(
        database=f.db, storage=f.storage, project_id=f.project.id, source_run_id=run.id
    )
    assert source["source_kind"] == "answer_page"
    assert source["page_metadata"]["meta_title"] == QUESTION
    assert source["article"].startswith(f"# {QUESTION}\n") and "meta_title" not in source["article"]
    pinned = await website_change.select_source(
        database=f.db,
        storage=f.storage,
        integrations=f.integrations,
        project_id=f.project.id,
        inputs={"source_run_id": str(run.id), "expected_repository": "acme/site"},
    )
    assert pinned["route"] == ROUTE and pinned["change"]["paths"] == [ROUTE]
    assert pinned["change"]["detail"]["page_type"] == "answer_page"
    assert pinned["change"]["approval"]["by"] == ACTOR
    assert pinned["publish"]["mode"] == "direct"
    # Approval starts website.change, never content.deliver.
    started = AsyncMock(return_value=SimpleNamespace(id=run.id))
    monkeypatch.setattr("tin_lite.run_service.start_workflow_run", started)
    await repository_delivery.start_approved_adaptation(
        runtime=SimpleNamespace(database=f.db), settings=None, run=run, intent=chosen
    )
    call = started.await_args.kwargs
    assert call["workflow"].id == repository_delivery.WEBSITE_CHANGE_ID
    assert call["input_payload"] == {
        "source": "content_draft",
        "source_run_id": str(run.id),
        "expected_repository": "acme/site",
    }
    # Without a chosen route, website.change asks the founder instead of guessing.
    await f.db.pool.execute(
        "UPDATE effect_receipts SET result = result - 'route' WHERE execution_key=$1",
        f"content-draft:{run.id}:delivery:choice",
    )
    f.storage.repo.edit({ROUTES_PATH: canonical_json({"routes": {}})})
    with pytest.raises(website_change.RouteNotChosen):
        await website_change.select_source(
            database=f.db,
            storage=f.storage,
            integrations=f.integrations,
            project_id=f.project.id,
            inputs={"source_run_id": str(run.id), "expected_repository": "acme/site"},
        )
    assert context["answer"]["route"] == ROUTE


# Refreshes wait six weeks per page, whichever workflow made them, and apply exact lines.


async def test_a_waiting_page_is_passed_over_and_both_refresh_workflows_share_the_wait(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch, first="refresh")
    assert (await f.sources.discover(project_id=f.project.id, program_id=f.configured.id))["next"][
        "item_id"
    ] == f.ids["refresh"]
    page_run, _ = await prepared(f, "refresh")
    # content.refresh sees the content.generate refresh in progress and leaves the page alone.
    old_refresh, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=refresh.WORKFLOW_ID,
        started_by_clerk_user_id=ACTOR,
        input_payload={},
    )
    assert (await f.refreshes.prepare(old_refresh))["page"] is None
    # Once that refresh went live ten days ago, the page waits and the next item comes first.
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', review_decision='approved', "
        "reviewed_at=now(), lease_active=false WHERE id=$1",
        page_run.id,
    )
    await save(
        f.db,
        delivery_key(page_run.id),
        "content_draft_delivery_v1",
        {"commit": "c" * 40, "delivered_at": (NOW - timedelta(days=10)).isoformat()},
    )
    discovery = await f.sources.discover(project_id=f.project.id, program_id=f.configured.id)
    waiting = next(i for i in discovery["items"] if i["id"] == f.ids["refresh"])
    assert waiting["passed_over"] == "refresh_waiting"
    assert discovery["next"]["item_id"] == f.ids["answer"]
    with pytest.raises(WorkflowInputError, match="live for less than six weeks"):
        await start(f, inputs={**inputs(f, "refresh"), "rewrite": True})


@pytest.mark.parametrize("mode", ["github_commit", "github_pr"])
async def test_an_approved_refresh_applies_only_the_exact_replacements(
    publication_db, monkeypatch, mode
):
    f = await fixture(publication_db, monkeypatch)
    connected(f, monkeypatch)
    run, context = await prepared(f, "refresh")
    delivery = ContentDelivery(database=f.db, storage=f.storage, integrations=f.integrations)
    chosen = await delivery.choose(run=run, mode=mode, actor=ACTOR)
    assert chosen["kind"] == "refresh" and chosen["path"] is None
    run = await approve(f, await publish(f, run, document(GOOD, context["refresh"])))
    f.integrations.github_repository_bundle = AsyncMock(
        return_value=SimpleNamespace(
            archive=archive({"app/pricing/page.tsx": PAGE_TSX, "README.md": "# Site\n"}),
            complete=True,
        )
    )
    f.integrations.github_commit_files = AsyncMock(
        return_value=GitHubCommitResult("acme/site", "main", "d" * 40, "https://github.test/c")
    )
    f.integrations.github_create_pull_request = AsyncMock(
        return_value=GitHubPullRequestResult("acme/site", "tin/refresh", 7, "https://github.test/7")
    )
    await delivery.deliver(run.id)
    receipt = (await f.db.get_effect(delivery_key(run.id))).result
    assert receipt["changed_paths"] == ["app/pricing/page.tsx"]
    assert receipt["page"] == PAGE_URL
    write = (
        f.integrations.github_commit_files
        if mode == "github_commit"
        else f.integrations.github_create_pull_request
    )
    [change] = write.await_args.kwargs["files"]
    assert 'title: "How to set up Example in two minutes"' in change.content
    assert "<h1>How to set up Example</h1>" in change.content
    assert change.content.count("\n") == PAGE_TSX.count("\n")
    # A refresh is applied in place; it is never a page to adapt or publish as Markdown.
    with pytest.raises(ValueError, match="page refresh"):
        await approved_article.select(
            database=f.db, storage=f.storage, project_id=f.project.id, source_run_id=run.id
        )
    assert str(run.id) not in json.dumps(await approved_article.discover(f.db, f.project.id))


# The retired entry points: hidden from discovery, still runnable for pinned runs and schedules.

# Digests of each retired definition (without its version, description and discovery flag)
# and its procedure files, as content.refresh 1.0.0 and content.answer_page 1.6.0 shipped.
RETIRED = {
    "content.refresh": (
        "1.1.0",
        "d4059a9cf5d9adcccc01e8e50287f734454c4fadb703e1d7eeca52d531e4b0db",
    ),
    "content.answer_page": (
        "1.7.0",
        "223f64f3aec7c8a2a8b2a2d14d9043570e6b859f263f3588d9ab8f70012f5bdc",
    ),
}


def contract_digest(key):
    definition, files = spec(key).definition_and_resource_files()
    kept = {
        k: v
        for k, v in definition.items()
        if k not in {"version", "description", "public_discovery"}
    }
    return hashlib.sha256(
        json.dumps(
            {
                "definition": kept,
                "files": {p: hashlib.sha256(c).hexdigest() for p, c in sorted(files.items())},
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


@pytest.mark.parametrize("key", sorted(RETIRED))
def test_a_retired_workflow_keeps_its_contract_and_leaves_discovery(key):
    version, digest = RETIRED[key]
    definition = spec(key).definition
    assert definition["version"] == version and definition["public_discovery"] is False
    assert definition["description"].startswith("Retired: Draft planned content")
    # Pinned runs and saved schedules run exactly what they ran before.
    assert contract_digest(key) == digest
    programs = json.loads(
        (Path(approved_article.__file__).parent / "growth_plan_assets/programs.json").read_text()
    )
    organic = next(p for p in programs["programs"] if p["id"] == "organic-traffic")
    assert key not in organic["tin"]["workflows"]
    assert "content.generate" in organic["tin"]["workflows"]


async def test_retired_workflows_are_hidden_but_saved_configurations_still_run(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    answer = spec("content.answer_page")
    await f.db.upsert_registry_workflow(
        workflow_id=answer.id,
        key=answer.key,
        title=answer.title,
        description=answer.description,
        executor=answer.executor,
        definition_repo_id="registry/workflows",
        definition_path=answer.definition_path,
        current_commit_sha="e" * 40,
        version_label=answer.version_label,
        definition=answer.definition,
    )
    server = mcp(f, monkeypatch)
    listed = str(
        structured(await server.call_tool("list_workflows", {"project_id": str(f.project.id)}))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get("/api/workflows", params={"project_id": str(f.project.id)})
    for catalog in (listed, response.text):
        assert "content.generate" in catalog
        assert "content.refresh" not in catalog and "content.answer_page" not in catalog
    # A saved weekly refresh still starts at its pinned revision.
    workflow = await f.db.get_workflow(refresh.WORKFLOW_ID)
    configured = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=workflow.id,
        definition_commit_sha="e" * 40,
        name="Weekly page refresh",
        inputs={"direction": ""},
        input_schema=workflow.definition["input_schema"],
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=PRIVATE_ACTOR,
    )
    await f.db.project_workflow_synced(
        project_workflow_id=configured.id, temporal_schedule_id=None, next_run_at=None
    )
    run = await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=PRIVATE_ACTOR,
        project_workflow_id=configured.id,
        input_payload={"direction": ""},
        trigger_source="schedule",
    )
    assert run.workflow_id == refresh.WORKFLOW_ID and run.status.value == "pending"
    assert (await f.refreshes.prepare(run))["page"]["path"] == "/pricing"
