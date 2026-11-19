"""Engagers runs daily: a new copy starts daily, and `schedule-linkedin-engagers` gives the
copies members saved before 1.5.0 a daily schedule as their member, at their own minute."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from test_catalog_executor_transition import WIKI
from test_connection_collection import USER
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import fixture
from test_linkedin_per_member import TEAMMATE, add_teammate
from test_personal_linkedin_workflows import Schedules, dispatcher, occurrence

from tin_lite import linkedin_engagers as engagers
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.linkedin_engagers_schedule import schedule_saved_copies

CURRENT = "f" * 40
OLD = "d" * 40


def builtin():
    return next(item for item in BUILTIN_WORKFLOWS if item.key == engagers.KEY)


async def catalog_engagers(f):
    """The engagers workflow as the catalog publishes it today, at CURRENT."""

    definition, _files = builtin().definition_and_files_with_wiki(WIKI)
    await f.db.pool.execute(
        """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                definition_path,current_commit_sha,version_label,definition)
           VALUES ($1,$2,$2,$2,'registry/workflows',$3,$4,$5,$6::jsonb)""",
        engagers.WORKFLOW_ID,
        engagers.KEY,
        builtin().definition_path,
        CURRENT,
        builtin().version_label,
        json.dumps(definition),
    )
    return definition


async def saved_before_daily(f, definition, *, by=USER, status="active"):
    """A copy saved under 1.4.0: pinned to OLD, with no schedule."""

    configured = uuid4()
    await f.db.pool.execute(
        """INSERT INTO project_workflows
           (id, project_id, workflow_id, definition_commit_sha, name, inputs, input_schema,
            schedule, status, request_id, created_by_clerk_user_id)
           VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,NULL,$8,$9,$10)""",
        configured,
        f.project.id,
        engagers.WORKFLOW_ID,
        OLD,
        builtin().title,
        json.dumps({"project_id": str(f.project.id)}),
        json.dumps(definition["input_schema"]),
        status,
        uuid4(),
        by,
    )
    return configured


def runtime(f, temporal):
    return SimpleNamespace(database=f.db, storage=f.storage, temporal=temporal)


def settings(f):
    """The fixture's settings, with the task queue Temporal's calendar names."""

    return SimpleNamespace(**vars(f.store.settings), task_queue="tin-test")


def test_engagers_can_run_daily_and_a_new_copy_starts_daily():
    definition = builtin().definition
    assert definition["schedule_modes"] == ["on_demand", "daily"]
    # Profile viewers starts at 08:00; a member's engagers read starts later.
    assert definition["default_schedule"] == {"cadence": "daily", "local_time": "13:00"}
    assert builtin().version_label == "1.5.0"


def test_each_member_reads_at_their_own_steady_minute_inside_the_window():
    project = UUID("ac51868d-1297-52c9-b265-e3f038caa55e")
    times = {engagers.daily_time(project, f"user_{index}") for index in range(40)}
    assert all("11:00" <= value < "16:00" for value in times)
    # Spread, not stacked: forty members land on many different minutes.
    assert len(times) > 30
    assert engagers.daily_time(project, "user_1") == engagers.daily_time(project, "user_1")


async def test_copies_saved_before_daily_run_daily_as_their_member(collection_db):
    f = await fixture(collection_db)
    definition = await catalog_engagers(f)
    await add_teammate(f)
    mine = await saved_before_daily(f, definition)
    paused = await saved_before_daily(f, definition, by=TEAMMATE, status="paused")
    left = await saved_before_daily(f, definition, by="user_WhoLeft")
    temporal = Schedules()

    # A dry run lists what it would do and changes nothing.
    lines = {
        line["project_workflow_id"]: line
        for line in await schedule_saved_copies(
            runtime=runtime(f, temporal), settings=settings(f), apply=False
        )
    }
    at = engagers.daily_time(f.project.id, USER)
    assert lines[str(mine)]["outcome"] == "would schedule"
    assert lines[str(mine)]["schedule"] == f"daily {at} UTC"
    assert lines[str(paused)]["outcome"] == "left as it is (paused)"
    assert lines[str(left)]["outcome"] == "left as it is (its member left the project)"
    assert (await f.db.get_project_workflow(mine)).schedule is None
    assert temporal.created == []

    lines = {
        line["project_workflow_id"]: line["outcome"]
        for line in await schedule_saved_copies(
            runtime=runtime(f, temporal), settings=settings(f), apply=True
        )
    }
    assert lines == {
        str(mine): "scheduled",
        str(paused): "left as it is (paused)",
        str(left): "left as it is (its member left the project)",
    }
    configured = await f.db.get_project_workflow(mine)
    # It moves to the definition that allows daily, so dispatch and later edits accept it.
    assert configured.definition_commit_sha == CURRENT
    assert configured.schedule == {"cadence": "daily", "local_time": at, "timezone": "UTC"}
    assert configured.status == "active" and configured.next_run_at is not None
    assert temporal.created == [f"tin-lite-project-workflow:{mine}"]
    summary = await f.db.pool.fetchval(
        "SELECT summary FROM activity_events WHERE project_id=$1 "
        "AND event_type='workflow_settings_updated' ORDER BY created_at DESC LIMIT 1",
        f.project.id,
    )
    assert summary == (
        f"{builtin().title} now runs daily at {at} (UTC). "
        "Change or remove the schedule on the workflow."
    )
    for untouched in (paused, left):
        assert (await f.db.get_project_workflow(untouched)).definition_commit_sha == OLD

    # Running it again changes nothing.
    again = await schedule_saved_copies(
        runtime=runtime(f, temporal), settings=settings(f), apply=True
    )
    assert {line["project_workflow_id"]: line["outcome"] for line in again}[str(mine)] == (
        "already scheduled"
    )
    assert len(temporal.created) == 1

    # Its occurrence starts a run as the member who saved it.
    dispatched = await dispatcher(f).dispatch_scheduled_workflow(occurrence(mine))
    run = await f.db.get_run(UUID(dispatched["run_id"]))
    assert run.trigger_source == "schedule" and run.started_by_clerk_user_id == USER


async def test_a_copy_whose_inputs_no_longer_fit_is_left_as_it_is(collection_db):
    f = await fixture(collection_db)
    definition = await catalog_engagers(f)
    changed = await saved_before_daily(f, definition)
    await f.db.pool.execute(
        "UPDATE project_workflows SET input_schema = $2::jsonb WHERE id = $1",
        changed,
        json.dumps({**definition["input_schema"], "additionalProperties": True}),
    )
    lines = await schedule_saved_copies(
        runtime=runtime(f, Schedules()), settings=settings(f), apply=True
    )
    assert lines[0]["outcome"] == "left as it is (its inputs changed)"
    assert (await f.db.get_project_workflow(changed)).schedule is None
