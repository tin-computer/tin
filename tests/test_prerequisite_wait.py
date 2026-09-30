"""A run started while its prerequisite is still running waits for it, or says what it lacks."""

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker
from test_procedure_publication import publication_db as publication_db
from test_project_codex_execution import temporal_env as temporal_env
from test_workflow_prerequisites import (
    ACTOR,
    HEAD,
    A,
    project_fixture,
    server,
    start,
    structured,
    succeeded_run,
)

from tin_lite import workflows as workflow_module
from tin_lite.activities import TinActivities
from tin_lite.activity_lanes import ActivityLaneInterceptor, trusted_task_queue, workflow_runner
from tin_lite.domain import PREREQUISITE_WAIT_MEMO
from tin_lite.workflow_prerequisites import PrerequisiteError
from tin_lite.workflows import AnswerPageWorkflow, ScheduledDispatchWorkflow

# Answer pages recommend the latest organic audit, whose fixed questions they answer.
ANSWER = ("organic.audit", "content.answer_page")


async def running_run(f, key, inputs, *, status="running"):
    workflow = f.workflows[key]
    run, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=workflow.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={"project_id": str(f.project.id), **inputs},
        definition_commit_sha=A,
        pinned_definition=workflow.definition,
    )
    await f.db.pool.execute("UPDATE workflow_runs SET status=$2 WHERE id=$1", run.id, status)
    return run


async def finish(f, run, status):
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status=$2, canonical_commit_sha=$3, finished_at=now() "
        "WHERE id=$1",
        run.id,
        status,
        HEAD if status == "succeeded" else None,
    )


def activities(f):
    return TinActivities(database=f.db, storage=f.storage, sandboxes=None, settings=f.settings)


async def test_answer_page_waits_for_a_running_audit_then_reads_it(publication_db, monkeypatch):
    f = await project_fixture(publication_db, extra=ANSWER)
    audit = await running_run(
        f, "organic.audit", {"site_url": "https://example.com/", "market": "US"}
    )
    tools = server(f, monkeypatch)
    started = structured(
        await tools.call_tool(
            "start_workflow",
            {"project_id": str(f.project.id), "workflow_id": "content.answer_page"},
        )
    )
    note = (
        f"This run waits up to 30 minutes for organic.audit run {audit.id} to finish, "
        "then reads its result."
    )
    assert started["status"] == "pending" and started["advisories"] == []
    assert started["prerequisite_notes"] == [note]
    assert note in started["relay"]
    [waiting] = started["waiting"]
    assert waiting["running_run_id"] == str(audit.id)
    assert waiting["suggested_call"] == {"tool": "get_run", "run_id": str(audit.id)}
    options = f.runtime.temporal.start_workflow.await_args.kwargs
    assert options["memo"] == {PREREQUISITE_WAIT_MEMO: [str(audit.id)]}

    run_id = started["id"]
    wait = activities(f).prerequisite_wait
    assert await wait({"run_id": run_id, "final": False}) is True
    assert (await f.db.get_run(UUID(run_id))).progress_summary == note

    await finish(f, audit, "succeeded")
    assert await wait({"run_id": run_id, "final": False}) is False
    run = await f.db.get_run(UUID(run_id))
    evidence = run.prerequisite_evidence
    assert evidence["waited"]["statuses"] == {str(audit.id): "succeeded"}
    assert evidence["waited"]["refused"] is None and "waiting" not in evidence
    assert evidence["items"][0]["satisfied"] is True
    assert evidence["items"][0]["evidence"]["run_id"] == str(audit.id)
    done = f"organic.audit run {audit.id} finished; this run reads its result."
    assert evidence["notes"] == [done] and run.progress_summary == done
    view = structured(await tools.call_tool("get_run", {"run_id": run_id}))
    assert view["prerequisite_notes"] == [done]
    # A retried activity reads the settled record; it neither waits nor re-checks.
    assert await wait({"run_id": run_id, "final": False}) is False


