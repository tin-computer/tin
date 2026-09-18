"""Deterministic activity routing; the original queue also drains legacy history.

Only the explicit trusted allowlist can leave the bounded Codex queue. There
is no semaphore inside polled activities and no second workflow engine.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.worker import (
    Interceptor,
    StartActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

CODEX_ACTIVITIES = frozenset(
    {
        "create_design_sandbox",
        "persist_design_artifact",
        "execute_code_workflow",
        "create_codex_procedure_sandbox",
        "persist_codex_procedure_artifact",
        "run_project_task_turn",
    }
)
TRUSTED_ACTIVITIES = frozenset(
    {
        "apply_project_task_changes",
        "character_approval",
        "character_design",
        "character_failure",
        "character_project",
        "character_review",
        "check_email_campaign_reply",
        "publish_code_workflow",
        "review_code_workflow",
        "approve_code_workflow",
        "project_code_workflow",
        "fail_code_workflow",
        "commit_codex_procedure_artifact",
        "commit_design_canonically",
        "complete_email_campaign",
        "complete_email_campaign_recipient",
        "content_plan_execute",
        "content_plan_failure",
        "deliver_content_draft",
        "dispatch_scheduled_workflow",
        "draft_answer_page",
        "draft_site_health_improvement",
        "email_campaign_follow_up_delay",
        "fail_email_campaign_recipient",
        "garden_project_memory",
        "generate_scan_report",
        "generate_visibility_audit",
        "generate_weekly_brief",
        "growth_onboarding_approval",
        "growth_onboarding_failure",
        "growth_onboarding_prepare",
        "growth_onboarding_report",
        "growth_onboarding_review",
        "growth_onboarding_setup",
        "growth_onboarding_step",
        "growth_plan_failure",
        "growth_plan_prepare",
        "growth_plan_publish",
        "growth_plan_write",
        "keyword_collect",
        "keyword_failure",
        "keyword_inspect",
        "keyword_prepare",
        "keyword_project",
        "keyword_publish",
        "keyword_review",
        "keyword_sample_count",
        "list_email_campaign_recipients",
        "open_site_health_pull_request",
        "organic_brand_checks",
        "organic_end_crawl",
        "organic_failure",
        "organic_observe",
        "organic_poll_crawl",
        "organic_prepare",
        "organic_prepare_panel",
        "organic_project",
        "organic_publish",
        "organic_start_crawl",
        "organic_system_failure",
        "organic_system_finish",
        "organic_system_prepare",
        "organic_system_progress",
        "organic_system_step",
        "organic_system_step_failure",
        "prepare_codex_procedure",
        "prepare_email_campaign",
        "project_answer_page_failure",
        "project_answer_page_result",
        "project_codex_procedure_failure",
        "project_codex_procedure_result",
        "project_design_failure",
        "project_design_result",
        "project_email_campaign_failure",
        "project_memory_failure",
        "project_memory_result",
        "project_scan_failure",
        "project_scan_result",
        "project_site_health_failure",
        "project_site_health_result",
        "project_task_failure",
        "project_visibility_failure",
        "project_visibility_result",
        "project_weekly_brief_failure",
        "project_weekly_brief_result",
        "record_answer_page_approval",
        "record_codex_procedure_approval",
        "record_email_campaign_approval",
        "record_project_task_stop",
        "request_answer_page_review",
        "request_codex_procedure_review",
        "receive_workflow_revision",
        "request_email_campaign_review",
        "reserve_email_campaign_delivery",
        "resolve_codex_project",
        "send_email_campaign_recipient",
        "style_extract",
        "style_failure",
        "style_prepare",
        "style_publish",
    }
)


def trusted_task_queue(base: str) -> str:
    return f"{base}-trusted"


class ActivityLaneInterceptor(Interceptor):
    def workflow_interceptor_class(
        self,
        input: WorkflowInterceptorClassInput,
    ) -> type[WorkflowInboundInterceptor]:
        return _LaneInbound


class _LaneInbound(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_LaneOutbound(outbound))


class _LaneOutbound(WorkflowOutboundInterceptor):
    def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle:
        base = workflow.info().task_queue
        if input.activity in TRUSTED_ACTIVITIES and input.task_queue in (None, base):
            input.task_queue = trusted_task_queue(base)
            # The workflow worker must not eagerly execute a trusted activity
            # on its legacy/Codex slot.
            input.disable_eager_execution = True
        return super().start_activity(input)
