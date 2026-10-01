"""Real local Temporal proof of the fixed recipe, never a Cloud or paid-provider test."""

import asyncio
import json
import shutil
from datetime import timedelta
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tin_lite.codex_execution import ProjectCodexExecution
from tin_lite.workflows import CodexProcedureWorkflow, OrganicTrafficSystemWorkflow


@workflow.defn(name="organic.traffic_system")
class LegacyOrganicSystem:
    """Pre-continuation command sequence, retained only to generate replay history."""

    @workflow.run
    async def run(self, run_id: str):
        async def call(name, argument, minutes=2):
            return await workflow.execute_activity(
                name,
                argument,
                start_to_close_timeout=timedelta(minutes=minutes),
                retry_policy=RetryPolicy(
                    maximum_attempts=3, maximum_interval=timedelta(seconds=10)
                ),
            )

        async def step(name):
            payload = {"run_id": run_id, "step": name}
            try:
                child = await call("organic_system_step", payload)
                if "run_id" in child:
                    await workflow.execute_child_workflow(
                        child["executor"],
                        child["run_id"],
                        id=child["temporal_workflow_id"],
                        task_queue=workflow.info().task_queue,
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                        cancellation_type=workflow.ChildWorkflowCancellationType.ABANDON,
                    )
            except Exception:
                await call("organic_system_step_failure", payload)
            await call("organic_system_progress", run_id)

        try:
            await call("organic_system_prepare", run_id)
            audit = asyncio.create_task(step("audit"))
            keywords = asyncio.create_task(step("keywords"))
            await audit
            technical = asyncio.create_task(step("technical"))
            await keywords
            await step("content")
            await technical
            if not await call("organic_system_finish", run_id, minutes=5):
                raise ApplicationError("One or more organic system steps could not finish.")
        except BaseException:
            await call("organic_system_failure", run_id)
            raise


@workflow.defn(name="organic.traffic_system")
class ContinuationOrganicSystem:
    """The v2 command sequence before weekly drafting, retained only for replay history."""

    @workflow.run
    async def run(self, run_id: str):
        async def call(name, argument, minutes=2):
            return await workflow.execute_activity(
                name,
                argument,
                start_to_close_timeout=timedelta(minutes=minutes),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        async def step(name):
            payload = {"run_id": run_id, "step": name}
            try:
                child = await call("organic_system_step", payload)
                if "run_id" in child:
                    await workflow.execute_child_workflow(
                        child["executor"],
                        child["run_id"],
                        id=child["temporal_workflow_id"],
                        task_queue=workflow.info().task_queue,
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                        cancellation_type=workflow.ChildWorkflowCancellationType.ABANDON,
                    )
            except Exception:
                await call("organic_system_step_failure", payload)
            await call("organic_system_progress", run_id)

        try:
            await call("organic_system_prepare", run_id)
            audit = asyncio.create_task(step("audit"))
            keywords = asyncio.create_task(step("keywords"))
            await audit
            technical = asyncio.create_task(step("technical"))
            await keywords
            await step("content")
            await technical
            if workflow.patched("organic-content-continuation-v1"):
                await step("draft")
                await step("delivery")
            if not await call("organic_system_finish", run_id, minutes=5):
                raise ApplicationError("One or more organic system steps could not finish.")
        except BaseException:
            await call("organic_system_failure", run_id)
            raise


@workflow.defn(name="recipe-test-child")
class RecipeChild:
    @workflow.run
    async def run(self, run_id: str):
        from datetime import timedelta

        success = await workflow.execute_activity(
            "recipe_child", run_id, start_to_close_timeout=timedelta(seconds=30)
        )
        if not success:
            raise ApplicationError("Fixture child failed", non_retryable=True)


RECIPES = {
    "legacy": LegacyOrganicSystem,
    "continuation": ContinuationOrganicSystem,
    "weekly": OrganicTrafficSystemWorkflow,
}


@pytest.mark.parametrize("keyword_fails", [False, True])
@pytest.mark.parametrize("recipe", list(RECIPES))
async def test_parallel_research_exact_dependencies_and_replay(keyword_fails, recipe):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("local Temporal CLI required")
    run_id = str(uuid4())
    ids = {
        step: str(uuid4())
        for step in ("audit", "keywords", "technical", "content", "draft", "delivery")
    }
    if recipe == "legacy":
        ids = {key: value for key, value in ids.items() if key not in {"draft", "delivery"}}
    if recipe == "weekly":
        # The v5 recipe's first page refresh runs as a child the system waits for.
        ids["refresh"] = str(uuid4())
    implementation = RECIPES[recipe]
    started, finished, calls, dispatches = set(), set(), [], {}
    research_started = asyncio.Event()
    injected_loss = False

    @activity.defn
    async def recipe_child(child_id: str) -> bool:
        step = next(key for key, value in ids.items() if value == child_id)
        started.add(step)
        if {"audit", "keywords"} <= started:
            research_started.set()
        if step in {"audit", "keywords"}:
            # Neither research step can finish until both have actually started.
            await asyncio.wait_for(research_started.wait(), 20)
        if step == "technical":
            assert "audit" in finished
        if step == "content":
            assert {"audit", "keywords"} <= finished
        if step == "draft":
            assert "content" in finished
        if step == "delivery":
            assert "draft" in finished
        if step == "refresh":
            # Slower than the rest, so the system has to wait for it before finishing.
            await asyncio.sleep(1)
        finished.add(step)
        return not (step == "keywords" and keyword_fails)

    def stub(name):
        @activity.defn(name=name)
        async def perform(argument):
            nonlocal injected_loss
            calls.append((name, argument))
            if name == "organic_system_step":
                step = argument["step"]
                if step in {"content", "draft", "delivery"} and keyword_fails:
                    return {"status": "blocked", "reason": "research_unavailable"}
                dispatches.setdefault(
                    step,
                    {
                        "run_id": ids[step],
                        "executor": "recipe-test-child",
                        "temporal_workflow_id": f"child:{ids[step]}",
                    },
                )
                if step == "audit" and not injected_loss:
                    injected_loss = True
                    raise ApplicationError("Fixture lost activity response")
                return dispatches[step]
            if name == "organic_system_weekly_articles":
                # Saved once the plan step is accounted for, beside the first draft.
                assert argument == run_id and "content" in accounted
                return {"status": "skipped" if keyword_fails else "succeeded"}
            if name == "organic_system_refresh":
                assert argument == run_id
                return {
                    "status": "succeeded",
                    "run_id": ids["refresh"],
                    "executor": "recipe-test-child",
                    "temporal_workflow_id": f"child:{ids['refresh']}",
                }
            if name == "organic_system_progress":
                accounted.update(finished | ({"content"} if keyword_fails else set()))
            if name == "organic_system_finish":
                assert "refresh" in finished or recipe != "weekly"
                return not keyword_fails

        return perform

    accounted = set()
    names = [
        "organic_system_prepare",
        "organic_system_step",
        "organic_system_step_failure",
        "organic_system_progress",
        "organic_system_finish",
        "organic_system_failure",
        "organic_system_weekly_articles",
        "organic_system_weekly_articles_failure",
        "organic_system_refresh",
    ]
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue="recipe-test",
            workflows=[implementation, RecipeChild],
            activities=[recipe_child, *[stub(name) for name in names]],
        ):
            handle = await env.client.start_workflow(
                implementation.run,
                run_id,
                id=f"recipe:{run_id}",
                task_queue="recipe-test",
            )
            if keyword_fails:
                from temporalio.client import WorkflowFailureError

                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 45)
            else:
                await asyncio.wait_for(handle.result(), 45)
            history = await handle.fetch_history()
        # Every older recipe history replays through the current code's patch boundaries.
        await Replayer(workflows=[OrganicTrafficSystemWorkflow]).replay_workflow(history)
    refreshed = {"refresh"} if recipe == "weekly" else set()
    assert finished == (
        {"audit", "keywords", "technical"} | refreshed if keyword_fails else set(ids)
    )
    weekly_calls = [name for name, _ in calls if name.startswith("organic_system_weekly")]
    assert weekly_calls == (["organic_system_weekly_articles"] if recipe == "weekly" else [])
    child_events = [
        event.start_child_workflow_execution_initiated_event_attributes
        for event in history.events
        if event.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]
    assert len(child_events) == len(finished)
    assert len({event.workflow_id for event in child_events}) == len(finished)
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            for payload in event.activity_task_scheduled_event_attributes.input.payloads:
                value = json.loads(payload.data)
                assert value == run_id or value in [{"run_id": run_id, "step": key} for key in ids]


