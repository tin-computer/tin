from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from temporalio.client import ScheduleOverlapPolicy
from test_procedure_publication import publication_db as publication_db

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.domain import (
    WEEKLY_BRIEF_WORKFLOW_NAME,
    Project,
    ProjectWorkflow,
    RunStatus,
    StaleSettingsRevisionError,
    Workflow,
    WorkflowRun,
    WorkflowStatus,
)
from tin_lite.project_workflow_operations import set_schedule_paused, sync_project_workflow
from tin_lite.run_service import start_workflow_run
from tin_lite.schedules import TemporalScheduleService, WorkflowSchedule, next_run_after
from tin_lite.weekly_brief import WeeklyBriefReporter, WeeklyBriefSource
from tin_lite.workflow_inputs import (
    WorkflowInputError,
    normalize_workflow_inputs,
    validate_input_schema,
)
from tin_lite.workflows import registered_workflow_implementations, registered_workflows


def test_weekly_schedule_preserves_local_tuesday_across_timezones() -> None:
    schedule = WorkflowSchedule(
        cadence="weekly",
        weekdays=["tuesday"],
        local_time="09:00",
        timezone="America/Los_Angeles",
    )

    next_run = next_run_after(schedule, datetime(2026, 8, 31, 20, 0, tzinfo=UTC))

    assert next_run == datetime(2026, 9, 1, 16, 0, tzinfo=UTC)


def test_schedule_rejects_ambiguous_or_invalid_shapes() -> None:
    with pytest.raises(ValidationError, match="weekly schedules require"):
        WorkflowSchedule(
            cadence="weekly",
            weekdays=[],
            local_time="09:00",
            timezone="UTC",
        )
    with pytest.raises(ValidationError, match="daily schedules do not accept"):
        WorkflowSchedule(
            cadence="daily",
            weekdays=["tuesday"],
            local_time="09:00",
            timezone="UTC",
        )


def test_temporal_schedule_uses_skip_overlap_and_one_dispatcher() -> None:
    service = TemporalScheduleService(
        client=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(task_queue="tin-lite"),  # type: ignore[arg-type]
    )
    schedule = WorkflowSchedule(
        cadence="weekly",
        weekdays=["tuesday"],
        local_time="09:00",
        timezone="America/Los_Angeles",
    )

    definition = service._definition(  # noqa: SLF001
        project_workflow_id="configured-1",
        schedule=schedule,
        paused=False,
    )

    assert definition.action.workflow == "tin.scheduled_dispatch"
    assert definition.action.args == ["configured-1"]
    assert definition.spec.time_zone_name == "America/Los_Angeles"
    assert definition.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert definition.policy.catchup_window.total_seconds() == 24 * 60 * 60
    assert definition.action.execution_timeout is None


def test_workflow_inputs_bind_project_and_apply_defaults() -> None:
    project_id = uuid4()
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project_id": {"type": "string", "format": "uuid"},
            "detail": {"type": "string", "enum": ["concise", "standard"], "default": "concise"},
        },
        "required": ["project_id"],
    }

    assert normalize_workflow_inputs(schema=schema, project_id=project_id, inputs={}) == {
        "detail": "concise"
    }
    with pytest.raises(WorkflowInputError, match="cannot be supplied"):
        normalize_workflow_inputs(
            schema=schema,
            project_id=project_id,
            inputs={"project_id": str(uuid4())},
        )


def test_workflow_ui_hints_are_versioned_inside_the_input_schema() -> None:
    weekly = next(item for item in BUILTIN_WORKFLOWS if item.key == WEEKLY_BRIEF_WORKFLOW_NAME)
    schema = weekly.input_schema
    assert schema is not None
    properties = schema["properties"]

    assert properties["detail"]["x-tin-ui"] == {"control": "select", "order": 10}
    assert properties["include_open_items"]["x-tin-ui"] == {
        "control": "segmented",
        "order": 20,
    }
    validate_input_schema(schema)

    invalid = {
        **schema,
        "properties": {
            **properties,
            "detail": {**properties["detail"], "x-tin-ui": {"control": "counter"}},
        },
    }
    with pytest.raises(WorkflowInputError, match="cannot use the 'counter' control"):
        validate_input_schema(invalid)


