"""Local Temporal proof that voice review gates composition, with replay."""

import asyncio
import shutil
from datetime import timedelta
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tin_lite.workflows import XDraftWorkflow


@workflow.defn(name="x-test-voice")
class Voice:
    def __init__(self):
        self.approved = False

    @workflow.signal
    def approve(self):
        self.approved = True

    @workflow.run
    async def run(self, run_id: str):
        ok = await workflow.execute_activity(
            "x_test_started", run_id, start_to_close_timeout=timedelta(seconds=10)
        )
        if not ok:
            raise ApplicationError("Capture failed", non_retryable=True)
        await workflow.wait_condition(lambda: self.approved)


@workflow.defn(name="x-test-compose")
class Compose:
    @workflow.run
    async def run(self, run_id: str):
        await workflow.execute_activity(
            "x_test_started", run_id, start_to_close_timeout=timedelta(seconds=10)
        )


@pytest.mark.parametrize("mode", ["review", "reuse", "failure"])
async def test_review_sequence_and_replay(mode):
    binary = shutil.which("temporal")
    if not binary:
        pytest.skip("local Temporal CLI required")
    parent, style, compose = (str(uuid4()) for _ in range(3))
    started = []
    calls = []
    waiting = asyncio.Event()

    @activity.defn
    async def x_test_started(run_id: str) -> bool:
        started.append(run_id)
        waiting.set()
        return mode != "failure"

    def stub(name):
        @activity.defn(name=name)
        async def call(argument):
            calls.append((name, argument))
            if name == "x_draft_step":
                step = argument["step"]
                if step == "style" and mode == "reuse":
                    return {"status": "skipped"}
                child = style if step == "style" else compose
                return {
                    "run_id": child,
                    "executor": "x-test-voice" if step == "style" else "x-test-compose",
                    "temporal_workflow_id": child,
                }

        return call

    names = ["x_draft_prepare", "x_draft_step", "x_draft_finish", "x_draft_failure"]
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue=parent,
            workflows=[XDraftWorkflow, Voice, Compose],
            activities=[x_test_started, *(stub(n) for n in names)],
        ):
            handle = await env.client.start_workflow(
                XDraftWorkflow.run, parent, id=parent, task_queue=parent
            )
            await asyncio.wait_for(waiting.wait(), 20)
            if mode == "review":
                assert started == [style]
                assert not any(
                    name == "x_draft_step" and value["step"] == "compose" for name, value in calls
                )
                await env.client.get_workflow_handle(style).signal("approve")
            if mode == "failure":
                with pytest.raises(WorkflowFailureError):
                    await handle.result()
                assert started == [style]
                assert calls[-1] == ("x_draft_failure", parent)
            else:
                await handle.result()
                assert started == ([compose] if mode == "reuse" else [style, compose])
                assert calls[-1] == ("x_draft_finish", parent)
            history = await handle.fetch_history()
        await Replayer(workflows=[XDraftWorkflow]).replay_workflow(history)
        for event in history.events:
            if event.HasField("activity_task_scheduled_event_attributes"):
                decoded = await env.client.data_converter.decode(
                    event.activity_task_scheduled_event_attributes.input.payloads
                )
                assert decoded[0] in [
                    parent,
                    {"run_id": parent, "step": "style"},
                    {"run_id": parent, "step": "compose"},
                ]
