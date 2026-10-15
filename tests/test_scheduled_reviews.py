"""Real Temporal histories and disposable Postgres; no paid model calls."""

import asyncio
import shutil
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import ResetReapplyType
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_procedure_publication import publication_db as publication_db

from tin_lite.activities import TinActivities
from tin_lite.activity_lanes import ActivityLaneInterceptor, trusted_task_queue, workflow_runner
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.codex_execution import ProjectCodexExecution
from tin_lite.domain import RunStatus
from tin_lite.schedules import ScheduledWorkflowSkip, TemporalScheduleService, WorkflowSchedule
from tin_lite.workflow_inputs import normalize_workflow_inputs
from tin_lite.workflows import CodexProcedureWorkflow, ScheduledDispatchWorkflow

SCHEDULE = WorkflowSchedule(cadence="daily", local_time="09:00", timezone="UTC")
# The procedure's drafting steps, which a review, restart or reset must never repeat.
DRAFTING = (
    "prepare_codex_procedure",
    "create_codex_procedure_sandbox",
    "persist_codex_procedure_artifact",
    "commit_codex_procedure_artifact",
)


class ReviewActivities:
    def __init__(self):
        self.run_id = str(uuid4())
        self.project = str(uuid4())
        self.child_id = f"codex.procedure:{self.run_id}"
        self.ready = asyncio.Event()
        self.calls = Counter()
        self.status = "pending"

    @activity.defn(name="dispatch_scheduled_workflow")
    async def dispatch(self, payload: dict) -> dict:
        self.calls["dispatch"] += 1
        return {
            "run_id": self.run_id,
            "executor": "codex.procedure",
            "temporal_workflow_id": self.child_id,
        }

    def registered(self):
        def stub(name):
            @activity.defn(name=name)
            async def execute(value):
                self.calls[name] += 1
                if name == "prepare_codex_procedure":
                    return False
                if name == "resolve_codex_project":
                    return self.project
                if name == "request_codex_procedure_review":
                    self.status = "needs_input"
                    self.ready.set()
                    return True
                if name == "project_codex_procedure_result":
                    self.status = "succeeded"
                return None

            return execute

        return [self.dispatch] + [
            stub(name)
            for name in (
                "resolve_codex_project",
                *DRAFTING,
                "request_codex_procedure_review",
                "record_codex_procedure_approval",
                "project_codex_procedure_result",
                "project_codex_procedure_failure",
                "deliver_content_draft",
            )
        ]


@asynccontextmanager
async def worker(env, queue, activities):
    registered = activities.registered()
    async with (
        Worker(
            env.client,
            task_queue=queue,
            workflows=[ScheduledDispatchWorkflow, CodexProcedureWorkflow, ProjectCodexExecution],
            activities=registered,
            workflow_runner=workflow_runner(),
            interceptors=[ActivityLaneInterceptor()],
            max_cached_workflows=0,
        ),
        Worker(env.client, task_queue=trusted_task_queue(queue), activities=registered),
    ):
        yield


async def review_boundary(child):
    # The activity's local event precedes its completion on the server.
    async with asyncio.timeout(20):
        while True:
            history = await child.fetch_history()
            if history.events[-1].HasField("workflow_task_completed_event_attributes"):
                return history
            await asyncio.sleep(0.05)


async def test_review_survives_25_hours_and_worker_restart_then_approves():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"review-{uuid4()}"
        activities = ReviewActivities()
        service = TemporalScheduleService(
            client=env.client, settings=SimpleNamespace(task_queue=queue)
        )
        definition = service._definition(
            project_workflow_id=str(uuid4()), schedule=SCHEDULE, paused=False
        )
        async with worker(env, queue, activities):
            parent = await env.client.start_workflow(
                definition.action.workflow,
                args=definition.action.args,
                id=definition.action.id,
                task_queue=queue,
                execution_timeout=definition.action.execution_timeout,
            )
            await asyncio.wait_for(activities.ready.wait(), 20)
            child = env.client.get_workflow_handle(activities.child_id)
            history = await review_boundary(child)
            await env.sleep(timedelta(hours=25))
            assert (await parent.describe()).status == WorkflowExecutionStatus.RUNNING
            assert (await child.describe()).status == WorkflowExecutionStatus.RUNNING
            assert activities.status == "needs_input"
            assert activities.calls["record_codex_procedure_approval"] == 0
        await Replayer(
            workflows=[CodexProcedureWorkflow],
            workflow_runner=workflow_runner(),
            interceptors=[ActivityLaneInterceptor()],
        ).replay_workflow(history)
        async with worker(env, queue, activities):
            await child.signal("approve")
            await asyncio.wait_for(parent.result(), 20)
        assert activities.status == "succeeded"
        for name in (
            *DRAFTING,
            "request_codex_procedure_review",
            "record_codex_procedure_approval",
            "project_codex_procedure_result",
            "deliver_content_draft",
        ):
            assert activities.calls[name] == 1
        assert activities.calls["project_codex_procedure_failure"] == 0