class FakeResponses:
    model = "gpt-6-luna"

    async def create(self, payload):
        assert payload["store"] is False
        return {
            "id": "resp_week",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                "# Acme weekly brief\n\n"
                                "## The week\n\nOne report moved.\n\n"
                                "## What moved\n\nThe scan finished.\n\n"
                                "## Needs attention\n\nNothing recorded.\n\n"
                                "## Next week\n\nReview the report."
                            ),
                        }
                    ],
                }
            ],
        }


@pytest.mark.asyncio
async def test_weekly_brief_appends_exact_sources() -> None:
    source = WeeklyBriefSource(
        label="scan report",
        artifact_ref="code.storage://projects/acme@abc/reports/SCAN.md",
        content="# Scan\n",
    )
    reporter = WeeklyBriefReporter(responses=FakeResponses(), skill_suite="evidence first")

    result = await reporter.report(
        project_name="Acme",
        period_start=datetime(2026, 8, 25, tzinfo=UTC),
        period_end=datetime(2026, 9, 1, tzinfo=UTC),
        inputs={"detail": "concise"},
        sources=[source],
    )

    assert "## Sources" in result["markdown"]
    assert source.artifact_ref in result["markdown"]
    assert result["source_refs"] == [source.artifact_ref]


def test_weekly_brief_is_an_explicit_registry_template_and_migration_is_narrow() -> None:
    weekly = next(item for item in BUILTIN_WORKFLOWS if item.key == WEEKLY_BRIEF_WORKFLOW_NAME)
    assert weekly.executor == WEEKLY_BRIEF_WORKFLOW_NAME
    assert weekly.input_schema["properties"]["detail"]["default"] == "concise"

    migration = (Path(__file__).parents[1] / "migrations" / "009_project_workflows.sql").read_text()
    assert "CREATE TABLE project_workflows" in migration
    assert "ADD COLUMN project_workflow_id" in migration
    assert "ADD COLUMN trigger_source" in migration
    assert "CREATE TABLE workflow_schedules" not in migration
    assert "CREATE TABLE workspaces" not in migration

    revision_migration = (
        Path(__file__).parents[1] / "migrations" / "010_project_workflow_settings_revision.sql"
    ).read_text()
    assert "ADD COLUMN settings_revision" in revision_migration
    assert "CREATE TABLE workflow_versions" not in revision_migration
    assert "CREATE TABLE workflow_config" not in revision_migration

    system_migration = (
        Path(__file__).parents[1] / "migrations" / "019_system_run_projection.sql"
    ).read_text()
    assert "ADD COLUMN timezone" in system_migration
    assert "ADD COLUMN skip_scheduled_for" in system_migration
    assert "ADD COLUMN progress_percent" in system_migration
    assert "ADD COLUMN result_summary" in system_migration
    assert "CREATE TABLE" not in system_migration

    surfaces_migration = (
        Path(__file__).parents[1] / "migrations" / "020_system_surfaces.sql"
    ).read_text()
    assert "CREATE TABLE saved_workflow_templates" in surfaces_migration
    assert "CREATE TABLE run_decisions" in surfaces_migration
    assert "CREATE TABLE project_mcp_usage" in surfaces_migration
    assert "CREATE TABLE workspaces" not in surfaces_migration
    assert "CREATE TABLE workflow_versions" not in surfaces_migration


def test_dispatcher_and_weekly_brief_are_registered_explicitly() -> None:
    registered_names = {item.__temporal_workflow_definition.name for item in registered_workflows()}

    assert "tin.scheduled_dispatch" in registered_names
    assert WEEKLY_BRIEF_WORKFLOW_NAME in registered_names
    assert WEEKLY_BRIEF_WORKFLOW_NAME in registered_workflow_implementations()


