"""The organic system saves weekly drafting: disposable Postgres, fixture Temporal client."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from test_organic_content import system_fixture
from test_private_workflows import ACTOR
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_draft, organic_system
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_system_activities import founder_timezone, weekly_section
from tin_lite.schedules import WorkflowSchedule
from tin_lite.workflow_definitions import ensure_schedule_allowed
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs

WEEKLY = WorkflowSchedule(
    cadence="weekly", weekdays=["tuesday"], local_time="10:00", timezone="UTC"
)


def definition(key):
    return next(w for w in BUILTIN_WORKFLOWS if w.key == key).definition


def test_catalog_offers_weekly_drafting_and_the_v3_recipe():
    recipe = definition(organic_system.KEY)
    assert recipe["organic_system_policy"] == organic_system.POLICY
    assert recipe["organic_system_policy"]["schedule"] == "weekly_articles"
    with pytest.raises(WorkflowInputError, match="does not support weekly"):
        ensure_schedule_allowed(recipe, WEEKLY)  # The recipe itself stays manual-only.
    inputs = normalize_workflow_inputs(
        schema=recipe["input_schema"],
        project_id=uuid4(),
        inputs={
            "site_url": "https://example.com/",
            "market": "US",
            "buyer_context": "Useful software for independent consultants.",
            "start_date": "2026-09-14",
        },
    )
    assert inputs["article_weekdays"] == ["tuesday"]
    assert inputs["article_local_time"] == "10:00"
    organic_system.check_inputs(inputs)
    for bad in (["tuesday", "tuesday"], ["Tuesday"]):
        with pytest.raises(WorkflowInputError):
            normalize_workflow_inputs(
                schema=recipe["input_schema"],
                project_id=uuid4(),
                inputs={**inputs, "article_weekdays": bad},
            )
        with pytest.raises(ValueError, match="drafting weekday"):
            organic_system.check_inputs({**inputs, "article_weekdays": bad})


async def test_founder_timezone_prefers_start_here_then_schedules_then_project():
    def database(*values, project="UTC"):
        return SimpleNamespace(
            pool=SimpleNamespace(fetchval=AsyncMock(side_effect=list(values))),
            get_project=AsyncMock(return_value=SimpleNamespace(timezone=project)),
        )

    project_id = uuid4()
    assert (
        await founder_timezone(database("America/Los_Angeles", "Europe/Berlin"), project_id)
        == "America/Los_Angeles"
    )
    assert await founder_timezone(database(None, "Europe/Berlin"), project_id) == "Europe/Berlin"
    assert (
        await founder_timezone(database("Not/AZone", None, project="Asia/Tokyo"), project_id)
        == "Asia/Tokyo"
    )
    assert await founder_timezone(database(None, None, project=None), project_id) == "UTC"


def test_weekly_section_names_days_time_and_the_review_hold():
    text = "\n".join(
        weekly_section(
            {
                "status": "succeeded",
                "project_workflow_id": "p",
                "weekdays": ["tuesday", "thursday"],
                "local_time": "10:00",
                "timezone": "America/Los_Angeles",
                "next_run_at": "2026-10-06T17:00:00+00:00",
            }
        )
    )
    assert "every Tuesday and Thursday at 10:00 (America/Los_Angeles), starting 2026-10-06." in text
    assert "no new draft starts while one is waiting" in text
    skipped = weekly_section({"status": "skipped", "reason": "weekly_articles_off"})
    assert "Reason: weekly articles off." in skipped


async def test_system_saves_one_weekly_schedule_for_its_program(publication_db, monkeypatch):
    client = SimpleNamespace(create_schedule=AsyncMock())
    f = await system_fixture(
        publication_db,
        monkeypatch,
        temporal=client,
        inputs={"article_weekdays": ["tuesday", "thursday"], "article_local_time": "09:30"},
    )
    before = datetime.now(UTC)
    result = await f.system.organic_system_weekly_articles(str(f.parent.id))
    assert await f.system.organic_system_weekly_articles(str(f.parent.id)) == result
    program_id = str(f.initial.project_workflow_id)
    assert result["status"] == "succeeded" and result["program_id"] == program_id
    configured = await f.db.get_project_workflow(UUID(result["project_workflow_id"]))
    assert configured.workflow_key == content_draft.KEY and configured.status == "active"
    assert configured.created_by_clerk_user_id == ACTOR
    assert configured.inputs["program_id"] == program_id
    assert configured.inputs["item_id"] == "" and configured.inputs["delivery"] == "program"
    schedule = WorkflowSchedule.model_validate(configured.schedule)
    assert schedule.weekdays == ["tuesday", "thursday"] and schedule.local_time == "09:30"
    assert schedule.timezone == result["timezone"] == "UTC"
    # Never on top of the system's own first article.
    assert schedule.start_at >= before + timedelta(days=7)
    assert configured.next_run_at >= schedule.start_at
    assert result["next_run_at"] == configured.next_run_at.isoformat()
    client.create_schedule.assert_awaited_once()
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM project_workflows WHERE project_id=$1 AND workflow_id=$2",
            f.project.id,
            f.workflow.id,
        )
        == 1
    )
    facts = await organic_system.system_facts(
        database=f.db, project_id=f.project.id, run_id=f.parent.id
    )
    assert facts["weekly_articles"] == result

    publish = AsyncMock()
    monkeypatch.setattr("tin_lite.organic_system_activities.publish_run_report", publish)
    await f.system.organic_system_finish(str(f.parent.id))
    report = publish.await_args.kwargs["content"].decode()
    assert "## Weekly articles" in report and result["project_workflow_id"] in report
    assert "every Tuesday and Thursday at 09:30 (UTC)" in report


@pytest.mark.parametrize(
    "case,status,reason",
    [
        ("off", "skipped", "weekly_articles_off"),
        ("no_plan", "skipped", "content_plan_unavailable"),
        ("v2", "skipped", "not_in_pinned_recipe"),
        ("no_temporal", "blocked", "scheduling_unavailable"),
    ],
)
async def test_system_saves_nothing_when_weekly_drafting_does_not_apply(
    publication_db, monkeypatch, case, status, reason
):
    client = SimpleNamespace(create_schedule=AsyncMock())
    f = await system_fixture(
        publication_db,
        monkeypatch,
        temporal=None if case == "no_temporal" else client,
        inputs={"article_weekdays": []} if case == "off" else None,
    )
    if case == "no_plan":
        await f.db.pool.execute(
            "DELETE FROM effect_receipts WHERE execution_key=$1",
            f"traffic:{f.parent.id}:step:content",
        )
    if case == "v2":
        await f.db.pool.execute(
            "UPDATE effect_receipts SET result=jsonb_set(result, '{policy}', $2::jsonb) "
            "WHERE execution_key=$1",
            f"traffic:{f.parent.id}:prepare",
            json.dumps(organic_system.CONTENT_POLICY),
        )
    result = await f.system.organic_system_weekly_articles(str(f.parent.id))
    assert result == {"status": status, "reason": reason}
    client.create_schedule.assert_not_awaited()
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM project_workflows WHERE project_id=$1 AND workflow_id=$2",
            f.project.id,
            f.workflow.id,
        )
        == 0
    )
    publish = AsyncMock()
    monkeypatch.setattr("tin_lite.organic_system_activities.publish_run_report", publish)
    await f.system.organic_system_finish(str(f.parent.id))
    report = publish.await_args.kwargs["content"].decode()
    # A v2 parent that reaches the new step does not grow a section it never promised.
    assert ("## Weekly articles" in report) == (case != "v2")
    assert "Plan dates do not automatically generate the rest of the roadmap." in report


async def test_failed_schedule_save_is_recorded_and_leaves_no_silent_configuration(
    publication_db, monkeypatch
):
    client = SimpleNamespace(create_schedule=AsyncMock(side_effect=RuntimeError("down")))
    f = await system_fixture(publication_db, monkeypatch, temporal=client)
    with pytest.raises(RuntimeError, match="down"):
        await f.system.organic_system_weekly_articles(str(f.parent.id))
    saved = await f.db.pool.fetchrow(
        "SELECT status, temporal_schedule_id FROM project_workflows "
        "WHERE project_id=$1 AND workflow_id=$2",
        f.project.id,
        f.workflow.id,
    )
    assert saved["status"] == "provisioning" and saved["temporal_schedule_id"] is None
    await f.system.organic_system_weekly_articles_failure(str(f.parent.id))
    await f.system.organic_system_weekly_articles_failure(str(f.parent.id))
    failed = {"status": "failed", "reason": "weekly_schedule_not_saved"}
    assert await f.system.organic_system_weekly_articles(str(f.parent.id)) == failed
    assert (
        await f.db.pool.fetchval(
            "SELECT status FROM project_workflows WHERE project_id=$1 AND workflow_id=$2",
            f.project.id,
            f.workflow.id,
        )
        == "archived"
    )
    facts = await organic_system.system_facts(
        database=f.db, project_id=f.project.id, run_id=f.parent.id
    )
    assert facts["weekly_articles"] == failed
