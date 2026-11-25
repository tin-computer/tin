"""intel.daily in Temporal: identifier-only history, partial days, and a failed analyst.

Activities are stubs by name; the analyst is a stub child workflow. No provider access.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError, WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tin_lite.workflows import IntelDailyWorkflow

FIXTURE = Path(__file__).parent / "fixtures" / "intel_daily_history.json"
FIXTURE_ID = "intel.daily:6f0c4a52-1e3b-4d7a-9c2e-8b5f1a3d7e90"


@workflow.defn(name="codex.procedure")
class Analyst:
    @workflow.run
    async def run(self, child_id: str) -> None:
        if child_id.startswith("fail"):
            raise ApplicationError("Synthetic analyst failure", non_retryable=True)


def stubs(calls: list, *, judge_fails=False, broken_group=None, slack_breaks=False, retry=None):
    """`retry`: None (no retry due), "ok" or "fail" (the second analyst's outcome)."""

    def note(name, value):
        calls.append((name, value))

    @activity.defn(name="intel_daily_prepare")
    async def prepare(run_id):
        note("prepare", run_id)
        return {"groups": ["rss", "gnews", "reddit", "x"]}

    @activity.defn(name="intel_daily_fetch")
    async def fetch(control):
        note("fetch", control)
        if control["group"] == broken_group:
            raise ApplicationError("Synthetic group crash", non_retryable=True)
        if control["group"] == "x":
            return {"items": 0, "sources": 1, "x": "x_member"}
        return {"items": 10, "sources": 3, "failed": 0}

    @activity.defn(name="intel_daily_load")
    async def load(run_id):
        note("load", run_id)
        return {"items": 30, "world": "missing"}

    @activity.defn(name="intel_daily_judge_start")
    async def judge_start(run_id):
        note("judge_start", run_id)
        child = f"{'fail' if judge_fails else 'ok'}-{uuid4()}"
        return {"run_id": child, "executor": "codex.procedure", "temporal_workflow_id": child}

    @activity.defn(name="intel_daily_judge_retry")
    async def judge_retry(run_id):
        note("judge_retry", run_id)
        if retry is None:
            return {}
        child = f"{retry}-{uuid4()}"
        return {
            "run_id": child,
            "executor": "codex.procedure",
            "temporal_workflow_id": child,
            "attempt": 2,
        }

    @activity.defn(name="intel_daily_judge_collect")
    async def judge_collect(run_id):
        note("judge_collect", run_id)
        if retry == "fail":
            raise ApplicationError(
                "The agent execution failed before a completed result was recovered.",
                {"tin_failure": {"kind": "explained", "message": "The analyst failed twice."}},
                non_retryable=True,
            )
        if judge_fails and retry is None:
            raise ApplicationError(
                "Your Tin credits ran out. Add credits in Billing, then retry.",
                {"tin_failure": {"kind": "needs_you", "message": "Your Tin credits ran out."}},
                non_retryable=True,
            )
        return {"stories": 12, "brief": "judge"}

    @activity.defn(name="intel_daily_publish")
    async def publish(run_id):
        note("publish", run_id)
        return {"links": 3}

    @activity.defn(name="intel_daily_post")
    async def post(run_id):
        note("post", run_id)
        if slack_breaks:
            raise ApplicationError("Synthetic Slack crash", non_retryable=True)
        return {"outcome": "sent"}

    @activity.defn(name="intel_daily_finish")
    async def finish(run_id):
        note("finish", run_id)

    @activity.defn(name="intel_daily_failure")
    async def failure(argument):
        note("failure", argument)

    return [
        prepare,
        fetch,
        load,
        judge_start,
        judge_retry,
        judge_collect,
        publish,
        post,
        finish,
        failure,
    ]


async def execute(run_id: str, activities, *, fails=False):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"intel-daily-{uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[IntelDailyWorkflow, Analyst],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                IntelDailyWorkflow.run, run_id, id=f"intel.daily:{run_id}", task_queue=queue
            )
            if fails:
                with pytest.raises(WorkflowFailureError):
                    await handle.result()
            else:
                await handle.result()
            history = await handle.fetch_history()
    await Replayer(workflows=[IntelDailyWorkflow]).replay_workflow(history)
    return history