@pytest.mark.asyncio
async def test_retry_lineage_reaches_the_persisted_run_before_temporal_starts() -> None:
    project_id = uuid4()
    workflow_id = uuid4()
    configured_id = uuid4()
    failed_run_id = uuid4()
    workflow = Workflow(
        id=workflow_id,
        project_id=None,
        key=WEEKLY_BRIEF_WORKFLOW_NAME,
        title="Create a weekly project brief",
        description="Summarize the durable project week.",
        executor=WEEKLY_BRIEF_WORKFLOW_NAME,
        definition_repo_id="registry/workflows",
        definition_path="workflows/project.weekly_brief.json",
        current_commit_sha="d" * 40,
        version_label="1.0.0",
        definition=next(
            item.definition for item in BUILTIN_WORKFLOWS if item.key == WEEKLY_BRIEF_WORKFLOW_NAME
        ),
        status=WorkflowStatus.ACTIVE,
    )

    class Database:
        create_values: dict | None = None

        async def create_run(self, **values):
            self.create_values = values
            return (
                WorkflowRun(
                    id=uuid4(),
                    project_id=project_id,
                    workflow_id=workflow_id,
                    executor=workflow.executor,
                    definition_commit_sha=workflow.current_commit_sha,
                    temporal_workflow_id=f"{workflow.executor}:retry",
                    thread_id=str(configured_id),
                    generation=2,
                    fencing_token=2,
                    status=RunStatus.PENDING,
                    project_workflow_id=configured_id,
                    retry_of_run_id=failed_run_id,
                ),
                True,
            )

    class Temporal:
        started: str | None = None

        async def start_workflow(self, workflow_run, run_id, *, id, task_queue):
            self.started = id

    database = Database()
    temporal = Temporal()
    run = await start_workflow_run(
        runtime=SimpleNamespace(
            database=database,
            temporal=temporal,
            storage=None,
            integrations=None,
        ),
        settings=SimpleNamespace(task_queue="tin-lite-test"),
        workflow=workflow,
        project_id=project_id,
        started_by_clerk_user_id="user_test",
        input_payload={
            "detail": "concise",
            "include_open_items": True,
            "focus": "",
        },
        project_workflow_id=configured_id,
        definition_commit_sha=workflow.current_commit_sha,
        input_schema=workflow.definition["input_schema"],
        retry_of_run_id=failed_run_id,
    )

    assert run.retry_of_run_id == failed_run_id
    assert database.create_values is not None
    assert database.create_values["retry_of_run_id"] == failed_run_id
    assert temporal.started == run.temporal_workflow_id


