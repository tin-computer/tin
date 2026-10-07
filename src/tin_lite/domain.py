from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

WORKFLOW_NAME = "content.design_md"
PROJECT_MEMORY_WORKFLOW_NAME = "project.memory"
SCAN_REPORT_WORKFLOW_NAME = "scan.report"
SITE_HEALTH_WORKFLOW_NAME = "site.health_improve"
VISIBILITY_AUDIT_WORKFLOW_NAME = "visibility.audit"
ANSWER_PAGE_WORKFLOW_NAME = "content.answer_page"
RESEARCH_DEEP_DIVE_WORKFLOW_NAME = "research.deep_dive"
PUBLIC_ARTICLE_WORKFLOW_NAME = "content.public_article"
CONTENT_DIAGRAM_WORKFLOW_NAME = "content.diagram"
EMAIL_SHORTLIST_WORKFLOW_NAME = "outreach.email_shortlist"
EMAIL_CAMPAIGN_WORKFLOW_NAME = "outreach.email_campaign"
WEEKLY_BRIEF_WORKFLOW_NAME = "project.weekly_brief"
PROJECT_TASK_WORKFLOW_NAME = "project.task"
QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME = "qa.signup_walkthrough"
PRODUCT_CODE_MAP_WORKFLOW_NAME = "product.code_map"
PRODUCT_DEEP_DIVE_WORKFLOW_NAME = "product.deep_dive"
QA_PRODUCT_AUDIT_WORKFLOW_NAME = "qa.product_audit"
GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME = "growth.onboarding_plan"
PAID_ADS_ASSESSMENT_WORKFLOW_NAME = "ads.assessment"
PAID_ADS_LAUNCH_WORKFLOW_NAME = "ads.launch"
PAID_ADS_MONITOR_WORKFLOW_NAME = "ads.monitor"
AWESOME_SUBMIT_WORKFLOW_NAME = "outreach.awesome_submit"
PAYMENT_RECOVERY_WORKFLOW_NAME = "revenue.payment_recovery"
# Shorter than a week, so an unanswered weekly run closes before the next one is due.
PAYMENT_RECOVERY_DECISION_DAYS = 6
CREATIVE_CHARACTER_WORKFLOW_NAME = "creative.character"
CREATIVE_PRODUCT_DEMO_WORKFLOW_NAME = "creative.product_demo"
CODEX_PROCEDURE_EXECUTOR = "codex.procedure"
# A run admitted while a prerequisite run is still in progress carries this Temporal memo
# (the awaited run ids) and waits, with no compute, up to this many minutes for them.
PREREQUISITE_WAIT_MEMO = "tin_prerequisite_wait"
PREREQUISITE_WAIT_MINUTES = 30
ARTIFACT_PATH = "DESIGN.md"
MEMORY_INDEX_PATH = "wiki/INDEX.md"
SCAN_REPORT_PATH = "reports/SCAN.md"
VISIBILITY_AUDIT_PATH = "reports/AI_VISIBILITY.md"
# Answer pages drafted before per-page names all saved here; their runs still point at it.
ANSWER_PAGE_PATH = "reports/ANSWER_PAGE.md"
ANSWER_PAGE_DIR = "content/answers"
RESEARCH_DEEP_DIVE_PATH = "reports/RESEARCH_DEEP_DIVE.md"
PUBLIC_ARTICLE_PATH = "reports/PUBLIC_ARTICLE.md"
EMAIL_SHORTLIST_PATH = "outreach/email/SHORTLIST.csv"
GROWTH_ONBOARDING_PLAN_PATH = "reports/GROWTH_ONBOARDING_PLAN.md"
PAID_ADS_REPORT_DIR = "reports/paid-ads"
PAID_ADS_CAMPAIGN_DIR = "ads/google"
SIGNUP_WALKTHROUGH_PATH_TEMPLATE = "reports/qa/signup/{host}/{started_at}.md"
PRODUCT_AUDIT_PATH_TEMPLATE = "reports/qa/audit/{host}/{started_at}.md"
TEST_IDENTITY_WRITE_CAPABILITY = "test_identity.write"
TEST_IDENTITY_SMS_CAPABILITY = "test_identity.sms.read"
# The creative studio's provider is Tin's own fal account, so its run-tool grant has no
# project-owned integration connection.
STUDIO_PROVIDER = "tin.studio"
STUDIO_VOICE_CAPABILITY = "studio.voice"
TEST_IDENTITY_STATUSES = ("pending", "active", "blocked", "failed", "retired")


TriggerClient = Literal["claude_code", "codex", "api"]
"""The client that started a run, as recorded on it; MCP tools expose it as an enum."""


def site_health_report_path(run_id: UUID | str) -> str:
    return f"reports/site-health/{run_id}.md"


def weekly_brief_path(period_end: datetime) -> str:
    return f"reports/weekly/{period_end.date().isoformat()}.md"


def weekly_brief_evidence_path(run_id: UUID | str) -> str:
    return f"reports/weekly/{run_id}/evidence.json"


