"""Real Temporal orchestration, synthetic compute, no model or project writes."""

from __future__ import annotations

import asyncio
import shutil
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.runtime import Runtime, TelemetryConfig
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tin_lite.activity_lanes import ActivityLaneInterceptor, trusted_task_queue
from tin_lite.codex_execution import (
    ProjectCodexExecution,
    execute_codex_slice,
    execute_project_codex,
)
from tin_lite.workflows import (
    CodeWorkflow,
    CodexProcedureWorkflow,
    DesignMdWorkflow,
    ProjectTaskWorkflow,
)


@workflow.defn
class ExecutionProbe:
    @workflow.run
    async def run(self, run_id: str) -> str | None:
        return await execute_project_codex(run_id, "task_turn")


@workflow.defn(name="project.task")
class LegacyTask:
    def __init__(self):
        self.resumed = False
        self.waiting = False

    @workflow.signal(name="resume")
    async def resume(self):
        self.resumed = True

    @workflow.query
    def ready(self) -> bool:
        return self.waiting

    @workflow.run
    async def run(self, run_id: str) -> None:
        await execute_codex_slice({"run_id": run_id, "kind": "task_turn", "turn_number": "1"})
        self.waiting = True
        await workflow.wait_condition(lambda: self.resumed)


@pytest.fixture
async def temporal_env():
    binary = shutil.which("temporal")
    if not binary:
        pytest.skip("local Temporal CLI is needed for project execution integration")
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        yield env


class Compute:
    def __init__(self):
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.active = Counter()
        self.peak = Counter()
        self.calls = Counter()
        self.reviewed = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cleanup = asyncio.Event()

    async def execute(self, run_id):
        project = run_id.split("/")[0]
        self.calls[run_id] += 1
        self.active[project] += 1
        self.peak[project] = max(self.peak[project], self.active[project])
        self.started.setdefault(run_id, asyncio.Event()).set()
        try:
            if run_id.endswith("fail"):
                raise ApplicationError("synthetic failure", non_retryable=True)
            event = self.release.get(run_id)
            while event and not event.is_set():
                activity.heartbeat("synthetic")
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.cleanup.wait()
            raise
        finally:
            self.active[project] -= 1
        return "completed"

    def activity(self, name):
        @activity.defn(name=name)
        async def call(payload):
            run_id = payload["run_id"] if isinstance(payload, dict) else payload
            if name == "resolve_codex_project":
                return run_id.split("/")[0]
            if name in {
                "run_project_task_turn",
                "execute_code_workflow",
                "persist_codex_procedure_artifact",
                "persist_design_artifact",
            }:
                result = await self.execute(run_id)
                if run_id.endswith("old") and payload.get("turn_number") == "1":
                    return "paused"
                return result
            if name == "request_codex_procedure_review":
                self.reviewed.set()
                return True
            if name == "prepare_codex_procedure":
                return False
            return None

        return call

    async def wait_started(self, run_id):
        await asyncio.wait_for(self.started.setdefault(run_id, asyncio.Event()).wait(), 15)


NAMES = [
    "resolve_codex_project",
    "execute_code_workflow",
    "publish_code_workflow",
    "review_code_workflow",
    "approve_code_workflow",
    "project_code_workflow",
    "fail_code_workflow",
    "run_project_task_turn",
    "prepare_codex_procedure",
    "create_codex_procedure_sandbox",
    "persist_codex_procedure_artifact",
    "commit_codex_procedure_artifact",
    "request_codex_procedure_review",
    "record_codex_procedure_approval",
    "project_codex_procedure_result",
    "deliver_content_draft",
    "project_codex_procedure_failure",
    "create_design_sandbox",
    "persist_design_artifact",
    "commit_design_canonically",
    "project_design_result",
    "project_design_failure",
    "project_task_failure",
    "record_project_task_stop",
]
WORKFLOWS = [
    ExecutionProbe,
    CodeWorkflow,
    ProjectCodexExecution,
    CodexProcedureWorkflow,
    ProjectTaskWorkflow,
    DesignMdWorkflow,
]


def workflow_worker(env, base):
    return Worker(
        env.client, task_queue=base, workflows=WORKFLOWS, interceptors=[ActivityLaneInterceptor()]
    )


def activity_worker(env, base, compute, *, trusted=False, capacity=2):
    from tin_lite.activity_lanes import TRUSTED_ACTIVITIES

    names = [n for n in NAMES if (n in TRUSTED_ACTIVITIES) == trusted]
    return Worker(
        env.client,
        task_queue=trusted_task_queue(base) if trusted else base,
        activities=[compute.activity(n) for n in names],
        max_concurrent_activities=capacity,
        max_heartbeat_throttle_interval=timedelta(milliseconds=100),
        default_heartbeat_throttle_interval=timedelta(milliseconds=100),
    )


async def start(env, base, run_id, cls=ExecutionProbe):
    return await env.client.start_workflow(cls.run, run_id, id=str(uuid4()), task_queue=base)