@pytest.fixture
async def schedule_env():
    # Download the SDK's dev server if the CLI is absent, so this also runs in CI.
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=shutil.which("temporal"), dev_server_log_level="error"
    ) as env:
        yield env


async def test_stored_schedule_migration_preserves_state_payloads_and_skip_overlap(schedule_env):
    env = schedule_env
    queue, configured = f"schedule-{uuid4()}", str(uuid4())
    service = TemporalScheduleService(client=env.client, settings=SimpleNamespace(task_queue=queue))
    legacy = service._definition(project_workflow_id=configured, schedule=SCHEDULE, paused=True)
    legacy.action.execution_timeout = timedelta(hours=24)
    legacy.action.memo = {"fixture": "preserve me"}
    legacy.state.note = "Founder paused this schedule"
    handle = await env.client.create_schedule(service.schedule_id(configured), legacy)
    before = (await handle.describe()).schedule
    assert await service.remove_legacy_review_timeout(configured)
    assert (await handle.describe()).schedule.action.execution_timeout == timedelta(hours=24)
    assert await service.remove_legacy_review_timeout(configured, apply=True)
    after = (await handle.describe()).schedule
    assert not after.action.execution_timeout
    assert (after.spec, after.policy, after.state) == (before.spec, before.policy, before.state)
    assert after.action.args == before.action.args
    assert after.action.memo == before.action.memo
    assert not await service.remove_legacy_review_timeout(configured, apply=True)
    activities = ReviewActivities()
    async with worker(env, queue, activities):
        await handle.trigger()
        await asyncio.wait_for(activities.ready.wait(), 20)
        child = env.client.get_workflow_handle(activities.child_id)
        await review_boundary(child)
        await handle.trigger()
        async with asyncio.timeout(20):
            while (await handle.describe()).info.num_actions_skipped_overlap != 1:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        assert activities.calls["dispatch"] == 1
        assert activities.status == "needs_input"
        await child.signal("approve")
        await asyncio.wait_for(child.result(), 20)
    await handle.delete()


async def test_terminated_review_can_reset_without_redrafting_or_approving(schedule_env):
    env = schedule_env
    queue = f"recovery-{uuid4()}"
    activities = ReviewActivities()
    async with worker(env, queue, activities):
        parent = await env.client.start_workflow(
            ScheduledDispatchWorkflow.run,
            str(uuid4()),
            id=f"legacy-{uuid4()}",
            task_queue=queue,
            execution_timeout=timedelta(seconds=15),
        )
        await asyncio.wait_for(activities.ready.wait(), 20)
        child = env.client.get_workflow_handle(activities.child_id)
        waiting = await review_boundary(child)
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(parent.result(), 30)
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(child.result(), 20)
        assert (await parent.describe()).status == WorkflowExecutionStatus.TIMED_OUT
        closed = await child.describe()
        assert closed.status == WorkflowExecutionStatus.TERMINATED
        history = await child.fetch_history()
        assert history.events[-1].workflow_execution_terminated_event_attributes.reason == (
            "by parent close policy"
        )
        assert activities.status == "needs_input"
        reset = await env.client.workflow_service.reset_workflow_execution(
            ResetWorkflowExecutionRequest(
                namespace=env.client.namespace,
                workflow_execution=WorkflowExecution(workflow_id=child.id, run_id=closed.run_id),
                reason="Restore synthetic scheduled review after parent timeout",
                workflow_task_finish_event_id=waiting.events[-1].event_id,
                request_id=str(uuid4()),
                reset_reapply_type=ResetReapplyType.RESET_REAPPLY_TYPE_NONE,
            )
        )
        recovered = env.client.get_workflow_handle(child.id, run_id=reset.run_id)
        await review_boundary(recovered)
        assert (await recovered.describe()).status == WorkflowExecutionStatus.RUNNING
        assert activities.status == "needs_input"
        assert all(activities.calls[name] == 1 for name in DRAFTING)
        assert activities.calls["request_codex_procedure_review"] == 1
        assert activities.calls["record_codex_procedure_approval"] == 0
        assert activities.calls["project_codex_procedure_failure"] == 0
        await recovered.signal("approve")
        await asyncio.wait_for(recovered.result(), 20)
        assert activities.status == "succeeded"
        assert all(activities.calls[name] == 1 for name in DRAFTING)
        assert activities.calls["record_codex_procedure_approval"] == 1