async def test_weekly_schedule_failure_is_recorded_without_failing_the_recipe():
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("local Temporal CLI required")
    run_id, calls = str(uuid4()), []

    def stub(name):
        @activity.defn(name=name)
        async def perform(argument):
            calls.append(name)
            if name == "organic_system_step":
                return {"status": "skipped", "reason": "not_requested"}
            if name == "organic_system_weekly_articles":
                raise ApplicationError("Fixture schedule service is down", non_retryable=True)
            if name == "organic_system_finish":
                return True

        return perform

    names = [
        "organic_system_prepare",
        "organic_system_step",
        "organic_system_step_failure",
        "organic_system_progress",
        "organic_system_finish",
        "organic_system_failure",
        "organic_system_weekly_articles",
        "organic_system_weekly_articles_failure",
        "organic_system_refresh",
    ]
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue="weekly-test",
            workflows=[OrganicTrafficSystemWorkflow],
            activities=[stub(name) for name in names],
        ):
            await asyncio.wait_for(
                env.client.execute_workflow(
                    OrganicTrafficSystemWorkflow.run,
                    run_id,
                    id=f"weekly:{run_id}",
                    task_queue="weekly-test",
                ),
                30,
            )
    assert calls.count("organic_system_weekly_articles") == 1
    assert calls.count("organic_system_weekly_articles_failure") == 1
    assert "organic_system_failure" not in calls
    assert calls[-1] == "organic_system_finish"


@pytest.mark.parametrize("no_change", [False, True])
async def test_shared_codex_preparation_short_circuits_without_sandbox(no_change):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("local Temporal CLI required")
    calls, run_id = [], str(uuid4())

    def stub(name):
        @activity.defn(name=name)
        async def perform(argument):
            assert argument == run_id
            calls.append(name)
            if name == "resolve_codex_project":
                return "repair-project"
            return no_change if name == "prepare_codex_procedure" else False

        return perform

    names = [
        "prepare_codex_procedure",
        "resolve_codex_project",
        "create_codex_procedure_sandbox",
        "persist_codex_procedure_artifact",
        "commit_codex_procedure_artifact",
        "request_codex_procedure_review",
        "project_codex_procedure_result",
    ]
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue="repair-test",
            workflows=[CodexProcedureWorkflow, ProjectCodexExecution],
            activities=[stub(name) for name in names],
        ):
            await asyncio.wait_for(
                env.client.execute_workflow(
                    CodexProcedureWorkflow.run,
                    run_id,
                    id=f"repair:{run_id}",
                    task_queue="repair-test",
                ),
                30,
            )
    assert calls == (["prepare_codex_procedure"] if no_change else names)