def visibility_evidence_path(run_id: UUID | str) -> str:
    return f"reports/visibility/{run_id}/evidence.json"


def answer_page_evidence_path(run_id: UUID | str) -> str:
    return f"reports/answer-page/{run_id}/evidence.json"


def answer_page_path(title: str, day: str, suffix: str | None = None) -> str:
    """Name each answer page by its date and question, so Files and chat can tell them apart.

    `suffix` (the start of the run ID) keeps a second page with the same question on the same
    day from replacing the first.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:80].rstrip("-")
    return f"{ANSWER_PAGE_DIR}/{day}-{slug or 'answer-page'}{f'-{suffix}' if suffix else ''}.md"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    NEEDS_INPUT = "needs_input"
    PAUSED = "paused"
    STOPPED = "stopped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class WorkflowStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class Workspace:
    id: UUID
    name: str
    created_by_clerk_user_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class Project:
    id: UUID
    name: str
    state_repo_id: str
    canonical_branch: str
    memory_commit_sha: str | None = None
    memory_index_path: str | None = None
    memory_index: str | None = None
    memory_updated_at: datetime | None = None
    timezone: str = "UTC"
    workspace_id: UUID | None = None
    workspace_name: str | None = None
    can_create_project_in_workspace: bool = False
    created_by_clerk_user_id: str | None = None
    member_count: int = 1
    deleted_at: datetime | None = None
    deleted_by_clerk_user_id: str | None = None


@dataclass(frozen=True)
class StoppedRunHandle:
    """A run stopped in product state whose Temporal execution and sandbox still need closing."""

    run_id: UUID
    temporal_workflow_id: str
    sandbox_id: str | None


@dataclass(frozen=True)
class ProjectMembership:
    project_id: UUID
    clerk_user_id: str
    created_at: datetime


@dataclass(frozen=True)
class ProjectInvitation:
    id: UUID
    project_id: UUID
    project_name: str
    email: str
    created_by_clerk_user_id: str
    accepted_by_clerk_user_id: str | None
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class ChatMessage:
    id: UUID
    project_id: UUID
    request_id: UUID
    role: str
    source: str
    content: str
    author_clerk_user_id: str | None
    response_id: str | None
    routed_workflow_key: str | None
    run_id: UUID | None
    created_at: datetime


@dataclass(frozen=True)
class ProjectTaskEntry:
    id: UUID
    run_id: UUID
    request_id: UUID | None
    kind: str
    source: str
    content: str
    author_clerk_user_id: str | None
    delivered_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class Workflow:
    id: UUID
    project_id: UUID | None
    key: str
    title: str
    description: str
    executor: str
    definition_repo_id: str
    definition_path: str
    current_commit_sha: str | None
    version_label: str
    definition: dict[str, Any]
    status: WorkflowStatus
    system_id: str | None = None
    system_name: str | None = None
    system_order: int | None = None
    forked_from_workflow_id: UUID | None = None
    forked_from_commit_sha: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ProjectWorkflow:
    id: UUID
    project_id: UUID
    workflow_id: UUID
    workflow_key: str
    workflow_title: str
    workflow_description: str
    version_label: str
    definition_commit_sha: str
    name: str
    inputs: dict[str, Any]
    input_schema: dict[str, Any]
    schedule: dict[str, Any] | None
    status: str
    temporal_schedule_id: str | None
    next_run_at: datetime | None
    last_run_id: UUID | None
    last_run_status: RunStatus | None
    last_artifact_path: str | None
    last_error: str | None
    settings_revision: int
    created_by_clerk_user_id: str
    created_at: datetime
    updated_at: datetime
    skip_scheduled_for: datetime | None = None
    last_result_summary: str | None = None
    last_artifact_title: str | None = None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    run_count: int = 0
    done_count: int = 0
    failed_count: int = 0
    typical_duration_seconds: float | None = None
    content_revision: dict[str, Any] | None = None
    # Whether today's definition draws how it runs (presentation.flow).
    workflow_drawn: bool = False


@dataclass(frozen=True)
class WorkflowRun:
    id: UUID
    project_id: UUID
    workflow_id: UUID
    executor: str
    definition_commit_sha: str | None
    temporal_workflow_id: str
    thread_id: str
    generation: int
    fencing_token: int
    status: RunStatus
    project_workflow_id: UUID | None = None
    trigger_source: str = "manual"
    scheduled_for: datetime | None = None
    review_required: bool = False
    review_root_run_id: UUID | None = None
    review_source_run_id: UUID | None = None
    review_version: int = 1
    review_decision: str | None = None
    review_requested_at: datetime | None = None
    reviewed_at: datetime | None = None
    started_by_clerk_user_id: str | None = None
    reviewed_by_clerk_user_id: str | None = None
    system_wiki_commit_sha: str | None = None
    sandbox_id: str | None = None
    lease_owner: str | None = None
    lease_active: bool = False
    lease_released_at: datetime | None = None
    ephemeral_branch: str | None = None
    expected_head_sha: str | None = None
    canonical_commit_sha: str | None = None
    artifact_path: str | None = None
    artifact_title: str | None = None
    artifact_ref: str | None = None
    retained_output: dict[str, Any] | None = None
    output_resolution: dict[str, Any] | None = None
    error_message: str | None = None
    input: dict[str, Any] | None = None
    task_title: str | None = None
    task_phase: str | None = None
    task_summary: str | None = None
    task_question: str | None = None
    task_question_requested_at: datetime | None = None
    task_turn_number: int = 0
    task_control: str | None = None
    task_result: str | None = None
    task_diff: dict[str, Any] | None = None
    task_has_changes: bool | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    trigger_client: TriggerClient | None = None
    started_by_oauth_client_id: str | None = None
    retry_of_run_id: UUID | None = None
    progress_mode: str = "indeterminate"
    progress_step: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    progress_percent: int | None = None
    progress_summary: str | None = None
    progress_updated_at: datetime | None = None
    heartbeat_at: datetime | None = None
    result_summary: str | None = None
    prerequisite_evidence: dict[str, Any] | None = None

    @property
    def workflow_name(self) -> str:
        """Compatibility name for the executor pinned to this run."""
        return self.executor


@dataclass(frozen=True)
class StudioUsage:
    lines: int
    characters: int


@dataclass(frozen=True)
class RunToolGrant:
    project_id: UUID
    run_id: UUID
    connection_id: UUID | None
    external_account_id: str
    sandbox_id: str
    provider_key: str
    capabilities: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True)
class ProjectTestIdentity:
    """A product account Tin created for its own walkthroughs; the password stays encrypted."""

    id: UUID
    project_id: UUID
    created_by_run_id: UUID
    target_host: str
    label: str
    email: str
    auth_kind: str
    username: str | None
    password_ciphertext: bytes | None
    credential_key_version: str | None
    status: str
    status_note: str | None
    verified_at: datetime | None
    last_used_run_id: UUID | None
    last_used_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    phone_number: str | None = None


@dataclass(frozen=True)
class TestIdentitySms:
    """One SMS a product sent to a Tin-owned test phone number."""

    id: UUID
    message_sid: str
    to_number: str
    from_number: str
    body: str
    received_at: datetime


@dataclass(frozen=True)
class EffectReceipt:
    execution_key: str
    operation: str
    status: str
    result: dict[str, Any] | None
    error_message: str | None = None


@dataclass(frozen=True)
class RunRollout:
    """Metadata for one captured Codex rollout; content is fetched separately."""

    id: int
    run_id: UUID
    generation: int
    execution_key: str
    activity_attempt: int
    stage: str
    sandbox_id: str
    thread_id: str | None
    filename: str
    size_bytes: int
    stored_bytes: int
    sha256: str
    truncated: bool
    redactions: int
    created_at: datetime


@dataclass(frozen=True)
class ActivityEvent:
    id: int
    project_id: UUID
    run_id: UUID | None
    event_type: str
    details: dict[str, Any]
    summary: str | None
    audience: str
    created_at: datetime
    workflow_key: str | None = None
    workflow_title: str | None = None


@dataclass(frozen=True)
class IntegrationConnection:
    id: UUID
    project_id: UUID
    provider_key: str
    status: str
    external_account_id: str | None
    external_account_label: str | None
    configuration: dict[str, Any]
    credential_ciphertext: bytes | None
    credential_key_version: str | None
    connected_by_clerk_user_id: str
    last_checked_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class IntegrationAuthAttempt:
    token_hash: str
    project_id: UUID
    provider_key: str
    clerk_user_id: str
    pkce_verifier_ciphertext: bytes | None
    expires_at: datetime
    used_at: datetime | None
    created_at: datetime
    requested_capabilities: tuple[str, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IntegrationCallReceipt:
    execution_key: str
    project_id: UUID
    connection_id: UUID | None
    provider_key: str
    capability: str
    request_fingerprint: str
    status: str
    response_summary: dict[str, Any] | None
    provider_request_id: str | None
    error_code: str | None
    run_id: UUID | None = None


class StaleGenerationError(RuntimeError):
    """Raised when a sandbox generation no longer owns the active session lease."""


# A run's result_summary holds one line of at most 160 characters (migration 019).
RESULT_LINE_CHARS = 160


def result_line(text: str | None) -> str | None:
    """A run's one-line result: whitespace collapsed, cut at a word with "…" when longer.

    Every writer of result_summary goes through this, so a long model summary can never fail
    the projection after its files are committed.
    """
    if text is None:
        return None
    line = " ".join(text.split())
    if len(line) <= RESULT_LINE_CHARS:
        return line
    return line[: RESULT_LINE_CHARS - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


class SideEffectConflictError(RuntimeError):
    """Raised when an execution key is reused for a different operation."""


class StaleSettingsRevisionError(RuntimeError):
    """Raised when saved workflow settings changed after an editor loaded them."""
