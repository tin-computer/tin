"""Local Temporal orchestration/replay proof; never connects to Temporal Cloud."""

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

from tin_lite.workflows import OrganicAuditWorkflow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_during_crawl", "engines"), [(False, False), (True, False), (False, True)]
)
async def test_native_workflow_retry_stop_and_identifier_only_history(stop_during_crawl, engines):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("local Temporal CLI is needed for the orchestration integration test")
    run_id = str(uuid4())
    calls = []
    poll_reached = asyncio.Event()
    publish_attempts = 0

    def stub(name):
        @activity.defn(name=name)
        async def implementation(control):
            nonlocal publish_attempts
            calls.append((name, control))
            if name == "organic_poll_crawl":
                poll_reached.set()
                return not stop_during_crawl
            if name == "organic_prepare_panel":
                return 2
            if name == "organic_prepare_ai_engines":
                # A v13 run measures the engines; earlier policies return no stage.
                return "engines" if engines else None
            if name == "ai_answers_measure":
                raise ApplicationError("Injected measurement failure", non_retryable=True)
            if name == "organic_publish":
                publish_attempts += 1
                if publish_attempts == 1:
                    raise ApplicationError("Injected lost publication response")
            if name == "organic_failure":
                raise AssertionError("Normal completion/stop must not fail")
            return None

        return implementation

    names = [
        "organic_prepare",
        "organic_start_crawl",
        "organic_poll_crawl",
        "organic_end_crawl",
        "organic_prepare_panel",
        "organic_observe",
        "organic_brand_checks",
        "organic_prepare_ai_engines",
        "ai_answers_measure",
        "organic_publish",
        "organic_project",
        "organic_failure",
    ]
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue="audit-local-test",
            workflows=[OrganicAuditWorkflow],
            activities=[stub(name) for name in names],
        ):
            handle = await env.client.start_workflow(
                OrganicAuditWorkflow.run,
                run_id,
                id=f"organic.audit:{run_id}",
                task_queue="audit-local-test",
            )
            if stop_during_crawl:
                await asyncio.wait_for(poll_reached.wait(), timeout=15)
                await handle.signal("stop")
            await asyncio.wait_for(handle.result(), timeout=30)
            history = await handle.fetch_history()
        await Replayer(workflows=[OrganicAuditWorkflow]).replay_workflow(history)
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            for item in event.activity_task_scheduled_event_attributes.input.payloads:
                value = json.loads(item.data)
                assert value == run_id or value in [
                    {"run_id": run_id, "index": 0},
                    {"run_id": run_id, "index": 1},
                    {"run_id": run_id, "stage": "engines"},
                ]
    if stop_during_crawl:
        assert not any(
            name in {"organic_observe", "organic_publish", "organic_project"} for name, _ in calls
        )
    else:
        assert publish_attempts == 2
        assert calls[-1] == ("organic_project", run_id)
        # A failed engine measurement is reported, not fatal; it runs before publication.
        names = [name for name, _ in calls]
        assert ("ai_answers_measure" in names) == engines
        assert names.index("organic_prepare_ai_engines") < names.index("organic_publish")
