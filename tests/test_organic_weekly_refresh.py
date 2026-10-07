"""The traffic system refreshes one page now and saves the weekly refresh after it."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from test_organic_content import system_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_refresh, organic_system
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.schedules import WorkflowSchedule


async def refresh_fixture(db, monkeypatch, *, policy=None):
    client = SimpleNamespace(create_schedule=AsyncMock(), start_workflow=AsyncMock())
    f = await system_fixture(db, monkeypatch, temporal=client, policy=policy)
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == content_refresh.KEY)
    await db.upsert_registry_workflow(
        workflow_id=spec.id,
        key=spec.key,
        title=spec.title,
        description=spec.description,
        executor=spec.executor,
        definition_repo_id="registry/workflows",
        definition_path=spec.definition_path,
        current_commit_sha="e" * 40,
        version_label=spec.version_label,
        definition=spec.definition,
    )
    if organic_system.refreshes_pages(policy or organic_system.REFRESH_POLICY):
        await db.pool.execute(
            "UPDATE effect_receipts SET result=jsonb_set(result, '{definitions,refresh}', "
            "$2::jsonb) WHERE execution_key=$1",
            f"traffic:{f.parent.id}:prepare",
            json.dumps(spec.definition),
        )
    f.client, f.refresh = client, spec
    return f


async def refresh_runs(f):
    return await f.db.pool.fetch(
        "SELECT id, project_workflow_id FROM workflow_runs WHERE workflow_id=$1 "
        "ORDER BY created_at",
        content_refresh.WORKFLOW_ID,
    )


def test_the_v5_recipe_refreshes_pages_and_older_recipes_do_not():
    recipe = next(w for w in BUILTIN_WORKFLOWS if w.key == organic_system.KEY).definition
    # New runs pin v8, which keeps v5's page refresh.
    assert recipe["organic_system_policy"]["version"] == "organic-traffic-v8"
    for policy in (
        organic_system.REFRESH_POLICY,
        organic_system.WEBSITE_POLICY,
        organic_system.POLICY,
    ):
        assert organic_system.refreshes_pages(policy)
        assert organic_system.schedules_articles(policy)
    assert not organic_system.refreshes_pages(organic_system.FALLBACK_POLICY)


async def test_the_systems_refresh_is_the_schedules_first_run(publication_db, monkeypatch):
    f = await refresh_fixture(publication_db, monkeypatch)
    before = datetime.now(UTC)
    await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "draft"})
    result = (await f.db.get_effect(f"traffic:{f.parent.id}:refresh")).result
    assert result["status"] == "succeeded" and not result.get("reused")
    configured = await f.db.get_project_workflow(UUID(result["project_workflow_id"]))
    assert configured.workflow_key == content_refresh.KEY and configured.status == "active"
    schedule = WorkflowSchedule.model_validate(configured.schedule)
    # Same weekday as today, at the drafting time, and the next scheduled run a week out.
    assert schedule.weekdays == [before.strftime("%A").casefold()]
    assert schedule.local_time == "10:00" and schedule.timezone == "UTC"
    assert schedule.start_at >= before.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=7)
    [child] = await refresh_runs(f)
    assert str(child["id"]) == result["run_id"]
    assert child["project_workflow_id"] == configured.id
    f.client.create_schedule.assert_awaited_once()
    started = [call.args for call in f.client.start_workflow.await_args_list]
    assert any(str(child["id"]) in json.dumps([str(arg) for arg in args]) for args in started)
    # A retried step starts nothing new.
    await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "draft"})
    assert len(await refresh_runs(f)) == 1
    f.client.create_schedule.assert_awaited_once()

    publish = AsyncMock()
    monkeypatch.setattr("tin_lite.organic_system_activities.publish_run_report", publish)
    await f.system.organic_system_finish(str(f.parent.id))
    report = publish.await_args.kwargs["content"].decode()
    assert "## Page refresh" in report
    assert "that run is the schedule's first" in report
    assert result["project_workflow_id"] in report


async def test_an_existing_weekly_refresh_is_kept_and_not_doubled(publication_db, monkeypatch):
    f = await refresh_fixture(publication_db, monkeypatch)
    first = await f.system.first_refresh(
        f.parent, (await f.db.get_effect(f"traffic:{f.parent.id}:prepare")).result
    )
    later = replace(f.parent, id=UUID(int=f.parent.id.int ^ 1))
    reused = await f.system._first_refresh(
        later, (await f.db.get_effect(f"traffic:{f.parent.id}:prepare")).result
    )
    assert reused["reused"] is True
    assert reused["project_workflow_id"] == first["project_workflow_id"]
    assert "run_id" not in reused
    assert len(await refresh_runs(f)) == 1


async def test_a_recipe_without_refresh_starts_none(publication_db, monkeypatch):
    f = await refresh_fixture(publication_db, monkeypatch, policy=organic_system.FALLBACK_POLICY)
    await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "draft"})
    assert await f.db.get_effect(f"traffic:{f.parent.id}:refresh") is None
    assert await refresh_runs(f) == []


async def test_without_temporal_the_refresh_is_blocked_not_guessed(publication_db, monkeypatch):
    f = await refresh_fixture(publication_db, monkeypatch)
    f.system.temporal = None
    prepared = (await f.db.get_effect(f"traffic:{f.parent.id}:prepare")).result
    assert await f.system.first_refresh(f.parent, prepared) == {
        "status": "blocked",
        "reason": "scheduling_unavailable",
    }
    assert await refresh_runs(f) == []


async def test_the_system_dispatches_its_refresh_as_a_child_it_waits_for(
    publication_db, monkeypatch
):
    f = await refresh_fixture(publication_db, monkeypatch)
    started = await f.system.organic_system_refresh(str(f.parent.id))
    [child] = await refresh_runs(f)
    # Prepared for the parent to run as its Temporal child, not started on its own.
    assert started["run_id"] == str(child["id"])
    assert started["executor"] == "codex.procedure" and started["temporal_workflow_id"]
    f.client.start_workflow.assert_not_awaited()
    run = await f.db.get_run(child["id"])
    assert run.status.value == "pending"
    # The draft step that follows finds the refresh already accounted for.
    await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "draft"})
    assert len(await refresh_runs(f)) == 1
    f.client.start_workflow.assert_not_awaited()
    assert await f.system.organic_system_refresh(str(f.parent.id)) == started


async def test_an_older_recipe_dispatches_no_refresh(publication_db, monkeypatch):
    f = await refresh_fixture(publication_db, monkeypatch, policy=organic_system.FALLBACK_POLICY)
    assert await f.system.organic_system_refresh(str(f.parent.id)) == {
        "status": "skipped",
        "reason": "not_in_pinned_recipe",
    }
    assert await refresh_runs(f) == []
