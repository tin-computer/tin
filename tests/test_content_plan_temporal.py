"""Local Temporal retry/replay proof, with no model/provider access."""

import shutil
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tin_lite.workflows import ContentPlanWorkflow


@workflow.defn(name="codex.procedure")
class FailingAgent:
    @workflow.run
    async def run(self, child_id: str) -> None:
        raise ApplicationError("Synthetic agent failure", non_retryable=True)


@pytest.mark.asyncio
async def test_content_plan_identifier_only_history_and_retry():
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Local Temporal CLI required")
    run_id, calls = str(uuid4()), []

    @activity.defn(name="content_plan_execute")
    async def execute(value: str):
        calls.append(value)
        if len(calls) == 1:
            raise ApplicationError("Synthetic transient publication error")

    @activity.defn(name="content_plan_failure")
    async def fail(value: str):
        raise AssertionError("This workflow should recover")

    @activity.defn(name="content_plan_research")
    async def research(value: str):
        # A pinned v8 or older plan, or a scheduled batch: no planning agent.
        return {}

    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue="content-plan-proof",
            workflows=[ContentPlanWorkflow],
            activities=[execute, fail, research],
        ):
            handle = await env.client.start_workflow(
                ContentPlanWorkflow.run,
                run_id,
                id=f"content-plan-proof:{run_id}",
                task_queue="content-plan-proof",
            )
            await handle.result()
            history = await handle.fetch_history()
        await Replayer(workflows=[ContentPlanWorkflow]).replay_workflow(history)
    assert calls == [run_id, run_id]
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            payloads = event.activity_task_scheduled_event_attributes.input.payloads
            assert len(payloads) == 1 and payloads[0].data == f'"{run_id}"'.encode()


@pytest.mark.asyncio
async def test_content_plan_dispatches_its_planning_agent_with_identifiers_only():
    """v9: the research activity prepares the agent's run, the parent waits for it as a child,
    and a failed agent still reaches content_plan_execute, which names why."""
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Local Temporal CLI required")
    run_id, calls = str(uuid4()), []
    child_id = str(uuid4())

    @activity.defn(name="content_plan_research")
    async def research(value: str):
        calls.append(("research", value))
        return {
            "run_id": child_id,
            "executor": "codex.procedure",
            "temporal_workflow_id": f"agent:{child_id}",
        }

    @activity.defn(name="content_plan_execute")
    async def execute(value: str):
        calls.append(("execute", value))

    @activity.defn(name="content_plan_failure")
    async def fail(value: str):
        raise AssertionError("The plan names the agent's failure itself")

    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue="content-plan-agent-proof",
            workflows=[ContentPlanWorkflow, FailingAgent],
            activities=[research, execute, fail],
        ):
            handle = await env.client.start_workflow(
                ContentPlanWorkflow.run,
                run_id,
                id=f"content-plan-agent-proof:{run_id}",
                task_queue="content-plan-agent-proof",
            )
            await handle.result()
            history = await handle.fetch_history()
        await Replayer(workflows=[ContentPlanWorkflow]).replay_workflow(history)
    assert calls == [("research", run_id), ("execute", run_id)]
    started = [
        event.start_child_workflow_execution_initiated_event_attributes
        for event in history.events
        if event.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]
    assert [(s.workflow_id, s.workflow_type.name) for s in started] == [
        (f"agent:{child_id}", "codex.procedure")
    ]
    assert started[0].input.payloads[0].data == f'"{child_id}"'.encode()
    assert any(
        event.HasField("child_workflow_execution_failed_event_attributes")
        for event in history.events
    )