@pytest.mark.asyncio
async def test_same_project_mixed_executors_overlap_across_workers(
    temporal_env,
):
    env, compute, base = temporal_env, Compute(), f"projects-{uuid4()}"
    compute.release["a/procedure"] = asyncio.Event()
    compute.release["a/design"] = asyncio.Event()
    histories = []
    second_client = await Client.connect(
        env.client.service_client.config.target_host, runtime=Runtime(telemetry=TelemetryConfig())
    )
    # Two independent compute workers prove this is not an in-process semaphore.
    async with (
        workflow_worker(env, base),
        activity_worker(env, base, compute),
        activity_worker(SimpleNamespace(client=second_client), base, compute),
        activity_worker(env, base, compute, trusted=True),
    ):
        first = await start(env, base, "a/procedure", CodexProcedureWorkflow)
        await compute.wait_started("a/procedure")
        queued = [await start(env, base, f"a/task-{i}", ProjectTaskWorkflow) for i in range(4)]
        queued.append(await start(env, base, "a/design", DesignMdWorkflow))
        queued.append(await start(env, base, "a/code", CodeWorkflow))
        other = await start(env, base, "b/task", ProjectTaskWorkflow)
        try:
            await asyncio.wait_for(other.result(), 10)
            await compute.wait_started("a/design")
            assert compute.active["a"] == 2
            assert compute.peak["a"] >= 2
            assert compute.peak["b"] == 1
        finally:
            compute.release["a/procedure"].set()
            compute.release["a/design"].set()
        await asyncio.wait_for(compute.reviewed.wait(), 10)
        # Review is still unapproved while subsequent executions in the SAME project finish.
        await asyncio.wait_for(asyncio.gather(*(h.result() for h in queued)), 80)
        assert compute.peak["a"] >= 2
        assert all(count == 1 for count in compute.calls.values())
        await first.signal("approve")
        await first.result()
        for handle in [first, other, *queued]:
            histories.append(await handle.fetch_history())
        histories.append(
            await env.client.get_workflow_handle("tin.run-codex:a/procedure:1").fetch_history()
        )
    for history in histories:
        await Replayer(
            workflows=WORKFLOWS, interceptors=[ActivityLaneInterceptor()]
        ).replay_workflow(history)


@pytest.mark.asyncio
async def test_worker_capacity_cancellation_and_failure_survive_worker_restart(
    temporal_env,
):
    env, compute, base = temporal_env, Compute(), f"cancel-{uuid4()}"
    async with (
        activity_worker(env, base, compute, capacity=1),
        activity_worker(env, base, compute, trusted=True),
    ):
        async with workflow_worker(env, base):
            failed = await start(env, base, "a/fail")
            with pytest.raises(WorkflowFailureError):
                await failed.result()
            assert compute.calls["a/fail"] == 1
            compute.release["a/held"] = asyncio.Event()
            held = await start(env, base, "a/held")
            await compute.wait_started("a/held")
            waiting = await start(env, base, "a/next")
            removed = await start(env, base, "a/cancelled-waiter")
            await removed.cancel()
            with pytest.raises(WorkflowFailureError):
                await removed.result()
        # Worker restart preserves the active child and queued activities.
        async with workflow_worker(env, base):
            await held.cancel()
            await asyncio.wait_for(compute.cancelled.wait(), 15)
            assert "a/next" not in compute.calls
            compute.cleanup.set()
            with pytest.raises(WorkflowFailureError):
                await held.result()
            assert await asyncio.wait_for(waiting.result(), 20) == "completed"
            assert compute.peak["a"] == 1
            assert "a/cancelled-waiter" not in compute.calls


@pytest.mark.asyncio
async def test_pre_upgrade_task_resumes_alongside_other_work_and_new_task_can_stop(temporal_env):
    env, compute, base = temporal_env, Compute(), f"upgrade-{uuid4()}"
    async with (
        activity_worker(env, base, compute),
        activity_worker(env, base, compute, trusted=True),
    ):
        async with Worker(
            env.client,
            task_queue=base,
            workflows=[LegacyTask],
            interceptors=[ActivityLaneInterceptor()],
        ):
            old = await start(env, base, "a/old", LegacyTask)
            await compute.wait_started("a/old")
            for _ in range(100):
                if await old.query(LegacyTask.ready):
                    break
                await asyncio.sleep(0.01)
            assert await old.query(LegacyTask.ready)
        async with workflow_worker(env, base):
            compute.release["a/held"] = asyncio.Event()
            held = await start(env, base, "a/held")
            await compute.wait_started("a/held")
            await old.signal("resume")
            stopped = await start(env, base, "a/stopped", ProjectTaskWorkflow)
            await stopped.signal("stop")
            try:
                await asyncio.wait_for(stopped.result(), 10)
                assert "a/stopped" not in compute.calls
                await asyncio.wait_for(old.result(), 10)
                assert compute.calls["a/old"] == 2
            finally:
                compute.release["a/held"].set()
            await asyncio.wait_for(asyncio.gather(held.result(), old.result()), 20)
            assert compute.calls["a/old"] == 2
            assert compute.peak["a"] == 2
            history = await old.fetch_history()
        await Replayer(
            workflows=WORKFLOWS, interceptors=[ActivityLaneInterceptor()]
        ).replay_workflow(history)


@pytest.mark.asyncio
async def test_queue_scope_comes_from_the_bound_run():
    from tin_lite.activities import TinActivities

    run_id, project_id = uuid4(), uuid4()
    instance = object.__new__(TinActivities)
    instance._require_run = AsyncMock(return_value=SimpleNamespace(project_id=project_id))
    assert await instance.resolve_codex_project(str(run_id)) == str(project_id)
    instance._require_run.assert_awaited_once_with(run_id)
