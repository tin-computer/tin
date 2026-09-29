"""Real disposable Postgres and MCP/HTTP, with fixture Temporal and isolated compute."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import SecretStr
from temporalio.testing import ActivityEnvironment
from test_billing import billed as billed
from test_billing import fund
from test_private_workflows import ACTOR, app, structured
from test_procedure_publication import publication_db as publication_db
from test_workflow_code import KEY, PATH, activate_code, setup

from tin_lite import code_schedules
from tin_lite.billing_contracts import NANOS_PER_DOLLAR, ProjectSpendingPolicy
from tin_lite.code_project_files import saved_source as saved_project_files
from tin_lite.schedules import WorkflowSchedule, next_run_after
from tin_lite.workflow_code import example_files

SCHEDULE = {"cadence": "daily", "local_time": "09:00", "timezone": "America/Los_Angeles"}


async def prepared(f, monkeypatch, *, model=False, connected=False):
    server, common, code = await setup(f, monkeypatch)
    if model:
        common._settings.luna_api_key = SecretStr("fixture-managed-model")
    files = example_files(KEY, model_steps=model)
    body = json.loads(files[PATH])
    body["definition"]["schedule_modes"] = ["on_demand", "daily", "weekly"]
    if connected:
        from test_project_connections import definition, integrations

        service = await integrations(f)
        common._integrations = code.services.integrations = service
        contract = definition()
        body["definition"]["integration_requirements"] = contract["integration_requirements"]
        body["definition"]["code"]["services"] = contract["code"]["services"]
    files[PATH] = json.dumps(body)
    f.revision = f.storage.repo.edit({p: raw.encode() for p, raw in files.items()})
    if model:
        selected = {"project_id": str(f.project.id), "path": PATH, "revision": f.revision}
        active = structured(
            await server.call_tool(
                "activate_workflow_package",
                {
                    **selected,
                    "expected_revision": None,
                    "request_id": str(uuid4()),
                },
            )
        )
    else:
        active = await activate_code(f, server)
    handle = SimpleNamespace(
        pause=AsyncMock(), unpause=AsyncMock(), delete=AsyncMock(), update=AsyncMock()
    )
    temporal = SimpleNamespace(create_schedule=AsyncMock(), get_schedule_handle=lambda _: handle)
    f.runtime.temporal = common._temporal = temporal
    common._integrations = f.runtime.integrations
    configured = structured(
        await server.call_tool(
            "create_project_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": active["workflow_id"],
                "name": "Daily fixture report",
                "inputs": {"minimum_cents": 1000},
                "request_id": str(uuid4()),
                "schedule": SCHEDULE,
            },
        )
    )
    return server, common, code, configured, handle


def occurrence(configured, **changes):
    return {
        "project_workflow_id": configured["id"],
        "occurrence_id": f"fixture:{uuid4()}",
        "scheduled_for": datetime.now(UTC).isoformat(),
        **changes,
    }


async def test_mcp_saved_code_schedules_at_zero_credits_and_preserves_accepted_inputs(
    billed, monkeypatch
):
    f = billed
    server, common, code, configured, handle = await prepared(f, monkeypatch)
    selected = {"project_id": str(f.project.id), "project_workflow_id": configured["id"]}
    setup_view = structured(await server.call_tool("get_code_workflow_setup", selected))
    assert setup_view["can_run"] and setup_view["can_schedule"]
    assert setup_view["estimate"]["estimated_usd"] == "0.00"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://test"
    ) as client:
        response = await client.post(
            f"/api/projects/{f.project.id}/workflow-setup",
            json={"project_workflow_id": configured["id"]},
        )
        assert response.status_code == 200 and response.json() == setup_view
    payload = occurrence(configured)
    first = await common.dispatch_scheduled_workflow(payload)
    run = await f.db.get_run(UUID(first["run_id"]))
    assert run.trigger_source == "schedule" and run.started_by_clerk_user_id == ACTOR
    assert run.input == {"minimum_cents": 1000}
    first_files = await saved_project_files(f.db, run, f.project)
    assert first_files["revision"] == f.storage.repo.head
    assert (await f.db.get_project_workflow(UUID(configured["id"]))).next_run_at > datetime.now(UTC)
    updated = structured(
        await server.call_tool(
            "update_project_workflow",
            {
                **selected,
                "name": configured["name"],
                "inputs": {"minimum_cents": 2000},
                "expected_settings_revision": configured["settings_revision"],
            },
        )
    )
    assert await common.dispatch_scheduled_workflow(payload) == first
    assert await saved_project_files(f.db, run, f.project) == first_files
    assert await common.dispatch_scheduled_workflow(occurrence(updated)) == {}  # overlap
    await ActivityEnvironment().run(code.execute, first["run_id"])
    await ActivityEnvironment().run(code.publish, first["run_id"])
    await code.project(first["run_id"])
    second = await common.dispatch_scheduled_workflow(occurrence(updated))
    next_run = await f.db.get_run(UUID(second["run_id"]))
    assert next_run.input == {"minimum_cents": 2000}
    assert next_run.definition_commit_sha == run.definition_commit_sha
    assert (await saved_project_files(f.db, next_run, f.project))["revision"] == f.storage.repo.head
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0
    assert await f.db.pool.fetchval("SELECT balance_nanos FROM billing_accounts") == 0
    result = structured(
        await server.call_tool(
            "archive_project_workflow",
            {
                **selected,
                "expected_settings_revision": updated["settings_revision"],
            },
        )
    )
    assert result["status"] == "archived" and handle.delete.await_count == 1
    assert await common.dispatch_scheduled_workflow(occurrence(updated)) == {}
    assert (await f.db.get_run(run.id)).status.value == "succeeded"


async def test_schedule_revocation_pauses_once_and_rechecks_on_resume(billed, monkeypatch):
    f = billed
    server, common, _, configured, handle = await prepared(f, monkeypatch)
    await f.db.pool.execute(
        "DELETE FROM project_memberships WHERE project_id=$1 AND clerk_user_id=$2",
        f.project.id,
        ACTOR,
    )
    payload = occurrence(configured)
    assert await common.dispatch_scheduled_workflow(payload) == {}
    assert await common.dispatch_scheduled_workflow(payload) == {}
    row = await f.db.get_project_workflow(UUID(configured["id"]))
    assert row.status == "paused" and row.next_run_at is None
    assert "author" in row.last_error
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE event_type='workflow_schedule_needs_attention'"
        )
        == 1
    )
    assert handle.pause.await_count == 2
    await f.db.grant_project_membership(project_id=f.project.id, clerk_user_id=ACTOR)
    resumed = structured(
        await server.call_tool(
            "set_project_workflow_paused",
            {
                "project_id": str(f.project.id),
                "project_workflow_id": configured["id"],
                "paused": False,
            },
        )
    )
    assert resumed["status"] == "active" and handle.unpause.await_count == 1
    assert (await common.dispatch_scheduled_workflow(occurrence(configured)))["run_id"]


async def test_schedule_without_project_revision_does_not_admit_a_run(billed, monkeypatch):
    f = billed
    _, common, _, configured, _ = await prepared(f, monkeypatch)

    async def unavailable(_repo, _branch):
        return None

    monkeypatch.setattr(f.storage, "head_sha", unavailable)
    assert await common.dispatch_scheduled_workflow(occurrence(configured)) == {}
    assert not await f.db.pool.fetchval("SELECT id FROM workflow_runs")
    assert not await f.db.pool.fetchval("SELECT run_id FROM billing_run_budgets")
    saved = await f.db.get_project_workflow(UUID(configured["id"]))
    assert saved.status == "paused" and "canonical revision" in saved.last_error


async def test_concurrent_occurrence_acceptance_wins_over_later_source_error(billed, monkeypatch):
    f = billed
    _, common, _, configured, _ = await prepared(f, monkeypatch)
    payload = occurrence(configured)
    selected = await f.db.get_project_workflow(UUID(configured["id"]))
    workflow = await f.db.get_workflow(selected.workflow_id)
    original = code_schedules.select_project_files
    accepted_run = None

    async def accept_then_fail(*, database, storage, project_id):
        nonlocal accepted_run
        source = await original(database=database, storage=storage, project_id=project_id)
        accepted_run, created = await f.db.create_run(
            project_id=selected.project_id,
            workflow_id=selected.workflow_id,
            started_by_clerk_user_id=selected.created_by_clerk_user_id,
            start_idempotency_key=f"schedule:{payload['occurrence_id']}",
            input_payload=selected.inputs,
            project_workflow_id=selected.id,
            definition_commit_sha=selected.definition_commit_sha,
            pinned_definition=workflow.definition,
            trigger_source="schedule",
            scheduled_for=datetime.fromisoformat(payload["scheduled_for"]),
            schedule_settings_revision=selected.settings_revision,
            code_project_files_source=source,
        )
        assert created
        raise ValueError("later dispatcher lost its source read")

    monkeypatch.setattr(code_schedules, "select_project_files", accept_then_fail)
    recovered = await common.dispatch_scheduled_workflow(payload)
    assert recovered["run_id"] == str(accepted_run.id)
    assert (await f.db.get_project_workflow(selected.id)).status == "active"
    assert await saved_project_files(f.db, accepted_run, f.project)


async def test_schedule_model_funding_is_fresh_and_admission_remains_authoritative(
    billed, monkeypatch
):
    f = billed
    server, common, _, configured, handle = await prepared(f, monkeypatch, model=True)
    assert configured["status"] == "paused"
    assert f.runtime.temporal.create_schedule.call_args[0][1].state.paused
    selected = {"project_id": str(f.project.id), "project_workflow_id": configured["id"]}
    with pytest.raises(ToolError, match="credits"):
        await server.call_tool("set_project_workflow_paused", {**selected, "paused": False})
    await fund(f)
    with pytest.raises(ToolError, match="standing"):
        await server.call_tool("set_project_workflow_paused", {**selected, "paused": False})
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=50 * NANOS_PER_DOLLAR,
            monthly_nanos=500 * NANOS_PER_DOLLAR,
            concurrency=5,
            schedule_max_nanos=NANOS_PER_DOLLAR,
            expected_revision=1,
        ),
    )
    await server.call_tool("set_project_workflow_paused", {**selected, "paused": False})
    run = await common.dispatch_scheduled_workflow(occurrence(configured))
    assert run["run_id"] and handle.unpause.await_count == 1
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 1
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0
    # Fresh admission catches insufficient available funds even when a positive balance
    # passed the advisory readout. No failed run/budget is inserted for this occurrence.
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='stopped' WHERE id=$1", UUID(run["run_id"])
    )
    await f.db.pool.execute("UPDATE billing_accounts SET balance_nanos=1")
    assert await common.dispatch_scheduled_workflow(occurrence(configured)) == {}
    assert await common.dispatch_scheduled_workflow(occurrence(configured)) == {}
    row = await f.db.get_project_workflow(UUID(configured["id"]))
    assert row.status == "paused" and "credits" in row.last_error.lower()
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 1
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 1


async def test_dispatch_response_loss_recovers_one_run_and_next_projection(billed, monkeypatch):
    f = billed
    _, common, _, configured, _ = await prepared(f, monkeypatch)
    create = f.db.create_run

    async def lose_response(**kwargs):
        await create(**kwargs)
        raise ConnectionError("fixture worker loss after commit")

    monkeypatch.setattr(f.db, "create_run", lose_response)
    payload = occurrence(configured)
    with pytest.raises(ConnectionError):
        await common.dispatch_scheduled_workflow(payload)
    recovered = await common.dispatch_scheduled_workflow(payload)
    assert recovered["run_id"]
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 1
    assert (await f.db.get_project_workflow(UUID(configured["id"]))).next_run_at > datetime.now(UTC)


async def test_ended_and_old_occurrences_do_not_create_backlog(billed, monkeypatch):
    f = billed
    _, common, _, configured, _ = await prepared(f, monkeypatch)
    assert (
        await common.dispatch_scheduled_workflow(
            occurrence(
                configured, scheduled_for=(datetime.now(UTC) - timedelta(days=30)).isoformat()
            )
        )
        == {}
    )
    schedule = {**SCHEDULE, "end_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    await f.db.pool.execute(
        "UPDATE project_workflows SET schedule=$2::jsonb WHERE id=$1",
        UUID(configured["id"]),
        json.dumps(schedule),
    )
    assert await common.dispatch_scheduled_workflow(occurrence(configured)) == {}
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0


def test_next_projection_matches_temporal_for_missing_and_repeated_local_times():
    spring = WorkflowSchedule(
        cadence="weekly", weekdays=["sunday"], local_time="02:30", timezone="America/Los_Angeles"
    )
    assert next_run_after(spring, datetime(2026, 3, 7, tzinfo=UTC)) == datetime(
        2026, 3, 15, 9, 30, tzinfo=UTC
    )
    fall = WorkflowSchedule(cadence="daily", local_time="01:30", timezone="America/Los_Angeles")
    assert next_run_after(fall, datetime(2026, 11, 1, 8, 45, tzinfo=UTC)) == datetime(
        2026, 11, 1, 9, 30, tzinfo=UTC
    )


async def test_connection_rotation_then_removal_pauses_without_a_backlog(billed, monkeypatch):
    from test_project_connections import CONFIG, PROVIDER

    f = billed
    server, common, _, configured, _ = await prepared(f, monkeypatch, connected=True)
    custom = f.runtime.integrations.custom
    try:
        secret = (await custom.secrets(f.project.id, ACTOR))[0]
        await custom.save_secrets(
            f.project.id,
            ACTOR,
            [
                {
                    "name": "CRM_KEY",
                    "value": "rotated-fixture-only",
                    "expected_revision": secret["revision"],
                }
            ],
        )
        first = await common.dispatch_scheduled_workflow(occurrence(configured))
        # This test covers fresh dispatch authority, not provider execution.
        await f.db.pool.execute(
            "UPDATE workflow_runs SET status='stopped' WHERE id=$1", UUID(first["run_id"])
        )
        secret = (await custom.secrets(f.project.id, ACTOR))[0]
        await custom.delete_secret(f.project.id, ACTOR, "CRM_KEY", secret["revision"])
        payload = occurrence(configured)
        assert await common.dispatch_scheduled_workflow(payload) == {}
        assert await common.dispatch_scheduled_workflow(occurrence(configured)) == {}
        row = await f.db.get_project_workflow(UUID(configured["id"]))
        assert row.status == "paused" and PROVIDER in row.last_error
        assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 1
        await custom.save_secrets(
            f.project.id,
            ACTOR,
            [
                {
                    "name": "CRM_KEY",
                    "value": "reconnected-fixture-only",
                    "expected_revision": None,
                }
            ],
        )
        connection = await f.db.get_integration_connection(
            project_id=f.project.id, provider_key=PROVIDER
        )
        await custom.save(
            f.project.id, ACTOR, PROVIDER, CONFIG, connection.configuration["revision"]
        )
        selected = {"project_id": str(f.project.id), "project_workflow_id": configured["id"]}
        await server.call_tool("set_project_workflow_paused", {**selected, "paused": False})
        assert (
            await common.dispatch_scheduled_workflow(
                occurrence(
                    configured, scheduled_for=(datetime.now(UTC) - timedelta(days=40)).isoformat()
                )
            )
            == {}
        )
        assert (await common.dispatch_scheduled_workflow(occurrence(configured)))["run_id"]
    finally:
        await f.runtime.integrations.close()


async def test_stale_occurrence_cannot_pause_or_overwrite_new_settings(billed, monkeypatch):
    from tin_lite.code_schedules import pause_for_issue

    f = billed
    server, common, _, configured, _ = await prepared(f, monkeypatch)
    old = await f.db.get_project_workflow(UUID(configured["id"]))
    selected = {"project_id": str(f.project.id), "project_workflow_id": configured["id"]}
    updated = structured(
        await server.call_tool(
            "update_project_workflow",
            {
                **selected,
                "name": "New clock",
                "inputs": {"minimum_cents": 5000},
                "schedule": {**SCHEDULE, "local_time": "13:45"},
                "expected_settings_revision": old.settings_revision,
            },
        )
    )
    await pause_for_issue(common, old, "Stale issue")
    await f.db.advance_project_workflow_schedule(
        project_workflow_id=old.id,
        next_run_at=None,
        expected_settings_revision=old.settings_revision,
    )
    row = await f.db.get_project_workflow(old.id)
    assert row.status == "active" and row.last_error is None
    assert row.next_run_at.isoformat() == updated["next_run_at"].replace("Z", "+00:00")


async def test_setup_rejects_invalid_inputs_and_cross_project_access(billed, monkeypatch):
    f = billed
    server, _, _, configured, _ = await prepared(f, monkeypatch)
    selected = {"project_id": str(f.project.id), "project_workflow_id": configured["id"]}
    with pytest.raises(ToolError):
        await server.call_tool(
            "get_code_workflow_setup", {**selected, "inputs": {"minimum_cents": "bad"}}
        )
    with pytest.raises(ToolError, match="not found"):
        await server.call_tool("get_code_workflow_setup", {**selected, "project_id": str(uuid4())})
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0


async def test_archive_retry_repairs_temporal_failure_without_future_dispatch(billed, monkeypatch):
    f = billed
    server, common, _, configured, handle = await prepared(f, monkeypatch)
    selected = {
        "project_id": str(f.project.id),
        "project_workflow_id": configured["id"],
        "expected_settings_revision": configured["settings_revision"],
    }
    handle.delete.side_effect = RuntimeError("temporary clock failure")
    with pytest.raises(ToolError, match="temporary"):
        await server.call_tool("archive_project_workflow", selected)
    assert await common.dispatch_scheduled_workflow(occurrence(configured)) == {}
    handle.delete.side_effect = None
    result = structured(await server.call_tool("archive_project_workflow", selected))
    assert result["status"] == "archived" and handle.delete.await_count == 2
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
