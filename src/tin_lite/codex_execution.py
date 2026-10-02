"""Run-owned compute, bounded by the existing activity worker capacity.

Historical executions keep their project-scoped child IDs for replay. Independent
runs use separate sandboxes; only canonical publication needs a project lock.
"""

from collections.abc import Callable
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

# One compute activity must outlive its sandbox: procedures may declare up to 7,200 s
# (procedures.MAX_SANDBOX_TIMEOUT_SECONDS) and design/task sandboxes use the configured
# TIN_LITE_SANDBOX_TIMEOUT, at most 7,200 s. Heartbeats still detect a lost worker in
# 20 s, and a retry recovers a checkpoint; it never buys the model work again. Changing
# activity timeouts changes no workflow command, so recorded histories still replay.
COMPUTE_START_TO_CLOSE = timedelta(hours=3)
COMPUTE_SCHEDULE_TO_CLOSE = timedelta(hours=6)


async def execute_project_codex(
    run_id: str,
    kind: str,
    *,
    turn_number: int = 1,
    stop_requested: Callable[[], bool] | None = None,
) -> str | None:
    project_id = await workflow.execute_activity(
        "resolve_codex_project",
        run_id,
        start_to_close_timeout=timedelta(minutes=1),
        retry_policy=RetryPolicy(maximum_attempts=5),
    )
    # Patch per turn so an existing task's next turn can use independent compute.
    child_id = (
        f"tin.run-codex:{run_id}:{turn_number}"
        if workflow.patched(f"parallel-project-compute-v1-{turn_number}")
        else f"tin.project-codex:{project_id}"
    )
    deadline = workflow.now() + timedelta(hours=4)
    delay_seconds = 1
    while True:
        if stop_requested is not None and stop_requested():
            await workflow.execute_activity(
                "record_project_task_stop",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return "stopped"
        try:
            child = await workflow.start_child_workflow(
                ProjectCodexExecution.run,
                {"run_id": run_id, "kind": kind, "turn_number": str(turn_number)},
                id=child_id,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
                cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
        except WorkflowAlreadyStartedError:
            if workflow.now() >= deadline:
                raise ApplicationError(
                    "This project's execution queue has been busy for four hours. Try again."
                    if kind == "code"
                    else (
                        "This project's Codex execution queue has been busy for four hours. "
                        "Try again."
                    ),
                    type="ProjectCodexQueueTimeout",
                    non_retryable=True,
                ) from None
            if stop_requested is None:
                await workflow.sleep(timedelta(seconds=delay_seconds))
            else:
                try:
                    await workflow.wait_condition(
                        stop_requested, timeout=timedelta(seconds=delay_seconds)
                    )
                except TimeoutError:
                    pass
            delay_seconds = min(30, delay_seconds * 2)
        else:
            # Never catch a compute failure as contention or automatically start
            # another child: the original activities own paid-call retry policy.
            return await child


async def execute_codex_slice(payload: dict[str, str], *, cancellable: bool = False) -> str | None:
    """The existing activity sequence, also used by unpatched historical paths."""
    run_id, kind = payload["run_id"], payload["kind"]
    options = (
        {"cancellation_type": workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED}
        if cancellable
        else {}
    )
    if kind == "code":
        await workflow.execute_activity(
            "execute_code_workflow",
            run_id,
            start_to_close_timeout=timedelta(minutes=4),
            heartbeat_timeout=timedelta(seconds=20),
            retry_policy=RetryPolicy(maximum_attempts=3, maximum_interval=timedelta(seconds=10)),
            **options,
        )
        return None
    if kind == "task_turn":
        return await workflow.execute_activity(
            "run_project_task_turn",
            {"run_id": run_id, "turn_number": payload["turn_number"]},
            start_to_close_timeout=COMPUTE_START_TO_CLOSE,
            schedule_to_close_timeout=COMPUTE_SCHEDULE_TO_CLOSE,
            heartbeat_timeout=timedelta(seconds=20),
            retry_policy=RetryPolicy(maximum_attempts=3, maximum_interval=timedelta(seconds=10)),
            **options,
        )
    if kind not in {"design", "procedure"}:
        raise ApplicationError("Unknown Codex execution kind", non_retryable=True)
    await workflow.execute_activity(
        "create_design_sandbox" if kind == "design" else "create_codex_procedure_sandbox",
        run_id,
        start_to_close_timeout=timedelta(minutes=3),
        retry_policy=RetryPolicy(maximum_attempts=3),
        **options,
    )
    await workflow.execute_activity(
        "persist_design_artifact" if kind == "design" else "persist_codex_procedure_artifact",
        run_id,
        start_to_close_timeout=COMPUTE_START_TO_CLOSE,
        schedule_to_close_timeout=COMPUTE_SCHEDULE_TO_CLOSE,
        heartbeat_timeout=timedelta(seconds=20),
        retry_policy=RetryPolicy(
            maximum_attempts=5 if kind == "design" else 3,
            maximum_interval=timedelta(seconds=10),
        ),
        **options,
    )
    return None


@workflow.defn(name="tin.project_codex_execution")
class ProjectCodexExecution:
    @workflow.run
    async def run(self, payload: dict[str, str]) -> str | None:
        return await execute_codex_slice(payload, cancellable=True)
