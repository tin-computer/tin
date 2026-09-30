"""Revision histories contain run IDs only, including retry/failure replay."""

import shutil
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tin_lite.workflows import XFeedbackWorkflow


@pytest.mark.parametrize("fail", [False, True])
async def test_revision_history_and_replay(fail):
    binary = shutil.which("temporal")
    if not binary:
        pytest.skip("local Temporal CLI required")
    run_id = str(uuid4())
    calls = []

    def stub(name):
        @activity.defn(name=name)
        async def call(argument: str):
            assert argument == run_id
            calls.append(name)
            if fail and name == "x_feedback_generate":
                raise ApplicationError("Synthetic invalid output", non_retryable=True)

        return call

    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue=run_id,
            workflows=[XFeedbackWorkflow],
            activities=[
                stub(n) for n in ["x_feedback_generate", "x_feedback_publish", "x_feedback_failure"]
            ],
        ):
            handle = await env.client.start_workflow(
                XFeedbackWorkflow.run, run_id, id=run_id, task_queue=run_id
            )
            if fail:
                with pytest.raises(WorkflowFailureError):
                    await handle.result()
            else:
                await handle.result()
            history = await handle.fetch_history()
        await Replayer(workflows=[XFeedbackWorkflow]).replay_workflow(history)
        assert calls == [
            "x_feedback_generate",
            "x_feedback_failure" if fail else "x_feedback_publish",
        ]
        for event in history.events:
            if event.HasField("activity_task_scheduled_event_attributes"):
                decoded = await env.client.data_converter.decode(
                    event.activity_task_scheduled_event_attributes.input.payloads
                )
                assert decoded == [run_id]
