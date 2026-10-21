"""Workflows that act through one member's own LinkedIn belong to that member (connection
collection, engagers, profile viewers, connection requests). A saved schedule runs as the member
who saved it, with their LinkedIn. The ICP file and People stay the project's.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from temporalio.service import RPCError, RPCStatusCode
from test_catalog_executor_transition import WIKI
from test_connection_collection import USER
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import fixture
from test_linkedin_per_member import TEAMMATE, add_teammate
from test_linkedin_profile_viewers import Cloud, viewers_run, work
from test_people_view import mcp, structured

from tin_lite import linkedin_profile_viewers as viewers
from tin_lite.activities import AUTHOR_LEFT, TinActivities
from tin_lite.api import router as api_router
from tin_lite.auth import AuthContext, require_user
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.connection_collection import ALREADY_SAVED, NOT_YOURS

DAILY = {"cadence": "daily", "local_time": "08:00", "timezone": "UTC"}


async def saved_viewers(f, *, by=USER):
    """The profile viewers workflow from the catalog, saved daily on the project by `by`."""

    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == viewers.KEY)
    definition, _files = builtin.definition_and_files_with_wiki(WIKI)
    await f.db.pool.execute(
        """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                definition_path,current_commit_sha,version_label,definition)
           VALUES ($1,$2,$2,$2,'registry/workflows',$3,$4,'1',$5::jsonb)
           ON CONFLICT DO NOTHING""",
        viewers.WORKFLOW_ID,
        viewers.KEY,
        builtin.definition_path,
        "e" * 40,
        json.dumps(definition),
    )
    workflow_id = await f.db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", viewers.KEY)
    configured = uuid4()
    await f.db.pool.execute(
        """INSERT INTO project_workflows
           (id, project_id, workflow_id, definition_commit_sha, name, inputs, input_schema,
            schedule, status, request_id, created_by_clerk_user_id)
           VALUES ($1,$2,$3,$4,$5,'{}'::jsonb,$6::jsonb,$7::jsonb,'active',$8,$9)""",
        configured,
        f.project.id,
        workflow_id,
        "e" * 40,
        builtin.title,
        json.dumps(definition["input_schema"]),
        json.dumps(DAILY),
        uuid4(),
        by,
    )
    return configured


class Temporal:
    """Temporal's schedules, as far as pausing one goes."""

    def __init__(self):
        self.paused = []

    def get_schedule_handle(self, schedule_id):
        paused = self.paused

        class Handle:
            async def pause(self, note=None):
                paused.append(schedule_id)

        return Handle()


def dispatcher(f):
    return TinActivities(
        database=f.db,
        storage=f.storage,
        sandboxes=None,
        settings=f.store.settings,
        integrations=SimpleNamespace(ensure_requirements=AsyncMock()),
        temporal=Temporal(),
    )


def occurrence(configured):
    return {
        "project_workflow_id": str(configured),
        "occurrence_id": f"fixture:{uuid4()}",
        "scheduled_for": datetime.now(UTC).isoformat(),
    }


async def test_a_saved_daily_read_runs_as_the_member_who_saved_it(collection_db):
    f = await fixture(collection_db)
    configured = await saved_viewers(f)
    dispatched = await dispatcher(f).dispatch_scheduled_workflow(occurrence(configured))
    run = await f.db.get_run(UUID(dispatched["run_id"]))
    assert run.trigger_source == "schedule" and run.started_by_clerk_user_id == USER
    # It reads with their own LinkedIn.
    await work(f, Cloud([])).viewers_prepare(str(run.id))
    assert (await f.db.get_run(run.id)).status.value == "running"


async def test_a_teammate_s_saved_read_runs_as_them_and_pauses_when_they_leave(collection_db):
    f = await fixture(collection_db)
    await add_teammate(f)
    configured = await saved_viewers(f, by=TEAMMATE)
    dispatched = await dispatcher(f).dispatch_scheduled_workflow(occurrence(configured))
    run = await f.db.get_run(UUID(dispatched["run_id"]))
    assert run.started_by_clerk_user_id == TEAMMATE
    # Once they leave the project, their schedule pauses and says why; nothing runs.
    await f.db.pool.execute(
        "DELETE FROM project_memberships WHERE project_id = $1 AND clerk_user_id = $2",
        f.project.id,
        TEAMMATE,
    )
    assert await dispatcher(f).dispatch_scheduled_workflow(occurrence(configured)) == {}
    paused = await f.db.get_project_workflow(configured)
    assert paused.status == "paused" and paused.last_error == AUTHOR_LEFT
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE project_workflow_id = $1", configured
        )
        == 1
    )


def http(f, user=USER, temporal=None):
    app = FastAPI()
    app.include_router(api_router)
    app.state.runtime = SimpleNamespace(
        database=f.db, storage=f.storage, temporal=temporal or Schedules()
    )
    app.state.settings = f.store.settings
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=user,
        token_type="session_token",  # noqa: S106
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://tin.test")


class Schedules(Temporal):
    """Temporal's schedules: created, updated and paused, as far as saving one goes."""

    def __init__(self):
        super().__init__()
        self.created = []

    async def create_schedule(self, schedule_id, definition):
        self.created.append(schedule_id)

    def get_schedule_handle(self, schedule_id):
        handle = super().get_schedule_handle(schedule_id)

        async def missing(*args, **kwargs):
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

        handle.update = handle.describe = handle.unpause = missing
        return handle


