from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    ChildWorkflowError,
    is_cancelled_exception,
)

from tin_lite.codex_execution import (
    ProjectCodexExecution,
    execute_codex_slice,
    execute_project_codex,
)
from tin_lite.domain import (
    ANSWER_PAGE_WORKFLOW_NAME,
    CODEX_PROCEDURE_EXECUTOR,
    CREATIVE_CHARACTER_WORKFLOW_NAME,
    EMAIL_CAMPAIGN_WORKFLOW_NAME,
    PROJECT_MEMORY_WORKFLOW_NAME,
    PROJECT_TASK_WORKFLOW_NAME,
    SCAN_REPORT_WORKFLOW_NAME,
    SITE_HEALTH_WORKFLOW_NAME,
    VISIBILITY_AUDIT_WORKFLOW_NAME,
    WEEKLY_BRIEF_WORKFLOW_NAME,
    WORKFLOW_NAME,
)

FAILURE_REASON_LIMIT = 600


def failure_reason(exc: BaseException) -> str:
    """The innermost cause's own words, so the run row says what failed, not just that it did."""
    cause: BaseException = exc
    depth = 0
    while (
        isinstance(cause, (ActivityError, ChildWorkflowError))
        and cause.cause is not None
        and depth < 8
    ):
        cause = cause.cause
        depth += 1
    if isinstance(cause, ApplicationError):
        label = cause.type or type(cause).__name__
        text = cause.message
    else:
        label = type(cause).__name__
        text = str(cause)
    text = " ".join(text.split()) if text else ""
    return f"{label}: {text or 'workflow failed'}"[:FAILURE_REASON_LIMIT]