def scheduled_inputs(history) -> list:
    found = []
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            payloads = event.activity_task_scheduled_event_attributes.input.payloads
            assert len(payloads) == 1
            found.append(json.loads(payloads[0].data))
    return found


async def test_a_day_with_x_skipped_and_no_world_stories_succeeds_with_identifiers_only():
    run_id, calls = str(uuid4()), []
    history = await execute(run_id, stubs(calls))
    names = [name for name, _ in calls]
    assert names == [
        "prepare",
        "fetch",
        "fetch",
        "fetch",
        "fetch",
        "load",
        "judge_start",
        "judge_retry",
        "judge_collect",
        "publish",
        "post",
        "finish",
    ]
    assert [value["group"] for name, value in calls if name == "fetch"] == [
        "rss",
        "gnews",
        "reddit",
        "x",
    ]
    # History holds the run ID and group names only: no items, configs, briefs or messages.
    for value in scheduled_inputs(history):
        assert value == run_id or value in [
            {"run_id": run_id, "group": g} for g in ("rss", "gnews", "reddit", "x")
        ]
    started = [
        event.start_child_workflow_execution_initiated_event_attributes
        for event in history.events
        if event.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]
    assert len(started) == 1 and started[0].workflow_type.name == "codex.procedure"


async def test_a_crashed_group_or_slack_never_fails_the_day():
    run_id, calls = str(uuid4()), []
    await execute(run_id, stubs(calls, broken_group="gnews", slack_breaks=True))
    names = [name for name, _ in calls]
    assert names.count("fetch") == 4 and "failure" not in names
    assert names[-2:] == ["post", "finish"]


async def test_a_failed_analyst_fails_the_run_with_its_own_reason():
    run_id, calls = str(uuid4()), []
    await execute(run_id, stubs(calls, judge_fails=True), fails=True)
    names = [name for name, _ in calls]
    assert "publish" not in names and names[-1] == "failure"
    failure = calls[-1][1]
    assert failure["run_id"] == run_id
    assert failure["kind"] == "needs_you" and "credits ran out" in failure["reason"]


async def test_the_recorded_history_still_replays():
    history = WorkflowHistory.from_json(FIXTURE_ID, FIXTURE.read_text())
    await Replayer(workflows=[IntelDailyWorkflow]).replay_workflow(history)


def children(history) -> list[str]:
    return [
        event.start_child_workflow_execution_initiated_event_attributes.workflow_id
        for event in history.events
        if event.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]


async def test_an_analyst_that_died_on_a_provider_error_runs_once_more():
    run_id, calls = str(uuid4()), []
    history = await execute(run_id, stubs(calls, judge_fails=True, retry="ok"))
    names = [name for name, _ in calls]
    started = children(history)
    assert len(started) == 2 and started[0].startswith("fail") and started[1].startswith("ok")
    assert names.count("judge_retry") == 1 and names[-2:] == ["post", "finish"]
    for value in scheduled_inputs(history):
        assert value == run_id or (isinstance(value, dict) and value.get("run_id") == run_id)


async def test_a_second_provider_failure_ends_the_run_with_its_reason():
    run_id, calls = str(uuid4()), []
    history = await execute(run_id, stubs(calls, judge_fails=True, retry="fail"), fails=True)
    names = [name for name, _ in calls]
    assert len(children(history)) == 2 and names.count("judge_retry") == 1
    assert "publish" not in names and names[-1] == "failure"
    failure = calls[-1][1]
    assert failure["kind"] == "explained" and "failed twice" in failure["reason"]


async def test_no_retry_due_means_one_analyst():
    run_id, calls = str(uuid4()), []
    history = await execute(run_id, stubs(calls, judge_fails=True), fails=True)
    assert len(children(history)) == 1