@pytest.mark.asyncio
async def test_project_workflow_api_binds_defaults_and_reads_back_postgres_projection() -> None:
    project_id = uuid4()
    workflow_id = uuid4()
    created_at = datetime.now(UTC)
    workflow = Workflow(
        id=workflow_id,
        project_id=None,
        key=WEEKLY_BRIEF_WORKFLOW_NAME,
        title="Create a weekly project brief",
        description="Summarize the durable project week.",
        executor=WEEKLY_BRIEF_WORKFLOW_NAME,
        definition_repo_id="registry/workflows",
        definition_path="workflows/project.weekly_brief.json",
        current_commit_sha="d" * 40,
        version_label="1.0.0",
        definition=next(
            item.definition for item in BUILTIN_WORKFLOWS if item.key == WEEKLY_BRIEF_WORKFLOW_NAME
        ),
        status=WorkflowStatus.ACTIVE,
    )

    class Database:
        configured: ProjectWorkflow | None = None
        edits: list[list[str]] = []

        async def has_project_access(self, *, project_id: object, clerk_user_id: str) -> bool:
            return project_id == globals_project_id and clerk_user_id == "user_test"

        async def get_project(self, requested_id):
            if requested_id != globals_project_id:
                return None
            return Project(globals_project_id, "Tin", "projects/tin", "main")

        async def get_workflow(self, requested_id):
            return workflow if requested_id == workflow_id else None

        async def create_project_workflow(self, **values):
            assert values["inputs"] == {
                "detail": "concise",
                "include_open_items": True,
                "focus": "",
            }
            self.configured = ProjectWorkflow(
                id=uuid4(),
                project_id=project_id,
                workflow_id=workflow_id,
                workflow_key=workflow.key,
                workflow_title=workflow.title,
                workflow_description=workflow.description,
                version_label=workflow.version_label,
                definition_commit_sha=workflow.current_commit_sha or "",
                name=values["name"],
                inputs=values["inputs"],
                input_schema=values["input_schema"],
                schedule=values["schedule"],
                status="provisioning",
                temporal_schedule_id=None,
                next_run_at=None,
                last_run_id=None,
                last_run_status=None,
                last_artifact_path=None,
                last_error=None,
                settings_revision=1,
                created_by_clerk_user_id="user_test",
                created_at=created_at,
                updated_at=created_at,
            )
            self.edits.append(["workflow created"])
            return self.configured

        async def update_project_workflow(self, **values):
            assert self.configured is not None
            if values["expected_settings_revision"] != self.configured.settings_revision:
                raise StaleSettingsRevisionError(
                    "workflow settings changed after this editor was opened"
                )
            self.configured = replace(
                self.configured,
                name=values["name"],
                inputs=values["inputs"],
                schedule=values["schedule"],
                status="provisioning",
                settings_revision=self.configured.settings_revision + 1,
            )
            self.edits.append(values["changed_fields"])
            return self.configured

        async def get_project_workflow(self, project_workflow_id):
            if self.configured is None or self.configured.id != project_workflow_id:
                return None
            return self.configured

        async def project_workflow_synced(self, **values):
            assert self.configured is not None
            self.configured = replace(
                self.configured,
                status="active",
                temporal_schedule_id=values["temporal_schedule_id"],
                next_run_at=values["next_run_at"],
            )
            return self.configured

        async def list_project_workflows(self, *, project_id):
            assert project_id == globals_project_id
            return [self.configured] if self.configured is not None else []

    globals_project_id = project_id
    database = Database()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_test",
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
    )
    app.state.settings = SimpleNamespace(task_queue="tin-lite-test")
    app.state.runtime = SimpleNamespace(database=database, temporal=SimpleNamespace())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        saved = await client.post(
            f"/api/projects/{project_id}/workflows",
            json={
                "workflow_id": str(workflow_id),
                "name": "Tuesday brief",
                "request_id": str(uuid4()),
            },
        )
        listed = await client.get(f"/api/projects/{project_id}/workflows")
        edited = await client.put(
            f"/api/projects/{project_id}/workflows/{saved.json()['id']}",
            json={
                "name": "Tuesday brief",
                "inputs": {
                    "detail": "standard",
                    "include_open_items": True,
                    "focus": "",
                },
                "schedule": None,
                "expected_settings_revision": 1,
            },
        )
        unchanged = await client.put(
            f"/api/projects/{project_id}/workflows/{saved.json()['id']}",
            json={
                "name": "Tuesday brief",
                "inputs": {
                    "detail": "standard",
                    "include_open_items": True,
                    "focus": "",
                },
                "schedule": None,
                "expected_settings_revision": 2,
            },
        )
        stale = await client.put(
            f"/api/projects/{project_id}/workflows/{saved.json()['id']}",
            json={
                "name": "Stale edit",
                "inputs": {
                    "detail": "concise",
                    "include_open_items": True,
                    "focus": "",
                },
                "schedule": None,
                "expected_settings_revision": 1,
            },
        )

    assert saved.status_code == 201
    assert saved.json()["status"] == "active"
    assert saved.json()["inputs"]["detail"] == "concise"
    assert listed.headers["X-Tin-Read-Source"] == "postgres"
    assert listed.json()[0]["name"] == "Tuesday brief"
    assert saved.json()["settings_revision"] == 1
    assert edited.status_code == 200
    assert edited.json()["settings_revision"] == 2
    assert unchanged.status_code == 200
    assert unchanged.json()["settings_revision"] == 2
    assert stale.status_code == 409
    assert "changed after this editor was opened" in stale.json()["detail"]
    assert database.edits == [["workflow created"], ["detail"]]