async def test_recommended_prerequisite_says_when_the_run_goes_without_it(
    publication_db, monkeypatch
):
    f = await project_fixture(publication_db, extra=ANSWER)
    tools = server(f, monkeypatch)
    args = {"project_id": str(f.project.id), "workflow_id": "content.answer_page"}
    # Nothing is running and nothing succeeded: the start says so and does not wait.
    started = structured(await tools.call_tool("start_workflow", args))
    assert started["prerequisite_notes"] == [
        "Tin starts this run without a successful organic.audit run. Run organic.audit "
        "and wait for it to succeed. Then start this workflow again to use it."
    ]
    assert "waiting" not in started
    assert "memo" not in f.runtime.temporal.start_workflow.await_args.kwargs

    wait = activities(f).prerequisite_wait
    for outcome in ("failed", "timed out"):
        audit = await running_run(
            f, "organic.audit", {"site_url": "https://example.com/", "market": "US"}
        )
        run_id = structured(await tools.call_tool("start_workflow", args))["id"]
        if outcome == "failed":
            await finish(f, audit, "failed")
            ending = "failed"
        else:
            ending = "did not finish within 30 minutes"
        # The workflow sets final once its wait reaches the limit.
        assert await wait({"run_id": run_id, "final": outcome != "failed"}) is False
        run = await f.db.get_run(UUID(run_id))
        assert run.prerequisite_evidence["notes"] == [
            "Tin runs this without a successful organic.audit run: "
            f"organic.audit run {audit.id} {ending}."
        ]
        assert run.status.value == "pending"
        await finish(f, audit, "stopped")


async def test_required_prerequisite_waits_then_refuses_when_its_run_fails(publication_db):
    f = await project_fixture(publication_db)
    signup = await running_run(f, "qa.signup_walkthrough", {"product_url": "https://a.example/"})
    run = await start(f, "product.deep_dive", {"product_url": "https://a.example/"})
    evidence = run.prerequisite_evidence
    # The run and the test account both come from the same running walkthrough.
    assert sorted(view["kind"] for view in evidence["waiting"]) == ["identity", "run"]
    assert {view["running_run_id"] for view in evidence["waiting"]} == {str(signup.id)}
    assert [view["kind"] for view in evidence["advisories"]] == ["artifact"]
    memo = f.runtime.temporal.start_workflow.await_args.kwargs["memo"]
    assert memo == {PREREQUISITE_WAIT_MEMO: [str(signup.id)]}

    await finish(f, signup, "failed")
    wait = activities(f).prerequisite_wait
    for _ in range(2):  # A retry repeats the same refusal from the pinned record.
        with pytest.raises(ApplicationError) as refused:
            await wait({"run_id": str(run.id), "final": False})
        assert refused.value.type == "PrerequisiteMissing" and refused.value.non_retryable
        assert refused.value.message.startswith(
            f"This run stopped before doing any work: qa.signup_walkthrough run {signup.id} "
            "failed. Map what the product actually does (product.deep_dive) needs 2 "
            "prerequisites first:"
        )
    stored = (await f.db.get_run(run.id)).prerequisite_evidence
    assert stored["waited"]["refused"] == refused.value.message


async def test_workflow_that_cannot_wait_names_the_running_prerequisite(
    publication_db, monkeypatch
):
    f = await project_fixture(
        publication_db, extra=("organic.audit", "organic.keyword_plan", "content.plan")
    )
    site = {"site_url": "https://a.example/", "market": "US"}
    audit = await running_run(f, "organic.audit", site)
    keywords = await succeeded_run(f, "organic.keyword_plan", site)
    with pytest.raises(ToolError) as caught:
        await server(f, monkeypatch).call_tool(
            "start_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": "content.plan",
                "inputs": {
                    "audit_run_id": str(audit.id),
                    "keyword_run_id": str(keywords.id),
                    "start_date": "2026-10-05",
                },
            },
        )
    text = str(caught.value)
    diagnostic = json.loads(text[text.index("{") :])
    [unmet] = [item for item in diagnostic["prerequisites"] if not item["satisfied"]]
    hint = (
        f"organic.audit run {audit.id} is still running; call get_run with run_id {audit.id} "
        "until it succeeds, then start this again."
    )
    assert unmet["how_to_satisfy"] == hint and hint in diagnostic["message"]
    assert unmet["suggested_call"] == {"tool": "get_run", "run_id": str(audit.id)}
    with pytest.raises(PrerequisiteError):
        await start(
            f,
            "content.plan",
            {
                "audit_run_id": str(audit.id),
                "keyword_run_id": str(keywords.id),
                "start_date": "2026-10-05",
            },
        )


# ---------------------------------------------------------------- Temporal


