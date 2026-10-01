"""The refresh run end to end on disposable Postgres: pick, wait, measure, report, deliver."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from test_content_programs import Storage
from test_content_refresh import EVIDENCE, GOOD, PAGE, PAGE_TSX, audit, document, finding
from test_procedure_publication import publication_db as publication_db
from test_technical_title_repair import archive

from tin_lite import content_refresh as refresh
from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.codex_api_pricing import PROCEDURE_MAXIMUMS, api_terms
from tin_lite.content_delivery import OPERATION, ContentDelivery, delivery_key, refresh_keys
from tin_lite.content_refresh_sources import ContentRefreshSources
from tin_lite.integrations import GitHubCommitResult, GitHubPullRequestResult
from tin_lite.organic_audit import audit_paths, canonical_json

ACTOR = "user_refresh"
NOW = datetime(2026, 9, 29, 16, tzinfo=UTC)
FINDINGS = audit(
    finding("search.low_ctr", "/pricing"),
    finding("search.near_page_one", "/guides/setup/"),
)


def spec(key):
    return next(w for w in BUILTIN_WORKFLOWS if w.key == key)


async def save(db, key, result, operation=refresh.PREPARATION):
    async with db.effect_lock(key, operation) as (conn, _):
        await db.start_effect(conn, execution_key=key, operation=operation)
        await db.complete_effect(conn, execution_key=key, result=result)


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


async def fixture(db, *, with_audit=True):
    storage = Storage()
    storage.definition = {}
    project = await db.create_project(name="Refresh proof", state_repo_id=storage.repo.id)
    await db.record_tin_user(ACTOR)
    await db.grant_project_membership(project_id=project.id, clerk_user_id=ACTOR)
    for key in ("organic.audit", refresh.KEY):
        item = spec(key)
        await db.upsert_registry_workflow(
            workflow_id=item.id,
            key=item.key,
            title=item.title,
            description=item.description,
            executor=item.executor,
            definition_repo_id="registry/workflows",
            definition_path=item.definition_path,
            current_commit_sha="e" * 40,
            version_label=item.version_label,
            definition=item.definition,
        )
    storage.repo.edit(
        {
            "brand/BRAND.md": b"# Brand\nThe marketing system for your coding agent.\n",
            "context/positioning.md": b"# Positioning\nLead with the whole system.\n",
        }
    )
    if with_audit:
        audit_run, _ = await db.create_run(
            project_id=project.id, workflow_id=spec("organic.audit").id, input_payload={}
        )
        paths = audit_paths(str(audit_run.id))
        revision = storage.repo.edit(
            {
                paths["findings.json"]: canonical_json(FINDINGS),
                paths["evidence.json"]: canonical_json(EVIDENCE),
            }
        )
        await db.pool.execute(
            "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, "
            "finished_at=now(), lease_active=false WHERE id=$1",
            audit_run.id,
            revision,
        )
    console = AsyncMock(
        side_effect=lambda **kw: {
            "rows": [
                {"clicks": 10, "impressions": 400, "position": 5.0}
                if kw["execution_key"].endswith(":before")
                else {"clicks": 25, "impressions": 500, "position": 4.1}
            ]
        }
    )
    integrations = SimpleNamespace(
        search_console_analytics=console,
        github_pull_request_state=AsyncMock(
            return_value={"state": "open", "merged": False, "merged_at": None}
        ),
    )
    readers = []

    def reader(hosts):
        readers.append(Reader(hosts))
        return readers[-1]

    sources = ContentRefreshSources(
        database=db, storage=storage, integrations=integrations, reader=reader, clock=lambda: NOW
    )
    return SimpleNamespace(
        db=db,
        storage=storage,
        project=project,
        integrations=integrations,
        readers=readers,
        sources=sources,
    )


async def refresh_run(f, **values):
    run, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=refresh.WORKFLOW_ID,
        started_by_clerk_user_id=ACTOR,
        input_payload={},
    )
    if values:
        sets = ", ".join(f"{name}=${index}" for index, name in enumerate(values, 2))
        await f.db.pool.execute(
            f"UPDATE workflow_runs SET {sets} WHERE id=$1",  # noqa: S608 - fixed test columns
            run.id,
            *values.values(),
        )
    return await f.db.get_run(run.id)


async def earlier(f, path, *, delivered=None, pull_request=None, status="succeeded"):
    reviewed = {"review_decision": "approved", "reviewed_at": NOW} if status == "succeeded" else {}
    run = await refresh_run(f, status=status, **reviewed)
    await save(
        f.db,
        f"{run.id}:{refresh.PREPARATION}",
        {"page": {"url": f"https://example.com{path}", "path": path}},
    )
    if delivered:
        await save(
            f.db,
            delivery_key(run.id),
            {"commit": "c" * 40, "delivered_at": delivered.isoformat()},
            OPERATION,
        )
    if pull_request:
        await save(
            f.db,
            delivery_key(run.id),
            {"repository": "acme/site", "number": pull_request, "url": "https://github.test"},
            OPERATION,
        )
    return run


async def test_the_refresh_picks_the_next_page_waits_six_weeks_and_measures_28_days(
    publication_db,
):
    f = await fixture(publication_db)
    old = await earlier(f, "/blog/old", delivered=NOW - timedelta(days=45))
    await earlier(f, "/guides/setup", delivered=NOW - timedelta(days=10))
    run = await refresh_run(f)
    context = await f.sources.prepare(run)
    # The setup guide has more impressions at stake, but its refresh went live 10 days ago.
    assert context["page"]["path"] == "/pricing"
    assert context["current"]["title"] == "Setup & configuration | Example"
    assert f.readers[0].urls == ["https://example.com/pricing"]
    assert context["positioning_sources"] == ["brand/BRAND.md", "context/positioning.md"]
    assert context["project_revision"] == f.storage.repo.head
    # The refresh live 45 days ago is measured: 28 days before and after, filtered to its page.
    [result] = context["results"]
    assert result["path"] == "/blog/old" and result["run_id"] == str(old.id)
    assert result["before"]["clicks"] == 10 and result["after"]["clicks"] == 25
    calls = f.integrations.search_console_analytics.await_args_list
    assert [call.kwargs["dimension_filters"][0]["expression"] for call in calls] == [
        "https://example.com/blog/old"
    ] * 2
    live = (NOW - timedelta(days=45)).date()
    assert calls[0].kwargs["start_date"] == (live - timedelta(days=28)).isoformat()
    assert calls[1].kwargs["end_date"] == (live + timedelta(days=27)).isoformat()
    assert "| /blog/old |" in context["results_markdown"]
    # A retried preparation reuses the saved choice without reading or measuring again.
    assert await f.sources.prepare(run) == context
    assert len(f.readers) == 1 and len(calls) == 2


async def test_an_open_or_pending_refresh_keeps_its_page_waiting(publication_db):
    f = await fixture(publication_db)
    await earlier(f, "/guides/setup", pull_request=41)
    await earlier(f, "/pricing", status="needs_input")
    run = await refresh_run(f)
    context = await f.sources.prepare(run)
    assert context["page"] is None
    assert "Waiting for results or review: /guides/setup, /pricing." in context["nothing_due"]
    f.integrations.github_pull_request_state.assert_awaited_once()


async def test_an_approved_refresh_not_yet_delivered_keeps_its_page_waiting(publication_db):
    f = await fixture(publication_db)
    stale = await earlier(f, "/guides/setup")  # Approved; its delivery has not happened yet.
    run = await refresh_run(f)
    context = await f.sources.prepare(run)
    assert context["page"]["path"] == "/pricing"
    # An approval that never reaches the site stops holding the page after six weeks.
    await f.db.pool.execute(
        "UPDATE workflow_runs SET created_at=$2 WHERE id=$1", stale.id, NOW - timedelta(weeks=7)
    )
    assert (await f.sources.prepare(await refresh_run(f)))["page"]["path"] == "/guides/setup"


async def test_every_open_refresh_pull_request_keeps_its_page_waiting(publication_db):
    f = await fixture(publication_db)
    # The oldest of six open pull requests is the setup guide's.
    await earlier(f, "/guides/setup", pull_request=40)
    for number in range(41, 46):
        await earlier(f, f"/blog/post-{number}", pull_request=number)
    run = await refresh_run(f)
    context = await f.sources.prepare(run)
    assert context["page"]["path"] == "/pricing"
    assert f.integrations.github_pull_request_state.await_count == 6


async def test_nothing_due_reports_and_finishes_without_compute_or_review(
    publication_db, monkeypatch
):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *args: None)
    f = await fixture(publication_db, with_audit=False)
    run = await refresh_run(f)
    assert run.review_required is True
    activities = TinActivities(
        database=f.db, storage=f.storage, settings=SimpleNamespace(), sandboxes=SimpleNamespace()
    )
    assert await activities._prepare_content_refresh(run.id) is True
    run = await f.db.get_run(run.id)
    assert run.status.value == "succeeded" and run.review_decision is None
    assert run.artifact_path == f"reports/content-refresh/{run.id}.md"
    report = f.storage.repo.trees[run.canonical_commit_sha][run.artifact_path][1].decode()
    assert report.startswith("# No page refresh this week")
    assert "Run the audit first" in report
    assert (
        await f.db.pool.fetchval("SELECT count(*) FROM run_decisions WHERE run_id=$1", run.id) == 0
    )


def binding():
    return SimpleNamespace(
        repository="acme/site",
        repository_id=11,
        connection_id=UUID("00000000-0000-4000-8000-0000000000aa"),
        installation_id=22,
        default_branch="main",
        head_sha="b" * 40,
    )


@pytest.mark.parametrize("mode", ["github_commit", "github_pr"])
async def test_approved_replacements_reach_the_source_and_follow_the_pick(
    publication_db, monkeypatch, mode
):
    f = await fixture(publication_db)
    monkeypatch.setattr(
        f.db,
        "get_integration_connection",
        AsyncMock(
            return_value=SimpleNamespace(
                status="connected", configuration={"selected_repository": "acme/site"}
            )
        ),
    )
    run = await refresh_run(f)
    context = await f.sources.prepare(run)
    assert context["page"]["path"] == "/guides/setup"
    f.integrations.github_repository_binding = AsyncMock(return_value=binding())
    f.integrations.github_repository_bundle = AsyncMock(
        return_value=SimpleNamespace(
            archive=archive({"app/guides/setup/page.tsx": PAGE_TSX, "README.md": "# Site\n"}),
            complete=True,
        )
    )
    f.integrations.github_commit_files = AsyncMock(
        return_value=GitHubCommitResult("acme/site", "main", "d" * 40, "https://github.test/c")
    )
    f.integrations.github_create_pull_request = AsyncMock(
        return_value=GitHubPullRequestResult("acme/site", "tin/refresh", 7, "https://github.test/7")
    )
    delivery = ContentDelivery(database=f.db, storage=f.storage, integrations=f.integrations)
    chosen = await delivery.choose(run=run, mode=mode, remember=False, actor=ACTOR)
    assert chosen["kind"] == "refresh" and chosen["repository_id"] == 11
    path = refresh.PATH_TEMPLATE.format(run_folder="2026-09-29-proof")
    revision = f.storage.repo.edit({path: document(GOOD, context)})
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', review_decision='approved', "
        "reviewed_at=now(), artifact_path=$2, canonical_commit_sha=$3, lease_active=false "
        "WHERE id=$1",
        run.id,
        path,
        revision,
    )
    await delivery.deliver(run.id)
    await delivery.deliver(run.id)  # A retry reuses the receipt.
    receipt = (await f.db.get_effect(delivery_key(run.id))).result
    assert receipt["changed_paths"] == ["app/guides/setup/page.tsx"]
    assert receipt["page"] == "https://example.com/guides/setup/"
    if mode == "github_commit":
        call = f.integrations.github_commit_files.await_args.kwargs
        f.integrations.github_create_pull_request.assert_not_awaited()
        assert receipt["commit"] == "d" * 40 and call["message"] == "Refresh: /guides/setup"
    else:
        call = f.integrations.github_create_pull_request.await_args.kwargs
        f.integrations.github_commit_files.assert_not_awaited()
        assert receipt["number"] == 7 and "| title |" in call["body"]
        assert "Tin did not build the site" in call["body"]
    [change] = call["files"]
    assert 'title: "How to set up Example in two minutes"' in change.content
    assert "<h1>How to set up Example</h1>" in change.content
    assert change.content.count("\n") == PAGE_TSX.count("\n")


async def test_text_the_source_does_not_hold_stops_delivery_with_the_reason(
    publication_db, monkeypatch
):
    f = await fixture(publication_db)
    monkeypatch.setattr(
        f.db,
        "get_integration_connection",
        AsyncMock(
            return_value=SimpleNamespace(
                status="connected", configuration={"selected_repository": "acme/site"}
            )
        ),
    )
    run = await refresh_run(f)
    context = await f.sources.prepare(run)
    f.integrations.github_repository_binding = AsyncMock(return_value=binding())
    # The site builds its title from a template, so the rendered title is not in the source.
    source = PAGE_TSX.replace("Setup & configuration | Example", "Setup & configuration")
    f.integrations.github_repository_bundle = AsyncMock(
        return_value=SimpleNamespace(archive=archive({"app/page.tsx": source}), complete=True)
    )
    f.integrations.github_commit_files = AsyncMock()
    delivery = ContentDelivery(database=f.db, storage=f.storage, integrations=f.integrations)
    await delivery.choose(run=run, mode="github_commit", remember=False, actor=ACTOR)
    path = refresh.PATH_TEMPLATE.format(run_folder="2026-09-29-missing")
    revision = f.storage.repo.edit({path: document(GOOD, context)})
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', review_decision='approved', "
        "reviewed_at=now(), artifact_path=$2, canonical_commit_sha=$3, lease_active=false "
        "WHERE id=$1",
        run.id,
        path,
        revision,
    )
    with pytest.raises(ValueError, match="could not find the page's current title"):
        await delivery.deliver(run.id)
    receipt = await f.db.get_effect(delivery_key(run.id))
    assert receipt.status == "failed" and "current title" in receipt.error_message
    f.integrations.github_commit_files.assert_not_awaited()


async def test_a_failed_delivery_retries_against_the_current_head(publication_db, monkeypatch):
    f = await fixture(publication_db)
    monkeypatch.setattr(
        f.db,
        "get_integration_connection",
        AsyncMock(
            return_value=SimpleNamespace(
                status="connected", configuration={"selected_repository": "acme/site"}
            )
        ),
    )
    run = await refresh_run(f)
    context = await f.sources.prepare(run)
    moved = SimpleNamespace(**{**vars(binding()), "head_sha": "c" * 40})
    f.integrations.github_repository_binding = AsyncMock(return_value=binding())
    f.integrations.github_repository_bundle = AsyncMock(
        return_value=SimpleNamespace(
            archive=archive({"app/guides/setup/page.tsx": PAGE_TSX}), complete=True
        )
    )
    f.integrations.github_create_pull_request = AsyncMock(
        side_effect=ValueError("GitHub refused the branch.")
    )
    delivery = ContentDelivery(database=f.db, storage=f.storage, integrations=f.integrations)
    await delivery.choose(run=run, mode="github_pr", remember=False, actor=ACTOR)
    path = refresh.PATH_TEMPLATE.format(run_folder="2026-09-29-retry")
    revision = f.storage.repo.edit({path: document(GOOD, context)})
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', review_decision='approved', "
        "reviewed_at=now(), artifact_path=$2, canonical_commit_sha=$3, lease_active=false "
        "WHERE id=$1",
        run.id,
        path,
        revision,
    )

    async def receipt(key, status, head=None):
        await f.db.record_integration_call(
            execution_key=key,
            project_id=f.project.id,
            run_id=run.id,
            connection_id=None,
            provider_key="infra.github",
            capability="contents.read" if head else "pull_requests.write",
            request_fingerprint="f" * 64,
            status=status,
            response_summary={"head_sha": head} if head else None,
        )

    first_read, first_write = refresh_keys(run.id, 0)
    await receipt(first_read, "completed", head="b" * 40)
    with pytest.raises(ValueError, match="refused the branch"):
        await delivery.deliver(run.id)
    # Nothing was written and main has not moved: the retry may reuse the same read.
    assert await delivery.refresh_attempt(run.id, "b" * 40) == 0
    # The write failed and main moved on: the retry reads the current head under new keys.
    await receipt(first_write, "failed")
    f.integrations.github_repository_binding = AsyncMock(return_value=moved)
    f.integrations.github_create_pull_request = AsyncMock(
        return_value=GitHubPullRequestResult("acme/site", "tin/refresh", 8, "https://github.test/8")
    )
    await delivery.deliver(run.id)
    second_read, second_write = refresh_keys(run.id, 1)
    assert f.integrations.github_repository_bundle.await_args.kwargs["execution_key"] == second_read
    call = f.integrations.github_create_pull_request.await_args.kwargs
    assert call["execution_key"] == second_write and call["expected_base_sha"] == "c" * 40
    assert (await f.db.get_effect(delivery_key(run.id))).result["number"] == 8
    # A write that may have opened a pull request keeps its keys, so a retry recovers it.
    await receipt(second_write, "started")
    assert await delivery.refresh_attempt(run.id, "d" * 40) == 1


def test_refresh_compute_has_its_own_ceiling_about_five_times_the_estimate():
    definition = spec(refresh.KEY).definition
    assert definition["procedure"]["output"]["validator"] == refresh.VALIDATOR
    assert PROCEDURE_MAXIMUMS[refresh.VALIDATOR] == 2_500_000_000
    terms = api_terms(definition)
    assert terms["maximum_nanos"] == 2_500_000_000
    assert json.dumps(terms)  # Pinned into the run as plain data.


def test_the_catalog_entry_is_weekly_reviewed_and_in_the_traffic_system():
    item = spec(refresh.KEY)
    assert item.id == refresh.WORKFLOW_ID and item.system == "organic-traffic"
    assert item.schedule_modes == ("on_demand", "weekly")
    definition, files = item.definition_and_resource_files()
    assert definition["human_review"]["eligible"] is True
    assert definition["procedure"]["output"]["path_template"] == refresh.PATH_TEMPLATE
    prompt = next(raw.decode() for path, raw in files.items() if path.endswith("PROMPT.md"))
    assert "Do not narrow, downplay or reframe it" in " ".join(prompt.split())
    assert {p.workflow or p.path for p in item.prerequisites} == {
        "organic.audit",
        refresh.STYLE_PATH,
    }
