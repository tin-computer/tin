from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite.answer_page import (
    AnswerPageDrafter,
    AnswerPageSource,
    validate_answer_page_artifacts,
)
from tin_lite.billing_contracts import BillingError
from tin_lite.code_storage import CodeStorage, reviewed_task_diff
from tin_lite.db import Database
from tin_lite.domain import (
    ANSWER_PAGE_PATH,
    ANSWER_PAGE_WORKFLOW_NAME,
    ARTIFACT_PATH,
    CODEX_PROCEDURE_EXECUTOR,
    EMAIL_CAMPAIGN_WORKFLOW_NAME,
    EMAIL_SHORTLIST_PATH,
    MEMORY_INDEX_PATH,
    PROJECT_MEMORY_WORKFLOW_NAME,
    PROJECT_TASK_WORKFLOW_NAME,
    QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME,
    SCAN_REPORT_PATH,
    SCAN_REPORT_WORKFLOW_NAME,
    SITE_HEALTH_WORKFLOW_NAME,
    STUDIO_PROVIDER,
    STUDIO_VOICE_CAPABILITY,
    TEST_IDENTITY_SMS_CAPABILITY,
    TEST_IDENTITY_WRITE_CAPABILITY,
    VISIBILITY_AUDIT_PATH,
    VISIBILITY_AUDIT_WORKFLOW_NAME,
    WEEKLY_BRIEF_WORKFLOW_NAME,
    IntegrationConnection,
    ProjectTestIdentity,
    RunStatus,
    StaleGenerationError,
    Workflow,
    WorkflowRun,
    answer_page_evidence_path,
    site_health_report_path,
    visibility_evidence_path,
    weekly_brief_evidence_path,
    weekly_brief_path,
)
from tin_lite.e2b_runtime import (
    E2BRuntime,
    RolloutSink,
    SandboxProcedureInput,
    SandboxTaskEvent,
    SandboxTaskInput,
)
from tin_lite.email_outreach import (
    build_campaign_plan,
    campaign_message_id,
    campaign_plan_path,
    parse_email_send_policy,
    parse_selected_shortlist,
    render_email_template,
)
from tin_lite.integrations import (
    GOOGLE_WORKSPACE_PROVIDER,
    GitHubFileChange,
    GitHubRepositorySnapshot,
    IntegrationDeliveryUnknownError,
    IntegrationError,
    IntegrationService,
    load_pinned_integration_requirements,
)
from tin_lite.memory import (
    MemoryGardener,
    MemorySource,
    extract_owned_section,
    validate_memory_index,
)
from tin_lite.model_usage import model_usage_scope
from tin_lite.procedures import (
    GITHUB_PULL_REQUEST_RESULT,
    GITHUB_REPOSITORY_WORKSPACE,
    IDENTITY_REUSE_ACTIVE,
    PROJECT_ARTIFACT_RESULT,
    REVIEWED_DIAGRAM_VALIDATORS,
    TIN_DIAGRAM_BRANDED_VALIDATOR,
    PinnedCodexProcedure,
    artifact_host,
    build_procedure_pull_request_receipt,
    load_pinned_codex_procedure,
    procedure_checkpoint_path,
    procedure_receipt_path,
    validate_procedure_artifact,
    validate_procedure_pull_request,
)
from tin_lite.publication import OutputCheckpoint, OutputConflictError, PublicationPendingError
from tin_lite.rollouts import RolloutCapture
from tin_lite.scan import ScanReporter, ScanSource, validate_scan_report
from tin_lite.schedules import ScheduledWorkflowSkip, WorkflowSchedule, next_run_after
from tin_lite.settings import Settings
from tin_lite.site_health import (
    SiteHealthImprover,
    SiteHealthProposal,
    build_site_health_report,
    fetch_live_page_evidence,
    validate_site_health_model_route,
)
from tin_lite.system_wiki import read_system_wiki_document
from tin_lite.usage_capture import external_usage_scope, observation_key
from tin_lite.visibility import (
    ResponseCheckpoint,
    ResponseRequest,
    VisibilityAuditor,
    VisibilityProtocolError,
    VisibilityRecoveryError,
    VisibilitySource,
    read_visibility_response_checkpoint,
    validate_visibility_artifacts,
    validate_visibility_publication,
    visibility_publication_facts,
    visibility_response_checkpoint,
)
from tin_lite.weekly_brief import (
    WeeklyBriefReporter,
    WeeklyBriefSource,
    validate_weekly_brief_artifacts,
)
from tin_lite.workflow_prerequisites import PrerequisiteError, evaluate_prerequisites

SANDBOX_HEARTBEAT_SECONDS = 5
T = TypeVar("T")

if TYPE_CHECKING:
    from temporalio.client import Client

FAILURE_MESSAGE_LIMIT = 600
RESTART_WINDOW_SECONDS = 120
RESTART_SUFFIX = " (Tin had just restarted)"
AUTO_RETRY_START_KEY_PREFIX = "auto-retry:"
ONBOARDING_START_KEY_PREFIX = "onboarding:"
_FAILURE_MESSAGE_KEYS = ("reason", "error", "message", "detail")
# Errors that a second attempt can fix: the switchboard was restarting (HTTP 502/503/504
# from Caddy, refused or reset connections, sandbox -> switchboard call timeouts) or the
# E2B envd stream dropped. Model and validation failures never match.
_TRANSIENT_FAILURE_PATTERN = re.compile(
    r"\b50[234]\b|bad gateway|service unavailable|gateway time-?out"
    r"|connection (?:refused|reset|error|aborted)|remote(?:ly)? ?disconnected"
    r"|remoteprotocolerror|connect(?:ion)?(?: ?timeout| timed out)|read ?timeout|timed out"
    r"|\benvd\b|stream (?:closed|reset|error)",
    re.IGNORECASE,
)


def process_uptime_seconds() -> float:
    """Seconds since this switchboard process started, on the clock MCP get_run reports."""
    try:
        from tin_lite.mcp_server import SERVICE_STARTED_AT
    except Exception:  # pragma: no cover - the clock is optional for failure projection
        return float("inf")
    return (datetime.now(UTC) - SERVICE_STARTED_AT).total_seconds()


def restarted_recently() -> bool:
    return process_uptime_seconds() < RESTART_WINDOW_SECONDS


def failure_message(
    payload: dict | None,
    exc: BaseException | None = None,
    *,
    default: str = "workflow failed",
    restarted: bool | None = None,
) -> str:
    """The failure text a run row stores: the first reason the payload or exception carries."""
    message = None
    if isinstance(payload, dict):
        for key in _FAILURE_MESSAGE_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                message = value.strip()
                break
    if message is None and exc is not None:
        text = str(exc).strip()
        message = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    if message is None:
        message = default
    message = scrub_secrets(message)
    if restarted is None:
        restarted = restarted_recently()
    if restarted and not message.endswith(RESTART_SUFFIX):
        message = message[: FAILURE_MESSAGE_LIMIT - len(RESTART_SUFFIX)] + RESTART_SUFFIX
    return message[:FAILURE_MESSAGE_LIMIT]


def transient_failure(message: str | None, *, restarted: bool = False) -> bool:
    """Whether one more attempt is worth starting without anyone changing anything."""
    if restarted:
        return True
    if not message:
        return False
    return _TRANSIENT_FAILURE_PATTERN.search(message) is not None


