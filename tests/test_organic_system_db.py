"""Disposable Postgres schemas: pinned child preparation and no-change receipts."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import organic_system
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_system_activities import OrganicSystemActivities
from tin_lite.organic_system_control import stop_system
from tin_lite.run_reports import publish_run_report
from tin_lite.workflow_inputs import normalize_workflow_inputs


async def parent_fixture(db, monkeypatch, *, policy=None):
    _, storage, run, _ = await activity_fixture(db)
    definitions = {row.key: row.definition for row in BUILTIN_WORKFLOWS}
    if policy is not None:
        definitions[organic_system.KEY] = {
            **definitions[organic_system.KEY],
            "organic_system_policy": policy,
        }
    inputs = normalize_workflow_inputs(
        schema=organic_system.INPUT_SCHEMA,
        project_id=run.project_id,
        inputs={
            "site_url": "https://example.com/",
            "market": "US",
            "buyer_context": "A useful product for founders planning their marketing.",
            "start_date": "2026-09-14",
        },
    )
    await db.pool.execute(
        "UPDATE workflows SET key=$2, executor=$2, definition=$3::jsonb WHERE id=$1",
        run.workflow_id,
        organic_system.KEY,
        json.dumps(definitions[organic_system.KEY]),
    )
    await db.pool.execute(
        "UPDATE workflow_runs SET executor=$2, input=$3::jsonb, lease_active=false, "
        "started_by_clerk_user_id='user_tester' WHERE id=$1",
        run.id,
        organic_system.KEY,
        json.dumps(inputs),
    )
    # The technical and delivery steps share one workflow, website.change.
    for workflow_key in sorted(set(organic_system.STEPS.values())):
        definition = definitions[workflow_key]
        await db.pool.execute(
            "INSERT INTO workflows (id,key,title,executor,definition_repo_id,definition_path,"
            "current_commit_sha,version_label,definition) "
            "VALUES ($1,$2,$2,$3,'registry/workflows',$4,$5,'1',$6::jsonb)",
            uuid4(),
            workflow_key,
            definition["executor"],
            f"workflows/{workflow_key}.json",
            "e" * 40,
            json.dumps(definition),
        )
    # v7's measurement packages, whose manifests sit in their own folders.
    from tin_lite.public_workflows import load_public_workflows

    packages = {}
    for package in await load_public_workflows():
        if package.key not in organic_system.MEASURE_STEPS.values():
            continue
        packages.update(package.files)
        await db.pool.execute(
            "INSERT INTO workflows (id,key,title,executor,definition_repo_id,definition_path,"
            "current_commit_sha,version_label,definition) "
            "VALUES ($1,$2,$2,$3,'registry/workflows',$4,$5,'1',$6::jsonb)",
            uuid4(),
            package.key,
            package.executor,
            package.definition_path,
            "e" * 40,
            json.dumps(package.definition),
        )
    monkeypatch.setattr(db, "has_project_access", AsyncMock(return_value=True))

    async def read(**kwargs):
        assert kwargs["commit_sha"] == run.definition_commit_sha
        if kwargs["path"] in packages:
            return packages[kwargs["path"]]
        return json.dumps(definitions[kwargs["path"][10:-5]]).encode()

    monkeypatch.setattr(storage, "read_canonical_artifact", read)
    settings = SimpleNamespace(
        dataforseo_login="fixture",
        dataforseo_password="fixture",  # noqa: S106 - synthetic local-test credential
        luna_api_key="fixture",
        keyword_plan_max_cost_usd=9,
        organic_audit_max_cost_usd=7,
        content_plan_max_cost_usd=1,
        temporal_task_queue="fixture",
    )
    activities = OrganicSystemActivities(
        database=db, storage=storage, settings=settings, integrations=SimpleNamespace()
    )
    return SimpleNamespace(
        db=db, storage=storage, run=await db.get_run(run.id), activities=activities
    )


async def test_parent_prepares_one_pinned_run_per_step_without_temporal_dispatch(
    publication_db, monkeypatch
):
    f = await parent_fixture(publication_db, monkeypatch)
    await f.activities.organic_system_prepare(str(f.run.id))
    for step in ("audit", "keywords"):
        payload = {"run_id": str(f.run.id), "step": step}
        first = await f.activities.organic_system_step(payload)
        second = await f.activities.organic_system_step(payload)
        assert first == second
        child = await f.db.get_run(UUID(first["run_id"]))
        assert child.definition_commit_sha == f.run.definition_commit_sha  # not latest eeeee...
        assert child.project_id == f.run.project_id
        assert child.input["site_url"] == f.run.input["site_url"]
    technical = await f.activities.organic_system_step(
        {"run_id": str(f.run.id), "step": "technical"}
    )
    # Fixes are on by default; without a succeeded audit there is nothing to fix from.
    assert technical == {"status": "blocked", "reason": "audit_unavailable"}
    content = await f.activities.organic_system_step({"run_id": str(f.run.id), "step": "content"})
    assert content == {"status": "blocked", "reason": "research_unavailable"}
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 3


async def test_a_run_pinned_to_a_retired_recipe_fails_at_preparation(publication_db, monkeypatch):
    # organic-traffic-v5 and earlier started organic.technical_fix and content.deliver, which
    # left the codebase. A run pinned to one stops at preparation with the reason, before any
    # child run or prepare receipt exists.
    retired = {
        "version": "organic-traffic-v5",
        "steps": {
            "audit": "organic.audit",
            "keywords": "organic.keyword_plan",
            "technical": "organic.technical_fix",
            "content": "content.plan",
            "draft": "content.generate",
            "delivery": "content.deliver",
        },
        "max_technical_findings": 1,
        "schedule": "weekly_articles",
        "content_fallback": "latest_saved_plan",
        "refresh": "weekly_refresh",
    }
    f = await parent_fixture(publication_db, monkeypatch, policy=retired)
    with pytest.raises(ApplicationError, match="Unsupported organic system policy") as error:
        await f.activities.organic_system_prepare(str(f.run.id))
    assert error.value.non_retryable
    assert await f.db.get_effect(f"traffic:{f.run.id}:prepare") is None
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 1
    with pytest.raises(ValueError, match="Unsupported organic system policy"):
        organic_system.policy_steps(retired)
    assert not organic_system.runs_recipe(retired)


@pytest.mark.parametrize("failed", [False, True])
async def test_summary_report_publishes_once_with_honest_status(
    publication_db, monkeypatch, failed
):
    f = await parent_fixture(publication_db, monkeypatch)
    for _ in range(2):
        await publish_run_report(
            database=f.db,
            storage=f.storage,
            run_id=f.run.id,
            workflow_key=organic_system.KEY,
            prefix="traffic",
            path=f"reports/organic-system/{f.run.id}/RESULT.md",
            content=b"# System result\n",
            summary="System result",
            failed=failed,
        )
    run = await f.db.get_run(f.run.id)
    assert run.status.value == ("failed" if failed else "succeeded")
    assert f.storage.repo.writes == 1
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events WHERE dedupe_key LIKE 'traffic:%'"
        )
        == 1
    )


@pytest.mark.parametrize("child_status", ["failed", "succeeded"])
async def test_parent_retry_links_only_its_failed_keyword_child(
    publication_db, monkeypatch, child_status
):
    f = await parent_fixture(publication_db, monkeypatch)
    await f.activities.organic_system_prepare(str(f.run.id))
    result = await f.activities.organic_system_step({"run_id": str(f.run.id), "step": "keywords"})
    old_child_id = UUID(result["run_id"])
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status=$2, lease_active=false, finished_at=now() WHERE id=$1",
        old_child_id,
        child_status,
    )
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='failed', lease_active=false, finished_at=now() "
        "WHERE id=$1",
        f.run.id,
    )
    template = await f.db.get_workflow(f.run.workflow_id)
    retried, _ = await f.db.create_run(
        project_id=f.run.project_id,
        workflow_id=f.run.workflow_id,
        started_by_clerk_user_id=f.run.started_by_clerk_user_id,
        input_payload=f.run.input,
        pinned_definition=template.definition,
        definition_commit_sha=f.run.definition_commit_sha,
        retry_of_run_id=f.run.id,
    )
    await f.activities.organic_system_prepare(str(retried.id))
    result = await f.activities.organic_system_step({"run_id": str(retried.id), "step": "keywords"})
    new_child = await f.db.get_run(UUID(result["run_id"]))
    assert new_child.id != old_child_id
    assert new_child.retry_of_run_id == (old_child_id if child_status == "failed" else None)
    assert new_child.definition_commit_sha == f.run.definition_commit_sha


async def test_parent_stop_fences_future_child_creation(publication_db, monkeypatch):
    f = await parent_fixture(publication_db, monkeypatch)
    await f.activities.organic_system_prepare(str(f.run.id))
    handle = SimpleNamespace(cancel=AsyncMock())
    runtime = SimpleNamespace(
        database=f.db, temporal=SimpleNamespace(get_workflow_handle=lambda _: handle)
    )
    result = await stop_system(runtime=runtime, run_id=f.run.id, actor="tester")
    assert result["status"] == "stopped"
    handle.cancel.assert_awaited_once()
    with pytest.raises(Exception, match="no longer active"):
        await f.activities.organic_system_step({"run_id": str(f.run.id), "step": "audit"})
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 1


async def test_content_child_uses_own_research_and_creates_one_manual_program(
    publication_db, monkeypatch
):
    f = await parent_fixture(publication_db, monkeypatch)
    await f.activities.organic_system_prepare(str(f.run.id))
    sources = {}
    for step in ("audit", "keywords"):
        child = await f.activities.organic_system_step({"run_id": str(f.run.id), "step": step})
        sources[step] = child["run_id"]
        # A succeeded research run always carries its publication revision; the content
        # child's declared prerequisites resolve exactly those runs.
        await f.db.pool.execute(
            "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, "
            "finished_at=now() WHERE id=$1",
            UUID(child["run_id"]),
            "d" * 40,
        )
    first = await f.activities.organic_system_step({"run_id": str(f.run.id), "step": "content"})
    assert (
        await f.activities.organic_system_step({"run_id": str(f.run.id), "step": "content"})
        == first
    )
    content = await f.db.get_run(UUID(first["run_id"]))
    assert content.input["audit_run_id"] == sources["audit"]
    assert content.input["keyword_run_id"] == sources["keywords"]
    assert content.definition_commit_sha == f.run.definition_commit_sha
    configured = await f.db.get_project_workflow(content.project_workflow_id)
    assert configured.schedule is None
    assert configured.definition_commit_sha == f.run.definition_commit_sha
    assert await f.db.pool.fetchval("SELECT count(*) FROM project_workflows") == 1


async def test_system_failure_names_a_specific_cause_and_hides_an_internal_one():
    from tin_lite.failures import INTERNAL_MESSAGE
    from tin_lite.organic_system_activities import FAILURE_AFTERWORD, FAILURE_FALLBACK

    failures = []

    async def project_failure(**values):
        failures.append(values)

    activities = OrganicSystemActivities(
        database=SimpleNamespace(project_failure=project_failure),
        storage=None,
        settings=None,
        integrations=None,
    )
    run_id = uuid4()
    await activities.organic_system_failure(str(run_id))
    await activities.organic_system_failure(
        {
            "run_id": str(run_id),
            "reason": "Your Tin credits ran out. Add credits in Billing, then retry.",
            "kind": "needs_you",
            "action": "billing",
            "detail": "BillingError: insufficient funds",
        }
    )
    await activities.organic_system_failure(
        {"run_id": str(run_id), "reason": "GitHub could not be reached", "kind": "transient"}
    )
    await activities.organic_system_failure(
        {"run_id": str(run_id), "reason": INTERNAL_MESSAGE, "kind": "internal", "detail": "x"}
    )
    plain, billing, transient, internal = failures
    # The older history's text is unchanged.
    assert plain["error_message"] == (
        "Organic traffic system could not finish. Completed child outputs remain available; "
        "paid requests were not automatically repeated."
    )
    assert plain["error_kind"] is None
    assert billing["error_message"] == (
        f"Your Tin credits ran out. Add credits in Billing, then retry. {FAILURE_AFTERWORD}"
    )
    assert billing["error_kind"] == "needs_you" and billing["error_action"] == "billing"
    assert transient["error_message"] == f"GitHub could not be reached. {FAILURE_AFTERWORD}"
    assert internal["error_message"] == f"{FAILURE_FALLBACK} {FAILURE_AFTERWORD}"
    assert internal["error_kind"] == "internal" and internal["error_detail"] == "x"