async def test_answer_page_holds_in_temporal_until_its_prerequisite_finishes(
    temporal_env, monkeypatch
):
    # Unsandboxed, so the short test timings below reach the workflow code.
    monkeypatch.setattr(workflow_module, "PREREQUISITE_POLL_INTERVAL", timedelta(seconds=0.1))
    monkeypatch.setattr(workflow_module, "PREREQUISITE_WAIT_MINUTES", 0.02)
    queue = f"prerequisite-wait-{uuid4()}"
    waited, plain, limited, refused = (str(uuid4()) for _ in range(4))
    events: list[tuple[str, str]] = []
    finals: list[bool] = []
    reasons: list[str] = []
    polls: dict[str, int] = {}

    def stub(name):
        @activity.defn(name=name)
        async def execute(value):
            run_id = value if isinstance(value, str) else value.get("run_id")
            events.append((name, run_id))
            if name == "prerequisite_wait":
                polls[run_id] = polls.get(run_id, 0) + 1
                if run_id == refused:
                    raise ApplicationError(
                        "This run stopped before doing any work.",
                        type="PrerequisiteMissing",
                        non_retryable=True,
                    )
                if run_id == limited:
                    finals.append(value["final"])
                    return not value["final"]
                return polls[run_id] < 3
            if name == "dispatch_scheduled_workflow":
                return {
                    "run_id": waited,
                    "executor": "content.answer_page",
                    "temporal_workflow_id": f"content.answer_page:{waited}",
                    "prerequisite_wait": [str(uuid4())],
                }
            if name == "request_answer_page_review":
                return False
            if name == "project_answer_page_failure":
                reasons.append(value["reason"])
            return None

        return execute

    names = [
        "dispatch_scheduled_workflow",
        "prerequisite_wait",
        "draft_answer_page",
        "request_answer_page_review",
        "record_answer_page_approval",
        "project_answer_page_result",
        "project_answer_page_failure",
        "deliver_content_draft",
    ]
    stubs = [stub(name) for name in names]
    histories = []
    async with (
        Worker(
            temporal_env.client,
            task_queue=queue,
            workflows=[AnswerPageWorkflow, ScheduledDispatchWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=stubs,
            interceptors=[ActivityLaneInterceptor()],
        ),
        Worker(temporal_env.client, task_queue=trusted_task_queue(queue), activities=stubs),
    ):
        client = temporal_env.client
        # A scheduled occurrence hands its wait to the child it starts.
        dispatch = await client.start_workflow(
            ScheduledDispatchWorkflow.run, str(uuid4()), id=f"dispatch-{queue}", task_queue=queue
        )
        await asyncio.wait_for(dispatch.result(), 20)
        for run_id, memo in ((plain, None), (limited, [str(uuid4())])):
            handle = await client.start_workflow(
                AnswerPageWorkflow.run,
                run_id,
                id=f"content.answer_page:{run_id}",
                task_queue=queue,
                **({"memo": {PREREQUISITE_WAIT_MEMO: memo}} if memo else {}),
            )
            await asyncio.wait_for(handle.result(), 20)
            histories.append(await handle.fetch_history())
        failing = await client.start_workflow(
            AnswerPageWorkflow.run,
            refused,
            id=f"content.answer_page:{refused}",
            task_queue=queue,
            memo={PREREQUISITE_WAIT_MEMO: [str(uuid4())]},
        )
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(failing.result(), 20)
        histories.append(await failing.fetch_history())
        child = client.get_workflow_handle(f"content.answer_page:{waited}")
        histories += [await dispatch.fetch_history(), await child.fetch_history()]

    def order(run_id):
        return [name for name, run in events if run == run_id]

    # The child polled three times, and only then drafted the page.
    assert order(waited)[:4] == ["prerequisite_wait"] * 3 + ["draft_answer_page"]
    # An ordinary start carries no memo and never touches the wait.
    assert order(plain)[0] == "draft_answer_page" and "prerequisite_wait" not in order(plain)
    # The time limit reaches the activity as final, which then settles the wait.
    assert finals[0] is False and finals[-1] is True and "draft_answer_page" in order(limited)
    # A refused wait fails the run before any work, through the usual failure projection.
    assert order(refused) == ["prerequisite_wait", "project_answer_page_failure"]
    assert reasons == ["PrerequisiteMissing: This run stopped before doing any work."]
    monkeypatch.undo()  # Replay with the shipped timings, in the production sandbox.
    for history in histories:
        await Replayer(
            workflows=[AnswerPageWorkflow, ScheduledDispatchWorkflow],
            workflow_runner=workflow_runner(),
            interceptors=[ActivityLaneInterceptor()],
        ).replay_workflow(history)