@pytest.mark.asyncio
async def test_my_system_skip_and_remove_controls_are_project_scoped() -> None:
    project_id = uuid4()
    scheduled_id = uuid4()
    manual_id = uuid4()
    workflow_id = uuid4()
    created_at = datetime(2026, 9, 4, tzinfo=UTC)
    next_run_at = datetime(2026, 9, 5, 16, tzinfo=UTC)

    def configured(item_id: object, *, scheduled: bool) -> ProjectWorkflow:
        return ProjectWorkflow(
            id=item_id,
            project_id=project_id,
            workflow_id=workflow_id,
            workflow_key="project.weekly_brief",
            workflow_title="Create a weekly project brief",
            workflow_description="Summarize the durable project week.",
            version_label="1.0.0",
            definition_commit_sha="d" * 40,
            name="Daily brief" if scheduled else "Manual brief",
            inputs={},
            input_schema={"type": "object", "properties": {}},
            schedule=(
                {
                    "cadence": "daily",
                    "local_time": "09:00",
                    "timezone": "America/Los_Angeles",
                }
                if scheduled
                else None
            ),
            status="active",
            temporal_schedule_id=("tin-project-workflow-test" if scheduled else None),
            next_run_at=next_run_at if scheduled else None,
            last_run_id=None,
            last_run_status=None,
            last_artifact_path=None,
            last_error=None,
            settings_revision=3,
            created_by_clerk_user_id="user_test",
            created_at=created_at,
            updated_at=created_at,
        )

    class Database:
        items = {
            scheduled_id: configured(scheduled_id, scheduled=True),
            manual_id: configured(manual_id, scheduled=False),
        }
        skipped: tuple[object, datetime, datetime] | None = None
        archived: object | None = None

        async def has_project_access(self, *, project_id: object, clerk_user_id: str) -> bool:
            return project_id == globals_project_id and clerk_user_id == "user_test"

        async def get_project(self, requested_id):
            if requested_id != globals_project_id:
                return None
            return Project(globals_project_id, "Tin", "projects/tin", "main")

        async def get_project_workflow(self, item_id, *, include_archived=False):
            item = self.items.get(item_id)
            return (
                item
                if item is not None and (include_archived or item.status != "archived")
                else None
            )

        async def skip_project_workflow_once(self, **values):
            item = self.items[values["project_workflow_id"]]
            self.skipped = (
                item.id,
                values["skipped_for"],
                values["next_run_at"],
            )
            item = replace(
                item,
                skip_scheduled_for=values["skipped_for"],
                next_run_at=values["next_run_at"],
            )
            self.items[item.id] = item
            return item

        async def archive_project_workflow(self, **values):
            item = self.items[values["project_workflow_id"]]
            if values["expected_settings_revision"] != item.settings_revision:
                raise StaleSettingsRevisionError(
                    "workflow settings changed after this editor was opened"
                )
            self.archived = item.id
            self.items[item.id] = replace(item, status="archived")

    globals_project_id = project_id
    database = Database()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_test",
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
    )
    app.state.settings = SimpleNamespace(task_queue="tin-lite-test")
    app.state.runtime = SimpleNamespace(database=database, temporal=SimpleNamespace())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        skipped = await client.post(
            f"/api/projects/{project_id}/workflows/{scheduled_id}/skip-once"
        )
        stale = await client.delete(
            f"/api/projects/{project_id}/workflows/{manual_id}",
            params={"expected_settings_revision": 2},
        )
        removed = await client.delete(
            f"/api/projects/{project_id}/workflows/{manual_id}",
            params={"expected_settings_revision": 3},
        )

    assert skipped.status_code == 200
    assert skipped.json()["skip_scheduled_for"] == "2026-09-05T16:00:00Z"
    assert skipped.json()["next_run_at"] == "2026-09-06T16:00:00Z"
    assert database.skipped == (
        scheduled_id,
        next_run_at,
        datetime(2026, 9, 6, 16, tzinfo=UTC),
    )
    assert stale.status_code == 409
    assert removed.status_code == 204
    assert database.archived == manual_id


