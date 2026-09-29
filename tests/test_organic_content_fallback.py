"""A v4 traffic system keeps going from the last finished plan when today's plan fails."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from test_organic_content import system_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_draft, organic_system
from tin_lite.organic_system_activities import fallback_section

FAILED_PLAN = {"status": "blocked", "reason": "research_unavailable"}


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