class TinActivities:
    async def _run_accounted_procedure(self, *, conn, run, sandbox_id, run_input):
        if (
            getattr(run_input, "context", {}).get("output", {}).get("validator")
            in REVIEWED_DIAGRAM_VALIDATORS
        ):
            await self._sandboxes.prepare_diagram(
                sandbox_id=sandbox_id,
                branded=run_input.context["output"]["validator"] == TIN_DIAGRAM_BRANDED_VALIDATOR,
            )
        if run_input.api_url is not None:
            from tin_lite.codex_api import run_api_attempt

            return await run_api_attempt(
                db=self._db,
                conn=conn,
                run=run,
                sandbox_id=sandbox_id,
                run_input=run_input,
                call=lambda api_input: self._sandboxes.run_procedure_and_kill(
                    sandbox_id=sandbox_id, run_input=api_input
                ),
            )
        raise ValueError("Codex procedures require protected API execution")

    def __init__(
        self,
        *,
        database: Database,
        storage: CodeStorage,
        sandboxes: E2BRuntime,
        settings: Settings,
        memory_gardener: MemoryGardener | None = None,
        scan_reporter: ScanReporter | None = None,
        visibility_auditor: VisibilityAuditor | None = None,
        answer_page_drafter: AnswerPageDrafter | None = None,
        weekly_brief_reporter: WeeklyBriefReporter | None = None,
        site_health_improver: SiteHealthImprover | None = None,
        integrations: IntegrationService | None = None,
        temporal: Client | None = None,
    ) -> None:
        self._db = database
        self._storage = storage
        self._sandboxes = sandboxes
        self._settings = settings
        self._memory_gardener = memory_gardener
        self._scan_reporter = scan_reporter
        self._visibility_auditor = visibility_auditor
        self._answer_page_drafter = answer_page_drafter
        self._weekly_brief_reporter = weekly_brief_reporter
        self._site_health_improver = site_health_improver
        self._integrations = integrations
        self._temporal = temporal

    @activity.defn(name="dispatch_scheduled_workflow")
    async def dispatch_scheduled_workflow(self, payload: dict[str, str]) -> dict[str, str]:
        from tin_lite.workflow_definitions import (
            ensure_schedule_allowed,
            resolve_execution_contract,
        )

        project_workflow_id = UUID(payload["project_workflow_id"])
        configured = await self._db.get_project_workflow(project_workflow_id, include_archived=True)
        if configured is None:
            # An archived configuration can still have an already queued occurrence.
            return {}
        if configured is not None:
            selected = await self._db.get_workflow(configured.workflow_id)
            if selected is not None and selected.executor == "workflow.code":
                from tin_lite.code_schedules import dispatch_code_schedule

                return await dispatch_code_schedule(self, configured, selected, payload)
        if configured.status == "archived":
            return {}
        if configured.status != "active" or configured.schedule is None:
            if configured.status == "paused" and configured.last_error:
                from tin_lite.code_schedules import pause_for_issue

                await pause_for_issue(self, configured, configured.last_error)
                return {}
            raise RuntimeError("scheduled workflow is not active")
        workflow_definition = await self._db.get_workflow(configured.workflow_id)
        if workflow_definition is None:
            raise RuntimeError("scheduled workflow definition is unavailable")
        workflow_definition = await resolve_execution_contract(
            storage=self._storage,
            workflow=workflow_definition,
            project_id=configured.project_id,
            revision=configured.definition_commit_sha,
            input_schema=configured.input_schema,
        )
        scheduled_for = datetime.fromisoformat(payload["scheduled_for"])
        schedule = WorkflowSchedule.model_validate(configured.schedule)
        ensure_schedule_allowed(workflow_definition.definition, schedule)
        if (schedule.end_at and scheduled_for >= schedule.end_at) or (
            schedule.start_at and scheduled_for < schedule.start_at
        ):
            await self._db.advance_project_workflow_schedule(
                project_workflow_id=configured.id,
                next_run_at=next_run_after(schedule, scheduled_for),
            )
            return {}
        if await self._db.consume_project_workflow_skip(
            project_workflow_id=configured.id,
            scheduled_for=scheduled_for,
        ):
            return {}
        evaluation = await evaluate_prerequisites(
            database=self._db,
            storage=self._storage,
            project_id=configured.project_id,
            workflow=workflow_definition,
            normalized_inputs=configured.inputs,
        )
        try:
            run, created = await self._db.create_run(
                project_id=configured.project_id,
                workflow_id=configured.workflow_id,
                start_idempotency_key=f"schedule:{payload['occurrence_id']}",
                input_payload=configured.inputs,
                project_workflow_id=configured.id,
                definition_commit_sha=configured.definition_commit_sha,
                pinned_definition=workflow_definition.definition,
                trigger_source="schedule",
                scheduled_for=scheduled_for,
                prerequisite_evidence=(
                    evaluation.evidence(inputs=configured.inputs) if evaluation.results else None
                ),
            )
        except BillingError as exc:
            from tin_lite.code_schedules import pause_for_issue

            await pause_for_issue(self, configured, str(exc))
            return {}
        except ScheduledWorkflowSkip:
            await self._db.advance_project_workflow_schedule(
                project_workflow_id=configured.id,
                next_run_at=next_run_after(schedule, scheduled_for),
                expected_settings_revision=configured.settings_revision,
            )
            return {}
        schedule = WorkflowSchedule.model_validate(configured.schedule)
        await self._db.advance_project_workflow_schedule(
            project_workflow_id=configured.id,
            next_run_at=next_run_after(schedule, scheduled_for),
        )
        if not created and run.status == RunStatus.FAILED:
            return {}
        if created:
            if evaluation.blocking:
                # A schedule cannot ask the founder to run something first; the failed run
                # projection is how the missing prerequisite becomes visible.
                error = PrerequisiteError.from_evaluation(
                    evaluation, workflow=workflow_definition, inputs=configured.inputs
                )
                await self._db.project_failure(
                    run_id=run.id, error_message=f"Prerequisite missing: {error}"
                )
                return {}
            try:
                if run.definition_commit_sha is None:
                    raise RuntimeError("scheduled run did not pin a definition commit")
                requirements = await load_pinned_integration_requirements(
                    storage=self._storage,
                    workflow=workflow_definition,
                    commit_sha=run.definition_commit_sha,
                )
                if requirements:
                    if self._integrations is None:
                        raise RuntimeError("integration service is unavailable")
                    await self._integrations.ensure_requirements(
                        project_id=configured.project_id,
                        requirements=requirements,
                    )
            except IntegrationError as exc:
                await self._db.project_failure(
                    run_id=run.id,
                    error_message=f"Integration unavailable: {exc}",
                )
                return {}
            except Exception as exc:
                await self._db.project_failure(
                    run_id=run.id,
                    error_message=failure_message(
                        {
                            "reason": (
                                "IntegrationPreflightError: workflow requirements could not be "
                                f"resolved: {_safe_failure(exc)}"
                            )
                        },
                        restarted=False,
                    ),
                )
                return {}
        return {
            "run_id": str(run.id),
            "executor": run.executor,
            "temporal_workflow_id": run.temporal_workflow_id,
        }

    @activity.defn(name="create_design_sandbox")
    async def create_design_sandbox(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:sandbox_create"
        operation = "sandbox_create"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            if existing is not None and existing.status == "failed":
                # Only sandbox creation is retryable here. The separate paid
                # model-attempt receipt is never reset or repurchased.
                await conn.execute(
                    "UPDATE effect_receipts SET status='started', error_message=NULL "
                    "WHERE execution_key=$1 AND operation='sandbox_create' AND status='failed'",
                    execution_key,
                )
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                run = await self._require_run(run_id)
                from tin_lite import design_api
                from tin_lite.codex_api import execution_profile, is_api_contract, select_contract

                design = design_api.procedure(
                    getattr(self._settings, "sandbox_timeout_seconds", 900)
                )
                previous = (existing.result or {}) if existing else {}
                modern = existing is None or previous.get("runner") == "design-api-adapter-v1"
                if existing is None:
                    # A failed preflight is not an old OAuth receipt on retry.
                    await self._db.save_effect_progress(
                        conn,
                        execution_key=execution_key,
                        result={"runner": "design-api-adapter-v1"},
                    )
                codex_auth = (existing.result or {}).get("codex_auth") if existing else None
                if codex_auth is None:
                    codex_auth = (
                        {"mode": "chatgpt_oauth"}
                        if not modern
                        else await select_contract(
                            db=self._db,
                            conn=conn,
                            run=run,
                            procedure=design,
                            settings=self._settings,
                        )
                    )
                creation = {
                    "codex_auth": codex_auth,
                    **({"runner": "design-api-adapter-v1"} if modern else {}),
                }
                if is_api_contract(codex_auth):
                    creation["timeout_seconds"] = (
                        (existing.result or {}).get("timeout_seconds") if existing else None
                    ) or design.sandbox.timeout_seconds
                    design = design_api.procedure(creation["timeout_seconds"])
                    creation["context"] = (
                        (existing.result or {}).get("context") if existing else None
                    ) or design.sandbox_context(inputs=run.input or {})
                await self._db.save_effect_progress(
                    conn, execution_key=execution_key, result=creation
                )
                # Keep the auth pin intact, but never create an unsafe OAuth runner.
                execution_profile(design.sandbox, codex_auth)
                if is_api_contract(codex_auth) and run.status not in {
                    RunStatus.PENDING,
                    RunStatus.RUNNING,
                }:
                    raise StaleGenerationError("The design run is no longer active")
                project = await self._require_project(run.project_id)
                repo = await self._storage.get_repo(project.state_repo_id)
                expected_head_sha = await self._storage.head_sha(repo, project.canonical_branch)
                if expected_head_sha is None:
                    raise RuntimeError("project state repository has no canonical head")
                ephemeral_branch = f"generations/{run.id}/{run.generation}"
                sandbox_id = await self._sandboxes.create(
                    execution_key=execution_key,
                    run_id=str(run_id),
                    **(
                        {"profile": execution_profile(design.sandbox, codex_auth)}
                        if is_api_contract(codex_auth)
                        else {}
                    ),
                )
                leased_run = await self._db.attach_sandbox(
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    lease_owner=execution_key,
                    expected_head_sha=expected_head_sha,
                    ephemeral_branch=ephemeral_branch,
                    **({"require_active": True} if is_api_contract(codex_auth) else {}),
                )
                await self._db.mark_run_running(run_id)
                result = {
                    **creation,
                    "sandbox_id": sandbox_id,
                    "fencing_token": leased_run.fencing_token,
                    "ephemeral_branch": ephemeral_branch,
                    "expected_head_sha": expected_head_sha,
                }
                await self._db.complete_effect(conn, execution_key=execution_key, result=result)
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="sandbox_created",
                    details={"sandbox_id": sandbox_id},
                    dedupe_key=f"{execution_key}:sandbox_created",
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="persist_design_artifact")
    async def persist_design_artifact(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:artifact_persist"
        operation = "artifact_persist"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            run = await self._require_run(run_id)
            project = await self._require_project(run.project_id)
            self._require_active_lease(run)
            if run.ephemeral_branch is None or run.expected_head_sha is None:
                raise RuntimeError("run lease has no durable branch metadata")
            recovered_artifact = await self._storage.read_ephemeral_artifact_if_exists(
                repo_id=project.state_repo_id,
                branch=run.ephemeral_branch,
                path=ARTIFACT_PATH,
            )
            if recovered_artifact is not None:
                self._validate_artifact(recovered_artifact)
                if run.sandbox_id is not None:
                    await self._sandboxes.kill(run.sandbox_id)
                await self._complete_artifact_persist(
                    conn=conn,
                    run_id=run_id,
                    execution_key=execution_key,
                    ephemeral_branch=run.ephemeral_branch,
                    sandbox_id=run.sandbox_id,
                    result={"reconciled_from_ephemeral_branch": True},
                )
                return

            from tin_lite import design_api
            from tin_lite.codex_api import execution_profile

            creation = await self._db.get_effect(f"{run_id}:sandbox_create")
            auth = (creation.result or {}).get("codex_auth") if creation else None
            auth = auth or {"mode": "chatgpt_oauth"}
            design = design_api.procedure(
                (creation.result or {}).get("timeout_seconds", 900) if creation else 900
            )
            profile = execution_profile(design.sandbox, auth)
            sandbox_id = await self._sandboxes.create(
                execution_key=f"{run_id}:sandbox_create",
                run_id=str(run_id),
                profile=profile,
            )
            if sandbox_id != run.sandbox_id:
                previous_sandbox_id = run.sandbox_id
                run = await self._db.attach_sandbox(
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    lease_owner=f"{run_id}:sandbox_create",
                    expected_head_sha=run.expected_head_sha,
                    ephemeral_branch=run.ephemeral_branch,
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="sandbox_recovered",
                    details={
                        "previous_sandbox_id": previous_sandbox_id,
                        "sandbox_id": sandbox_id,
                    },
                    dedupe_key=f"{execution_key}:sandbox_recovered:{sandbox_id}",
                )
            remotes = self._storage.sandbox_remotes(
                repo_id=project.state_repo_id,
                branch=run.ephemeral_branch,
                subject=f"run:{run.id}",
            )
            public_host = urlsplit(self._settings.switchboard_public_url).hostname
            if not public_host:
                raise RuntimeError("TIN_LITE_PUBLIC_URL must include a hostname")
            proxy_url = (
                self._settings.forward_proxy_url.get_secret_value()
                if self._settings.forward_proxy_url
                else None
            )
            # API auth replaces the login, not Git's fenced-egress transport.
            # The run-bound API relay itself bypasses this proxy via NO_PROXY.
            if public_host not in {"127.0.0.1", "localhost"} and proxy_url is None:
                raise RuntimeError("TIN_LITE_PROXY_URL is required outside local development")
            try:
                run_values = dict(
                    execution_key=execution_key,
                    canonical_url=remotes.canonical_url,
                    canonical_auth_header=remotes.canonical_auth_header,
                    canonical_branch=project.canonical_branch,
                    ephemeral_url=remotes.ephemeral_url,
                    ephemeral_auth_header=remotes.ephemeral_auth_header,
                    ephemeral_branch=run.ephemeral_branch,
                    proxy_url=proxy_url,
                    no_proxy=public_host,
                    rollout_sink=self._rollout_sink(
                        run_id=run_id,
                        generation=run.generation,
                        execution_key=execution_key,
                        stage="codex",
                        sandbox_id=sandbox_id,
                    ),
                )
                context = (creation.result or {}).get("context")
                if not isinstance(context, dict):
                    raise ValueError("The API design run has no pinned controller context")
                result = await self._await_with_heartbeats(
                    self._run_accounted_procedure(
                        conn=conn,
                        run=run,
                        sandbox_id=sandbox_id,
                        run_input=SandboxProcedureInput(
                            **run_values,
                            context=context,
                            output_path=ARTIFACT_PATH,
                            output_max_bytes=design.output_max_bytes,
                            isolated=True,
                            timeout_seconds=profile.timeout_seconds,
                            project_revision=run.expected_head_sha,
                            api_url=(
                                f"{self._settings.switchboard_public_url.rstrip('/')}"
                                f"/internal/codex-api/{run_id}/v1"
                            ),
                        ),
                    ),
                    details={"sandbox_id": sandbox_id, "stage": "codex"},
                )
                ephemeral_commit_sha = result.ephemeral_commit_sha
                if await self._sandboxes.is_running(sandbox_id):
                    raise RuntimeError("E2B sandbox still exists after explicit kill")
                artifact = await self._storage.read_ephemeral_artifact(
                    repo_id=project.state_repo_id,
                    branch=run.ephemeral_branch,
                    path=ARTIFACT_PATH,
                )
                self._validate_artifact(artifact)
                await self._complete_artifact_persist(
                    conn=conn,
                    run_id=run_id,
                    execution_key=execution_key,
                    ephemeral_branch=run.ephemeral_branch,
                    sandbox_id=sandbox_id,
                    result={
                        "ephemeral_commit_sha": ephemeral_commit_sha,
                        "sandbox_killed": True,
                    },
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    def _progress_sink(self, run_id: UUID) -> Callable[[str], Awaitable[None]]:
        """Project redacted controller narration to Postgres, never to Temporal."""

        async def sink(text: str) -> None:
            await self._db.project_run_narration(run_id=run_id, summary=text)

        return sink

    def _rollout_sink(
        self,
        *,
        run_id: UUID,
        generation: int,
        execution_key: str,
        stage: str,
        sandbox_id: str,
    ) -> RolloutSink:
        """Persist redacted Codex rollouts for one sandbox and record counts only."""

        async def sink(capture: RolloutCapture) -> None:
            inserted = await self._db.insert_run_rollouts(
                run_id=run_id,
                generation=generation,
                execution_key=execution_key,
                activity_attempt=activity.info().attempt if activity.in_activity() else 1,
                stage=stage,
                sandbox_id=sandbox_id,
                files=capture.files,
            )
            await self._db.add_activity(
                run_id=run_id,
                event_type="rollouts_captured",
                details={
                    "stage": stage,
                    "sandbox_id": sandbox_id,
                    "files": len(capture.files),
                    "inserted": inserted,
                    "bytes": sum(file.size_bytes for file in capture.files),
                    "stored_bytes": sum(len(file.content) for file in capture.files),
                    "truncated": sum(1 for file in capture.files if file.truncated),
                    "skipped": capture.skipped,
                    "error": capture.error,
                },
                audience="internal",
                dedupe_key=f"{execution_key}:rollouts_captured:{sandbox_id}",
            )

        return sink

    async def _fail_run(
        self,
        run_id: UUID,
        payload: dict | None,
        *,
        default: str = "workflow failed",
        exc: BaseException | None = None,
        message: str | None = None,
    ) -> str:
        """Store the failure with its reason, then start one retry if the cause was transient."""
        restarted = restarted_recently()
        if message is None:
            message = failure_message(payload, exc, default=default, restarted=restarted)
        await self._db.project_failure(run_id=run_id, error_message=message)
        await self._auto_retry_after_transient_failure(run_id, message=message, restarted=restarted)
        return message

    async def _auto_retry_eligible(self, run: WorkflowRun) -> bool:
        """Only unattended starts get a silent second attempt: schedules and onboarding setup."""
        if run.retry_of_run_id is not None:
            return False
        if run.trigger_source == "schedule":
            return True
        read_start_key = getattr(self._db, "run_start_idempotency_key", None)
        if read_start_key is None:
            return False
        start_key = await read_start_key(run.id)
        return isinstance(start_key, str) and start_key.startswith(ONBOARDING_START_KEY_PREFIX)

    async def _auto_retry_after_transient_failure(
        self, run_id: UUID, *, message: str, restarted: bool
    ) -> UUID | None:
        if not transient_failure(message, restarted=restarted):
            return None
        run = await self._db.get_run(run_id)
        if run is None or run.status != RunStatus.FAILED:
            return None
        if not await self._auto_retry_eligible(run):
            return None
        start_key = f"{AUTO_RETRY_START_KEY_PREFIX}{run.id}"
        already = await self._db.get_run_by_start_key(
            project_id=run.project_id, start_idempotency_key=start_key
        )
        if already is not None:
            # An earlier attempt of this failure activity started the retry.
            return already.id
        if self._temporal is None:
            return None
        workflow_definition = await self._db.get_workflow(run.workflow_id)
        if workflow_definition is None:
            return None
        configured = None
        if run.project_workflow_id is not None:
            configured = await self._db.get_project_workflow(run.project_workflow_id)
            if configured is None or configured.project_id != run.project_id:
                return None
        started_by = run.started_by_clerk_user_id or (
            configured.created_by_clerk_user_id if configured is not None else None
        )
        if started_by is None:
            return None
        from tin_lite.run_service import start_workflow_run

        try:
            retry = await start_workflow_run(
                runtime=SimpleNamespace(
                    database=self._db,
                    storage=self._storage,
                    integrations=self._integrations,
                    temporal=self._temporal,
                ),
                settings=self._settings,
                workflow=workflow_definition,
                project_id=run.project_id,
                started_by_clerk_user_id=started_by,
                start_idempotency_key=start_key,
                input_payload=(
                    configured.inputs
                    if configured is not None
                    else {k: v for k, v in (run.input or {}).items() if k != "project_id"}
                ),
                project_workflow_id=run.project_workflow_id,
                definition_commit_sha=(
                    configured.definition_commit_sha
                    if configured is not None
                    else run.definition_commit_sha
                ),
                input_schema=configured.input_schema if configured is not None else None,
                trigger_source=run.trigger_source,
                trigger_client=run.trigger_client,
                started_by_oauth_client_id=run.started_by_oauth_client_id,
                retry_of_run_id=run.id,
            )
        except Exception as exc:
            activity.logger.warning(
                "automatic retry did not start",
                extra={"run_id": str(run.id), "error": _safe_failure(exc)},
            )
            return None
        await self._db.record_run_auto_retry(
            run_id=run.id,
            retry_run_id=retry.id,
            failure=message,
            restarted_recently=restarted,
        )
        return retry.id

    async def _await_with_heartbeats(
        self,
        operation: Awaitable[T],
        *,
        details: dict[str, str],
        procedure_run: WorkflowRun | None = None,
    ) -> T:
        task = asyncio.ensure_future(operation)
        try:
            while True:
                activity.heartbeat(details)
                if procedure_run is not None:
                    current = await self._require_run(procedure_run.id)
                    if (
                        current.status.value not in {"pending", "running"}
                        or not current.lease_active
                        or current.generation != procedure_run.generation
                        or current.fencing_token != procedure_run.fencing_token
                        or current.sandbox_id != procedure_run.sandbox_id
                    ):
                        raise StaleGenerationError("The procedure is no longer active")
                done, _ = await asyncio.wait({task}, timeout=SANDBOX_HEARTBEAT_SECONDS)
                if task in done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
                with suppress(BaseException):
                    await asyncio.shield(task)

    async def _github_procedure_bundle(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        sandbox_id: str,
        expected_binding=None,
    ):
        if self._integrations is None:
            raise RuntimeError("GitHub procedure workspace is unavailable")
        return await self._await_with_heartbeats(
            self._integrations.github_repository_bundle(
                project_id=project_id,
                execution_key=f"{run_id}:procedure_repository_workspace",
                run_id=run_id,
                **({"expected_binding": expected_binding} if expected_binding else {}),
            ),
            details={
                "sandbox_id": sandbox_id,
                "stage": "procedure_repository_workspace",
            },
        )

    async def _github_procedure_workspace(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        sandbox_id: str,
        expected_binding=None,
    ):
        if self._integrations is None:
            raise RuntimeError("GitHub procedure workspace is unavailable")
        bundle = await self._github_procedure_bundle(
            project_id=project_id,
            run_id=run_id,
            sandbox_id=sandbox_id,
            **({"expected_binding": expected_binding} if expected_binding else {}),
        )
        evidence = await self._await_with_heartbeats(
            self._integrations.github_open_pull_requests(
                project_id=project_id,
                execution_key=f"{run_id}:procedure_open_pull_requests",
                base_branch=bundle.default_branch,
                run_id=run_id,
                **({"expected_binding": expected_binding} if expected_binding else {}),
            ),
            details={
                "sandbox_id": sandbox_id,
                "stage": "procedure_open_pull_requests",
            },
        )
        return bundle, evidence

    async def _complete_artifact_persist(
        self,
        *,
        conn,
        run_id: UUID,
        execution_key: str,
        ephemeral_branch: str,
        sandbox_id: str | None,
        result: dict,
    ) -> None:
        await self._db.complete_effect(
            conn,
            execution_key=execution_key,
            result={"ephemeral_branch": ephemeral_branch, **result},
        )
        await self._db.add_activity(
            run_id=run_id,
            event_type="artifact_persisted",
            details={"ephemeral_branch": ephemeral_branch},
            dedupe_key=f"{execution_key}:artifact_persisted",
        )
        if sandbox_id is not None:
            await self._db.add_activity(
                run_id=run_id,
                event_type="sandbox_killed",
                details={"sandbox_id": sandbox_id},
                dedupe_key=f"{execution_key}:sandbox_killed",
            )

    @staticmethod
    def _validate_artifact(artifact: bytes) -> None:
        if not artifact or len(artifact) > 1_000_000:
            raise ValueError("DESIGN.md must contain between 1 byte and 1 MB")
        artifact.decode("utf-8")

    @activity.defn(name="commit_design_canonically")
    async def commit_design_canonically(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:canonical_commit"
        operation = "canonical_commit"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                await self._db.release_lease(run_id)
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                self._require_active_lease(run)
                if run.lease_owner is None or run.sandbox_id is None:
                    raise RuntimeError("run lease tuple is incomplete")
                if not await self._db.validate_lease(
                    project_id=run.project_id,
                    thread_id=run.thread_id,
                    generation=run.generation,
                    lease_owner=run.lease_owner,
                    fencing_token=run.fencing_token,
                    sandbox_id=run.sandbox_id,
                ):
                    raise StaleGenerationError("lease tuple no longer owns the session")
                if run.ephemeral_branch is None or run.expected_head_sha is None:
                    raise RuntimeError("run is missing its persisted branch metadata")
                artifact = await self._storage.read_ephemeral_artifact(
                    repo_id=project.state_repo_id,
                    branch=run.ephemeral_branch,
                    path=ARTIFACT_PATH,
                )
                if not artifact or len(artifact) > 1_000_000:
                    raise ValueError("DESIGN.md must contain between 1 byte and 1 MB")
                artifact.decode("utf-8")
                canonical_sha = await self._storage.create_canonical_commit(
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    expected_head_sha=run.expected_head_sha,
                    artifact_path=ARTIFACT_PATH,
                    artifact=artifact,
                    execution_key=execution_key,
                    run_id=str(run_id),
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={"canonical_commit_sha": canonical_sha},
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="canonical_commit_created",
                    details={"canonical_commit_sha": canonical_sha},
                    dedupe_key=f"{execution_key}:canonical_commit_created",
                )
                await self._db.release_lease(run_id)
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="project_design_result")
    async def project_design_result(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:projection_update"
        operation = "projection_update"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                canonical = await self._db.get_effect(f"{run_id}:canonical_commit")
                if canonical is None or canonical.status != "completed" or canonical.result is None:
                    raise RuntimeError("canonical commit step has not completed")
                sha = str(canonical.result["canonical_commit_sha"])
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                artifact_ref = f"code.storage://{project.state_repo_id}@{sha}/{ARTIFACT_PATH}"
                await self._db.project_success(
                    run_id=run_id,
                    canonical_commit_sha=sha,
                    artifact_ref=artifact_ref,
                    artifact_path=ARTIFACT_PATH,
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={"artifact_ref": artifact_ref},
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="projection_updated",
                    dedupe_key=f"{execution_key}:projection_updated",
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="project_design_failure")
    async def project_design_failure(self, payload: dict[str, str]) -> None:
        run_id = UUID(payload["run_id"])
        run = await self._db.get_run(run_id)
        cleanup_error: Exception | None = None
        if run is not None and run.sandbox_id is not None:
            try:
                await self._sandboxes.kill(run.sandbox_id)
            except Exception as exc:
                cleanup_error = exc
        await self._fail_run(run_id, payload)
        await self._db.release_lease(run_id)
        if cleanup_error is not None:
            raise cleanup_error

    @activity.defn(name="garden_project_memory")
    async def garden_project_memory(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:memory_commit"
        operation = "memory_commit"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                if self._memory_gardener is None:
                    raise RuntimeError("project.memory requires a configured memory gardener")
                run = await self._require_run(run_id)
                reporter = await self._pinned_native_reporter(
                    run, self._memory_gardener, "project.memory"
                )
                project = await self._require_project(run.project_id)
                await self._db.mark_run_running(run_id)
                async with self._db.project_state_lock(conn, project.id):
                    source_runs = await self._db.list_memory_source_runs(
                        project_id=project.id,
                        exclude_run_id=run_id,
                    )
                    sources: list[MemorySource] = []
                    for source_run in source_runs:
                        if (
                            source_run.canonical_commit_sha is None
                            or source_run.artifact_path is None
                            or source_run.artifact_ref is None
                        ):
                            raise RuntimeError("memory source run has no durable artifact")
                        source_content = await self._storage.read_canonical_artifact(
                            repo_id=project.state_repo_id,
                            commit_sha=source_run.canonical_commit_sha,
                            path=source_run.artifact_path,
                        )
                        sources.append(
                            MemorySource(
                                run_id=source_run.id,
                                workflow_key=source_run.executor,
                                artifact_ref=source_run.artifact_ref,
                                content=source_content.decode("utf-8"),
                            )
                        )
                    owned_section: str | None = None
                    if project.memory_commit_sha is not None:
                        current_index = await self._storage.read_canonical_artifact_if_exists(
                            repo_id=project.state_repo_id,
                            commit_sha=project.memory_commit_sha,
                            path=MEMORY_INDEX_PATH,
                        )
                        if current_index is not None:
                            owned_section = extract_owned_section(current_index.decode("utf-8"))
                    with external_usage_scope(self._db, conn, run_id, "memory"):
                        memory_index = await self._await_with_heartbeats(
                            reporter.garden(
                                project_name=project.name,
                                sources=sources,
                                owned_section=owned_section,
                            ),
                            details={"stage": "memory_gardener"},
                        )
                    canonical_sha, changed = await self._storage.publish_state_document(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        path=MEMORY_INDEX_PATH,
                        content=memory_index,
                        workflow_key=PROJECT_MEMORY_WORKFLOW_NAME,
                        execution_key=execution_key,
                        run_id=str(run_id),
                    )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="memory_gardened",
                    details={"source_count": len(sources), "changed": changed},
                    dedupe_key=f"{execution_key}:memory_gardened",
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={
                        "canonical_commit_sha": canonical_sha,
                        "artifact_path": MEMORY_INDEX_PATH,
                        "changed": changed,
                        "source_run_ids": [str(source.run_id) for source in sources],
                    },
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="project_memory_result")
    async def project_memory_result(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:memory_projection"
        operation = "memory_projection"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                committed = await self._db.get_effect(f"{run_id}:memory_commit")
                if committed is None or committed.status != "completed" or committed.result is None:
                    raise RuntimeError("memory commit step has not completed")
                sha = str(committed.result["canonical_commit_sha"])
                path = str(committed.result["artifact_path"])
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                content = await self._storage.read_canonical_artifact(
                    repo_id=project.state_repo_id,
                    commit_sha=sha,
                    path=path,
                )
                validate_memory_index(content, sources=[])
                artifact_ref = f"code.storage://{project.state_repo_id}@{sha}/{path}"
                await self._db.project_memory_success(
                    run_id=run_id,
                    canonical_commit_sha=sha,
                    artifact_ref=artifact_ref,
                    artifact_path=path,
                    memory_index=content.decode("utf-8"),
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="project_memory_updated",
                    details={"artifact_ref": artifact_ref},
                    summary="Project memory was updated.",
                    audience="product",
                    dedupe_key=f"{execution_key}:project_memory_updated",
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={"artifact_ref": artifact_ref},
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="project_memory_failure")
    async def project_memory_failure(self, payload: dict[str, str]) -> None:
        await self._fail_run(UUID(payload["run_id"]), payload)

    @activity.defn(name="generate_scan_report")
    async def generate_scan_report(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:scan_commit"
        operation = "scan_commit"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                if self._scan_reporter is None:
                    raise RuntimeError("scan.report requires a configured scan reporter")
                run = await self._require_run(run_id)
                reporter = await self._pinned_native_reporter(
                    run, self._scan_reporter, "scan.report"
                )
                project = await self._require_project(run.project_id)
                if run.system_wiki_commit_sha is None:
                    raise RuntimeError("scan.report run has no pinned system wiki version")
                await self._db.mark_run_running(run_id)
                async with self._db.project_state_lock(conn, project.id):
                    system_ref, system_content = await read_system_wiki_document(
                        storage=self._storage,
                        commit_sha=run.system_wiki_commit_sha,
                    )
                    sources = [
                        ScanSource(
                            label="system wiki · project scanning principles",
                            artifact_ref=system_ref.artifact_ref,
                            content=system_content,
                        )
                    ]
                    if (
                        project.memory_commit_sha is not None
                        and project.memory_index_path is not None
                    ):
                        memory_content = await self._storage.read_canonical_artifact(
                            repo_id=project.state_repo_id,
                            commit_sha=project.memory_commit_sha,
                            path=project.memory_index_path,
                        )
                        sources.append(
                            ScanSource(
                                label="project memory",
                                artifact_ref=(
                                    f"code.storage://{project.state_repo_id}"
                                    f"@{project.memory_commit_sha}/{project.memory_index_path}"
                                ),
                                content=memory_content.decode("utf-8"),
                            )
                        )
                    else:
                        source_runs = await self._db.list_memory_source_runs(
                            project_id=project.id,
                            exclude_run_id=run_id,
                        )
                        for source_run in source_runs:
                            if source_run.executor == SCAN_REPORT_WORKFLOW_NAME:
                                continue
                            if (
                                source_run.canonical_commit_sha is None
                                or source_run.artifact_path is None
                                or source_run.artifact_ref is None
                            ):
                                raise RuntimeError("scan source run has no durable artifact")
                            source_content = await self._storage.read_canonical_artifact(
                                repo_id=project.state_repo_id,
                                commit_sha=source_run.canonical_commit_sha,
                                path=source_run.artifact_path,
                            )
                            sources.append(
                                ScanSource(
                                    label=source_run.executor,
                                    artifact_ref=source_run.artifact_ref,
                                    content=source_content.decode("utf-8"),
                                )
                            )
                    sources.append(
                        ScanSource(
                            label="current integration availability",
                            artifact_ref=f"tin.project://{project.id}/integrations",
                            content=await self._integration_evidence(project),
                        )
                    )
                    with external_usage_scope(self._db, conn, run_id, "scan"):
                        report = await self._await_with_heartbeats(
                            reporter.report(
                                project_name=project.name,
                                sources=sources,
                            ),
                            details={"stage": "scan_reporter"},
                        )
                    canonical_sha, changed = await self._storage.publish_state_document(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        path=SCAN_REPORT_PATH,
                        content=report,
                        workflow_key=SCAN_REPORT_WORKFLOW_NAME,
                        execution_key=execution_key,
                        run_id=str(run_id),
                    )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="scan_report_created",
                    details={"source_count": len(sources), "changed": changed},
                    dedupe_key=f"{execution_key}:scan_report_created",
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={
                        "canonical_commit_sha": canonical_sha,
                        "artifact_path": SCAN_REPORT_PATH,
                        "changed": changed,
                        "source_refs": [source.artifact_ref for source in sources],
                    },
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="project_scan_result")
    async def project_scan_result(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:scan_projection"
        operation = "scan_projection"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                committed = await self._db.get_effect(f"{run_id}:scan_commit")
                if committed is None or committed.status != "completed" or committed.result is None:
                    raise RuntimeError("scan commit step has not completed")
                sha = str(committed.result["canonical_commit_sha"])
                path = str(committed.result["artifact_path"])
                source_refs = committed.result.get("source_refs")
                if not isinstance(source_refs, list) or not all(
                    isinstance(item, str) for item in source_refs
                ):
                    raise RuntimeError("scan commit has invalid source references")
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                content = await self._storage.read_canonical_artifact(
                    repo_id=project.state_repo_id,
                    commit_sha=sha,
                    path=path,
                )
                validate_scan_report(
                    content,
                    sources=[
                        ScanSource(label="durable source", artifact_ref=ref, content="")
                        for ref in source_refs
                    ],
                )
                artifact_ref = f"code.storage://{project.state_repo_id}@{sha}/{path}"
                await self._db.project_success(
                    run_id=run_id,
                    canonical_commit_sha=sha,
                    artifact_ref=artifact_ref,
                    artifact_path=path,
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="scan_report_ready",
                    details={"artifact_ref": artifact_ref},
                    summary="Project scan report is ready.",
                    audience="product",
                    dedupe_key=f"{execution_key}:scan_report_ready",
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={"artifact_ref": artifact_ref},
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="project_scan_failure")
    async def project_scan_failure(self, payload: dict[str, str]) -> None:
        await self._fail_run(UUID(payload["run_id"]), payload)

    @activity.defn(name="draft_site_health_improvement")
    async def draft_site_health_improvement(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:site_health_draft"
        operation = "site_health_draft"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                if self._site_health_improver is None or self._integrations is None:
                    raise RuntimeError("site.health_improve requires its trusted services")
                run = await self._require_run(run_id)
                if run.executor != SITE_HEALTH_WORKFLOW_NAME:
                    raise RuntimeError("run is not a site-health workflow")
                route_key = await self._pinned_site_health_model_route(run_id)
                inputs = run.input or {}
                site_url = inputs.get("site_url")
                focus = inputs.get("focus", "automatic")
                context = inputs.get("context", "")
                change_budget = inputs.get("change_budget", 1)
                if (
                    not isinstance(site_url, str)
                    or focus not in {"automatic", "accessibility", "technical_seo", "reliability"}
                    or not isinstance(context, str)
                    or len(context) > 2000
                    or not isinstance(change_budget, int)
                    or isinstance(change_budget, bool)
                    or not 1 <= change_budget <= 3
                ):
                    raise RuntimeError("site-health workflow inputs are invalid")
                await self._db.mark_run_running(run_id)
                snapshot = await self._await_with_heartbeats(
                    self._integrations.github_repository_snapshot(
                        project_id=run.project_id,
                        execution_key=f"{run_id}:github_snapshot",
                        run_id=run_id,
                    ),
                    details={"stage": "github_snapshot"},
                )
                live_page = await self._await_with_heartbeats(
                    fetch_live_page_evidence(site_url),
                    details={"stage": "site_health_probe"},
                )
                with model_usage_scope(run_id=run_id, step="site-health:draft", conn=conn):
                    proposal = await self._await_with_heartbeats(
                        self._site_health_improver.draft(
                            route_key=route_key,
                            site_url=site_url,
                            focus=focus,
                            context=context,
                            change_budget=change_budget,
                            live_page=live_page,
                            snapshot=snapshot,
                        ),
                        details={"stage": "site_health_model"},
                    )
                result = {
                    "proposal": proposal.receipt(),
                    "snapshot": {
                        "repository": snapshot.repository,
                        "default_branch": snapshot.default_branch,
                        "head_sha": snapshot.head_sha,
                        "paths": [item.path for item in snapshot.files],
                    },
                    "live_page": live_page.definition(),
                    "site_url": site_url,
                    "model_route": route_key,
                }
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result=result,
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="site_health_improvement_drafted",
                    details={
                        "repository": snapshot.repository,
                        "head_sha": snapshot.head_sha,
                        "file_count": len(proposal.files),
                    },
                    dedupe_key=f"{execution_key}:drafted",
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="open_site_health_pull_request")
    async def open_site_health_pull_request(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:site_health_delivery"
        operation = "site_health_delivery"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                if self._integrations is None:
                    raise RuntimeError("site.health_improve requires GitHub")
                drafted = await self._db.get_effect(f"{run_id}:site_health_draft")
                if drafted is None or drafted.status != "completed" or drafted.result is None:
                    raise RuntimeError("site-health draft step has not completed")
                proposal_value = drafted.result.get("proposal")
                snapshot_value = drafted.result.get("snapshot")
                site_url = drafted.result.get("site_url")
                if (
                    not isinstance(proposal_value, dict)
                    or not isinstance(snapshot_value, dict)
                    or not isinstance(site_url, str)
                ):
                    raise RuntimeError("site-health draft receipt is invalid")
                proposal = SiteHealthProposal.from_receipt(proposal_value)
                repository = snapshot_value.get("repository")
                default_branch = snapshot_value.get("default_branch")
                head_sha = snapshot_value.get("head_sha")
                if not all(
                    isinstance(item, str) and item
                    for item in (repository, default_branch, head_sha)
                ):
                    raise RuntimeError("site-health repository snapshot is invalid")
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                pull_request = await self._integrations.github_create_pull_request(
                    project_id=run.project_id,
                    execution_key=f"{run_id}:github_pull_request",
                    title=proposal.pull_request_title,
                    body=proposal.pull_request_body,
                    files=proposal.files,
                    base_branch=default_branch,
                    expected_base_sha=head_sha,
                    run_id=run_id,
                )
                snapshot = GitHubRepositorySnapshot(
                    repository=repository,
                    default_branch=default_branch,
                    head_sha=head_sha,
                    files=(),
                )
                report = build_site_health_report(
                    site_url=site_url,
                    snapshot=snapshot,
                    proposal=proposal,
                    pull_request_url=pull_request.url,
                    pull_request_number=pull_request.number,
                    pull_request_branch=pull_request.branch,
                )
                path = site_health_report_path(run_id)
                async with self._db.project_state_lock(conn, project.id):
                    canonical_sha, changed = await self._storage.publish_state_document(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        path=path,
                        content=report,
                        workflow_key=SITE_HEALTH_WORKFLOW_NAME,
                        execution_key=execution_key,
                        run_id=str(run_id),
                    )
                result = {
                    "canonical_commit_sha": canonical_sha,
                    "artifact_path": path,
                    "artifact_changed": changed,
                    "pull_request_url": pull_request.url,
                    "pull_request_number": pull_request.number,
                    "pull_request_branch": pull_request.branch,
                    "repository": pull_request.repository,
                }
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result=result,
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="site_health_pull_request_opened",
                    details={
                        "repository": pull_request.repository,
                        "pull_request_url": pull_request.url,
                        "pull_request_number": pull_request.number,
                    },
                    dedupe_key=f"{execution_key}:pull_request_opened",
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="project_site_health_result")
    async def project_site_health_result(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:site_health_projection"
        operation = "site_health_projection"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                delivery = await self._db.get_effect(f"{run_id}:site_health_delivery")
                if delivery is None or delivery.status != "completed" or delivery.result is None:
                    raise RuntimeError("site-health delivery step has not completed")
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                sha = str(delivery.result["canonical_commit_sha"])
                path = str(delivery.result["artifact_path"])
                pull_request_url = str(delivery.result["pull_request_url"])
                pull_request_number = int(delivery.result["pull_request_number"])
                artifact_ref = f"code.storage://{project.state_repo_id}@{sha}/{path}"
                await self._db.project_success(
                    run_id=run_id,
                    canonical_commit_sha=sha,
                    artifact_ref=artifact_ref,
                    artifact_path=path,
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="site_health_pull_request_ready",
                    details={
                        "kind": "runs",
                        "status": "succeeded",
                        "external_url": pull_request_url,
                        "external_label": f"Review PR #{pull_request_number}",
                        "artifact_ref": artifact_ref,
                    },
                    summary=f"Site-health improvement is ready as PR #{pull_request_number}.",
                    audience="product",
                    dedupe_key=f"{execution_key}:pull_request_ready",
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={
                        "artifact_ref": artifact_ref,
                        "pull_request_url": pull_request_url,
                    },
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="project_site_health_failure")
    async def project_site_health_failure(self, payload: dict[str, str]) -> None:
        await self._fail_run(UUID(payload["run_id"]), payload)

    @activity.defn(name="generate_visibility_audit")
    async def generate_visibility_audit(self, run_id_text: str) -> None:
        if self._visibility_auditor is None:
            raise RuntimeError("visibility.audit requires a configured visibility auditor")
        run_id = UUID(run_id_text)
        committed = await self._db.get_effect(f"{run_id}:visibility_commit")
        if committed is not None and committed.status == "completed":
            return
        run = await self._require_run(run_id)
        reporter = await self._pinned_native_reporter(
            run, self._visibility_auditor, "visibility.audit"
        )
        project = await self._require_project(run.project_id)
        raw_target_request = (run.input or {}).get("target", "this project")
        if not isinstance(raw_target_request, str):
            raise RuntimeError("visibility target input is invalid")
        target_request = raw_target_request.strip()
        if not 1 <= len(target_request) <= 200:
            raise RuntimeError("visibility target input is invalid")
        await self._db.mark_run_running(run_id)
        sources = await self._visibility_sources(run_id=run_id, project=project)

        panel = await self._await_with_heartbeats(
            self._visibility_effect(
                run_id=run_id,
                step_id="panel",
                operation="visibility_panel",
                execute=lambda checkpoint: reporter.prepare_panel(
                    project_name=project.name,
                    target_request=target_request,
                    sources=sources,
                    checkpoint=checkpoint,
                ),
            ),
            details={"stage": "visibility_panel"},
        )
        answer_tasks: list[Awaitable[dict]] = []
        answer_slots: list[tuple[str, str]] = []
        answer_limit = asyncio.Semaphore(3)

        async def answer_once(*, question_id: str, question_text: str, searched: bool) -> dict:
            mode = "searched" if searched else "probe"
            async with answer_limit:
                return await self._visibility_effect(
                    run_id=run_id,
                    step_id=f"answer:{question_id}:{mode}",
                    operation="visibility_answer",
                    execute=lambda checkpoint: reporter.answer(
                        question=question_text,
                        searched=searched,
                        checkpoint=checkpoint,
                    ),
                )

        for question in panel["questions"]:
            for mode, searched in (("searched", True), ("probe", False)):
                question_id = str(question["id"])
                question_text = str(question["text"])
                answer_slots.append((question_id, mode))
                answer_tasks.append(
                    answer_once(
                        question_id=question_id,
                        question_text=question_text,
                        searched=searched,
                    )
                )
        answers = await self._await_with_heartbeats(
            asyncio.gather(*answer_tasks, return_exceptions=True),
            details={"stage": "visibility_answers"},
        )
        # Let all dispatched calls save their receipts before projecting a failure.
        for answer in answers:
            if isinstance(answer, BaseException):
                raise answer
        grouped: dict[str, dict[str, dict]] = {}
        for (question_id, mode), answer in zip(answer_slots, answers, strict=True):
            grouped.setdefault(question_id, {})[mode] = answer
        measurements = [
            {
                "question_id": str(question["id"]),
                "searched": grouped[str(question["id"])]["searched"],
                "probe": grouped[str(question["id"])]["probe"],
            }
            for question in panel["questions"]
        ]
        adjudication = await self._await_with_heartbeats(
            self._visibility_effect(
                run_id=run_id,
                step_id="adjudication",
                operation="visibility_adjudication",
                execute=lambda checkpoint: reporter.adjudicate(
                    panel=panel,
                    measurements=measurements,
                    checkpoint=checkpoint,
                ),
            ),
            details={"stage": "visibility_adjudication"},
        )

        evidence_path = visibility_evidence_path(run_id)
        report, evidence = reporter.build_artifacts(
            run_id=str(run_id),
            project_name=project.name,
            target_request=target_request,
            source_refs=[source.artifact_ref for source in sources],
            panel=panel,
            measurements=measurements,
            adjudication=adjudication,
            evidence_path=evidence_path,
        )
        validate_visibility_artifacts(
            report, evidence, run_id=str(run_id), evidence_path=evidence_path
        )
        execution_key = f"{run_id}:visibility_commit"
        operation = "visibility_commit"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                async with self._db.project_state_lock(conn, project.id):
                    canonical_sha, changed = await self._storage.publish_state_documents(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        documents={
                            VISIBILITY_AUDIT_PATH: report,
                            evidence_path: evidence,
                        },
                        workflow_key=VISIBILITY_AUDIT_WORKFLOW_NAME,
                        execution_key=execution_key,
                        run_id=str(run_id),
                    )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="visibility_audit_created",
                    details={
                        "panel_hash": panel["panel_hash"],
                        "question_count": len(panel["questions"]),
                        "changed": changed,
                    },
                    dedupe_key=f"{execution_key}:visibility_audit_created",
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={
                        "canonical_commit_sha": canonical_sha,
                        "artifact_path": VISIBILITY_AUDIT_PATH,
                        "evidence_path": evidence_path,
                        "source_refs": [source.artifact_ref for source in sources],
                        "changed": changed,
                        "publication": visibility_publication_facts(
                            run=run, canonical_sha=canonical_sha, report=report, evidence=evidence
                        ),
                    },
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="project_visibility_result")
    async def project_visibility_result(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:visibility_projection"
        operation = "visibility_projection"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                committed = await self._db.get_effect(f"{run_id}:visibility_commit")
                if committed is None or committed.status != "completed" or committed.result is None:
                    raise RuntimeError("visibility commit step has not completed")
                sha = str(committed.result["canonical_commit_sha"])
                path = str(committed.result["artifact_path"])
                evidence_path = str(committed.result["evidence_path"])
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                if path != VISIBILITY_AUDIT_PATH or evidence_path != visibility_evidence_path(
                    run_id
                ):
                    raise ValueError("visibility receipt has unexpected artifact paths")
                publication = committed.result.get("publication")
                if publication is None:
                    # Older completed commit receipts are immutable. Validate once
                    # under a separate receipt, then reuse it across projection retries.
                    publication = await self._legacy_visibility_publication(run, project, sha)
                validate_visibility_publication(publication, run=run, canonical_sha=sha)
                artifact_ref = f"code.storage://{project.state_repo_id}@{sha}/{path}"
                await self._db.complete_visibility_projection(
                    conn,
                    execution_key=execution_key,
                    run_id=run_id,
                    canonical_commit_sha=sha,
                    artifact_ref=artifact_ref,
                    artifact_path=path,
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    async def _legacy_visibility_publication(self, run, project, sha: str) -> dict:
        execution_key = f"{run.id}:visibility_publication_validation"
        operation = "visibility_publication_validation"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                validate_visibility_publication(existing.result, run=run, canonical_sha=sha)
                return existing.result
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                async with asyncio.timeout(60):
                    report = await self._storage.read_canonical_artifact(
                        repo_id=project.state_repo_id, commit_sha=sha, path=VISIBILITY_AUDIT_PATH
                    )
                    evidence = await self._storage.read_canonical_artifact(
                        repo_id=project.state_repo_id,
                        commit_sha=sha,
                        path=visibility_evidence_path(run.id),
                    )
                result = visibility_publication_facts(
                    run=run, canonical_sha=sha, report=report, evidence=evidence
                )
                await self._db.complete_effect(conn, execution_key=execution_key, result=result)
                return result
            except Exception as exc:
                await self._db.fail_effect(
                    conn, execution_key=execution_key, error_message=_safe_failure(exc)
                )
                raise

    @activity.defn(name="project_visibility_failure")
    async def project_visibility_failure(self, payload: dict[str, str]) -> None:
        await self._fail_run(UUID(payload["run_id"]), payload)

    @activity.defn(name="draft_answer_page")
    async def draft_answer_page(self, run_id_text: str) -> None:
        if self._answer_page_drafter is None:
            raise RuntimeError("content.answer_page requires a configured answer-page drafter")
        run_id = UUID(run_id_text)
        run = await self._require_run(run_id)
        reporter = await self._pinned_native_reporter(
            run, self._answer_page_drafter, "content.answer_page"
        )
        project = await self._require_project(run.project_id)
        await self._db.mark_run_running(run_id)
        sources = await self._answer_page_sources(run_id=run_id, project=project)
        draft = await self._await_with_heartbeats(
            self._answer_page_effect(
                run_id=run_id,
                execute=lambda: reporter.draft(
                    project_name=project.name,
                    sources=sources,
                ),
            ),
            details={"stage": "answer_page_draft"},
        )
        evidence_path = answer_page_evidence_path(run_id)
        page, evidence = reporter.build_artifacts(
            run_id=str(run_id),
            source_refs=[source.artifact_ref for source in sources],
            draft=draft,
            artifact_path=ANSWER_PAGE_PATH,
            evidence_path=evidence_path,
        )
        execution_key = f"{run_id}:answer_page_commit"
        operation = "answer_page_commit"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                async with self._db.project_state_lock(conn, project.id):
                    canonical_sha, changed = await self._storage.publish_state_documents(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        documents={ANSWER_PAGE_PATH: page, evidence_path: evidence},
                        workflow_key=ANSWER_PAGE_WORKFLOW_NAME,
                        execution_key=execution_key,
                        run_id=str(run_id),
                    )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="answer_page_drafted",
                    details={"source_count": len(sources), "changed": changed},
                    dedupe_key=f"{execution_key}:answer_page_drafted",
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={
                        "canonical_commit_sha": canonical_sha,
                        "artifact_path": ANSWER_PAGE_PATH,
                        "evidence_path": evidence_path,
                        "source_refs": [source.artifact_ref for source in sources],
                        "changed": changed,
                    },
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="project_answer_page_result")
    async def project_answer_page_result(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:answer_page_projection"
        operation = "answer_page_projection"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                committed = await self._db.get_effect(f"{run_id}:answer_page_commit")
                if committed is None or committed.status != "completed" or committed.result is None:
                    raise RuntimeError("answer-page commit step has not completed")
                sha = str(committed.result["canonical_commit_sha"])
                path = str(committed.result["artifact_path"])
                evidence_path = str(committed.result["evidence_path"])
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                content = await self._storage.read_canonical_artifact(
                    repo_id=project.state_repo_id,
                    commit_sha=sha,
                    path=path,
                )
                evidence = await self._storage.read_canonical_artifact(
                    repo_id=project.state_repo_id,
                    commit_sha=sha,
                    path=evidence_path,
                )
                validate_answer_page_artifacts(
                    content,
                    evidence,
                    artifact_path=path,
                    evidence_path=evidence_path,
                )
                artifact_ref = f"code.storage://{project.state_repo_id}@{sha}/{path}"
                await self._db.project_success(
                    run_id=run_id,
                    canonical_commit_sha=sha,
                    artifact_ref=artifact_ref,
                    artifact_path=path,
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="answer_page_ready",
                    details={"artifact_ref": artifact_ref},
                    summary="Answer page draft is ready.",
                    audience="product",
                    dedupe_key=f"{execution_key}:answer_page_ready",
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={"artifact_ref": artifact_ref},
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="request_answer_page_review")
    async def request_answer_page_review(self, run_id_text: str) -> bool:
        run_id = UUID(run_id_text)
        committed = await self._db.get_effect(f"{run_id}:answer_page_commit")
        if committed is None or committed.status != "completed" or committed.result is None:
            raise RuntimeError("answer-page commit step has not completed")
        sha = str(committed.result["canonical_commit_sha"])
        path = str(committed.result["artifact_path"])
        evidence_path = str(committed.result["evidence_path"])
        run = await self._require_run(run_id)
        project = await self._require_project(run.project_id)
        content = await self._storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=sha,
            path=path,
        )
        evidence = await self._storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=sha,
            path=evidence_path,
        )
        validate_answer_page_artifacts(
            content,
            evidence,
            artifact_path=path,
            evidence_path=evidence_path,
        )
        artifact_ref = f"code.storage://{project.state_repo_id}@{sha}/{path}"
        return await self._db.request_human_review(
            run_id=run_id,
            canonical_commit_sha=sha,
            artifact_ref=artifact_ref,
            artifact_path=path,
        )

    @activity.defn(name="record_answer_page_approval")
    async def record_answer_page_approval(self, run_id_text: str) -> None:
        await self._db.record_human_review(
            run_id=UUID(run_id_text),
            decision="approved",
        )

    @activity.defn(name="project_answer_page_failure")
    async def project_answer_page_failure(self, payload: dict[str, str]) -> None:
        await self._fail_run(UUID(payload["run_id"]), payload)

    @activity.defn(name="generate_weekly_brief")
    async def generate_weekly_brief(self, run_id_text: str) -> None:
        if self._weekly_brief_reporter is None:
            raise RuntimeError("project.weekly_brief requires a configured weekly brief reporter")
        run_id = UUID(run_id_text)
        run = await self._require_run(run_id)
        reporter = await self._pinned_native_reporter(
            run, self._weekly_brief_reporter, "project.weekly_brief"
        )
        project = await self._require_project(run.project_id)
        await self._db.mark_run_running(run_id)
        period_end = run.scheduled_for or run.created_at or datetime.now(UTC)
        period_start = period_end - timedelta(days=7)
        sources = await self._weekly_brief_sources(
            run_id=run_id,
            project=project,
            period_start=period_start,
            period_end=period_end,
        )
        result = await self._await_with_heartbeats(
            self._weekly_brief_effect(
                run_id=run_id,
                execute=lambda: reporter.report(
                    project_name=project.name,
                    period_start=period_start,
                    period_end=period_end,
                    inputs=run.input or {},
                    sources=sources,
                ),
            ),
            details={"stage": "weekly_brief"},
        )
        artifact_path = weekly_brief_path(period_end)
        evidence_path = weekly_brief_evidence_path(run_id)
        report, evidence = reporter.build_artifacts(
            run_id=str(run_id),
            artifact_path=artifact_path,
            evidence_path=evidence_path,
            result=result,
        )
        execution_key = f"{run_id}:weekly_brief_commit"
        operation = "weekly_brief_commit"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                async with self._db.project_state_lock(conn, project.id):
                    canonical_sha, changed = await self._storage.publish_state_documents(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        documents={artifact_path: report, evidence_path: evidence},
                        workflow_key=WEEKLY_BRIEF_WORKFLOW_NAME,
                        execution_key=execution_key,
                        run_id=str(run_id),
                    )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="weekly_brief_created",
                    details={"source_count": len(sources), "changed": changed},
                    dedupe_key=f"{execution_key}:weekly_brief_created",
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={
                        "canonical_commit_sha": canonical_sha,
                        "artifact_path": artifact_path,
                        "evidence_path": evidence_path,
                        "changed": changed,
                    },
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn, execution_key=execution_key, error_message=_safe_failure(exc)
                )
                raise

    @activity.defn(name="project_weekly_brief_result")
    async def project_weekly_brief_result(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:weekly_brief_projection"
        operation = "weekly_brief_projection"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                committed = await self._db.get_effect(f"{run_id}:weekly_brief_commit")
                if committed is None or committed.status != "completed" or committed.result is None:
                    raise RuntimeError("weekly brief commit step has not completed")
                sha = str(committed.result["canonical_commit_sha"])
                path = str(committed.result["artifact_path"])
                evidence_path = str(committed.result["evidence_path"])
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                content = await self._storage.read_canonical_artifact(
                    repo_id=project.state_repo_id, commit_sha=sha, path=path
                )
                evidence = await self._storage.read_canonical_artifact(
                    repo_id=project.state_repo_id, commit_sha=sha, path=evidence_path
                )
                validate_weekly_brief_artifacts(
                    content,
                    evidence,
                    artifact_path=path,
                    evidence_path=evidence_path,
                )
                artifact_ref = f"code.storage://{project.state_repo_id}@{sha}/{path}"
                await self._db.project_success(
                    run_id=run_id,
                    canonical_commit_sha=sha,
                    artifact_ref=artifact_ref,
                    artifact_path=path,
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="weekly_brief_ready",
                    details={"artifact_ref": artifact_ref},
                    summary="Your weekly project brief is ready.",
                    audience="product",
                    dedupe_key=f"{execution_key}:weekly_brief_ready",
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={"artifact_ref": artifact_ref},
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn, execution_key=execution_key, error_message=_safe_failure(exc)
                )
                raise

    @activity.defn(name="project_weekly_brief_failure")
    async def project_weekly_brief_failure(self, payload: dict[str, str]) -> None:
        await self._fail_run(UUID(payload["run_id"]), payload)

    @activity.defn(name="prepare_email_campaign")
    async def prepare_email_campaign(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:email_campaign_prepare"
        operation = "email_campaign_prepare"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                run = await self._require_run(run_id)
                if run.executor != EMAIL_CAMPAIGN_WORKFLOW_NAME:
                    raise RuntimeError("run is not an email campaign")
                if run.status is RunStatus.STOPPED:
                    raise RuntimeError("email campaign was stopped")
                project = await self._require_project(run.project_id)
                await self._db.mark_run_running(run_id)
                inputs = run.input or {}
                subject = str(inputs.get("subject", "")).strip()
                body = str(inputs.get("body", "")).strip()
                raw_follow_up = str(inputs.get("follow_up_body", "")).strip()
                follow_up_body = raw_follow_up or None
                follow_up_delay_days = (
                    int(inputs.get("follow_up_delay_days", 4)) if follow_up_body else None
                )
                send_interval_seconds = int(inputs.get("send_interval_seconds", 60))
                send_policy = parse_email_send_policy(
                    daily_send_cap=int(inputs.get("daily_send_cap", 25)),
                    send_interval_seconds=send_interval_seconds,
                    send_window_start=str(inputs.get("send_window_start", "09:00")),
                    send_window_end=str(inputs.get("send_window_end", "17:00")),
                    send_timezone=str(inputs.get("send_timezone", "UTC")),
                )
                workspace_connection = await self._db.get_integration_connection(
                    project_id=project.id,
                    provider_key=GOOGLE_WORKSPACE_PROVIDER,
                )
                required_capabilities = {
                    "gmail.messages.send",
                    "gmail.messages.read",
                    "gmail.history.read",
                }
                granted_capabilities = (
                    workspace_connection.configuration.get("granted_capabilities", [])
                    if workspace_connection is not None
                    else []
                )
                if (
                    workspace_connection is None
                    or workspace_connection.status != "connected"
                    or not workspace_connection.external_account_id
                    or not isinstance(granted_capabilities, list)
                    or not required_capabilities.issubset(granted_capabilities)
                ):
                    raise RuntimeError(
                        "Google Workspace does not grant the email campaign capabilities"
                    )
                shortlist_history = await self._storage.canonical_file_history(
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    path=EMAIL_SHORTLIST_PATH,
                    limit=1,
                )
                if not shortlist_history:
                    raise RuntimeError("project state has no email shortlist revision")
                shortlist_commit_sha = shortlist_history[0]["revision"]
                shortlist = await self._storage.read_canonical_artifact(
                    repo_id=project.state_repo_id,
                    commit_sha=shortlist_commit_sha,
                    path=EMAIL_SHORTLIST_PATH,
                )
                recipients = parse_selected_shortlist(
                    shortlist,
                    run_id=run_id,
                    subject=subject,
                    body=body,
                    follow_up_body=follow_up_body,
                )
                plan_path = campaign_plan_path(run_id)
                plan = build_campaign_plan(
                    run_id=run_id,
                    shortlist_path=EMAIL_SHORTLIST_PATH,
                    shortlist_commit_sha=shortlist_commit_sha,
                    recipients=recipients,
                    subject_template=subject,
                    body_template=body,
                    follow_up_template=follow_up_body,
                    follow_up_delay_days=follow_up_delay_days,
                    send_interval_seconds=send_interval_seconds,
                    daily_send_cap=send_policy.daily_send_cap,
                    send_window_start=send_policy.window_start.strftime("%H:%M"),
                    send_window_end=send_policy.window_end.strftime("%H:%M"),
                    send_timezone=send_policy.timezone,
                    sender_account=(
                        workspace_connection.external_account_label
                        or str(workspace_connection.configuration.get("email", "Google Workspace"))
                    ),
                )
                async with self._db.project_state_lock(conn, project.id):
                    plan_commit_sha, changed = await self._storage.publish_state_documents(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        documents={plan_path: plan},
                        workflow_key=EMAIL_CAMPAIGN_WORKFLOW_NAME,
                        execution_key=execution_key,
                        run_id=str(run_id),
                    )
                await self._db.create_email_campaign(
                    run_id=run_id,
                    project_id=project.id,
                    connection_id=workspace_connection.id,
                    external_account_id=workspace_connection.external_account_id,
                    shortlist_path=EMAIL_SHORTLIST_PATH,
                    shortlist_commit_sha=shortlist_commit_sha,
                    plan_path=plan_path,
                    plan_commit_sha=plan_commit_sha,
                    follow_up_delay_days=follow_up_delay_days,
                    send_interval_seconds=send_interval_seconds,
                    daily_send_cap=send_policy.daily_send_cap,
                    send_window_start=send_policy.window_start,
                    send_window_end=send_policy.window_end,
                    send_timezone=send_policy.timezone,
                    recipients=recipients,
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="email_campaign_prepared",
                    details={"recipient_count": len(recipients), "changed": changed},
                    dedupe_key=f"{execution_key}:prepared",
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={
                        "plan_path": plan_path,
                        "plan_commit_sha": plan_commit_sha,
                        "recipient_count": len(recipients),
                    },
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn, execution_key=execution_key, error_message=_safe_failure(exc)
                )
                raise

    @activity.defn(name="request_email_campaign_review")
    async def request_email_campaign_review(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        campaign = await self._db.get_email_campaign(run_id)
        if campaign is None:
            raise RuntimeError("email campaign snapshot is unavailable")
        project = await self._require_project(campaign["project_id"])
        artifact_ref = (
            f"code.storage://{project.state_repo_id}@{campaign['review_commit_sha']}"
            f"/{campaign['review_path']}"
        )
        review_required = await self._db.request_human_review(
            run_id=run_id,
            canonical_commit_sha=campaign["review_commit_sha"],
            artifact_ref=artifact_ref,
            artifact_path=campaign["review_path"],
            summary=(
                f"Email campaign for {campaign['recipient_count']} recipient(s) is ready. "
                "Nothing will be sent until you approve it."
            ),
        )
        if not review_required:
            raise RuntimeError("email campaign must require explicit human approval")

    @activity.defn(name="record_email_campaign_approval")
    async def record_email_campaign_approval(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        await self._db.record_human_review(
            run_id=run_id,
            decision="approved",
            summary="You approved the exact email campaign snapshot.",
        )
        await self._db.approve_email_campaign(run_id=run_id)

    @activity.defn(name="list_email_campaign_recipients")
    async def list_email_campaign_recipients(self, run_id_text: str) -> list[str]:
        return [
            str(recipient_id)
            for recipient_id in await self._db.list_email_campaign_recipient_ids(
                run_id=UUID(run_id_text)
            )
        ]

    @activity.defn(name="reserve_email_campaign_delivery")
    async def reserve_email_campaign_delivery(self, payload: dict[str, str]) -> int:
        return await self._db.reserve_outreach_delivery(
            recipient_id=UUID(payload["recipient_id"]),
            stage=payload["stage"],
        )

    @activity.defn(name="send_email_campaign_recipient")
    async def send_email_campaign_recipient(self, payload: dict[str, str]) -> None:
        recipient_id = UUID(payload["recipient_id"])
        stage = payload["stage"]
        recipient = await self._db.get_email_campaign_recipient(recipient_id)
        if recipient is None:
            raise RuntimeError("email campaign recipient is unavailable")
        if recipient["campaign_status"] not in {"approved", "running", "completed"}:
            raise RuntimeError("email campaign is not approved")
        campaign_input = recipient["campaign_input"]
        subject_template = str(campaign_input.get("subject", "")).strip()
        body_template = str(campaign_input.get("body", "")).strip()
        follow_up_template = str(recipient.get("effective_follow_up_body") or "").strip()
        if stage == "initial":
            if recipient["status"] in {
                "initial_sent",
                "replied",
                "follow_up_sent",
                "completed",
            }:
                return
            subject = render_email_template(subject_template, name=recipient["display_name"])
            body = render_email_template(body_template, name=recipient["display_name"])
            thread_id = None
            in_reply_to = None
        elif stage == "follow_up":
            if recipient["status"] in {"replied", "follow_up_sent", "completed"}:
                return
            if recipient["status"] != "initial_sent" or not follow_up_template:
                raise RuntimeError("email recipient is not ready for a follow-up")
            subject = render_email_template(subject_template, name=recipient["display_name"])
            subject = subject if subject.casefold().startswith("re:") else f"Re: {subject}"
            body = render_email_template(follow_up_template, name=recipient["display_name"])
            thread_id = recipient["provider_thread_id"]
            in_reply_to = campaign_message_id(
                f"{recipient['campaign_run_id']}:email:{recipient_id}:initial"
            )
        else:
            raise RuntimeError("email campaign stage is unsupported")
        execution_key = f"{recipient['campaign_run_id']}:email:{recipient_id}:{stage}"
        await self._db.start_outreach_delivery(execution_key=execution_key)
        try:
            sent = await self._integrations.workspace_send_message(
                project_id=recipient["project_id"],
                run_id=recipient["campaign_run_id"],
                connection_id=recipient["integration_connection_id"],
                external_account_id=recipient["external_account_id"],
                execution_key=execution_key,
                recipient_email=recipient["address"],
                recipient_name=recipient["display_name"],
                subject=subject,
                body=body,
                thread_id=thread_id,
                in_reply_to=in_reply_to,
            )
        except IntegrationDeliveryUnknownError:
            await self._db.fail_outreach_delivery(
                execution_key=execution_key,
                error_code="delivery_unconfirmed",
                unknown=True,
            )
            raise
        except Exception as exc:
            await self._db.fail_outreach_delivery(
                execution_key=execution_key,
                error_code=type(exc).__name__,
                unknown=False,
            )
            raise
        await self._db.record_email_sent(
            recipient_id=recipient_id,
            stage=stage,
            gmail_message_id=sent["id"],
            gmail_thread_id=sent["thread_id"],
            execution_key=execution_key,
        )
        await self._db.add_activity(
            run_id=recipient["campaign_run_id"],
            event_type="email_sent",
            details={"recipient_id": str(recipient_id), "stage": stage},
            dedupe_key=f"{execution_key}:activity",
        )

    @activity.defn(name="check_email_campaign_reply")
    async def check_email_campaign_reply(self, recipient_id_text: str) -> bool:
        recipient_id = UUID(recipient_id_text)
        recipient = await self._db.get_email_campaign_recipient(recipient_id)
        if recipient is None:
            raise RuntimeError("email campaign recipient is unavailable")
        if recipient["status"] == "replied":
            return True
        if recipient["status"] != "initial_sent":
            return False
        if recipient["provider_thread_id"] is None or recipient["initial_sent_at"] is None:
            raise RuntimeError("email recipient has no sent-message receipt")
        execution_key = f"{recipient['campaign_run_id']}:email:{recipient_id}:reply-check"
        replied = await self._integrations.workspace_thread_has_reply(
            project_id=recipient["project_id"],
            run_id=recipient["campaign_run_id"],
            connection_id=recipient["integration_connection_id"],
            external_account_id=recipient["external_account_id"],
            thread_id=recipient["provider_thread_id"],
            after=recipient["initial_sent_at"],
            execution_key=execution_key,
        )
        await self._db.record_email_reply_check(recipient_id=recipient_id, replied=replied)
        return replied

    @activity.defn(name="email_campaign_follow_up_delay")
    async def email_campaign_follow_up_delay(self, recipient_id_text: str) -> int:
        recipient = await self._db.get_email_campaign_recipient(UUID(recipient_id_text))
        if recipient is None:
            raise RuntimeError("email campaign recipient is unavailable")
        campaign_input = recipient["campaign_input"]
        if not str(campaign_input.get("follow_up_body", "")).strip():
            return 0
        return int(recipient["follow_up_delay_days"] or 0)

    @activity.defn(name="complete_email_campaign_recipient")
    async def complete_email_campaign_recipient(self, recipient_id_text: str) -> None:
        await self._db.complete_email_recipient(recipient_id=UUID(recipient_id_text))

    @activity.defn(name="fail_email_campaign_recipient")
    async def fail_email_campaign_recipient(self, payload: dict[str, str]) -> None:
        await self._db.fail_email_recipient(
            recipient_id=UUID(payload["recipient_id"]),
            error_code=payload.get("reason", "recipient workflow failed"),
        )

    @activity.defn(name="complete_email_campaign")
    async def complete_email_campaign(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        campaign = await self._db.complete_email_campaign(run_id=run_id)
        project = await self._require_project(campaign["project_id"])
        artifact_ref = (
            f"code.storage://{project.state_repo_id}@{campaign['review_commit_sha']}"
            f"/{campaign['review_path']}"
        )
        await self._db.project_success(
            run_id=run_id,
            canonical_commit_sha=campaign["review_commit_sha"],
            artifact_ref=artifact_ref,
            artifact_path=campaign["review_path"],
        )

    @activity.defn(name="project_email_campaign_failure")
    async def project_email_campaign_failure(self, payload: dict[str, str]) -> None:
        await self._db.fail_email_campaign(
            run_id=UUID(payload["run_id"]),
            error_code=payload.get("reason", "email campaign failed"),
        )

    @activity.defn
    async def resolve_codex_project(self, run_id_text: str) -> str:
        # The project comes from the authorized run, never a caller's queue key.
        run = await self._require_run(UUID(run_id_text))
        return str(run.project_id)

    @activity.defn
    async def prepare_codex_procedure(self, run_id_text: str) -> bool:
        from tin_lite import content_draft
        from tin_lite.content_draft_sources import ContentDraftSources
        from tin_lite.technical_fix_execution import TechnicalFixExecution

        run_id = UUID(run_id_text)
        _definition, procedure = await self._pinned_codex_procedure(run_id)
        if procedure.optional_repository:
            from tin_lite.procedure_repository import select_repository

            await select_repository(self._db, await self._require_run(run_id), procedure)
        if procedure.output_validator == "brand-design-capture.v1":
            from tin_lite.brand_capture import BrandCaptureSources

            await self._await_with_heartbeats(
                BrandCaptureSources(
                    database=self._db, storage=self._storage, integrations=self._integrations
                ).prepare(await self._require_run(run_id), procedure),
                details={"stage": "brand_capture_preparation"},
            )
            return False
        from tin_lite import content_repository_delivery

        if _definition.id == content_repository_delivery.WORKFLOW_ID:
            run = await self._require_run(run_id)
            if run.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                return False
            await content_repository_delivery.saved_source(self._db, run_id)
            await self._db.mark_run_running(run_id)
            await self._db.project_run_progress(
                run_id=run_id,
                mode="steps",
                current=1,
                total=3,
                step="prepare_article_delivery",
                summary="Approved article pinned. Waiting to adapt it to the website repository.",
            )
            return False
        if procedure.output_validator in content_draft.VALIDATORS:
            run = await self._require_run(run_id)
            if run.status.value not in {"pending", "running"}:
                raise StaleGenerationError("The draft is no longer active")
            context = await self._await_with_heartbeats(
                ContentDraftSources(database=self._db, storage=self._storage).prepare(
                    run, output_validator=procedure.output_validator
                ),
                details={"stage": "content_draft_preparation"},
            )
            await self._db.mark_run_running(run_id)
            await self._db.project_run_progress(
                run_id=run_id,
                mode="steps",
                current=1,
                total=3,
                step="waiting_to_draft",
                summary=(
                    f"Ready to draft: {context['item']['title']}. Waiting for the writing worker."
                )[:240],
            )
            return False
        if procedure.repair_policy is None:
            return False
        run = await self._require_run(run_id)
        await self._db.mark_run_running(run_id)
        await self._db.project_run_progress(
            run_id=run_id,
            mode="steps",
            current=0,
            total=3,
            step="verify_source",
            summary="Checking the selected audit finding and its current source.",
        )
        execution = TechnicalFixExecution(
            database=self._db, storage=self._storage, integrations=self._integrations
        )
        handled = await self._await_with_heartbeats(
            execution.prepare(run, policy=procedure.repair_policy),
            details={"stage": "technical_verification"},
        )
        if not handled:
            await self._db.project_run_progress(
                run_id=run_id,
                mode="steps",
                current=1,
                total=3,
                step="prepare_patch",
                summary="Preparing a bounded metadata-only change.",
            )
        return handled

    @activity.defn(name="create_codex_procedure_sandbox")
    async def create_codex_procedure_sandbox(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:procedure_sandbox_create"
        operation = "procedure_sandbox_create"
        sandbox_id = None
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                _workflow_definition, procedure = await self._pinned_codex_procedure(run_id)
                run = await self._require_run(run_id)
                from tin_lite import content_draft

                if (
                    procedure.output_validator in content_draft.VALIDATORS
                    and procedure.content_draft_context is None
                ):
                    raise ValueError("Prepare the selected draft sources before compute.")
                await self._check_private_attempt(run, _workflow_definition)
                from tin_lite.codex_api import execution_profile, select_contract

                codex_auth = (existing.result or {}).get("codex_auth") if existing else None
                if codex_auth is None:
                    # Existing create attempts predate this pilot and keep OAuth.
                    codex_auth = (
                        {"mode": "chatgpt_oauth"}
                        if existing
                        else await select_contract(
                            db=self._db,
                            conn=conn,
                            run=run,
                            procedure=procedure,
                            settings=self._settings,
                        )
                    )
                if existing is None or existing.status == "started":
                    await self._db.save_effect_progress(
                        conn, execution_key=execution_key, result={"codex_auth": codex_auth}
                    )
                if run.status.value not in {"pending", "running"}:
                    raise StaleGenerationError("The procedure is no longer active")
                project = await self._require_project(run.project_id)
                repo = await self._storage.get_repo(project.state_repo_id)
                expected_head_sha = await self._storage.head_sha(repo, project.canonical_branch)
                if procedure.content_draft_context is not None:
                    expected_head_sha = procedure.content_draft_context["project_revision"]
                if procedure.brand_capture_context is not None:
                    expected_head_sha = procedure.brand_capture_context["project_revision"]
                elif procedure.output_validator == "brand-design-capture.v1":
                    raise ValueError("Brand capture must pin its sources before compute")
                # New revision packets pin current reference files separately from
                # the original article's brief/style/evidence. Legacy packets keep
                # their existing checkout semantics.
                if revision_sha := (procedure.review_revision_context or {}).get(
                    "project_revision"
                ):
                    expected_head_sha = revision_sha
                if expected_head_sha is None:
                    raise RuntimeError("project state repository has no canonical head")
                if procedure.output_validator == TIN_DIAGRAM_BRANDED_VALIDATOR:
                    from tin_lite.brand_diagrams import prepare

                    await prepare(self._storage, project, expected_head_sha)
                if procedure.documents:
                    from tin_lite.procedure_documents import validate_document

                    for path, maximum in zip(
                        procedure.documents.destinations,
                        (procedure.output_max_bytes, procedure.documents.companion_max_bytes),
                        strict=True,
                    ):
                        entry = await self._storage.read_output_destination(
                            repo_id=project.state_repo_id,
                            revision=expected_head_sha,
                            path=path,
                        )
                        if entry is not None:
                            validate_document(entry[1], maximum)
                ephemeral_branch = f"procedures/{run.id}/{run.generation}"
                identity_id: str | None = None
                identity_mode: str | None = None
                if procedure.identity.enabled:
                    identity, identity_mode = await self._resolve_test_identity(
                        run=run, procedure=procedure
                    )
                    identity_id = str(identity.id)
                sandbox_id = await self._sandboxes.create(
                    execution_key=execution_key,
                    run_id=str(run_id),
                    profile=execution_profile(procedure.sandbox, codex_auth),
                )
                leased = await self._db.attach_sandbox(
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    lease_owner=execution_key,
                    expected_head_sha=expected_head_sha,
                    ephemeral_branch=ephemeral_branch,
                    require_active=True,
                )
                await self._db.mark_run_running(run_id)
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={
                        "sandbox_id": sandbox_id,
                        "fencing_token": leased.fencing_token,
                        "ephemeral_branch": ephemeral_branch,
                        "expected_head_sha": expected_head_sha,
                        "sandbox_profile": execution_profile(procedure.sandbox, codex_auth).profile,
                        "codex_auth": codex_auth,
                        "identity_id": identity_id,
                        "identity_mode": identity_mode,
                    },
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="procedure_sandbox_created",
                    dedupe_key=f"{execution_key}:sandbox_created",
                )
            except BaseException as exc:
                if sandbox_id is not None:
                    with suppress(Exception):
                        await self._sandboxes.kill(sandbox_id)
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="persist_codex_procedure_artifact")
    async def persist_codex_procedure_artifact(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:procedure_artifact_persist"
        operation = "procedure_artifact_persist"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                if existing.result is None:
                    raise RuntimeError("completed procedure persist has no result")
                await self._db.complete_procedure_persist(
                    conn,
                    execution_key=execution_key,
                    run_id=run_id,
                    result=existing.result,
                    sandbox_killed=bool(existing.result.get("sandbox_killed")),
                )
                if not existing.result.get("sandbox_killed"):
                    await self._cleanup_persisted_procedure(conn, run_id, execution_key)
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            run = await self._require_run(run_id)
            workflow_definition, procedure = await self._pinned_codex_procedure(run_id)
            project = await self._require_project(run.project_id)
            self._require_active_lease(run)
            if run.ephemeral_branch is None or run.expected_head_sha is None:
                raise RuntimeError("procedure run lease has no durable branch metadata")
            checkpoint_path = (
                procedure.output_path
                if procedure.result_kind == PROJECT_ARTIFACT_RESULT
                else procedure_checkpoint_path(run_id)
            )
            if checkpoint_path is None:
                raise RuntimeError("procedure result has no checkpoint path")
            recovered_revision = None
            from tin_lite import content_repository_delivery
            from tin_lite.codex_api import attempt_failure, attempt_key, record_attempt_failure

            api_attempt = await self._db.get_effect(attempt_key(run_id), conn=conn)

            repository_delivery = run.workflow_id == content_repository_delivery.WORKFLOW_ID
            if (
                procedure.result_kind == PROJECT_ARTIFACT_RESULT
                or procedure.repair_policy
                or repository_delivery
            ):
                if run.ephemeral_branch != f"procedures/{run.id}/{run.generation}":
                    raise RuntimeError("procedure checkpoint does not belong to this generation")
                recovered_revision = await self._storage.procedure_checkpoint_revision(
                    repo_id=project.state_repo_id,
                    branch=run.ephemeral_branch,
                )
                if (
                    recovered_revision is None
                    and api_attempt is not None
                    and api_attempt.status == "completed"
                    and (api_attempt.result or {}).get("generation") == run.generation
                    and (api_attempt.result or {}).get("definition_commit_sha")
                    == run.definition_commit_sha
                ):
                    recovered_revision = (api_attempt.result or {}).get("procedure_revision")
                recovered = (
                    await self._storage.read_procedure_checkpoint(
                        repo_id=project.state_repo_id,
                        revision=recovered_revision,
                        path=checkpoint_path,
                        binary=procedure.binary_output,
                    )
                    if recovered_revision is not None
                    else None
                )
            else:
                recovered = await self._storage.read_ephemeral_artifact_if_exists(
                    repo_id=project.state_repo_id,
                    branch=run.ephemeral_branch,
                    path=checkpoint_path,
                )
            if recovered is not None:
                diagram_validation = {}
                if procedure.result_kind == PROJECT_ARTIFACT_RESULT:
                    validate_procedure_artifact(
                        recovered,
                        spec=procedure,
                        base=await self._procedure_artifact_base(run=run, procedure=procedure),
                    )
                    if procedure.output_validator in REVIEWED_DIAGRAM_VALIDATORS:
                        diagram_validation = {
                            "diagram_validation": await self._await_with_heartbeats(
                                self._sandboxes.validate_diagram(
                                    content=recovered,
                                    run_id=str(run_id),
                                    revision=recovered_revision,
                                ),
                                details={"stage": "diagram_validation"},
                                procedure_run=run,
                            )
                        }
                else:
                    manifest = validate_procedure_pull_request(recovered, spec=procedure)
                    if repository_delivery:
                        source = await content_repository_delivery.saved_source(self._db, run_id)
                        content_repository_delivery.validate_copy(manifest, source)
                if procedure.identity.enabled:
                    await self._reject_identity_leak(run_id=run_id, content=recovered)
                    await self._reject_card_leak(
                        run_id=run_id, procedure=procedure, content=recovered
                    )
                await self._complete_procedure_persist(
                    conn=conn,
                    run_id=run_id,
                    execution_key=execution_key,
                    ephemeral_branch=run.ephemeral_branch,
                    sandbox_id=None,
                    result={
                        "checkpoint_path": checkpoint_path,
                        "summary": f"{workflow_definition.title} produced its result.",
                        "message": "The procedure result is ready.",
                        "reconciled_from_ephemeral_branch": True,
                        **diagram_validation,
                        "sandbox_killed": False,
                        **(
                            {"ephemeral_commit_sha": recovered_revision}
                            if procedure.repair_policy or repository_delivery
                            else {}
                        ),
                        **(
                            {
                                "checkpoint": OutputCheckpoint.create(
                                    run=run,
                                    revision=recovered_revision,
                                    path=checkpoint_path,
                                    media_type=procedure.output_media_type or "text/markdown",
                                    content=recovered,
                                    companions=await self._procedure_companions(
                                        run, project, procedure, recovered_revision
                                    ),
                                ).to_dict()
                            }
                            if recovered_revision is not None
                            and procedure.result_kind == PROJECT_ARTIFACT_RESULT
                            else {}
                        ),
                    },
                )
                await self._cleanup_persisted_procedure(conn, run_id, execution_key)
                return

            sandbox_id: str | None = None
            try:
                from tin_lite import interrupted_procedure

                async def retain_interrupted(content=None):
                    await interrupted_procedure.retain(
                        db=self._db,
                        conn=conn,
                        storage=self._storage,
                        run=run,
                        project=project,
                        spec=procedure,
                        base=await self._procedure_artifact_base(run=run, procedure=procedure),
                        content=content,
                    )

                if api_attempt is not None:
                    sandbox_id = run.sandbox_id  # Cleanup the old attempt; never allocate another.
                    await record_attempt_failure(conn, attempt_key(run_id))
                    await retain_interrupted()
                    failure = attempt_failure(api_attempt.result or {})
                    raise ApplicationError(
                        str(failure), type=type(failure).__name__, non_retryable=True
                    )
                await self._check_private_attempt(run, workflow_definition)
                from tin_lite.codex_api import execution_profile, pinned_contract

                codex_auth = await pinned_contract(self._db, run_id, conn=conn)
                runtime_profile = execution_profile(procedure.sandbox, codex_auth)
                sandbox_id = await self._sandboxes.create(
                    execution_key=f"{run_id}:procedure_sandbox_create",
                    run_id=str(run_id),
                    profile=runtime_profile,
                )
                if sandbox_id != run.sandbox_id:
                    previous_sandbox_id = run.sandbox_id
                    run = await self._db.attach_sandbox(
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        lease_owner=f"{run_id}:procedure_sandbox_create",
                        expected_head_sha=run.expected_head_sha,
                        ephemeral_branch=run.ephemeral_branch,
                        require_active=True,
                    )
                    await self._db.add_activity(
                        run_id=run_id,
                        event_type="procedure_sandbox_recovered",
                        details={"previous_sandbox_id": previous_sandbox_id},
                        dedupe_key=f"{execution_key}:sandbox_recovered:{sandbox_id}",
                    )
                remotes = self._storage.sandbox_remotes(
                    repo_id=project.state_repo_id,
                    branch=run.ephemeral_branch,
                    subject=f"run:{run.id}:procedure",
                )
                public_host = urlsplit(self._settings.switchboard_public_url).hostname
                if not public_host:
                    raise RuntimeError("TIN_LITE_PUBLIC_URL must include a hostname")
                proxy_url = (
                    self._settings.forward_proxy_url.get_secret_value()
                    if self._settings.forward_proxy_url
                    else None
                )
                if public_host not in {"127.0.0.1", "localhost"} and proxy_url is None:
                    raise RuntimeError("TIN_LITE_PROXY_URL is required outside local development")
                workspace_archive: bytes | None = None
                workspace_evidence: bytes | None = None
                workspace_context: dict[str, str | int] | None = None
                run_tools_url: str | None = None
                run_tools_grant: str | None = None
                requirements = await load_pinned_integration_requirements(
                    storage=self._storage,
                    workflow=workflow_definition,
                    commit_sha=run.definition_commit_sha,
                )
                workspace_requirement = next(
                    (
                        requirement
                        for requirement in requirements
                        if requirement.provider_key == GOOGLE_WORKSPACE_PROVIDER
                    ),
                    None,
                )
                payment_card = await self._load_run_card(run_id, procedure)
                identity_context: dict[str, str] | None = None
                redact: tuple[str, ...] = ()
                if procedure.identity.enabled:
                    if workspace_requirement is None:
                        raise RuntimeError("test identities require the Google Workspace mailbox")
                    identity = await self._db.get_test_identity_for_run(run_id=run_id)
                    if identity is None:
                        raise RuntimeError("procedure run has no test identity")
                    identity_mode = await self._db.get_test_identity_use_mode(run_id=run_id)
                    password = self._require_integrations().open_test_identity_password(identity)
                    identity_context = {
                        "identity_id": str(identity.id),
                        "email": identity.email,
                        "password": password,
                        "mode": identity_mode or "created",
                    }
                    # Identities minted before the number existed still get it: the number is
                    # Tin's, not the identity's, until a pool makes that distinction real.
                    test_phone = identity.phone_number or getattr(
                        self._settings, "test_phone_number", None
                    )
                    if getattr(self._settings, "test_phone_enabled", False) and test_phone:
                        identity_context["phone"] = test_phone
                    redact = (password,)
                if payment_card:
                    from tin_lite.payment_card_guard import card_secrets

                    redact += card_secrets(payment_card)
                if workspace_requirement is not None and not procedure.services:
                    workspace_connection = await self._connected_workspace(
                        project_id=run.project_id,
                        capabilities=workspace_requirement.capabilities,
                    )
                    granted = tuple(workspace_requirement.capabilities)
                    if procedure.identity.enabled:
                        granted = (*granted, TEST_IDENTITY_WRITE_CAPABILITY)
                        if identity_context is not None and "phone" in identity_context:
                            granted = (*granted, TEST_IDENTITY_SMS_CAPABILITY)
                    run_tools_grant = secrets.token_urlsafe(32)
                    await self._db.create_run_tool_grant(
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        token=run_tools_grant,
                        connection_id=workspace_connection.id,
                        external_account_id=workspace_connection.external_account_id or "",
                        provider_key=GOOGLE_WORKSPACE_PROVIDER,
                        capabilities=granted,
                        ttl_seconds=procedure.sandbox.timeout_seconds + 60,
                    )
                    run_tools_url = (
                        f"{self._settings.switchboard_public_url.rstrip('/')}"
                        "/internal/run-tools/mcp"
                    )
                if procedure.services:
                    from tin_lite.procedure_services import SERVICE_CAPABILITY, SERVICE_PROVIDER

                    run_tools_grant = secrets.token_urlsafe(32)
                    await self._db.create_run_tool_grant(
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        token=run_tools_grant,
                        connection_id=None,
                        external_account_id="",
                        provider_key=SERVICE_PROVIDER,
                        capabilities=(SERVICE_CAPABILITY,),
                        ttl_seconds=procedure.sandbox.timeout_seconds + 60,
                    )
                    run_tools_url = (
                        f"{self._settings.switchboard_public_url.rstrip('/')}"
                        "/internal/run-tools/mcp"
                    )
                if procedure.sandbox.studio:
                    # The studio toolkit's voice step calls the switchboard with this grant;
                    # a studio procedure cannot also mount Workspace tools because one sandbox
                    # carries exactly one run-tool grant.
                    if run_tools_grant is not None:
                        raise RuntimeError("studio procedures cannot also require Workspace tools")
                    run_tools_grant = secrets.token_urlsafe(32)
                    await self._db.create_run_tool_grant(
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        token=run_tools_grant,
                        connection_id=None,
                        external_account_id="",
                        provider_key=STUDIO_PROVIDER,
                        capabilities=(STUDIO_VOICE_CAPABILITY,),
                        ttl_seconds=procedure.sandbox.timeout_seconds + 60,
                    )
                    run_tools_url = (
                        f"{self._settings.switchboard_public_url.rstrip('/')}"
                        "/internal/run-tools/mcp"
                    )
                if procedure.result_kind == GITHUB_PULL_REQUEST_RESULT:
                    technical = None
                    expected_binding = None
                    from tin_lite import content_repository_delivery

                    content_source = None
                    if run.workflow_id == content_repository_delivery.WORKFLOW_ID:
                        content_source = await content_repository_delivery.saved_source(
                            self._db, run_id
                        )
                        expected_binding = content_repository_delivery.binding_from(content_source)
                    if procedure.repair_policy:
                        from tin_lite.technical_fix_execution import binding_from, prepared_result

                        technical = await prepared_result(self._db, run_id)
                        expected_binding = binding_from(technical)
                    bundle, open_pull_requests = await self._github_procedure_workspace(
                        project_id=run.project_id,
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        **({"expected_binding": expected_binding} if expected_binding else {}),
                    )
                    workspace_archive = bundle.archive
                    workspace_evidence = open_pull_requests.document
                    workspace_context = {
                        "provider_key": "infra.github",
                        "repository": bundle.repository,
                        "default_branch": bundle.default_branch,
                        "head_sha": bundle.head_sha,
                        "file_count": bundle.file_count,
                        "open_pull_request_count": open_pull_requests.pull_request_count,
                        "open_pull_request_file_count": open_pull_requests.file_count,
                        "open_pull_request_evidence_truncated": int(open_pull_requests.truncated),
                    }
                    if technical is not None:
                        workspace_context["technical_fix"] = technical
                    if content_source is not None:
                        workspace_context["content_delivery"] = content_source
                elif procedure.workspace_kind == GITHUB_REPOSITORY_WORKSPACE:
                    from tin_lite.procedure_repository import select_repository

                    use_repository = await select_repository(self._db, run, procedure)
                    if use_repository:
                        # A read-only repository snapshot: Codex reads it and writes only the
                        # declared project artifact into the separate project-state checkout.
                        bundle = await self._github_procedure_bundle(
                            project_id=run.project_id,
                            run_id=run_id,
                            sandbox_id=sandbox_id,
                        )
                        if procedure.optional_repository:
                            selected = await self._db.get_effect(
                                f"{run.id}:procedure_repository_selection"
                            )
                            if bundle.repository != selected.result["repository"]:
                                raise ValueError("The selected source repository changed.")
                        workspace_archive = bundle.archive
                        workspace_context = {
                            "provider_key": "infra.github",
                            "repository": bundle.repository,
                            "default_branch": bundle.default_branch,
                            "head_sha": bundle.head_sha,
                            "file_count": bundle.file_count,
                        }
                        workspace_context["complete"] = bundle.complete
                    else:
                        workspace_context = {"kind": "project.state", "repository_available": False}
                procedure_inputs: dict[str, object] = dict(run.input or {})
                from tin_lite import content_draft

                if procedure.output_validator in content_draft.VALIDATORS:
                    await self._db.project_run_progress(
                        run_id=run_id,
                        mode="steps",
                        current=1,
                        total=3,
                        step="draft",
                        summary="Writing the selected article from its pinned guide and evidence.",
                    )
                result = await self._await_with_heartbeats(
                    self._run_accounted_procedure(
                        conn=conn,
                        run=run,
                        sandbox_id=sandbox_id,
                        run_input=SandboxProcedureInput(
                            execution_key=execution_key,
                            canonical_url=remotes.canonical_url,
                            canonical_auth_header=remotes.canonical_auth_header,
                            canonical_branch=project.canonical_branch,
                            ephemeral_url=remotes.ephemeral_url,
                            ephemeral_auth_header=remotes.ephemeral_auth_header,
                            ephemeral_branch=run.ephemeral_branch,
                            proxy_url=proxy_url,
                            no_proxy=public_host,
                            context=procedure.sandbox_context(
                                inputs=procedure_inputs,
                                workspace=workspace_context,
                                identity=identity_context,
                                payment_card=payment_card,
                            ),
                            output_path=checkpoint_path,
                            output_max_bytes=procedure.output_max_bytes,
                            interrupted_output_sink=(
                                retain_interrupted
                                if interrupted_procedure.eligible(procedure)
                                else None
                            ),
                            # Card runs keep no agent narration, as they keep no rollouts.
                            progress_sink=None if payment_card else self._progress_sink(run_id),
                            project_revision=(
                                run.expected_head_sha
                                if (
                                    procedure.output_validator in REVIEWED_DIAGRAM_VALIDATORS
                                    or (procedure.review_revision_context or {}).get(
                                        "project_revision"
                                    )
                                )
                                else None
                            ),
                            result_kind=procedure.result_kind,
                            run_tools_url=run_tools_url,
                            run_tools_grant=run_tools_grant,
                            workspace_archive=workspace_archive,
                            workspace_evidence=workspace_evidence,
                            browser=procedure.sandbox.browser,
                            studio=procedure.sandbox.studio,
                            isolated=runtime_profile.isolated,
                            api_url=(
                                f"{self._settings.switchboard_public_url.rstrip('/')}/internal/"
                                f"codex-api/{run_id}/v1"
                            ),
                            timeout_seconds=procedure.sandbox.timeout_seconds,
                            redact=redact,
                            rollout_sink=None
                            if payment_card
                            else self._rollout_sink(
                                run_id=run_id,
                                generation=run.generation,
                                execution_key=execution_key,
                                stage="codex_procedure",
                                sandbox_id=sandbox_id,
                            ),
                        ),
                    ),
                    details={"sandbox_id": sandbox_id, "stage": "codex_procedure"},
                    procedure_run=run,
                )
                if await self._sandboxes.is_running(sandbox_id):
                    raise RuntimeError("E2B procedure sandbox still exists after explicit kill")
                checkpoint = (
                    await self._storage.read_procedure_checkpoint(
                        repo_id=project.state_repo_id,
                        revision=result.ephemeral_commit_sha,
                        path=checkpoint_path,
                        binary=procedure.binary_output,
                    )
                    if procedure.result_kind == PROJECT_ARTIFACT_RESULT or repository_delivery
                    else await self._storage.read_ephemeral_artifact(
                        repo_id=project.state_repo_id,
                        branch=run.ephemeral_branch,
                        path=checkpoint_path,
                    )
                )
                diagram_validation = {}
                if procedure.result_kind == PROJECT_ARTIFACT_RESULT:
                    validate_procedure_artifact(
                        checkpoint,
                        spec=procedure,
                        base=await self._procedure_artifact_base(run=run, procedure=procedure),
                    )
                    if procedure.output_validator in REVIEWED_DIAGRAM_VALIDATORS:
                        diagram_validation = {
                            "diagram_validation": await self._await_with_heartbeats(
                                self._sandboxes.validate_diagram(
                                    content=checkpoint,
                                    run_id=str(run_id),
                                    revision=result.ephemeral_commit_sha,
                                ),
                                details={"stage": "diagram_validation"},
                                procedure_run=run,
                            )
                        }
                        if result.diagram_review:
                            if (
                                result.diagram_review.get("source_sha256")
                                != (diagram_validation["diagram_validation"]["source_sha256"])
                            ):
                                raise RuntimeError("Diagram changed after its visual inspection")
                            diagram_validation["diagram_inspection"] = result.diagram_review
                else:
                    manifest = validate_procedure_pull_request(checkpoint, spec=procedure)
                    from tin_lite import content_repository_delivery

                    if run.workflow_id == content_repository_delivery.WORKFLOW_ID:
                        source = await content_repository_delivery.saved_source(self._db, run_id)
                        content_repository_delivery.validate_copy(manifest, source)
                if procedure.identity.enabled:
                    await self._reject_identity_leak(run_id=run_id, content=checkpoint)
                    await self._reject_card_leak(
                        run_id=run_id, procedure=procedure, content=checkpoint
                    )
                await self._complete_procedure_persist(
                    conn=conn,
                    run_id=run_id,
                    execution_key=execution_key,
                    ephemeral_branch=run.ephemeral_branch,
                    sandbox_id=sandbox_id,
                    result={
                        "checkpoint_path": checkpoint_path,
                        "result_kind": procedure.result_kind,
                        "ephemeral_commit_sha": result.ephemeral_commit_sha,
                        **diagram_validation,
                        **(
                            {
                                "checkpoint": OutputCheckpoint.create(
                                    run=run,
                                    revision=result.ephemeral_commit_sha,
                                    path=checkpoint_path,
                                    media_type=procedure.output_media_type or "text/markdown",
                                    content=checkpoint,
                                    companions=await self._procedure_companions(
                                        run, project, procedure, result.ephemeral_commit_sha
                                    ),
                                ).to_dict()
                            }
                            if procedure.result_kind == PROJECT_ARTIFACT_RESULT
                            else {}
                        ),
                        "summary": result.summary,
                        "message": result.message,
                        "sandbox_killed": True,
                    },
                )
            except BaseException as exc:
                if sandbox_id is not None:
                    with suppress(Exception):
                        await self._sandboxes.kill(sandbox_id)
                failed_attempt = await self._db.get_effect(attempt_key(run_id), conn=conn)
                reported_failure = exc
                if failed_attempt is not None and (failed_attempt.result or {}).get("outcome") in {
                    "failed",
                    "cancelled",
                    "timed_out",
                    "unconfirmed",
                }:
                    reported_failure = attempt_failure(failed_attempt.result or {})
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(reported_failure),
                )
                raise

    async def _cleanup_persisted_procedure(self, conn, run_id: UUID, execution_key: str) -> None:
        run = await self._require_run(run_id)
        if run.sandbox_id is not None:
            await self._sandboxes.kill(run.sandbox_id)
            await self._db.add_activity(
                conn=conn,
                run_id=run_id,
                event_type="sandbox_killed",
                details={"stage": "codex_procedure"},
                dedupe_key=f"{execution_key}:sandbox_killed",
            )

    async def _complete_procedure_persist(
        self,
        *,
        conn,
        run_id: UUID,
        execution_key: str,
        ephemeral_branch: str,
        sandbox_id: str | None,
        result: dict,
    ) -> None:
        from tin_lite import content_draft
        from tin_lite.content_editorial_judgment import validate_pair

        # Keep classification with retained checkpoints as well as canonical output.
        # Applying/keeping a saved assessment must never make it look like an article.
        run = await self._require_run(run_id)
        if str(run.workflow_id) == "00000000-0000-4000-8000-000000000031":
            prepared = await self._db.get_effect(content_draft.receipt_key(run_id))
            context = prepared.result if prepared and prepared.status == "completed" else {}
            if (context or {}).get("output_validator") == content_draft.EDITORIAL_VALIDATOR:
                checkpoint = OutputCheckpoint.load(result["checkpoint"], run=run)
                project = await self._require_project(run.project_id)
                article = await self._storage.read_procedure_checkpoint(
                    repo_id=project.state_repo_id,
                    revision=checkpoint.ephemeral_commit_sha,
                    path=checkpoint.artifact_path,
                )
                notes = await self._storage.read_procedure_checkpoint(
                    repo_id=project.state_repo_id,
                    revision=checkpoint.ephemeral_commit_sha,
                    path=content_draft.notes_path(checkpoint.artifact_path),
                )
                result = {**result, "content_editorial": validate_pair(article, notes, context)}
        await self._db.complete_procedure_persist(
            conn,
            execution_key=execution_key,
            run_id=run_id,
            result={"ephemeral_branch": ephemeral_branch, **result},
            sandbox_killed=sandbox_id is not None,
        )

    @activity.defn(name="commit_codex_procedure_artifact")
    async def commit_codex_procedure_artifact(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:procedure_canonical_commit"
        operation = "procedure_canonical_commit"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                if existing.result is None:
                    raise RuntimeError("completed publication has no recorded result")
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                await self._db.complete_procedure_publication(
                    conn,
                    run_id=run_id,
                    execution_key=execution_key,
                    result=existing.result,
                    artifact_ref=(
                        f"code.storage://{project.state_repo_id}"
                        f"@{existing.result['canonical_commit_sha']}/{existing.result['artifact_path']}"
                    ),
                )
                await self._db.release_lease(run_id)
                return
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                run = await self._require_run(run_id)
                workflow_definition, procedure = await self._pinned_codex_procedure(run_id)
                project = await self._require_project(run.project_id)
                if procedure.result_kind == PROJECT_ARTIFACT_RESULT:
                    await self._publish_project_procedure(
                        conn=conn,
                        run=run,
                        project=project,
                        workflow_definition=workflow_definition,
                        procedure=procedure,
                        execution_key=execution_key,
                        existing=existing,
                    )
                    return
                self._require_active_lease(run)
                if run.lease_owner is None or run.sandbox_id is None:
                    raise RuntimeError("procedure run lease tuple is incomplete")
                if not await self._db.validate_lease(
                    project_id=run.project_id,
                    thread_id=run.thread_id,
                    generation=run.generation,
                    lease_owner=run.lease_owner,
                    fencing_token=run.fencing_token,
                    sandbox_id=run.sandbox_id,
                ):
                    raise StaleGenerationError("procedure lease no longer owns the session")
                if run.ephemeral_branch is None or run.expected_head_sha is None:
                    raise RuntimeError("procedure run is missing its branch metadata")
                checkpoint_path = (
                    procedure.output_path
                    if procedure.result_kind == PROJECT_ARTIFACT_RESULT
                    else procedure_checkpoint_path(run_id)
                )
                if checkpoint_path is None:
                    raise RuntimeError("procedure result has no checkpoint path")
                from tin_lite import content_repository_delivery

                immutable_checkpoint = bool(procedure.repair_policy) or (
                    run.workflow_id == content_repository_delivery.WORKFLOW_ID
                )
                checkpoint = (
                    b""
                    if immutable_checkpoint
                    else await self._storage.read_ephemeral_artifact(
                        repo_id=project.state_repo_id,
                        branch=run.ephemeral_branch,
                        path=checkpoint_path,
                    )
                )
                persisted = await self._db.get_effect(f"{run_id}:procedure_artifact_persist")
                if persisted is None or persisted.status != "completed" or persisted.result is None:
                    raise RuntimeError("procedure artifact persist step has not completed")
                if immutable_checkpoint:
                    revision = persisted.result.get("ephemeral_commit_sha")
                    if not isinstance(revision, str):
                        raise ValueError("The repository procedure has no immutable checkpoint")
                    checkpoint = await self._storage.read_procedure_checkpoint(
                        repo_id=project.state_repo_id,
                        revision=revision,
                        path=checkpoint_path,
                    )
                external_result: dict[str, object] = {}
                if procedure.result_kind == GITHUB_PULL_REQUEST_RESULT:
                    if self._integrations is None:
                        raise RuntimeError("GitHub procedure delivery is unavailable")
                    manifest = validate_procedure_pull_request(checkpoint, spec=procedure)
                    technical, expected_binding = None, None
                    copy_proof = None
                    if run.workflow_id == content_repository_delivery.WORKFLOW_ID:
                        source = await content_repository_delivery.saved_source(self._db, run_id)
                        copy_proof = content_repository_delivery.validate_copy(manifest, source)
                        expected_binding = content_repository_delivery.binding_from(source)
                    if procedure.repair_policy:
                        from tin_lite import technical_fix
                        from tin_lite.technical_fix_execution import (
                            TechnicalFixExecution,
                            binding_from,
                            prepared_result,
                        )

                        technical = await prepared_result(self._db, run_id)
                        expected_binding = binding_from(technical)
                        execution = TechnicalFixExecution(
                            database=self._db,
                            storage=self._storage,
                            integrations=self._integrations,
                        )
                        delivered = await self._db.get_integration_call_receipt(
                            f"{run_id}:procedure_pull_request"
                        )
                        if delivered and delivered.status == "completed":
                            # A delivered PR stays recoverable when the website later changes.
                            technical_fix.validate_manifest(manifest, technical)
                        else:
                            await execution.validate_delivery(run, manifest, technical)
                    pull_request = None
                    no_change = manifest.get("outcome") == "no_change" and (
                        technical is not None or procedure.allow_no_change
                    )
                    if not no_change:
                        pull_request = await self._integrations.github_create_pull_request(
                            project_id=run.project_id,
                            execution_key=f"{run_id}:procedure_pull_request",
                            title=str(manifest["title"]),
                            body=str(manifest["body"]),
                            files=tuple(
                                GitHubFileChange(path=item["path"], content=item["content"])
                                for item in manifest["files"]
                            ),
                            base_branch=str(manifest["default_branch"]),
                            expected_base_sha=str(manifest["head_sha"]),
                            run_id=run_id,
                            **({"expected_binding": expected_binding} if expected_binding else {}),
                            **({"allow_unrelated_base_advance": True} if copy_proof else {}),
                        )
                    artifact_path = procedure_receipt_path(spec=procedure, run_id=run_id)
                    if technical is not None:
                        receipt = technical_fix.report(
                            technical,
                            reason="no_safe_patch" if no_change else None,
                            pull_request=pull_request,
                        )
                    else:
                        receipt = build_procedure_pull_request_receipt(
                            workflow_title=workflow_definition.title,
                            manifest=manifest,
                            **(
                                {
                                    "pull_request_url": pull_request.url,
                                    "pull_request_number": pull_request.number,
                                    "pull_request_branch": pull_request.branch,
                                }
                                if pull_request is not None
                                else {}
                            ),
                        )
                        if copy_proof:
                            receipt += (
                                "\n## Approved source\n\n"
                                f"Tin article run: {source['source_run_id']}\n\n"
                                "The approved Markdown source is preserved exactly. This check "
                                "does not prove rendered equivalence or a successful site build. "
                                "Review the PR checks and site preview before merging.\n"
                            ).encode()
                    async with self._db.project_state_lock(conn, project.id):
                        canonical_sha, _changed = await self._storage.publish_state_document(
                            repo_id=project.state_repo_id,
                            branch=project.canonical_branch,
                            path=artifact_path,
                            content=receipt,
                            workflow_key=workflow_definition.key,
                            execution_key=execution_key,
                            run_id=str(run_id),
                        )
                    if pull_request is not None:
                        external_result = {
                            "external_url": pull_request.url,
                            "repository": pull_request.repository,
                            "pull_request_number": pull_request.number,
                            "pull_request_branch": pull_request.branch,
                        }
                        await self._db.add_activity(
                            run_id=run_id,
                            event_type="codex_procedure_pull_request_opened",
                            details=external_result,
                            dedupe_key=f"{execution_key}:pull_request_opened",
                        )
                    if technical is not None:
                        external_result.update(
                            {
                                "outcome": "no_change" if no_change else "pull_request",
                                "summary": "No safe patch proposed; finding remains unresolved."
                                if no_change
                                else "Verified title-only PR ready for review.",
                                "message": receipt.decode(),
                            }
                        )
                    elif no_change:
                        external_result = {
                            "outcome": "no_change",
                            "repository": str(manifest["repository"]),
                            "summary": "No change proposed; no pull request was opened.",
                            "message": receipt.decode(),
                        }
                        await self._db.add_activity(
                            run_id=run_id,
                            event_type="codex_procedure_no_change",
                            details={"repository": str(manifest["repository"])},
                            dedupe_key=f"{execution_key}:no_change",
                        )
                else:
                    raise RuntimeError("procedure result kind is unsupported")
                await self._db.complete_procedure_publication(
                    conn,
                    run_id=run_id,
                    execution_key=execution_key,
                    artifact_ref=f"code.storage://{project.state_repo_id}@{canonical_sha}/{artifact_path}",
                    result={
                        "canonical_commit_sha": canonical_sha,
                        "artifact_path": artifact_path,
                        "result_kind": procedure.result_kind,
                        "summary": persisted.result.get("summary"),
                        "message": persisted.result.get("message"),
                        **external_result,
                    },
                )
                await self._db.release_lease(run_id)
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=(
                        str(exc)
                        if isinstance(exc, ApplicationError)
                        and exc.type in {"OutputConflictError", "PublicationPendingError"}
                        else _safe_failure(exc)
                    ),
                )
                raise

    async def _validate_procedure_publication_lease(self, run: WorkflowRun) -> None:
        self._require_active_lease(run)
        if run.lease_owner is None or run.sandbox_id is None:
            raise StaleGenerationError("procedure run lease tuple is incomplete")
        if not await self._db.validate_lease(
            project_id=run.project_id,
            thread_id=run.thread_id,
            generation=run.generation,
            lease_owner=run.lease_owner,
            fencing_token=run.fencing_token,
            sandbox_id=run.sandbox_id,
        ):
            raise StaleGenerationError("procedure lease no longer owns the session")

    async def _procedure_companions(self, run, project, procedure, revision):
        from tin_lite import content_draft

        if not procedure.companion_path:
            return ()
        raw = await self._storage.read_procedure_checkpoint(
            repo_id=project.state_repo_id, revision=revision, path=procedure.companion_path
        )
        from tin_lite import article_review

        if procedure.documents:
            from tin_lite.procedure_documents import validate_document

            validate_document(raw, procedure.documents.companion_max_bytes)
            if procedure.output_validator == "brand-design-capture.v1":
                from tin_lite.brand_capture import validate_pair

                primary = await self._storage.read_procedure_checkpoint(
                    repo_id=project.state_repo_id, revision=revision, path=procedure.output_path
                )
                await validate_pair(self._storage, project, run, primary, raw)
        elif procedure.output_validator == article_review.VALIDATOR:
            article_review.validate_notes(
                raw, revision=procedure.review_revision_context is not None
            )
        else:
            content_draft.validate_notes(raw, procedure.content_draft_context)
        if procedure.review_revision_context is not None:
            article_review.validate_changes(raw)
        return (
            OutputCheckpoint.create(
                run=run,
                revision=revision,
                path=procedure.companion_path,
                media_type="text/markdown",
                content=raw,
            ),
        )

    async def _publish_project_procedure(
        self, *, conn, run, project, workflow_definition, procedure, execution_key, existing
    ) -> None:
        intent = (existing.result or {}).get("publication") if existing is not None else None
        if intent is None:
            await self._validate_procedure_publication_lease(run)
        persisted = await self._db.get_effect(f"{run.id}:procedure_artifact_persist")
        if persisted is None or persisted.status != "completed" or persisted.result is None:
            raise RuntimeError("procedure artifact persist step has not completed")
        checkpoint_data = (intent or {}).get("checkpoint") or persisted.result.get("checkpoint")
        if checkpoint_data is not None:
            checkpoint = OutputCheckpoint.load(checkpoint_data, run=run)
            if checkpoint.artifact_path != procedure.output_path or checkpoint.media_type != (
                procedure.output_media_type or "text/markdown"
            ):
                raise ValueError("saved output does not match the pinned procedure contract")
            revision = checkpoint.ephemeral_commit_sha
        else:
            # Older receipts may not have an immutable identity. Freeze it in the
            # unfinished publication receipt, never overwrite the completed persist.
            if run.ephemeral_branch != f"procedures/{run.id}/{run.generation}":
                raise ValueError("saved output has no run-bound checkpoint branch")
            revision = persisted.result.get("ephemeral_commit_sha")
            if revision is None:
                revision = await self._storage.procedure_checkpoint_revision(
                    repo_id=project.state_repo_id,
                    branch=run.ephemeral_branch,
                )
            if revision is None:
                raise ValueError("saved output has no recoverable revision")
        content = await self._storage.read_procedure_checkpoint(
            repo_id=project.state_repo_id,
            revision=revision,
            path=procedure.output_path,
            binary=procedure.binary_output,
        )
        validate_procedure_artifact(
            content,
            spec=procedure,
            base=await self._procedure_artifact_base(run=run, procedure=procedure),
        )
        if procedure.identity:
            await self._reject_identity_leak(run_id=run.id, content=content)
            await self._reject_card_leak(run_id=run.id, procedure=procedure, content=content)
        if checkpoint_data is None:
            checkpoint = OutputCheckpoint.create(
                run=run,
                revision=revision,
                path=procedure.output_path,
                media_type=procedure.output_media_type or "text/markdown",
                content=content,
            )
            intent = {
                "version": 1,
                "checkpoint": checkpoint.to_dict(),
                "attempted_parent_sha": checkpoint.source_base_sha,
                "no_change": False,
            }
            await self._db.save_publication_intent(conn, execution_key=execution_key, intent=intent)
        checkpoint.validate_content(content)
        if checkpoint.companions != await self._procedure_companions(
            run, project, procedure, revision
        ):
            raise ValueError("Saved companions differ from the pinned output contract.")
        from tin_lite import content_draft
        from tin_lite.content_editorial_judgment import LABELS, NO_DRAFT, validate_pair

        editorial = None
        if procedure.output_validator == content_draft.EDITORIAL_VALIDATOR:
            notes = await self._storage.read_procedure_checkpoint(
                repo_id=project.state_repo_id, revision=revision, path=procedure.companion_path
            )
            editorial = validate_pair(content, notes, procedure.content_draft_context)
        if procedure.output_validator in REVIEWED_DIAGRAM_VALIDATORS:
            proof = persisted.result.get("diagram_validation") or {}
            if (
                proof.get("checker") != "tin-diagram-check.v1"
                or proof.get("source_sha256") != checkpoint.sha256
                or proof.get("revision") != revision
                or proof.get("themes") != ["light", "dark"]
            ):
                raise ValueError("Diagram publication has no matching trusted visual check")
        await self._db.retain_procedure_output(
            conn,
            run_id=run.id,
            checkpoint=checkpoint.to_dict(),
            reason="publication_pending",
        )

        async def save_intent(value):
            await self._db.save_publication_intent(conn, execution_key=execution_key, intent=value)

        async def validate_lease():
            await self._validate_procedure_publication_lease(run)

        try:
            async with self._db.project_state_lock(conn, project.id):
                sha, changed = await self._storage.publish_procedure_output(
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    checkpoint=checkpoint,
                    content=content,
                    execution_key=execution_key,
                    workflow_key=workflow_definition.key,
                    intent=intent,
                    legacy_attempt=existing is not None,
                    save_intent=save_intent,
                    validate_lease=validate_lease,
                )
                result = {
                    "canonical_commit_sha": sha,
                    "artifact_path": checkpoint.artifact_path,
                    "result_kind": procedure.result_kind,
                    "changed": changed,
                    "checkpoint": checkpoint.to_dict(),
                    "summary": persisted.result.get("summary"),
                    "message": persisted.result.get("message"),
                }
                if editorial:
                    result["content_editorial"] = editorial
                    if editorial["outcome"] in NO_DRAFT:
                        result["summary"] = (
                            f"{LABELS[editorial['outcome']]}: "
                            f"{procedure.content_draft_context['item']['title']}. "
                            "No article drafted."
                        )
                if procedure.review_revision_context is not None:
                    from tin_lite.article_review import change_summary

                    revision_notes = await self._storage.read_procedure_checkpoint(
                        repo_id=project.state_repo_id,
                        revision=revision,
                        path=procedure.companion_path,
                    )
                    result["review_change_summary"] = change_summary(revision_notes)
                await self._db.complete_procedure_publication(
                    conn,
                    run_id=run.id,
                    execution_key=execution_key,
                    result=result,
                    artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{checkpoint.artifact_path}",
                )
        except (OutputConflictError, PublicationPendingError) as exc:
            conflict = isinstance(exc, OutputConflictError)
            await self._db.retain_procedure_output(
                conn,
                run_id=run.id,
                checkpoint=checkpoint.to_dict(),
                reason="output_conflict" if conflict else "reconciliation_pending",
            )
            reason = (
                "Your result was saved, but this file changed before publication. "
                "The current file was left untouched."
                if conflict
                else "Your result was saved. Publication has not yet been confirmed."
            )
            raise ApplicationError(reason, type=type(exc).__name__, non_retryable=conflict) from exc
        await self._db.release_lease(run.id)

    @activity.defn(name="request_codex_procedure_review")
    async def request_codex_procedure_review(self, run_id_text: str) -> bool:
        run_id = UUID(run_id_text)
        canonical = await self._db.get_effect(f"{run_id}:procedure_canonical_commit")
        if canonical is None or canonical.status != "completed" or canonical.result is None:
            raise RuntimeError("procedure canonical commit step has not completed")
        run = await self._require_run(run_id)
        from tin_lite.content_editorial_judgment import no_draft

        if no_draft(canonical.result):
            # Only the new, validated no-copy publication takes this branch. The pinned
            # review requirement and all historical article approvals stay unchanged.
            return False
        workflow_definition = await self._db.get_workflow(run.workflow_id)
        if workflow_definition is None:
            raise RuntimeError("procedure workflow definition is unavailable")
        project = await self._require_project(run.project_id)
        sha = str(canonical.result["canonical_commit_sha"])
        path = str(canonical.result["artifact_path"])
        artifact_ref = f"code.storage://{project.state_repo_id}@{sha}/{path}"
        from tin_lite.content_delivery import ContentDelivery

        delivery = await ContentDelivery(database=self._db, storage=self._storage).status(run)
        artifact_title = None
        if workflow_definition.key in {"content.generate", "content.public_article"}:
            # These drafts live at a run-owned path, so their heading is the readable label.
            from tin_lite.content_delivery import display_title

            artifact_title = display_title(
                await self._storage.read_canonical_artifact(
                    repo_id=project.state_repo_id, commit_sha=sha, path=path
                )
            )
        required = await self._db.request_human_review(
            run_id=run_id,
            canonical_commit_sha=sha,
            artifact_ref=artifact_ref,
            artifact_path=path,
            artifact_title=artifact_title,
            summary=(
                f"{workflow_definition.title} is ready for your review. Approval opens an unmerged "
                f"GitHub PR in {delivery['repository']}"
                + ("." if delivery.get("system_run_id") else f" at {delivery['path']}.")
                if delivery and delivery.get("approval_label")
                else f"{workflow_definition.title} is ready for your review."
            ),
        )
        if required:
            from tin_lite.organic_content import project_review_progress

            await project_review_progress(self._db, run)
        return required

    @activity.defn(name="record_codex_procedure_approval")
    async def record_codex_procedure_approval(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        run = await self._require_run(run_id)
        from tin_lite.reviewed_documents import ReviewedDocuments, document_spec

        if await document_spec(self._db, self._storage, run):
            await ReviewedDocuments(database=self._db, storage=self._storage).apply(run_id)
        workflow_definition = await self._db.get_workflow(run.workflow_id)
        if workflow_definition is None:
            raise RuntimeError("procedure workflow definition is unavailable")
        await self._db.record_human_review(
            run_id=run_id,
            decision="approved",
            summary=f"You approved {workflow_definition.title}.",
        )

    @activity.defn(name="receive_workflow_revision")
    async def receive_workflow_revision(self, payload: dict[str, str]) -> dict:
        async with self._db.pool.acquire() as conn, conn.transaction():
            command = await conn.fetchrow(
                "SELECT * FROM workflow_review_commands WHERE id=$1 AND coordinator_run_id=$2 "
                "AND action='revise' FOR UPDATE",
                UUID(payload["command_id"]),
                UUID(payload["run_id"]),
            )
            if not command:
                raise ApplicationError(
                    "Revision command does not belong to this run.", non_retryable=True
                )
            successor = await conn.fetchrow(
                "SELECT id, temporal_workflow_id, status FROM workflow_runs WHERE id=$1",
                command["successor_run_id"],
            )
            await conn.execute(
                "UPDATE workflow_review_commands SET dispatch_state='received', received_at=now() "
                "WHERE id=$1",
                command["id"],
            )
            return {
                "run_id": str(successor["id"]),
                "temporal_workflow_id": successor["temporal_workflow_id"],
                "stopped": successor["status"] == "stopped",
            }

    @activity.defn(name="project_codex_procedure_result")
    async def project_codex_procedure_result(self, run_id_text: str) -> None:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:procedure_projection"
        operation = "procedure_projection"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                run = await self._require_run(run_id)
                if run.status is not RunStatus.SUCCEEDED:
                    return
                # Reconcile completed projection facts without reviving a terminal
                # failure or reinterpreting a waiting review gate.
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                canonical = await self._db.get_effect(f"{run_id}:procedure_canonical_commit")
                if canonical is None or canonical.status != "completed" or canonical.result is None:
                    raise RuntimeError("procedure canonical commit step has not completed")
                sha = str(canonical.result["canonical_commit_sha"])
                path = str(canonical.result["artifact_path"])
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                from tin_lite.reviewed_documents import document_spec

                if await document_spec(self._db, self._storage, run):
                    applied = await self._db.get_effect(f"{run.id}:procedure_document_apply")
                    if not applied or applied.status != "completed":
                        raise RuntimeError("The approved project documents have not been applied")
                artifact_ref = f"code.storage://{project.state_repo_id}@{sha}/{path}"
                if path == MEMORY_INDEX_PATH:
                    # A section-owning procedure rewrote project memory; Luna and the memory
                    # endpoint read only the Postgres projection, so refresh it here.
                    memory_index = await self._storage.read_canonical_artifact(
                        repo_id=project.state_repo_id,
                        commit_sha=sha,
                        path=path,
                    )
                    validate_memory_index(memory_index, sources=[])
                    await self._db.refresh_project_memory_projection(
                        project_id=project.id,
                        canonical_commit_sha=sha,
                        artifact_path=path,
                        memory_index=memory_index.decode("utf-8"),
                    )
                    await self._db.add_activity(
                        run_id=run_id,
                        event_type="project_memory_updated",
                        details={"artifact_ref": artifact_ref},
                        summary="Project memory was updated.",
                        audience="product",
                        dedupe_key=f"{execution_key}:project_memory_updated",
                    )
                summary = canonical.result.get("summary")
                details: dict[str, object] = {"artifact_ref": artifact_ref}
                external_url = canonical.result.get("external_url")
                if isinstance(external_url, str) and external_url.startswith("https://"):
                    details["external_url"] = external_url
                repository = canonical.result.get("repository")
                if isinstance(repository, str):
                    details["repository"] = repository
                await self._db.complete_procedure_projection(
                    conn,
                    run_id=run_id,
                    execution_key=execution_key,
                    canonical_commit_sha=sha,
                    artifact_ref=artifact_ref,
                    artifact_path=path,
                    details=details,
                    summary=(
                        str(summary)[:1000]
                        if isinstance(summary, str) and summary.strip()
                        else "The procedure result is ready."
                    ),
                )
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="project_codex_procedure_failure")
    async def project_codex_procedure_failure(self, payload: dict[str, str]) -> None:
        run_id = UUID(payload["run_id"])
        run = await self._db.get_run(run_id)
        reason = failure_message(payload, default="Codex procedure failed", restarted=False)
        for step in (
            "procedure_sandbox_create",
            "procedure_artifact_persist",
            "procedure_canonical_commit",
            "procedure_projection",
        ):
            receipt = await self._db.get_effect(f"{run_id}:{step}")
            if receipt is not None and receipt.status == "failed" and receipt.error_message:
                reason = receipt.error_message
                break
        reason = await self._fail_run(run_id, {"reason": reason})
        await self._db.finalize_pending_test_identity(
            run_id=run_id,
            status="failed",
            note=f"The run failed before the account was proven: {reason}",
        )
        await self._db.release_lease(run_id)
        if run is not None and run.sandbox_id is not None:
            await self._sandboxes.kill(run.sandbox_id)

    @activity.defn(name="deliver_content_draft")
    async def deliver_content_draft(self, run_id_text: str) -> None:
        from tin_lite import content_repository_delivery
        from tin_lite.content_delivery import ContentDelivery

        run = await self._require_run(UUID(run_id_text))
        if run.workflow_id == content_repository_delivery.WORKFLOW_ID:
            # Normal successful procedures already delivered their PR. Only the
            # member-requested retry operation reconciles a terminal failed attempt.
            if run.status == RunStatus.SUCCEEDED:
                return
            await self._await_with_heartbeats(
                content_repository_delivery.recover_delivery(
                    database=self._db,
                    storage=self._storage,
                    integrations=self._integrations,
                    run=run,
                ),
                details={"stage": "content_delivery_recovery"},
            )
            return
        await self._await_with_heartbeats(
            ContentDelivery(
                database=self._db, storage=self._storage, integrations=self._integrations
            ).deliver(UUID(run_id_text)),
            details={"stage": "content_delivery"},
        )

    @activity.defn(name="run_project_task_turn")
    async def run_project_task_turn(self, payload: dict[str, str]) -> str:
        run_id = UUID(str(payload["run_id"]))
        turn_number = int(payload["turn_number"])
        execution_key = f"{run_id}:task_turn:{turn_number}"
        operation = "project_task_turn"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                if existing.result is None:
                    raise RuntimeError("completed task turn has no result")
                return str(existing.result["outcome"])
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            sandbox_id: str | None = None
            try:
                run = await self._require_run(run_id)
                if run.executor != PROJECT_TASK_WORKFLOW_NAME:
                    raise RuntimeError("run is not a project task")
                if run.task_turn_number >= turn_number:
                    raise RuntimeError("task turn projection exists without a completed receipt")
                from tin_lite import codex_api, task_api
                from tin_lite.procedures import SandboxProfile

                if run.task_control in {"pause", "stop"}:
                    outcome = "paused" if run.task_control == "pause" else "stopped"
                    await self._db.project_task_outcome(
                        run_id=run_id,
                        outcome=outcome,
                        summary="Saved the current task checkpoint.",
                        message=(
                            "The task is paused at a saved checkpoint."
                            if outcome == "paused"
                            else "The task was stopped. No changes were applied."
                        ),
                        execution_key=execution_key,
                    )
                    await self._db.complete_effect(
                        conn, execution_key=execution_key, result={"outcome": outcome}
                    )
                    return outcome

                auth = await task_api.auth_contract(
                    self._db, conn, run, self._settings, legacy_turn=existing is not None
                )
                codex_api.execution_profile(
                    SandboxProfile(timeout_seconds=self._settings.sandbox_timeout_seconds), auth
                )
                recovered = await task_api.recovered_turn(self._db, conn, run, turn_number)

                project = await self._require_project(run.project_id)
                expected_head_sha = run.expected_head_sha
                if expected_head_sha is None:
                    repo = await self._storage.get_repo(project.state_repo_id)
                    expected_head_sha = await self._storage.head_sha(repo, project.canonical_branch)
                if expected_head_sha is None:
                    raise RuntimeError("project state repository has no canonical head")
                ephemeral_branch = run.ephemeral_branch or f"tasks/{run.id}/{run.generation}"
                if recovered is None:
                    sandbox_id = await self._sandboxes.create(
                        execution_key=f"{execution_key}:sandbox_create",
                        run_id=str(run_id),
                        **(
                            {
                                "profile": codex_api.execution_profile(
                                    SandboxProfile(
                                        timeout_seconds=self._settings.sandbox_timeout_seconds
                                    ),
                                    auth,
                                )
                            }
                        ),
                    )
                    run = await self._db.attach_sandbox(
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        lease_owner=f"{run_id}:project_task",
                        expected_head_sha=expected_head_sha,
                        ephemeral_branch=ephemeral_branch,
                        require_active=True,
                    )
                    await self._db.set_task_running(run_id=run_id, sandbox_id=sandbox_id)
                remotes = self._storage.sandbox_remotes(
                    repo_id=project.state_repo_id,
                    branch=ephemeral_branch,
                    subject=f"run:{run.id}:turn:{turn_number}",
                )
                public_host = urlsplit(self._settings.switchboard_public_url).hostname
                if not public_host:
                    raise RuntimeError("TIN_LITE_PUBLIC_URL must include a hostname")
                proxy_url = (
                    self._settings.forward_proxy_url.get_secret_value()
                    if self._settings.forward_proxy_url
                    else None
                )
                if public_host not in {"127.0.0.1", "localhost"} and proxy_url is None:
                    raise RuntimeError("TIN_LITE_PROXY_URL is required outside local development")
                entries = await self._db.list_task_entries(run_id=run_id)
                context_delivery_ids = {
                    item.id
                    for item in entries
                    if item.source == "founder"
                    and item.kind in {"direction", "answer"}
                    and item.delivered_at is None
                }
                transcript = [
                    {"source": item.source, "kind": item.kind, "content": item.content}
                    for item in entries[-50:]
                    if item.kind != "event"
                ]
                instruction = str((run.input or {}).get("instruction", "")).strip()
                activity_attempt = activity.info().attempt
                attempt_message = (
                    "Preparing an isolated project workspace."
                    if activity_attempt == 1
                    else "Retrying this turn in a fresh isolated project workspace."
                )
                await self._db.append_task_event(
                    run_id=run_id,
                    entry_id=uuid5(
                        NAMESPACE_URL,
                        f"tin-lite:{run_id}:turn:{turn_number}:attempt:{activity_attempt}:prepare",
                    ),
                    content=attempt_message,
                )

                async def record_task_event(event: SandboxTaskEvent) -> None:
                    try:
                        await self._db.append_task_event(
                            run_id=run_id,
                            entry_id=uuid5(
                                NAMESPACE_URL,
                                (
                                    f"tin-lite:{run_id}:turn:{turn_number}:"
                                    f"attempt:{activity_attempt}:event:{event.sequence}:{event.kind}"
                                ),
                            ),
                            content=event.message,
                        )
                    except Exception:
                        activity.logger.warning(
                            "project task progress projection failed",
                            extra={"run_id": str(run_id), "event_kind": event.kind},
                            exc_info=True,
                        )

                task_input = SandboxTaskInput(
                    execution_key=execution_key,
                    canonical_url=remotes.canonical_url,
                    canonical_auth_header=remotes.canonical_auth_header,
                    canonical_branch=project.canonical_branch,
                    ephemeral_url=remotes.ephemeral_url,
                    ephemeral_auth_header=remotes.ephemeral_auth_header,
                    ephemeral_branch=ephemeral_branch,
                    proxy_url=proxy_url,
                    no_proxy=public_host,
                    run_id=str(run_id),
                    context={"instruction": instruction, "transcript": transcript},
                    context_delivery_ids=tuple(str(value) for value in context_delivery_ids),
                    isolated=True,
                    timeout_seconds=self._settings.sandbox_timeout_seconds,
                    api_url=(
                        f"{self._settings.switchboard_public_url.rstrip('/')}/internal/codex-api/{run_id}/v1"
                    ),
                    rollout_sink=self._rollout_sink(
                        run_id=run_id,
                        generation=run.generation,
                        execution_key=execution_key,
                        stage="project_task",
                        sandbox_id=sandbox_id,
                    ),
                )

                async def call_task(supplied):
                    return await self._sandboxes.run_task_and_kill(
                        sandbox_id=sandbox_id, run_input=supplied, on_event=record_task_event
                    )

                task_result = recovered or await self._await_with_heartbeats(
                    codex_api.run_api_attempt(
                        db=self._db,
                        conn=conn,
                        run=run,
                        sandbox_id=sandbox_id,
                        run_input=task_input,
                        call=call_task,
                        turn_number=turn_number,
                    ),
                    details={"stage": "project_task", "turn": str(turn_number)},
                )
                task_diff = None
                if task_result.has_changes and task_result.outcome != "stopped":
                    task_diff, _ = await self._storage.get_task_branch_diff(
                        repo_id=project.state_repo_id,
                        branch=ephemeral_branch,
                        base_branch=project.canonical_branch,
                        expected_base_sha=expected_head_sha,
                    )
                # A recovered turn did not see messages arriving after its checkpoint.
                # Its receipt carries only the entries that were actually supplied.
                delivered_ids = {UUID(value) for value in task_result.delivered_entry_ids}
                for entry_id in delivered_ids:
                    await self._db.mark_task_entry_delivered(entry_id=entry_id, run_id=run_id)
                pending_directions = await self._db.undelivered_task_directions(run_id=run_id)
                projected_outcome = task_result.outcome
                if pending_directions and projected_outcome not in {"paused", "stopped"}:
                    projected_outcome = "continue"
                await self._db.project_task_outcome(
                    run_id=run_id,
                    outcome=projected_outcome,
                    summary=task_result.summary or "Codex finished the task turn.",
                    message=(
                        task_result.message
                        or task_result.summary
                        or "Codex finished the task turn."
                    ),
                    question=task_result.question,
                    task_diff=task_diff,
                    has_changes=task_result.has_changes,
                    execution_key=execution_key,
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={"outcome": projected_outcome},
                )
                await self._db.add_activity(
                    run_id=run_id,
                    event_type="sandbox_killed",
                    details={"stage": "project_task"},
                    dedupe_key=f"{execution_key}:sandbox_killed",
                )
                return projected_outcome
            except Exception as exc:
                if sandbox_id is not None:
                    with suppress(Exception):
                        await self._sandboxes.kill(sandbox_id)
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    @activity.defn(name="apply_project_task_changes")
    async def apply_project_task_changes(self, run_id_text: str) -> str:
        run_id = UUID(run_id_text)
        execution_key = f"{run_id}:task_apply"
        operation = "project_task_apply"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                return "applied"
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                run = await self._require_run(run_id)
                project = await self._require_project(run.project_id)
                self._require_active_lease(run)
                if (
                    run.lease_owner is None
                    or run.sandbox_id is None
                    or run.ephemeral_branch is None
                    or run.expected_head_sha is None
                    or run.task_diff is None
                ):
                    raise RuntimeError("task review has no complete fenced diff")
                if not await self._db.validate_lease(
                    project_id=run.project_id,
                    thread_id=run.thread_id,
                    generation=run.generation,
                    lease_owner=run.lease_owner,
                    fencing_token=run.fencing_token,
                    sandbox_id=run.sandbox_id,
                ):
                    raise StaleGenerationError("task generation no longer owns the session")
                async with self._db.project_state_lock(conn, project.id):
                    raw_diff = reviewed_task_diff(run.task_diff)
                    expected_diff_sha = str(run.task_diff["sha256"])
                    repo = await self._storage.get_repo(project.state_repo_id)
                    current_head_sha = await self._storage.head_sha(repo, project.canonical_branch)
                    if current_head_sha is None:
                        raise RuntimeError("project state repository has no canonical head")
                    canonical_sha = await self._storage.apply_task_diff(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        expected_head_sha=current_head_sha,
                        raw_diff=raw_diff,
                        expected_diff_sha256=expected_diff_sha,
                        execution_key=execution_key,
                        run_id=str(run_id),
                    )
                await self._db.complete_task_approval(
                    run_id=run_id, canonical_commit_sha=canonical_sha
                )
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result={"canonical_commit_sha": canonical_sha},
                )
                return "applied"
            except Exception as exc:
                await self._db.fail_effect(
                    conn, execution_key=execution_key, error_message=_safe_failure(exc)
                )
                await self._db.defer_task_approval(
                    run_id=run_id,
                    summary=(
                        "Tin could not apply the reviewed changes to the latest project state. "
                        "The exact proposal remains isolated and can be reviewed again."
                    ),
                )
                return "retry"

    @activity.defn(name="record_project_task_stop")
    async def record_project_task_stop(self, run_id_text: str) -> None:
        await self._db.project_task_outcome(
            run_id=UUID(run_id_text),
            outcome="stopped",
            summary="Stopped by the founder.",
            message="The task was stopped. No changes were applied.",
        )

    @activity.defn(name="project_task_failure")
    async def project_task_failure(self, payload: dict[str, str]) -> None:
        run_id = UUID(payload["run_id"])
        run = await self._db.get_run(run_id)
        if run is not None and run.sandbox_id is not None:
            await self._sandboxes.kill(run.sandbox_id)
        await self._fail_run(run_id, payload, default="project task failed")
        await self._db.release_lease(run_id)

    async def _weekly_brief_effect(
        self,
        *,
        run_id: UUID,
        execute: Callable[[], Awaitable[dict[str, object]]],
    ) -> dict:
        execution_key = f"{run_id}:weekly_brief:model"
        operation = "weekly_brief_model"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                if existing.result is None:
                    raise RuntimeError("completed weekly brief effect has no result")
                return existing.result
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                with external_usage_scope(self._db, conn, run_id, "weekly_brief"):
                    result = await execute()
                await self._db.complete_effect(conn, execution_key=execution_key, result=result)
                return result
            except Exception as exc:
                await self._db.fail_effect(
                    conn, execution_key=execution_key, error_message=_safe_failure(exc)
                )
                raise

    async def _pinned_native_reporter(self, run, reporter, key):
        # Test doubles and separately supplied implementations retain their own contracts.
        if not isinstance(
            reporter,
            (
                AnswerPageDrafter,
                VisibilityAuditor,
                WeeklyBriefReporter,
                MemoryGardener,
                ScanReporter,
            ),
        ):
            return reporter
        from tin_lite.native_skill_pins import pinned_suite, suite_for_workflow

        if run.definition_commit_sha:
            definition = json.loads(
                await self._storage.read_canonical_artifact(
                    repo_id="registry/workflows",
                    commit_sha=run.definition_commit_sha,
                    path=f"workflows/{key}.json",
                )
            )
            suite = pinned_suite(definition, key)
        else:
            suite = suite_for_workflow(key, legacy=True)
        return type(reporter)(responses=reporter._responses, skill_suite=suite)

    async def _integration_evidence(self, project):
        from tin_lite.workflow_evidence import integration_inventory

        return json.dumps(await integration_inventory(self._db, project.id), sort_keys=True)

    async def _weekly_brief_sources(
        self,
        *,
        run_id: UUID,
        project,
        period_start: datetime,
        period_end: datetime,
    ) -> list[WeeklyBriefSource]:
        sources: list[WeeklyBriefSource] = []
        remaining = 220_000

        def append(label: str, artifact_ref: str, content: str) -> None:
            nonlocal remaining
            if remaining <= 0:
                return
            bounded = content.encode()[: min(16_000, remaining)].decode("utf-8", errors="ignore")
            if not bounded.strip():
                return
            sources.append(
                WeeklyBriefSource(label=label, artifact_ref=artifact_ref, content=bounded)
            )
            remaining -= len(bounded.encode())

        append(
            "current integration availability",
            f"tin.project://{project.id}/integrations",
            await self._integration_evidence(project),
        )

        list_files = getattr(self._storage, "list_canonical_files", None)
        if list_files is not None and getattr(project, "canonical_branch", None):
            paths, revision = await list_files(
                repo_id=project.state_repo_id, branch=project.canonical_branch
            )
            context_paths = [
                path for path in paths if path.startswith("context/") and path.endswith(".md")
            ][:3]
            for path in context_paths:
                content = await self._storage.read_canonical_artifact(
                    repo_id=project.state_repo_id, commit_sha=revision, path=path
                )
                append(
                    "founder and project context",
                    f"code.storage://{project.state_repo_id}@{revision}/{path}",
                    content[:8000].decode("utf-8", errors="ignore"),
                )

        if project.memory_commit_sha is not None and project.memory_index_path is not None:
            content = await self._storage.read_canonical_artifact(
                repo_id=project.state_repo_id,
                commit_sha=project.memory_commit_sha,
                path=project.memory_index_path,
            )
            append(
                "project memory",
                (
                    f"code.storage://{project.state_repo_id}"
                    f"@{project.memory_commit_sha}/{project.memory_index_path}"
                ),
                content.decode("utf-8"),
            )

        runs = await self._db.list_runs_for_period(
            project_id=project.id,
            period_start=period_start,
            period_end=period_end,
            exclude_run_id=run_id,
        )
        # Read decision evidence before general activity can exhaust the context budget.
        runs = sorted(
            runs, key=lambda r: 0 if (r.artifact_path or "").startswith("reports/analytics/") else 1
        )
        for source_run in runs:
            ref = source_run.artifact_ref or f"tin.run://{source_run.id}"
            if source_run.canonical_commit_sha is not None and source_run.artifact_path is not None:
                content = await self._storage.read_canonical_artifact(
                    repo_id=project.state_repo_id,
                    commit_sha=source_run.canonical_commit_sha,
                    path=source_run.artifact_path,
                )
                body = content.decode("utf-8")
            else:
                body = json.dumps(
                    {
                        "workflow": source_run.executor,
                        "status": source_run.status.value,
                        "summary": source_run.task_summary,
                        "result": source_run.task_result,
                        "error": source_run.error_message,
                    },
                    ensure_ascii=False,
                )
            append(source_run.executor, ref, body)

        events = await self._db.list_product_activity_for_period(
            project_id=project.id,
            period_start=period_start,
            period_end=period_end,
            exclude_run_id=run_id,
        )
        for event in events:
            append(
                event.workflow_title or event.event_type,
                f"tin.activity://{project.id}/{event.id}",
                json.dumps(
                    {
                        "occurred_at": event.created_at.isoformat(),
                        "event": event.event_type,
                        "summary": event.summary,
                        "details": event.details,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        return sources

    async def _answer_page_effect(
        self,
        *,
        run_id: UUID,
        execute: Callable[[], Awaitable[dict[str, object]]],
    ) -> dict:
        execution_key = f"{run_id}:answer_page:model"
        operation = "answer_page_model"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                if existing.result is None:
                    raise RuntimeError("completed answer-page effect has no result")
                return existing.result
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)
            try:
                with external_usage_scope(self._db, conn, run_id, "answer_page"):
                    result = await execute()
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result=result,
                )
                return result
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                raise

    async def _answer_page_sources(self, *, run_id: UUID, project) -> list[AnswerPageSource]:
        sources: list[AnswerPageSource] = []
        if project.memory_commit_sha is not None and project.memory_index_path is not None:
            content = await self._storage.read_canonical_artifact(
                repo_id=project.state_repo_id,
                commit_sha=project.memory_commit_sha,
                path=project.memory_index_path,
            )
            sources.append(
                AnswerPageSource(
                    label="project memory",
                    artifact_ref=(
                        f"code.storage://{project.state_repo_id}"
                        f"@{project.memory_commit_sha}/{project.memory_index_path}"
                    ),
                    content=content.decode("utf-8"),
                )
            )

        source_runs = await self._db.list_memory_source_runs(
            project_id=project.id,
            exclude_run_id=run_id,
        )
        visibility_runs = [
            source_run
            for source_run in source_runs
            if source_run.executor == VISIBILITY_AUDIT_WORKFLOW_NAME
        ]
        selected_runs = (
            visibility_runs[-1:] if visibility_runs else ([] if sources else source_runs[-5:])
        )
        # Availability is useful context, but cannot replace durable project evidence.
        sources.insert(
            0,
            AnswerPageSource(
                label="current integration availability",
                artifact_ref=f"tin.project://{project.id}/integrations",
                content=await self._integration_evidence(project),
            ),
        )
        for source_run in selected_runs:
            if (
                source_run.canonical_commit_sha is None
                or source_run.artifact_path is None
                or source_run.artifact_ref is None
            ):
                raise RuntimeError("answer-page source run has no durable artifact")
            content = await self._storage.read_canonical_artifact(
                repo_id=project.state_repo_id,
                commit_sha=source_run.canonical_commit_sha,
                path=source_run.artifact_path,
            )
            sources.append(
                AnswerPageSource(
                    label=(
                        "latest AI visibility audit"
                        if source_run.executor == VISIBILITY_AUDIT_WORKFLOW_NAME
                        else source_run.executor
                    ),
                    artifact_ref=source_run.artifact_ref,
                    content=content.decode("utf-8"),
                )
            )
        return sources

    async def _visibility_effect(
        self,
        *,
        run_id: UUID,
        step_id: str,
        operation: str,
        execute: Callable[[ResponseCheckpoint], Awaitable[dict[str, object]]],
    ) -> dict:
        execution_key = f"{run_id}:visibility:{step_id}"
        async with self._db.effect_lock(execution_key, operation) as (conn, existing):
            if existing is not None and existing.status == "completed":
                if existing.result is None:
                    raise RuntimeError("completed visibility effect has no result")
                return existing.result
            await self._db.start_effect(conn, execution_key=execution_key, operation=operation)

            async def checkpoint(request: ResponseRequest) -> dict:
                # This child receipt is serialized by the owning effect's lock and
                # reuses its connection. Accounting receipts remain metadata-only.
                response_key = f"{execution_key}:response"
                saved = await self._db.get_effect(response_key, conn=conn)
                if saved is not None:
                    if saved.status != "completed":
                        raise VisibilityRecoveryError(
                            "visibility model response could not be recovered; "
                            "the request was not repeated"
                        )
                    return read_visibility_response_checkpoint(saved.result)
                if (
                    await self._db.get_effect(
                        observation_key(run_id, f"visibility:{step_id}", "responses"), conn=conn
                    )
                    is not None
                ):
                    raise VisibilityRecoveryError(
                        "visibility model request was already attempted without a recoverable "
                        "response; the request was not repeated"
                    )
                # The provider's existing usage observation records dispatch intent.
                # A billing rejection before that intent must remain retryable as
                # an admission failure, not become an uncertain paid attempt here.
                response = await request()
                result = visibility_response_checkpoint(response)
                await self._db.start_effect(
                    conn, execution_key=response_key, operation="visibility_response_v1"
                )
                await self._db.complete_effect(conn, execution_key=response_key, result=result)
                return read_visibility_response_checkpoint(result)

            try:
                with external_usage_scope(self._db, conn, run_id, f"visibility:{step_id}"):
                    result = await execute(checkpoint)
                await self._db.complete_effect(
                    conn,
                    execution_key=execution_key,
                    result=result,
                )
                return result
            except Exception as exc:
                await self._db.fail_effect(
                    conn,
                    execution_key=execution_key,
                    error_message=_safe_failure(exc),
                )
                if isinstance(exc, (VisibilityProtocolError, VisibilityRecoveryError)):
                    raise ApplicationError(
                        str(exc), type=type(exc).__name__, non_retryable=True
                    ) from exc
                raise

    async def _visibility_sources(self, *, run_id: UUID, project) -> list[VisibilitySource]:
        if project.memory_commit_sha is not None and project.memory_index_path is not None:
            content = await self._storage.read_canonical_artifact(
                repo_id=project.state_repo_id,
                commit_sha=project.memory_commit_sha,
                path=project.memory_index_path,
            )
            return [
                VisibilitySource(
                    label="current integration availability",
                    artifact_ref=f"tin.project://{project.id}/integrations",
                    content=await self._integration_evidence(project),
                ),
                VisibilitySource(
                    label="project memory",
                    artifact_ref=(
                        f"code.storage://{project.state_repo_id}"
                        f"@{project.memory_commit_sha}/{project.memory_index_path}"
                    ),
                    content=content.decode("utf-8"),
                ),
            ]
        sources: list[VisibilitySource] = [
            VisibilitySource(
                label="current integration availability",
                artifact_ref=f"tin.project://{project.id}/integrations",
                content=await self._integration_evidence(project),
            )
        ]
        for source_run in await self._db.list_memory_source_runs(
            project_id=project.id,
            exclude_run_id=run_id,
        ):
            if source_run.executor == VISIBILITY_AUDIT_WORKFLOW_NAME:
                continue
            if (
                source_run.canonical_commit_sha is None
                or source_run.artifact_path is None
                or source_run.artifact_ref is None
            ):
                raise RuntimeError("visibility source run has no durable artifact")
            content = await self._storage.read_canonical_artifact(
                repo_id=project.state_repo_id,
                commit_sha=source_run.canonical_commit_sha,
                path=source_run.artifact_path,
            )
            sources.append(
                VisibilitySource(
                    label=source_run.executor,
                    artifact_ref=source_run.artifact_ref,
                    content=content.decode("utf-8"),
                )
            )
        return sources

    async def _require_run(self, run_id: UUID):
        run = await self._db.get_run(run_id)
        if run is None:
            raise LookupError(f"run {run_id} does not exist")
        return run

    def _require_integrations(self) -> IntegrationService:
        if self._integrations is None:
            raise RuntimeError("integration service is not configured")
        return self._integrations

    async def _connected_workspace(
        self, *, project_id: UUID, capabilities: tuple[str, ...]
    ) -> IntegrationConnection:
        connection = await self._db.get_integration_connection(
            project_id=project_id,
            provider_key=GOOGLE_WORKSPACE_PROVIDER,
        )
        if (
            connection is None
            or connection.status != "connected"
            or not connection.external_account_id
        ):
            raise RuntimeError("Google Workspace is not connected")
        granted_capabilities = connection.configuration.get("granted_capabilities", [])
        if not isinstance(granted_capabilities, list) or not set(capabilities).issubset(
            granted_capabilities
        ):
            raise RuntimeError("Google Workspace does not grant the procedure's capabilities")
        return connection

    async def _resolve_test_identity(
        self, *, run: WorkflowRun, procedure: PinnedCodexProcedure
    ) -> tuple[ProjectTestIdentity, str]:
        """Bind the run's product account once and say whether it was created or reused.

        A retried attempt returns the same identity. A procedure that declares
        `identity.reuse: active` first binds the project's newest active account on the run's
        product host, minted by an earlier run, and only mints a new one when none exists and
        `identity.create` allows it.
        """
        existing = await self._db.get_test_identity_for_run(run_id=run.id)
        if existing is not None:
            mode = await self._db.get_test_identity_use_mode(run_id=run.id)
            return existing, mode or "created"
        product_url = (run.input or {}).get("product_url")
        try:
            host = artifact_host(product_url)
        except ValueError as exc:
            raise RuntimeError("test identities require a product URL with a hostname") from exc
        if procedure.identity.reuse == IDENTITY_REUSE_ACTIVE:
            reusable = await self._db.find_reusable_test_identity(
                project_id=run.project_id, target_host=host
            )
            if reusable is not None:
                bound = await self._db.bind_test_identity_to_run(
                    run_id=run.id, identity_id=reusable.id, project_id=run.project_id
                )
                await self._db.add_activity(
                    run_id=run.id,
                    event_type="test_identity_reused",
                    details={
                        "identity_id": str(bound.id),
                        "created_by_run_id": str(bound.created_by_run_id),
                        "target_host": host,
                    },
                    dedupe_key=f"{run.id}:test_identity_reused",
                )
                return bound, "reused"
        if not procedure.identity.create:
            raise RuntimeError("no active test identity exists for this product host")
        return await self._ensure_test_identity(run=run, procedure=procedure, host=host), "created"

    async def _ensure_test_identity(
        self, *, run: WorkflowRun, procedure: PinnedCodexProcedure, host: str | None = None
    ) -> ProjectTestIdentity:
        """Mint the run's product account once; retries reuse the same alias and password."""
        existing = await self._db.get_test_identity_for_run(run_id=run.id)
        if existing is not None:
            return existing
        connection = await self._connected_workspace(
            project_id=run.project_id, capabilities=("gmail.messages.read",)
        )
        mailbox = connection.configuration.get("email")
        if not isinstance(mailbox, str) or mailbox.count("@") != 1:
            raise RuntimeError("Google Workspace connection has no mailbox address")
        if host is None:
            product_url = (run.input or {}).get("product_url")
            host = urlsplit(str(product_url)).hostname if isinstance(product_url, str) else None
        if not host:
            raise RuntimeError("test identities require a product URL with a hostname")
        identity_id = uuid4()
        local, _, domain = mailbox.partition("@")
        alias = f"{local}+tin-{identity_id.hex[:8]}@{domain}"
        password = secrets.token_urlsafe(18)
        ciphertext, key_version = self._require_integrations().seal_test_identity_password(
            project_id=run.project_id, identity_id=identity_id, password=password
        )
        return await self._db.create_test_identity(
            identity_id=identity_id,
            project_id=run.project_id,
            run_id=run.id,
            target_host=host.casefold(),
            label=f"{procedure.workflow_key} {run.id.hex[:8]}",
            email=alias,
            auth_kind="password",
            password_ciphertext=ciphertext,
            credential_key_version=key_version,
            phone_number=getattr(self._settings, "test_phone_number", None),
        )

    async def _procedure_artifact_base(
        self, *, run: WorkflowRun, procedure: PinnedCodexProcedure
    ) -> bytes | None:
        """The canonical content a section-owning procedure must keep outside its section."""
        if procedure.output_section is None or procedure.output_path is None:
            return None
        if run.expected_head_sha is None:
            raise RuntimeError("procedure run has no pinned canonical head")
        project = await self._require_project(run.project_id)
        return await self._storage.read_canonical_artifact_if_exists(
            repo_id=project.state_repo_id,
            commit_sha=run.expected_head_sha,
            path=procedure.output_path,
        )

    async def _load_run_card(self, run_id, procedure):
        if procedure.workflow_key != QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME:
            return None
        from tin_lite.run_payment_card import load

        return await load(self._db, self._require_integrations(), run_id=run_id)

    async def _reject_card_leak(self, *, run_id, procedure, content):
        from tin_lite.payment_card_guard import reject_card_leak

        reject_card_leak(content, await self._load_run_card(run_id, procedure))

    async def _reject_identity_leak(self, *, run_id: UUID, content: bytes) -> None:
        identity = await self._db.get_test_identity_for_run(run_id=run_id)
        if identity is None:
            return
        password = self._require_integrations().open_test_identity_password(identity)
        if password and password.encode("utf-8") in content:
            raise RuntimeError("procedure artifact contains the test identity secret")

    async def _check_private_attempt(self, run, workflow):
        if workflow.project_id is None:
            return
        from tin_lite.private_workflows import require_private_execution

        require_private_execution(self._settings, workflow, run.project_id)
        if not run.started_by_clerk_user_id or not await self._db.has_project_access(
            project_id=run.project_id, clerk_user_id=run.started_by_clerk_user_id
        ):
            raise LookupError(
                "The private workflow's initiating member no longer has project access"
            )

    async def _pinned_codex_procedure(self, run_id: UUID) -> tuple[Workflow, PinnedCodexProcedure]:
        run = await self._require_run(run_id)
        if run.executor != CODEX_PROCEDURE_EXECUTOR:
            raise RuntimeError("run is not a Codex procedure")
        if run.definition_commit_sha is None:
            raise RuntimeError("Codex procedure run did not pin a definition commit")
        workflow_definition = await self._db.get_workflow(run.workflow_id)
        if workflow_definition is None:
            raise RuntimeError("Codex procedure workflow definition is unavailable")
        if workflow_definition.project_id is not None:
            from tin_lite.private_workflows import validate_private_definition
            from tin_lite.workflow_packages import load_workflow_source

            project = await self._require_project(run.project_id)
            if (
                workflow_definition.project_id != run.project_id
                or workflow_definition.definition_repo_id != project.state_repo_id
            ):
                raise RuntimeError("private procedure source does not belong to its project")
            source = await load_workflow_source(
                storage=self._storage,
                repo_id=project.state_repo_id,
                commit_sha=run.definition_commit_sha,
                definition_path=workflow_definition.definition_path,
            )
            validate_private_definition(source.definition)
            workflow_definition = replace(
                workflow_definition,
                definition=source.definition,
                current_commit_sha=run.definition_commit_sha,
            )
        procedure = await load_pinned_codex_procedure(
            storage=self._storage,
            repo_id=workflow_definition.definition_repo_id,
            commit_sha=run.definition_commit_sha,
            definition_path=workflow_definition.definition_path,
        )
        procedure = procedure.resolve_inputs(
            run.input or {}, started_at=run.created_at, run_id=run.id
        )
        from tin_lite import content_draft

        if procedure.output_validator in content_draft.VALIDATORS:
            from tin_lite.content_draft_sources import ContentDraftSources

            prepared = await ContentDraftSources(database=self._db, storage=self._storage).saved(
                run.id
            )
            procedure = replace(procedure, content_draft_context=prepared)
        if procedure.output_validator == "brand-design-capture.v1":
            from tin_lite.brand_capture import BrandCaptureSources

            prepared = await BrandCaptureSources(database=self._db, storage=self._storage).saved(
                run.id
            )
            procedure = replace(procedure, brand_capture_context=prepared)
        if procedure.output_validator == TIN_DIAGRAM_BRANDED_VALIDATOR and run.expected_head_sha:
            from tin_lite.brand_diagrams import prepare

            context = await prepare(
                self._storage, await self._require_project(run.project_id), run.expected_head_sha
            )
            procedure = replace(procedure, diagram_brand_context=context)
        if run.review_source_run_id is not None:
            from tin_lite.workflow_reviews import saved_revision_context

            revision = await saved_revision_context(self._db, self._storage, run)
            if revision is None:
                raise RuntimeError("The revision's pinned feedback context is missing.")
            procedure = replace(procedure, review_revision_context=revision)
        if procedure.workflow_key != workflow_definition.key:
            raise RuntimeError("pinned Codex procedure key does not match its catalog row")
        return workflow_definition, procedure

    async def _pinned_site_health_model_route(self, run_id: UUID) -> str:
        run = await self._require_run(run_id)
        if run.executor != SITE_HEALTH_WORKFLOW_NAME or run.definition_commit_sha is None:
            raise RuntimeError("site-health run did not pin its definition")
        workflow_definition = await self._db.get_workflow(run.workflow_id)
        if workflow_definition is None:
            raise RuntimeError("site-health workflow definition is unavailable")
        if workflow_definition.current_commit_sha == run.definition_commit_sha:
            definition = workflow_definition.definition
        else:
            raw = await self._storage.read_canonical_artifact(
                repo_id=workflow_definition.definition_repo_id,
                commit_sha=run.definition_commit_sha,
                path=workflow_definition.definition_path,
            )
            try:
                definition = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("pinned site-health definition is invalid") from exc
        if (
            not isinstance(definition, dict)
            or definition.get("key") != SITE_HEALTH_WORKFLOW_NAME
            or definition.get("executor") != SITE_HEALTH_WORKFLOW_NAME
        ):
            raise RuntimeError("pinned site-health definition does not match its executor")
        return validate_site_health_model_route(definition)

    async def _require_project(self, project_id: UUID):
        project = await self._db.get_project(project_id)
        if project is None:
            raise LookupError(f"project {project_id} does not exist")
        return project

    @staticmethod
    def _require_active_lease(run) -> None:
        if not run.lease_active:
            raise StaleGenerationError("run generation does not own the active lease")


_SECRET_IN_TEXT = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|password|authorization)=)[^\s&\"']+"
    r"|\b(?:sk|rk|re|ghp|gho|ghu|pat|xoxb|xoxp)_[A-Za-z0-9_-]{8,}\b"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)


def scrub_secrets(text: str) -> str:
    """Mask credentials an exception text may carry (headers, query strings, key prefixes, JWTs)."""

    def mask(match: re.Match[str]) -> str:
        lead = match.group(1) or ""
        return f"{lead}[redacted]"

    return _SECRET_IN_TEXT.sub(mask, text)


def _safe_failure(exc: BaseException) -> str:
    """Name the exception with its text, so a failed receipt says what actually went wrong."""
    text = scrub_secrets(" ".join(str(exc).split()))
    return f"{type(exc).__name__}: {text or 'operation failed'}"[:FAILURE_MESSAGE_LIMIT]