@pytest.mark.asyncio
async def test_resync_and_resume_keep_an_armed_skip_off_the_next_run(monkeypatch) -> None:
    schedule = WorkflowSchedule(cadence="daily", local_time="09:00", timezone="UTC")
    # Pin the clock: Monday 07:00, before the skipped 09:00 run.
    now = datetime(2026, 9, 28, 7, tzinfo=UTC)
    monkeypatch.setattr(
        "tin_lite.project_workflow_operations.next_run_after",
        lambda schedule, after=None: next_run_after(schedule, after or now),
    )
    skipped_for = next_run_after(schedule, now)
    created_at = datetime(2026, 9, 4, tzinfo=UTC)
    configured = ProjectWorkflow(
        id=uuid4(),
        project_id=uuid4(),
        workflow_id=uuid4(),
        workflow_key="project.weekly_brief",
        workflow_title="Create a weekly project brief",
        workflow_description="Summarize the durable project week.",
        version_label="1.0.0",
        definition_commit_sha="d" * 40,
        name="Daily brief",
        inputs={},
        input_schema={"type": "object", "properties": {}},
        schedule=schedule.model_dump(mode="json"),
        status="active",
        temporal_schedule_id="tin-project-workflow-test",
        next_run_at=next_run_after(schedule, skipped_for),
        last_run_id=None,
        last_run_status=None,
        last_artifact_path=None,
        last_error=None,
        settings_revision=3,
        created_by_clerk_user_id="user_test",
        created_at=created_at,
        updated_at=created_at,
        skip_scheduled_for=skipped_for,
    )

    class Database:
        def __init__(self) -> None:
            self.synced: list[datetime | None] = []

        async def project_workflow_synced(self, **values):
            self.synced.append(values["next_run_at"])
            return configured

    handle = SimpleNamespace(update=AsyncMock(), unpause=AsyncMock())
    database = Database()
    runtime = SimpleNamespace(
        database=database, temporal=SimpleNamespace(get_schedule_handle=lambda _id: handle)
    )
    settings = SimpleNamespace(task_queue="tin-lite-test")

    await sync_project_workflow(
        runtime=runtime,
        settings=settings,
        configured=configured,
        previous_schedule=configured.schedule,
    )
    await set_schedule_paused(
        runtime=runtime,
        settings=settings,
        configured=replace(configured, status="paused", next_run_at=None),
        paused=False,
    )

    assert database.synced == [next_run_after(schedule, skipped_for)] * 2


async def test_schedule_edit_disarms_skip_once_but_rename_keeps_it(publication_db) -> None:
    db = publication_db
    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == WEEKLY_BRIEF_WORKFLOW_NAME)
    workflow = await db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=builtin.key,
        title=builtin.title,
        description=builtin.description,
        executor=builtin.executor,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha="a" * 40,
        version_label="1",
        definition=builtin.definition,
    )
    project = await db.create_project(name="Skip fixture", state_repo_id=f"projects/{uuid4()}")
    inputs = normalize_workflow_inputs(
        schema=builtin.input_schema, project_id=project.id, inputs={}
    )
    schedule = {"cadence": "daily", "local_time": "09:00", "timezone": "UTC"}
    configured = await db.create_project_workflow(
        project_id=project.id,
        workflow_id=workflow.id,
        definition_commit_sha="a" * 40,
        name="Daily brief",
        inputs=inputs,
        input_schema=builtin.input_schema,
        schedule=schedule,
        request_id=uuid4(),
        created_by_clerk_user_id="user_fixture",
    )
    skipped_for = datetime(2026, 9, 28, 9, tzinfo=UTC)
    await db.project_workflow_synced(
        project_workflow_id=configured.id,
        temporal_schedule_id=TemporalScheduleService.schedule_id(str(configured.id)),
        next_run_at=skipped_for,
    )
    configured = await db.skip_project_workflow_once(
        project_workflow_id=configured.id,
        project_id=project.id,
        skipped_for=skipped_for,
        next_run_at=datetime(2026, 9, 29, 9, tzinfo=UTC),
        clerk_user_id="user_fixture",
        workflow_key=builtin.key,
        workflow_title=builtin.title,
    )
    edit = dict(
        project_workflow_id=configured.id,
        project_id=project.id,
        name="Morning brief",
        inputs=inputs,
        clerk_user_id="user_fixture",
        workflow_key=builtin.key,
        workflow_title=builtin.title,
    )

    renamed = await db.update_project_workflow(
        **edit,
        schedule=schedule,
        expected_settings_revision=configured.settings_revision,
        changed_fields=["name"],
    )
    assert renamed.skip_scheduled_for == skipped_for

    # The skipped 09:00 no longer exists, so Tuesday's 08:00 must not be swallowed in its place.
    moved = await db.update_project_workflow(
        **edit,
        schedule={**schedule, "local_time": "08:00"},
        expected_settings_revision=renamed.settings_revision,
        changed_fields=["schedule"],
    )
    assert moved.skip_scheduled_for is None
    assert not await db.consume_project_workflow_skip(
        project_workflow_id=configured.id,
        scheduled_for=datetime(2026, 9, 29, 8, tzinfo=UTC),
    )