async def test_only_the_member_who_saved_it_changes_pauses_runs_or_removes_it(
    collection_db, monkeypatch
):
    f = await fixture(collection_db)
    await add_teammate(f)
    configured = await saved_viewers(f)
    base = f"/api/projects/{f.project.id}/workflows/{configured}"
    saved = await f.db.get_project_workflow(configured)
    async with http(f, TEAMMATE) as teammate:
        attempts = [
            await teammate.put(
                base,
                json={
                    "name": "Mine now",
                    "inputs": {},
                    "schedule": DAILY,
                    "expected_settings_revision": saved.settings_revision,
                },
            ),
            await teammate.post(f"{base}/pause"),
            await teammate.post(f"{base}/resume"),
            await teammate.post(f"{base}/skip-once"),
            await teammate.post(f"{base}/runs", headers={"Idempotency-Key": str(uuid4())}),
            await teammate.post(f"{base}/retry", headers={"Idempotency-Key": str(uuid4())}),
            await teammate.delete(
                base, params={"expected_settings_revision": saved.settings_revision}
            ),
        ]
    assert [a.status_code for a in attempts] == [403] * 7
    assert attempts[0].json()["detail"] == NOT_YOURS
    assert (await f.db.get_project_workflow(configured)).status == "active"
    # Over MCP too.
    server = mcp(f, monkeypatch, actor=TEAMMATE)
    with pytest.raises(ToolError, match="only they can change or run it"):
        await server.call_tool(
            "set_project_workflow_paused",
            {
                "project_id": str(f.project.id),
                "project_workflow_id": str(configured),
                "paused": True,
            },
        )
    # The member who saved it can.
    async with http(f) as owner:
        paused = await owner.post(f"{base}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"


async def test_each_member_saves_their_own_copy_once(collection_db):
    f = await fixture(collection_db)
    await add_teammate(f)
    await saved_viewers(f)
    body = {"workflow_id": str(viewers.WORKFLOW_ID), "name": "Profile viewers", "inputs": {}}
    async with http(f) as owner:
        again = await owner.post(
            f"/api/projects/{f.project.id}/workflows", json={**body, "request_id": str(uuid4())}
        )
    assert again.status_code == 422 and again.json()["detail"] == ALREADY_SAVED
    async with http(f, TEAMMATE) as teammate:
        theirs = await teammate.post(
            f"/api/projects/{f.project.id}/workflows", json={**body, "request_id": str(uuid4())}
        )
    assert theirs.status_code == 201, theirs.text
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM project_workflows WHERE project_id = $1 AND workflow_id = $2",
            f.project.id,
            viewers.WORKFLOW_ID,
        )
        == 2
    )


async def personal_run(f, *, by, status="running"):
    """A profile viewers run on the project, started by `by`, with one Activity line."""

    run_id = UUID(await viewers_run(f, by=by))
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status = $2, started_at = now() WHERE id = $1", run_id, status
    )
    await f.db.pool.execute(
        """INSERT INTO activity_events (project_id, run_id, event_type, details, summary, audience)
           VALUES ($1, $2, 'run_succeeded', '{}'::jsonb, '4 new profile views.', 'product')""",
        f.project.id,
        run_id,
    )
    return run_id


class Profiles:
    """Clerk's member profiles: teammates' names."""

    async def member_profile(self, member):
        return {"display_name": "Emre Example" if member == TEAMMATE else "Ege Example"}


async def test_my_system_shows_your_own_personal_workflows_and_activity_names_a_teammate_s(
    collection_db, monkeypatch
):
    f = await fixture(collection_db)
    await add_teammate(f)
    mine = await personal_run(f, by=USER)
    theirs = await personal_run(f, by=TEAMMATE)
    mine_saved = await saved_viewers(f)
    theirs_saved = await saved_viewers(f, by=TEAMMATE)
    project = f"/api/projects/{f.project.id}"
    client = http(f)
    client._transport.app.state.auth = Profiles()
    async with client as ege:
        runs = {item["id"]: item for item in (await ege.get(f"{project}/runs")).json()}
        saved = {item["id"]: item for item in (await ege.get(f"{project}/workflows")).json()}
        today = datetime.now(UTC).date()
        monday = today - timedelta(days=today.weekday())
        week_answer = await ege.get(f"{project}/week", params={"start": monday.isoformat()})
        assert week_answer.status_code == 200, week_answer.text
        week = week_answer.json()
        summary = (await ege.get(f"{project}/system")).json()
        activity = (await ege.get(f"{project}/activity")).json()
    # Both are listed, marked: the dashboard keeps only yours in My system.
    assert (runs[str(mine)]["personal"], runs[str(mine)]["mine"]) == (True, True)
    assert (runs[str(theirs)]["personal"], runs[str(theirs)]["mine"]) == (True, False)
    assert saved[str(mine_saved)]["mine"] and not saved[str(theirs_saved)]["mine"]
    # The calendar and the running count are yours alone.
    calendar = {run["id"] for run in week["runs"]}
    assert str(mine) in calendar and str(theirs) not in calendar
    assert {item["project_workflow_id"] for item in week["occurrences"]} <= {str(mine_saved)}
    running = [run for run in runs.values() if run["status"] in {"pending", "running"}]
    assert summary["running_count"] == len(running) - 1
    # Activity keeps both; a teammate's says whose LinkedIn, by first name only.
    by_run = {item["run_id"]: item for item in activity}
    assert by_run[str(mine)]["teammate"] is None
    assert by_run[str(theirs)]["teammate"] == "Emre"
    assert TEAMMATE not in json.dumps(activity)
    # Over MCP, the lists are My system's.
    server = mcp(f, monkeypatch)
    listed = structured(
        await server.call_tool("list_project_runs", {"project_id": str(f.project.id)})
    )
    assert str(mine) in {run["id"] for run in listed["result"]}
    assert str(theirs) not in {run["id"] for run in listed["result"]}
    workflows = structured(
        await server.call_tool("list_project_workflows", {"project_id": str(f.project.id)})
    )
    assert [item["id"] for item in workflows["result"]] == [str(mine_saved)]
