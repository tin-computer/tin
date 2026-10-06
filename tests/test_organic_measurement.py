"""v7: the traffic system runs the traffic snapshot and Page decisions and saves both weekly."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

from test_organic_weekly_refresh import refresh_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import organic_system
from tin_lite.organic_system_activities import measure_time
from tin_lite.schedules import WorkflowSchedule

SNAPSHOT, DECISIONS = organic_system.SNAPSHOT_KEY, organic_system.DECISIONS_KEY


async def fixture(db, monkeypatch, *, policy=organic_system.POLICY):
    # The fixture's integration lookup reports every provider connected, Search Console too.
    return await refresh_fixture(db, monkeypatch, policy=policy)


async def runs_of(f, key):
    return await f.db.pool.fetch(
        "SELECT r.id, r.project_workflow_id, r.input FROM workflow_runs r "
        "JOIN workflows w ON w.id=r.workflow_id WHERE w.key=$1 ORDER BY r.created_at",
        key,
    )


async def measure(f, step):
    return await f.system.organic_system_measurement({"run_id": str(f.parent.id), "step": step})


def test_measurement_runs_before_the_refresh_in_order():
    assert measure_time("10:00", "snapshot") == "08:00"
    assert measure_time("10:00", "decisions") == "09:00"
    # Early refreshes squeeze both to the start of the day, the snapshot still first.
    assert measure_time("01:00", "snapshot") == "00:00"
    assert measure_time("01:00", "decisions") == "00:30"
    assert measure_time("00:15", "decisions") == "00:30"


def test_only_v7_measures():
    assert organic_system.measures_pages(organic_system.POLICY)
    for policy in (
        organic_system.WEBSITE_POLICY,
        organic_system.REFRESH_POLICY,
        organic_system.LEGACY_POLICY,
    ):
        assert not organic_system.measures_pages(policy)
    assert organic_system.child_executor(SNAPSHOT) == "workflow.code"
    assert organic_system.child_executor(DECISIONS) == "workflow.code"


async def test_the_system_saves_both_weekly_and_prepares_each_run(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    before = datetime.now(UTC)
    snapshot = await measure(f, "snapshot")
    decisions = await measure(f, "decisions")
    for step, key, result, at in (
        ("snapshot", SNAPSHOT, snapshot, "08:00"),
        ("decisions", DECISIONS, decisions, "09:00"),
    ):
        assert result["status"] == "succeeded" and not result.get("reused")
        assert result["executor"] == "workflow.code" and result["temporal_workflow_id"]
        configured = await f.db.get_project_workflow(UUID(result["project_workflow_id"]))
        assert configured.workflow_key == key and configured.status == "active"
        schedule = WorkflowSchedule.model_validate(configured.schedule)
        # The refresh's day, ahead of its 10:00 slot; the next scheduled run is a week out.
        assert schedule.weekdays == [before.strftime("%A").casefold()]
        assert schedule.local_time == at and schedule.timezone == "UTC"
        assert schedule.start_at >= before.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=7)
        [child] = await runs_of(f, key)
        assert str(child["id"]) == result["run_id"]
        assert child["project_workflow_id"] == configured.id
        # Prepared for the parent to dispatch as its child, not started on its own.
        assert (await f.db.get_run(child["id"])).status.value == "pending"
        # A retried step returns the same run and saves nothing new.
        assert await measure(f, step) == result
        assert len(await runs_of(f, key)) == 1
    configured = await f.db.get_project_workflow(UUID(snapshot["project_workflow_id"]))
    assert configured.inputs["website_hosts"] == ["example.com"]
    f.client.start_workflow.assert_not_awaited()

    publish = AsyncMock()
    monkeypatch.setattr("tin_lite.organic_system_activities.publish_run_report", publish)
    await f.system.organic_system_finish(str(f.parent.id))
    report = publish.await_args.kwargs["content"].decode()
    assert "## Weekly measurement" in report and "### Page decisions" in report
    assert "ahead of the weekly page refresh" in report
    assert snapshot["project_workflow_id"] in report


async def test_an_existing_weekly_snapshot_is_kept_and_run_now(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    first = await measure(f, "snapshot")
    # A later system run finds the saved schedule: it keeps it and measures with its inputs.
    prepared = (await f.db.get_effect(f"traffic:{f.parent.id}:prepare")).result
    later = replace(f.parent, id=UUID(int=f.parent.id.int ^ 1))
    result = await f.system._measurement(later, prepared, "snapshot")
    assert result["reused"] is True
    assert result["project_workflow_id"] == first["project_workflow_id"]
    runs = await runs_of(f, SNAPSHOT)
    assert len(runs) == 2
    assert {r["project_workflow_id"] for r in runs} == {UUID(first["project_workflow_id"])}


async def test_a_pinned_v6_recipe_measures_nothing(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, policy=organic_system.WEBSITE_POLICY)
    for step in organic_system.MEASURE_STEPS:
        assert await measure(f, step) == {"status": "skipped", "reason": "not_in_pinned_recipe"}
    assert await runs_of(f, SNAPSHOT) == [] and await runs_of(f, DECISIONS) == []


async def test_without_temporal_measurement_is_blocked_not_guessed(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    f.system.temporal = None
    assert await measure(f, "snapshot") == {"status": "blocked", "reason": "scheduling_unavailable"}
    assert await runs_of(f, SNAPSHOT) == []


async def test_without_search_console_measurement_is_blocked_with_its_reason(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    from tin_lite.integrations import IntegrationError

    monkeypatch.setattr(
        f.system.integrations,
        "ensure_requirements",
        AsyncMock(side_effect=IntegrationError("Google Search Console is not connected")),
    )
    result = await measure(f, "snapshot")
    assert result["status"] == "blocked"
    assert result["reason"] == "search_console_unavailable"
    # Nothing is saved that could only fail each week.
    assert await runs_of(f, SNAPSHOT) == []
    assert not await f.db.pool.fetchval(
        "SELECT count(*) FROM project_workflows pw JOIN workflows w ON w.id=pw.workflow_id "
        "WHERE w.key=$1",
        SNAPSHOT,
    )