@workflow.defn(name="tin.scheduled_dispatch")
class ScheduledDispatchWorkflow:
    @workflow.run
    async def run(self, project_workflow_id: str) -> None:
        dispatched = await workflow.execute_activity(
            "dispatch_scheduled_workflow",
            {
                "project_workflow_id": project_workflow_id,
                "occurrence_id": workflow.info().workflow_id,
                "scheduled_for": workflow.now().isoformat(),
            },
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        # Older completed histories always contain run_id, so this remains replay-compatible.
        if "run_id" not in dispatched:
            return
        await workflow.execute_child_workflow(
            str(dispatched["executor"]),
            str(dispatched["run_id"]),
            id=str(dispatched["temporal_workflow_id"]),
            task_queue=workflow.info().task_queue,
        )


@workflow.defn(name=WORKFLOW_NAME)
class DesignMdWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            if workflow.patched("project-codex-design-v1"):
                await execute_project_codex(run_id, "design")
            else:
                await execute_codex_slice({"run_id": run_id, "kind": "design"})
            await workflow.execute_activity(
                "commit_design_canonically",
                run_id,
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            await workflow.execute_activity(
                "project_design_result",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException as exc:
            await workflow.execute_activity(
                "project_design_failure",
                {"run_id": run_id, "reason": failure_reason(exc)},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name=PROJECT_MEMORY_WORKFLOW_NAME)
class ProjectMemoryWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "garden_project_memory",
                run_id,
                start_to_close_timeout=timedelta(minutes=3),
                schedule_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    maximum_interval=timedelta(seconds=10),
                ),
            )
            await workflow.execute_activity(
                "project_memory_result",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException as exc:
            await workflow.execute_activity(
                "project_memory_failure",
                {"run_id": run_id, "reason": failure_reason(exc)},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name=SCAN_REPORT_WORKFLOW_NAME)
class ScanReportWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "generate_scan_report",
                run_id,
                start_to_close_timeout=timedelta(minutes=3),
                schedule_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    maximum_interval=timedelta(seconds=10),
                ),
            )
            await workflow.execute_activity(
                "project_scan_result",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException as exc:
            await workflow.execute_activity(
                "project_scan_failure",
                {"run_id": run_id, "reason": failure_reason(exc)},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name=SITE_HEALTH_WORKFLOW_NAME)
class SiteHealthWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "draft_site_health_improvement",
                run_id,
                start_to_close_timeout=timedelta(minutes=15),
                schedule_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    maximum_interval=timedelta(seconds=10),
                ),
            )
            await workflow.execute_activity(
                "open_site_health_pull_request",
                run_id,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            await workflow.execute_activity(
                "project_site_health_result",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException as exc:
            await workflow.execute_activity(
                "project_site_health_failure",
                {"run_id": run_id, "reason": failure_reason(exc)},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name="organic.audit")
class OrganicAuditWorkflow:
    def __init__(self) -> None:
        self._stopped = False

    @workflow.signal(name="stop")
    async def stop(self) -> None:
        self._stopped = True

    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "organic_prepare",
                run_id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=5, maximum_interval=timedelta(seconds=30)
                ),
            )
            try:
                await workflow.execute_activity(
                    "organic_start_crawl",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(
                        maximum_attempts=5, maximum_interval=timedelta(seconds=30)
                    ),
                )
            except Exception:
                # Component failure yields explicit missing evidence, not a second
                # crawl or an invented clean bill of health.
                await workflow.execute_activity(
                    "organic_end_crawl",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            deadline = workflow.now() + timedelta(hours=1)
            for _ in range(120):
                if self._stopped or workflow.now() >= deadline:
                    break
                try:
                    done = await workflow.execute_activity(
                        "organic_poll_crawl",
                        run_id,
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                except Exception:
                    break
                if done:
                    break
                try:
                    await workflow.wait_condition(
                        lambda: self._stopped, timeout=timedelta(seconds=30)
                    )
                except TimeoutError:
                    pass
            await workflow.execute_activity(
                "organic_end_crawl",
                run_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if self._stopped:
                return
            count = await workflow.execute_activity(
                "organic_prepare_panel",
                run_id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            for index in range(count):
                if self._stopped:
                    return
                await workflow.execute_activity(
                    "organic_observe",
                    {"run_id": run_id, "index": index},
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            if self._stopped:
                return
            await workflow.execute_activity(
                "organic_brand_checks",
                run_id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            await workflow.execute_activity(
                "organic_publish",
                run_id,
                start_to_close_timeout=timedelta(minutes=3),
                schedule_to_close_timeout=timedelta(days=7),
                retry_policy=RetryPolicy(
                    maximum_interval=timedelta(minutes=5),
                    non_retryable_error_types=["OutputConflictError", "ValueError"],
                ),
            )
            await workflow.execute_activity(
                "organic_project",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=10),
            )
        except BaseException:
            await workflow.execute_activity(
                "organic_failure",
                run_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name=VISIBILITY_AUDIT_WORKFLOW_NAME)
class VisibilityAuditWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "generate_visibility_audit",
                run_id,
                start_to_close_timeout=timedelta(minutes=10),
                schedule_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    maximum_interval=timedelta(seconds=10),
                ),
            )
            await workflow.execute_activity(
                "project_visibility_result",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException as exc:
            await workflow.execute_activity(
                "project_visibility_failure",
                {"run_id": run_id, "reason": failure_reason(exc)},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name=ANSWER_PAGE_WORKFLOW_NAME)
class AnswerPageWorkflow:
    def __init__(self) -> None:
        self._approved = False

    @workflow.signal(name="approve")
    async def approve(self) -> None:
        self._approved = True

    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "draft_answer_page",
                run_id,
                start_to_close_timeout=timedelta(minutes=10),
                schedule_to_close_timeout=timedelta(minutes=20),
                heartbeat_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    maximum_interval=timedelta(seconds=10),
                ),
            )
            if workflow.patched("answer-page-human-review-v1"):
                review_required = await workflow.execute_activity(
                    "request_answer_page_review",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
                if review_required:
                    await workflow.wait_condition(lambda: self._approved)
                    await workflow.execute_activity(
                        "record_answer_page_approval",
                        run_id,
                        start_to_close_timeout=timedelta(minutes=1),
                        retry_policy=RetryPolicy(maximum_attempts=5),
                    )
            await workflow.execute_activity(
                "project_answer_page_result",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException as exc:
            await workflow.execute_activity(
                "project_answer_page_failure",
                {"run_id": run_id, "reason": failure_reason(exc)},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
        if workflow.patched("answer-page-delivery-v1"):
            # The reviewer's pick (pull request or publish now) ships the approved page; the
            # approved draft stays successful and readable whatever delivery does.
            try:
                await execute_content_delivery(run_id)
            except ActivityError:
                workflow.logger.warning(
                    "Answer page delivery needs attention", extra={"run_id": run_id}
                )


@workflow.defn(name=CREATIVE_CHARACTER_WORKFLOW_NAME)
class CharacterDesignWorkflow:
    """Design a brand character with prepared inputs and direct model calls; no sandbox."""

    def __init__(self) -> None:
        self._approved = False

    @workflow.signal(name="approve")
    async def approve(self) -> None:
        self._approved = True

    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "character_design",
                run_id,
                start_to_close_timeout=timedelta(minutes=15),
                schedule_to_close_timeout=timedelta(minutes=30),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    maximum_interval=timedelta(seconds=10),
                ),
            )
            review_required = await workflow.execute_activity(
                "character_review",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            if review_required:
                await workflow.wait_condition(lambda: self._approved)
                await workflow.execute_activity(
                    "character_approval",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
            await workflow.execute_activity(
                "character_project",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException as exc:
            await workflow.execute_activity(
                "character_failure",
                {"run_id": run_id, "reason": failure_reason(exc)},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name=PROJECT_TASK_WORKFLOW_NAME)
class ProjectTaskWorkflow:
    def __init__(self) -> None:
        self._resume = False
        self._approved = False
        self._stop = False

    @workflow.signal(name="resume")
    async def resume(self) -> None:
        self._resume = True

    @workflow.signal(name="approve")
    async def approve(self) -> None:
        self._approved = True

    @workflow.signal(name="stop")
    async def stop(self) -> None:
        self._stop = True

    @workflow.run
    async def run(self, run_id: str) -> None:
        turn_number = 1
        try:
            while True:
                # Patch per turn: an old task's next resumed turn must use the
                # project gate even when earlier turns predate this deployment.
                if workflow.patched(f"project-codex-task-v1-{turn_number}"):
                    outcome = await execute_project_codex(
                        run_id,
                        "task_turn",
                        turn_number=turn_number,
                        stop_requested=lambda: self._stop,
                    )
                else:
                    outcome = await execute_codex_slice(
                        {"run_id": run_id, "kind": "task_turn", "turn_number": str(turn_number)}
                    )
                if outcome in {"completed", "stopped"}:
                    return
                if outcome == "continue":
                    turn_number += 1
                    continue
                if outcome == "review":
                    while True:
                        await workflow.wait_condition(
                            lambda: self._approved or self._resume or self._stop
                        )
                        if self._stop:
                            await workflow.execute_activity(
                                "record_project_task_stop",
                                run_id,
                                start_to_close_timeout=timedelta(minutes=1),
                                retry_policy=RetryPolicy(maximum_attempts=3),
                            )
                            return
                        if self._resume:
                            self._resume = False
                            self._approved = False
                            turn_number += 1
                            break
                        self._approved = False
                        apply_outcome = await workflow.execute_activity(
                            "apply_project_task_changes",
                            run_id,
                            start_to_close_timeout=timedelta(minutes=5),
                            retry_policy=RetryPolicy(maximum_attempts=5),
                        )
                        if apply_outcome in {None, "applied"}:
                            return
                        if apply_outcome != "retry":
                            raise RuntimeError("project task apply returned an unsupported outcome")
                    continue
                if outcome not in {"needs_input", "paused"}:
                    raise RuntimeError("project task returned an unsupported outcome")
                await workflow.wait_condition(lambda: self._resume or self._stop)
                if self._stop:
                    await workflow.execute_activity(
                        "record_project_task_stop",
                        run_id,
                        start_to_close_timeout=timedelta(minutes=1),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                    return
                self._resume = False
                turn_number += 1
        except BaseException as exc:
            await workflow.execute_activity(
                "project_task_failure",
                {"run_id": run_id, "reason": failure_reason(exc)},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name="workflow.code")
class CodeWorkflow:
    def __init__(self):
        self._approved = False

    @workflow.signal(name="approve")
    async def approve(self):
        self._approved = True

    @workflow.run
    async def run(self, run_id: str):
        try:
            # The existing project execution position also bounds pure-code compute.
            await execute_project_codex(run_id, "code")
            await workflow.execute_activity(
                "publish_code_workflow",
                run_id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            review = await workflow.execute_activity(
                "review_code_workflow",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            if review:
                await workflow.wait_condition(lambda: self._approved)
                await workflow.execute_activity(
                    "approve_code_workflow",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
            await workflow.execute_activity(
                "project_code_workflow",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException:
            await workflow.execute_activity(
                "fail_code_workflow",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name=CODEX_PROCEDURE_EXECUTOR)
class CodexProcedureWorkflow:
    def __init__(self) -> None:
        self._approved = False
        self._revision_commands: list[str] = []
        self._seen_revision_commands: set[str] = set()

    @workflow.signal(name="request_revision")
    def request_revision(self, command_id: str) -> None:
        if command_id not in self._seen_revision_commands:
            self._seen_revision_commands.add(command_id)
            self._revision_commands.append(command_id)

    @workflow.signal(name="approve")
    async def approve(self) -> None:
        self._approved = True

    @workflow.run
    async def run(self, run_id: str) -> str | None:
        delivery_enabled = workflow.patched("reviewed-content-delivery-v1")
        try:
            if workflow.patched("codex-procedure-trusted-preparation-v1"):
                handled = await workflow.execute_activity(
                    "prepare_codex_procedure",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=15),
                    heartbeat_timeout=timedelta(seconds=20),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                if handled:
                    return
            if workflow.patched("project-codex-procedure-v1"):
                await execute_project_codex(run_id, "procedure")
            else:
                await execute_codex_slice({"run_id": run_id, "kind": "procedure"})
            await workflow.execute_activity(
                "commit_codex_procedure_artifact",
                run_id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            review_required = await workflow.execute_activity(
                "request_codex_procedure_review",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            if review_required:
                await workflow.wait_condition(lambda: self._approved or self._revision_commands)
                if self._revision_commands:
                    # This branch exists only after the new signal: existing histories
                    # keep their exact activity sequence. The replaced run never enters
                    # the old approval, success or delivery path.
                    while True:
                        command_id = self._revision_commands.pop(0)
                        child = await workflow.execute_activity(
                            "receive_workflow_revision",
                            {"run_id": run_id, "command_id": command_id},
                            start_to_close_timeout=timedelta(minutes=1),
                            retry_policy=RetryPolicy(maximum_attempts=5),
                        )
                        if child.get("stopped"):
                            raise asyncio.CancelledError()
                        try:
                            result = await workflow.execute_child_workflow(
                                CodexProcedureWorkflow.run,
                                child["run_id"],
                                id=child["temporal_workflow_id"],
                                task_queue=workflow.info().task_queue,
                            )
                            return result or child["run_id"]
                        except ChildWorkflowError as exc:
                            if is_cancelled_exception(exc):
                                raise asyncio.CancelledError() from exc
                            # The failed revision is visible in Postgres. Only another
                            # accepted user command buys another execution; never retry
                            # an uncertain supplier attempt by rerunning the ancestor.
                            await workflow.wait_condition(lambda: bool(self._revision_commands))
                await workflow.execute_activity(
                    "record_codex_procedure_approval",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
            await workflow.execute_activity(
                "project_codex_procedure_result",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException as exc:
            await workflow.execute_activity(
                "project_codex_procedure_failure",
                {"run_id": run_id, "reason": failure_reason(exc)},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise

        if delivery_enabled:
            try:
                await execute_content_delivery(run_id)
            except ActivityError:
                # The approved artifact remains successful and readable. Delivery has
                # its own receipted outcome and can be retried without running Codex.
                workflow.logger.warning(
                    "Content delivery needs attention", extra={"run_id": run_id}
                )


async def execute_content_delivery(run_id: str) -> None:
    await workflow.execute_activity(
        "deliver_content_draft",
        run_id,
        start_to_close_timeout=timedelta(minutes=5),
        heartbeat_timeout=timedelta(seconds=20),
        retry_policy=RetryPolicy(maximum_attempts=3),
    )


@workflow.defn(name="tin.content_draft_delivery")
class ContentDraftDeliveryWorkflow:
    """Retry one approved draft's delivery, without recreating the draft run."""

    @workflow.run
    async def run(self, run_id: str) -> None:
        await execute_content_delivery(run_id)


@workflow.defn(name=WEEKLY_BRIEF_WORKFLOW_NAME)
class WeeklyBriefWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "generate_weekly_brief",
                run_id,
                start_to_close_timeout=timedelta(minutes=5),
                schedule_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    maximum_interval=timedelta(seconds=10),
                ),
            )
            await workflow.execute_activity(
                "project_weekly_brief_result",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException as exc:
            await workflow.execute_activity(
                "project_weekly_brief_failure",
                {"run_id": run_id, "reason": failure_reason(exc)},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name="outreach.email_recipient")
class EmailRecipientWorkflow:
    @workflow.run
    async def run(self, recipient_id: str) -> None:
        try:
            while True:
                wait_seconds = await workflow.execute_activity(
                    "reserve_email_campaign_delivery",
                    {"recipient_id": recipient_id, "stage": "initial"},
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
                if wait_seconds <= 0:
                    break
                await workflow.sleep(timedelta(seconds=wait_seconds))
            await workflow.execute_activity(
                "send_email_campaign_recipient",
                {"recipient_id": recipient_id, "stage": "initial"},
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            delay_days = await workflow.execute_activity(
                "email_campaign_follow_up_delay",
                recipient_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            if delay_days > 0:
                await workflow.sleep(timedelta(days=delay_days))
                replied = await workflow.execute_activity(
                    "check_email_campaign_reply",
                    recipient_id,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
                if not replied:
                    while True:
                        wait_seconds = await workflow.execute_activity(
                            "reserve_email_campaign_delivery",
                            {"recipient_id": recipient_id, "stage": "follow_up"},
                            start_to_close_timeout=timedelta(minutes=1),
                            retry_policy=RetryPolicy(maximum_attempts=5),
                        )
                        if wait_seconds <= 0:
                            break
                        await workflow.sleep(timedelta(seconds=wait_seconds))
                    await workflow.execute_activity(
                        "send_email_campaign_recipient",
                        {"recipient_id": recipient_id, "stage": "follow_up"},
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=RetryPolicy(maximum_attempts=5),
                    )
            await workflow.execute_activity(
                "complete_email_campaign_recipient",
                recipient_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException as exc:
            await workflow.execute_activity(
                "fail_email_campaign_recipient",
                {
                    "recipient_id": recipient_id,
                    "reason": f"{type(exc).__name__}: recipient workflow failed",
                },
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name=EMAIL_CAMPAIGN_WORKFLOW_NAME)
class EmailCampaignWorkflow:
    def __init__(self) -> None:
        self._approved = False

    @workflow.signal(name="approve")
    async def approve(self) -> None:
        self._approved = True

    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "prepare_email_campaign",
                run_id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            await workflow.execute_activity(
                "request_email_campaign_review",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            await workflow.wait_condition(lambda: self._approved)
            await workflow.execute_activity(
                "record_email_campaign_approval",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            recipient_ids = await workflow.execute_activity(
                "list_email_campaign_recipients",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            children = []
            for recipient_id in recipient_ids:
                children.append(
                    await workflow.start_child_workflow(
                        EmailRecipientWorkflow.run,
                        recipient_id,
                        id=f"email-recipient:{recipient_id}",
                        task_queue=workflow.info().task_queue,
                    )
                )
            await asyncio.gather(*children)
            await workflow.execute_activity(
                "complete_email_campaign",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except BaseException as exc:
            await workflow.execute_activity(
                "project_email_campaign_failure",
                {"run_id": run_id, "reason": failure_reason(exc)},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name="organic.keyword_plan")
class KeywordPlanWorkflow:
    def __init__(self) -> None:
        self._stopped = False

    @workflow.signal(name="stop")
    async def stop(self) -> None:
        self._stopped = True

    @workflow.run
    async def run(self, run_id: str) -> None:
        async def execute(name, argument=run_id, *, minutes=5):
            return await workflow.execute_activity(
                name,
                argument,
                start_to_close_timeout=timedelta(minutes=minutes),
                retry_policy=RetryPolicy(
                    maximum_attempts=3, maximum_interval=timedelta(seconds=10)
                ),
            )

        try:
            for name in ("keyword_prepare", "keyword_collect"):
                if self._stopped:
                    return
                await execute(name, minutes=25 if name == "keyword_collect" else 5)
            count = await execute("keyword_sample_count")
            for index in range(count):
                if self._stopped:
                    return
                await execute("keyword_inspect", {"run_id": run_id, "index": index}, minutes=2)
            for name in ("keyword_review", "keyword_publish", "keyword_project"):
                if self._stopped:
                    return
                await execute(name)
        except BaseException:
            if not self._stopped:
                await execute("keyword_failure")
                raise


@workflow.defn(name="content.plan")
class ContentPlanWorkflow:
    def __init__(self) -> None:
        self._stopped = False

    @workflow.signal(name="stop")
    async def stop(self) -> None:
        self._stopped = True

    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "content_plan_execute",
                run_id,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(
                    maximum_attempts=3, maximum_interval=timedelta(seconds=10)
                ),
            )
        except BaseException:
            if self._stopped:
                return
            await workflow.execute_activity(
                "content_plan_failure",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name="organic.traffic_system")
class OrganicTrafficSystemWorkflow:
    """Fixed composition; only run identifiers and finite step names enter history."""

    @workflow.run
    async def run(self, run_id: str) -> None:
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
            succeeded = await call("organic_system_finish", run_id, minutes=5)
            if not succeeded:
                raise ApplicationError("One or more organic system steps could not finish.")
        except BaseException:
            await call("organic_system_failure", run_id)
            raise


@workflow.defn(name="style.capture")
class StyleCaptureWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            for step in ("style_prepare", "style_extract", "style_publish"):
                await workflow.execute_activity(
                    step,
                    run_id,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
        except BaseException:
            await workflow.execute_activity(
                "style_failure",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name="growth.onboarding_plan")
class GrowthOnboardingPlanWorkflow:
    """Read, write, save. Only the run identifier enters history; activities hold every result."""

    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "growth_plan_prepare",
                run_id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            # One activity owns the model sequence; each step is receipted, so a retry replays
            # completed steps from their effects and buys nothing twice.
            await workflow.execute_activity(
                "growth_plan_write",
                run_id,
                start_to_close_timeout=timedelta(minutes=20),
                heartbeat_timeout=timedelta(minutes=6),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            await workflow.execute_activity(
                "growth_plan_publish",
                run_id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except BaseException:
            await workflow.execute_activity(
                "growth_plan_failure",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn(name="growth.onboarding")
class GrowthOnboardingWorkflow:
    """Plan, hold for picks and connections, then set up; only run identifiers enter history."""

    def __init__(self) -> None:
        self._approved = False

    @workflow.signal(name="approve")
    async def approve(self) -> None:
        self._approved = True

    @workflow.run
    async def run(self, run_id: str) -> None:
        async def call(name, argument, minutes=2):
            return await workflow.execute_activity(
                name,
                argument,
                start_to_close_timeout=timedelta(minutes=minutes),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        try:
            await call("growth_onboarding_prepare", run_id)
            child = await call("growth_onboarding_step", {"run_id": run_id, "step": "plan"})
            await workflow.execute_child_workflow(
                child["executor"],
                child["run_id"],
                id=child["temporal_workflow_id"],
                task_queue=workflow.info().task_queue,
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                cancellation_type=workflow.ChildWorkflowCancellationType.ABANDON,
            )
            review_required = await call("growth_onboarding_review", run_id)
            if review_required:
                await workflow.wait_condition(lambda: self._approved)
                await call("growth_onboarding_approval", run_id)
            started = await call("growth_onboarding_setup", run_id, minutes=5)
            for item in started:
                await workflow.start_child_workflow(
                    item["executor"],
                    item["run_id"],
                    id=item["temporal_workflow_id"],
                    task_queue=workflow.info().task_queue,
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                    cancellation_type=workflow.ChildWorkflowCancellationType.ABANDON,
                )
            await call("growth_onboarding_report", run_id, minutes=5)
        except BaseException:
            await call("growth_onboarding_failure", run_id)
            raise


def registered_workflows() -> list[type]:
    return [
        CodeWorkflow,
        ContentDraftDeliveryWorkflow,
        ProjectCodexExecution,
        StyleCaptureWorkflow,
        OrganicTrafficSystemWorkflow,
        GrowthOnboardingWorkflow,
        GrowthOnboardingPlanWorkflow,
        ScheduledDispatchWorkflow,
        ContentPlanWorkflow,
        DesignMdWorkflow,
        ProjectMemoryWorkflow,
        ScanReportWorkflow,
        SiteHealthWorkflow,
        VisibilityAuditWorkflow,
        OrganicAuditWorkflow,
        KeywordPlanWorkflow,
        AnswerPageWorkflow,
        CharacterDesignWorkflow,
        CodexProcedureWorkflow,
        WeeklyBriefWorkflow,
        ProjectTaskWorkflow,
        EmailCampaignWorkflow,
        EmailRecipientWorkflow,
    ]


def registered_workflow_implementations() -> dict[str, type]:
    """Explicit executor-to-implementation map used by product catalog dispatch."""
    return {
        "workflow.code": CodeWorkflow,
        "style.capture": StyleCaptureWorkflow,
        "organic.traffic_system": OrganicTrafficSystemWorkflow,
        "growth.onboarding": GrowthOnboardingWorkflow,
        "growth.onboarding_plan": GrowthOnboardingPlanWorkflow,
        "content.plan": ContentPlanWorkflow,
        CREATIVE_CHARACTER_WORKFLOW_NAME: CharacterDesignWorkflow,
        WORKFLOW_NAME: DesignMdWorkflow,
        PROJECT_MEMORY_WORKFLOW_NAME: ProjectMemoryWorkflow,
        SCAN_REPORT_WORKFLOW_NAME: ScanReportWorkflow,
        SITE_HEALTH_WORKFLOW_NAME: SiteHealthWorkflow,
        VISIBILITY_AUDIT_WORKFLOW_NAME: VisibilityAuditWorkflow,
        "organic.audit": OrganicAuditWorkflow,
        "organic.keyword_plan": KeywordPlanWorkflow,
        ANSWER_PAGE_WORKFLOW_NAME: AnswerPageWorkflow,
        CODEX_PROCEDURE_EXECUTOR: CodexProcedureWorkflow,
        WEEKLY_BRIEF_WORKFLOW_NAME: WeeklyBriefWorkflow,
        PROJECT_TASK_WORKFLOW_NAME: ProjectTaskWorkflow,
        EMAIL_CAMPAIGN_WORKFLOW_NAME: EmailCampaignWorkflow,
    }