async def test_scheduled_admission_skips_active_restored_run_and_reuses_occurrence(
    publication_db, monkeypatch
):
    db = publication_db
    key, executor = "research.deep_dive", "codex.procedure"
    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == key)
    definition = builtin.definition
    workflow = await db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=key,
        title=builtin.title,
        description=builtin.description,
        executor=executor,
        definition_repo_id="registry/workflows",
        definition_path=f"{key}.json",
        current_commit_sha="a" * 40,
        version_label="1",
        definition=definition,
    )
    project = await db.create_project(name="Review fixture", state_repo_id=f"projects/{uuid4()}")
    inputs = normalize_workflow_inputs(
        schema=definition["input_schema"],
        project_id=project.id,
        inputs={"question": "How do buyers compare the fixture product with its rivals?"},
    )
    configured = await db.create_project_workflow(
        project_id=project.id,
        workflow_id=workflow.id,
        definition_commit_sha="a" * 40,
        name="Daily draft",
        inputs=inputs,
        input_schema=definition["input_schema"],
        schedule=SCHEDULE.model_dump(mode="json"),
        request_id=uuid4(),
        created_by_clerk_user_id="user_fixture",
    )
    configured = await db.project_workflow_synced(
        project_workflow_id=configured.id,
        temporal_schedule_id=TemporalScheduleService.schedule_id(str(configured.id)),
        next_run_at=None,
    )
    options = dict(
        project_id=project.id,
        workflow_id=workflow.id,
        input_payload=inputs,
        project_workflow_id=configured.id,
        definition_commit_sha="a" * 40,
        trigger_source="schedule",
        scheduled_for=datetime.now(UTC),
    )
    results = await asyncio.gather(
        db.create_run(**options, start_idempotency_key="schedule:first"),
        db.create_run(**options, start_idempotency_key="schedule:second"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ScheduledWorkflowSkip) for result in results) == 1
    accepted = next(index for index, result in enumerate(results) if isinstance(result, tuple))
    run, created = results[accepted]
    accepted_key = ("schedule:first", "schedule:second")[accepted]
    assert created
    for status in ("pending", "running", "needs_input", "paused"):
        await db.pool.execute("UPDATE workflow_runs SET status=$2 WHERE id=$1", run.id, status)
        with pytest.raises(ScheduledWorkflowSkip, match="earlier run"):
            await db.create_run(**options, start_idempotency_key="schedule:later")
        same, created = await db.create_run(**options, start_idempotency_key=accepted_key)
        assert same.id == run.id and not created
    await db.pool.execute("UPDATE workflow_runs SET status='needs_input' WHERE id=$1", run.id)
    common = object.__new__(TinActivities)
    common._db, common._storage = db, SimpleNamespace()
    monkeypatch.setattr(
        "tin_lite.workflow_definitions.resolve_execution_contract", AsyncMock(return_value=workflow)
    )
    monkeypatch.setattr(
        "tin_lite.activities.evaluate_prerequisites",
        AsyncMock(return_value=SimpleNamespace(results=[], blocking=False)),
    )
    assert (
        await common.dispatch_scheduled_workflow(
            {
                "project_workflow_id": str(configured.id),
                "occurrence_id": "later",
                "scheduled_for": options["scheduled_for"].isoformat(),
            }
        )
        == {}
    )
    assert (await db.get_project_workflow(configured.id)).next_run_at > options["scheduled_for"]
    assert await db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 1
    await db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", run.id)
    later, created = await db.create_run(**options, start_idempotency_key="schedule:later")
    assert created and later.id != run.id and later.status == RunStatus.PENDING
