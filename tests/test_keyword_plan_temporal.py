"""Local Temporal retry, stop and replay proof. No Temporal Cloud or paid providers."""

from __future__ import annotations

import asyncio
import json
import shutil
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tin_lite.workflows import KeywordPlanWorkflow


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_native_keyword_retry_stop_and_identifier_only_history(stop):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Local Temporal CLI is required")
    run_id, calls = str(uuid4()), []
    inspecting, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    def stub(name):
        @activity.defn(name=name)
        async def implementation(control):
            nonlocal attempts
            calls.append((name, control))
            if name == "keyword_sample_count":
                return 2
            if name == "keyword_inspect_batch" and stop:
                inspecting.set()
                await release.wait()
            if name == "keyword_publish":
                attempts += 1
                if attempts == 1:
                    raise ApplicationError("Injected lost publication response")
            if name == "keyword_failure":
                raise AssertionError("Completion and stop must not fail")

        return implementation

    names = [
        "keyword_prepare",
        "keyword_collect",
        "keyword_sample_count",
        "keyword_inspect",
        "keyword_inspect_batch",
        "keyword_review",
        "keyword_publish",
        "keyword_project",
        "keyword_failure",
    ]
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue="keyword-local-test",
            workflows=[KeywordPlanWorkflow],
            activities=[stub(name) for name in names],
        ):
            handle = await env.client.start_workflow(
                KeywordPlanWorkflow.run,
                run_id,
                id=f"organic.keyword_plan:{run_id}",
                task_queue="keyword-local-test",
            )
            if stop:
                await asyncio.wait_for(inspecting.wait(), timeout=15)
                await handle.signal("stop")
                release.set()
            await asyncio.wait_for(handle.result(), timeout=30)
            history = await handle.fetch_history()
        await Replayer(workflows=[KeywordPlanWorkflow]).replay_workflow(history)
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            for payload in event.activity_task_scheduled_event_attributes.input.payloads:
                assert json.loads(payload.data) == run_id
    # New runs inspect every sample in one batch activity instead of one activity per sample.
    assert [name for name, _ in calls].count("keyword_inspect_batch") == 1
    assert "keyword_inspect" not in [name for name, _ in calls]
    if stop:
        assert not any(
            name in {"keyword_review", "keyword_publish", "keyword_project"} for name, _ in calls
        )
    else:
        assert attempts == 2 and calls[-1] == ("keyword_project", run_id)
