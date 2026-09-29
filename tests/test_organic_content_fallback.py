"""A v4 traffic system keeps going from the last finished plan when today's plan fails."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from test_organic_content import effect, system_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_draft, organic_system
from tin_lite.organic_system_activities import fallback_section

FAILED_PLAN = {"status": "blocked", "reason": "research_unavailable"}


async def another_parent(f):
    workflow = await f.db.get_workflow(f.parent.workflow_id)
    run, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=workflow.id,
        started_by_clerk_user_id=f.parent.started_by_clerk_user_id,
        input_payload=f.parent.input,
        pinned_definition=workflow.definition,
        definition_commit_sha=f.parent.definition_commit_sha,
    )
    prepared = await f.system.saved(f.parent.id, "prepare")
    await effect(f.db, f"traffic:{run.id}:prepare", prepared)
    return run


@pytest.mark.parametrize("paused", [False, True])
async def test_fallback_preserves_the_existing_schedule(publication_db, monkeypatch, paused):
    client = SimpleNamespace(create_schedule=AsyncMock())
    f = await system_fixture(publication_db, monkeypatch, temporal=client, content=FAILED_PLAN)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', finished_at=now() WHERE id=$1", f.initial.id
    )
    first = await f.system.organic_system_weekly_articles(str(f.parent.id))
    if paused:
        await f.db.set_project_workflow_paused(
            project_workflow_id=UUID(first["project_workflow_id"]),
            project_id=f.project.id,
            paused=True,
        )
    second = await another_parent(f)
    reused = await f.system.organic_system_weekly_articles(str(second.id))
    assert reused["project_workflow_id"] == first["project_workflow_id"]
    assert reused["status"] == ("skipped" if paused else "succeeded")
    if paused:
        assert reused["reason"] == "existing_schedule_paused"
    client.create_schedule.assert_awaited_once()


async def test_concurrent_fallbacks_save_only_one_schedule(publication_db, monkeypatch):
    client = SimpleNamespace(create_schedule=AsyncMock())
    f = await system_fixture(publication_db, monkeypatch, temporal=client, content=FAILED_PLAN)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', finished_at=now() WHERE id=$1", f.initial.id
    )
    second = await another_parent(f)
    first_result, second_result = await asyncio.gather(
        f.system.organic_system_weekly_articles(str(f.parent.id)),
        f.system.organic_system_weekly_articles(str(second.id)),
    )
    assert first_result["project_workflow_id"] == second_result["project_workflow_id"]
    client.create_schedule.assert_awaited_once()


async def test_draft_and_weekly_articles_use_the_last_finished_plan(publication_db, monkeypatch):
    client = SimpleNamespace(create_schedule=AsyncMock())
    f = await system_fixture(publication_db, monkeypatch, temporal=client, content=FAILED_PLAN)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', finished_at=now() WHERE id=$1", f.initial.id
    )
    program_id = str(f.initial.project_workflow_id)

    payload = {"run_id": str(f.parent.id), "step": "draft"}
    result = await f.system.organic_system_step(payload)
    run = await f.db.get_run(UUID(result["run_id"]))
    assert run.input["program_id"] == program_id
    selected = (await f.db.get_effect(content_draft.selection_key(run.id))).result
    assert selected["mode"] == "next"

    weekly = await f.system.organic_system_weekly_articles(str(f.parent.id))
    assert weekly["status"] == "succeeded" and weekly["program_id"] == program_id
    # Chosen once: a retry reads the same program even if a newer plan finishes meanwhile.
    saved = (await f.db.get_effect(f"traffic:{f.parent.id}:content_fallback")).result
    assert saved["program"]["program_id"] == program_id

    publish = AsyncMock()
    monkeypatch.setattr("tin_lite.organic_system_activities.publish_run_report", publish)
    await f.system.organic_system_finish(str(f.parent.id))
    report = publish.await_args.kwargs["content"].decode()
    assert "## Content plan used" in report
    assert "This run's content plan did not finish" in report


async def test_an_older_recipe_still_stops_without_its_own_plan(publication_db, monkeypatch):
    f = await system_fixture(
        publication_db,
        monkeypatch,
        content=FAILED_PLAN,
        policy=organic_system.WEEKLY_POLICY,
    )
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', finished_at=now() WHERE id=$1", f.initial.id
    )
    result = await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "draft"})
    assert result == {"status": "blocked", "reason": "content_plan_unavailable"}
    assert await f.db.get_effect(f"traffic:{f.parent.id}:content_fallback") is None


async def test_no_finished_plan_means_nothing_to_draft_from(publication_db, monkeypatch):
    f = await system_fixture(publication_db, monkeypatch, content=FAILED_PLAN)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='failed' WHERE project_workflow_id=$1",
        f.initial.project_workflow_id,
    )
    result = await f.system.organic_system_step({"run_id": str(f.parent.id), "step": "draft"})
    assert result == {"status": "blocked", "reason": "content_plan_unavailable"}
    saved = (await f.db.get_effect(f"traffic:{f.parent.id}:content_fallback")).result
    assert saved == {"program": None}


def test_the_report_names_the_plan_it_used():
    lines = fallback_section(
        {"program_id": "p", "name": "Organic traffic — https://example.com/", "planned_at": None}
    )
    assert "saved in 'Organic traffic — https://example.com/'" in "\n".join(lines)
