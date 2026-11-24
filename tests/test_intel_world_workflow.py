"""intel.world in a local Temporal: failed sources, no items, the judge child, identifier-only
history. No Temporal Cloud, sandbox or provider."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tin_lite.activity_lanes import ActivityLaneInterceptor
from tin_lite.workflows import IntelWorldWorkflow

FIXTURE = Path(__file__).parent / "fixtures" / "intel_world_history.json"
NAMES = [
    "intel_world_prepare",
    "intel_world_fetch",
    "intel_world_load",
    "intel_world_judge_start",
    "intel_world_judge_collect",
    "intel_world_unwrap",
    "intel_world_publish",
    "intel_world_post",
    "intel_world_finish",
    "intel_world_failure",
]
CHILD = {"outcome": "succeeded"}


@workflow.defn(name="codex.procedure")
class JudgeStub:
    @workflow.run
    async def run(self, run_id: str) -> None:
        await workflow.sleep(0.1)
        if CHILD["outcome"] != "succeeded":
            raise ApplicationError("The agent's turn did not finish", non_retryable=True)


def stubs(calls, *, items=1500, collect=None, fetch_fails=("special",)):
    def stub(name):
        @activity.defn(name=name)
        async def implementation(control):
            calls.append((name, control))
            if name == "intel_world_prepare":
                return {"groups": ["gnews", "feeds", "special"]}
            if name == "intel_world_fetch" and control["group"] in fetch_fails:
                raise ApplicationError("every host refused", non_retryable=True)
            if name == "intel_world_fetch":
                return {"group": control["group"], "sources": 20, "items": 500}
            if name == "intel_world_load":
                if not items:
                    raise ApplicationError(
                        "No items were fetched: every world source failed or was empty.",
                        non_retryable=True,
                    )
                return {"items": items}
            if name == "intel_world_judge_start":
                child = str(uuid4())
                return {
                    "run_id": child,
                    "executor": "codex.procedure",
                    "temporal_workflow_id": f"codex.procedure:{child}",
                }
            if name == "intel_world_judge_collect":
                if collect is not None:
                    return collect()
                return {"stories": 120, "status": "ready"}
            return None

        return implementation

    return [stub(name) for name in NAMES]


async def run_world(env, calls, **kwargs):
    run_id = str(uuid4())
    async with (
        Worker(
            env.client,
            task_queue="intel-world-test",
            workflows=[IntelWorldWorkflow, JudgeStub],
            activities=stubs(calls, **kwargs),
            interceptors=[ActivityLaneInterceptor()],
        ),
        Worker(
            env.client,
            task_queue="intel-world-test-trusted",
            activities=stubs(calls, **kwargs),
        ),
    ):
        handle = await env.client.start_workflow(
            IntelWorldWorkflow.run,
            run_id,
            id=f"intel.world:{run_id}",
            task_queue="intel-world-test",
        )
        try:
            await asyncio.wait_for(handle.result(), timeout=60)
            outcome = "succeeded"
        except Exception:  # noqa: BLE001 — the case under test decides
            outcome = "failed"
        return run_id, outcome, await handle.fetch_history()


@pytest.fixture
async def env():
    binary = shutil.which("temporal") or os.environ.get("TEMPORAL_CLI")
    if binary is None:
        pytest.skip("Local Temporal CLI is required")
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as environment:
        yield environment


def assert_identifier_only(history, run_id):
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            for payload in event.activity_task_scheduled_event_attributes.input.payloads:
                value = json.loads(payload.data)
                assert value == run_id or (
                    isinstance(value, dict)
                    and set(value) <= {"run_id", "group", "reason", "kind", "action", "detail"}
                    and value["run_id"] == run_id
                ), value


async def test_a_failed_group_is_skipped_and_the_day_finishes(env):
    calls: list = []
    CHILD["outcome"] = "succeeded"
    run_id, outcome, history = await run_world(env, calls)
    assert outcome == "succeeded"
    names = [name for name, _ in calls]
    assert names.count("intel_world_fetch") == 3
    assert names[-1] == "intel_world_finish" and "intel_world_failure" not in names
    # Groups run one at a time, in order.
    groups = [control["group"] for name, control in calls if name == "intel_world_fetch"]
    assert groups == ["gnews", "feeds", "special"]
    assert_identifier_only(history, run_id)
    await Replayer(
        workflows=[IntelWorldWorkflow], interceptors=[ActivityLaneInterceptor()]
    ).replay_workflow(history)
    if os.environ.get("TIN_RECORD_INTEL_HISTORY"):
        FIXTURE.write_text(json.dumps(history.to_json_dict(), indent=1) + "\n")


async def test_no_items_fails_before_the_judge(env):
    calls: list = []
    _run_id, outcome, _history = await run_world(env, calls, items=0)
    names = [name for name, _ in calls]
    assert outcome == "failed"
    assert "intel_world_judge_start" not in names and names[-1] == "intel_world_failure"
    failure = calls[-1][1]
    assert "No items were fetched" in failure["reason"]


async def test_a_failed_judge_is_partial_with_enough_stories(env):
    calls: list = []
    CHILD["outcome"] = "failed"
    _run_id, outcome, _history = await run_world(
        env, calls, collect=lambda: {"stories": 40, "status": "partial"}
    )
    assert outcome == "succeeded"
    assert [name for name, _ in calls][-1] == "intel_world_finish"


async def test_a_failed_judge_with_few_stories_fails_the_day(env):
    calls: list = []
    CHILD["outcome"] = "failed"

    def collect():
        raise ApplicationError("The world judge stopped with 3 stories", non_retryable=True)

    _run_id, outcome, _history = await run_world(env, calls, collect=collect)
    names = [name for name, _ in calls]
    assert outcome == "failed" and names[-1] == "intel_world_failure"
    assert "intel_world_publish" not in names
