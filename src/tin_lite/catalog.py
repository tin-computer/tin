from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from tin_lite import (
    content_draft,
    content_plan,
    content_plan_editorial,
    content_repository_delivery,
    growth_onboarding,
    growth_plan,
    organic_system,
    paid_ads,
    paid_ads_launch,
    paid_ads_monitor,
    style_capture,
    technical_fix,
)
from tin_lite.character_design import MODEL_ROUTE as CHARACTER_MODEL_ROUTE
from tin_lite.code_storage import CodeStorage
from tin_lite.db import Database
from tin_lite.domain import (
    ANSWER_PAGE_WORKFLOW_NAME,
    CODEX_PROCEDURE_EXECUTOR,
    CONTENT_DIAGRAM_WORKFLOW_NAME,
    CREATIVE_CHARACTER_WORKFLOW_NAME,
    CREATIVE_PRODUCT_DEMO_WORKFLOW_NAME,
    EMAIL_CAMPAIGN_WORKFLOW_NAME,
    EMAIL_SHORTLIST_PATH,
    EMAIL_SHORTLIST_WORKFLOW_NAME,
    GROWTH_ONBOARDING_PLAN_PATH,
    GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME,
    MEMORY_INDEX_PATH,
    PRODUCT_AUDIT_PATH_TEMPLATE,
    PRODUCT_CODE_MAP_WORKFLOW_NAME,
    PRODUCT_DEEP_DIVE_WORKFLOW_NAME,
    PROJECT_MEMORY_WORKFLOW_NAME,
    PROJECT_TASK_WORKFLOW_NAME,
    PUBLIC_ARTICLE_WORKFLOW_NAME,
    QA_PRODUCT_AUDIT_WORKFLOW_NAME,
    QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME,
    RESEARCH_DEEP_DIVE_PATH,
    RESEARCH_DEEP_DIVE_WORKFLOW_NAME,
    SCAN_REPORT_WORKFLOW_NAME,
    SIGNUP_WALKTHROUGH_PATH_TEMPLATE,
    SITE_HEALTH_WORKFLOW_NAME,
    VISIBILITY_AUDIT_WORKFLOW_NAME,
    WEEKLY_BRIEF_WORKFLOW_NAME,
    WORKFLOW_NAME,
)
from tin_lite.integrations import (
    ADS_PROVIDER,
    GITHUB_PROVIDER,
    GOOGLE_WORKSPACE_PROVIDER,
    GSC_PROVIDER,
    IntegrationRequirement,
    parse_integration_requirements,
)
from tin_lite.keyword_plan import (
    KEY as KEYWORD_KEY,
)
from tin_lite.keyword_plan import (
    ROUTE_KEY as KEYWORD_ROUTE_KEY,
)
from tin_lite.keyword_plan_v5 import (
    INSTRUCTIONS as KEYWORD_INSTRUCTIONS,
)
from tin_lite.keyword_plan_v5 import (
    POLICY as KEYWORD_POLICY,
)
from tin_lite.keyword_plan_v5 import (
    SCHEMAS as KEYWORD_SCHEMAS,
)
from tin_lite.model_providers import ModelCapability, ModelRoute, ProviderName
from tin_lite.organic_audit import AUDIT_KEY, AUDIT_POLICY, MARKETS
from tin_lite.organic_audit_ai import AI_CONTRACT, AI_SCHEMAS
from tin_lite.procedures import (
    BROWSER_SANDBOX_PROFILE,
    CODE_MAP_SECTION,
    EMAIL_SHORTLIST_VALIDATOR,
    FEATURE_MAP_SECTION,
    IDENTITY_REUSE_ACTIVE,
    MEMORY_SECTION_PARENT,
    MEMORY_SECTION_VALIDATOR,
    OPEN_SANDBOX_EGRESS,
    PRODUCT_AUDIT_VALIDATOR,
    SIGNUP_WALKTHROUGH_VALIDATOR,
    STUDIO_SANDBOX_PROFILE,
    TIN_DIAGRAM_BRANDED_VALIDATOR,
    CodexProcedureSource,
    GitHubPullRequestProcedure,
    GitHubRepositoryWorkspace,
    OutputSection,
    ProjectSkillDependency,
    SandboxProfile,
    TestIdentityPolicy,
)
from tin_lite.public_workflows import load_public_workflows
from tin_lite.studio import STUDIO_VOICES
from tin_lite.studio_contracts import (
    DEMO_VIDEO_MEDIA_TYPE,
    DEMO_VIDEO_VALIDATOR,
    MAX_DEMO_VIDEO_BYTES,
)
from tin_lite.system_wiki import SystemWikiRef
from tin_lite.workflow_diagrams import DiagramEdge, DiagramNode, WorkflowDiagram
from tin_lite.workflow_inputs import validate_input_schema
from tin_lite.workflow_prerequisites import (
    WorkflowPrerequisite,
    parse_workflow_prerequisites,
    validate_prerequisite_graph,
)
from tin_lite.writing_style import STYLE_PATH

REGISTRY_REPO_ID = "registry/workflows"
START_HERE_SYSTEM = "start-here"
ORGANIC_TRAFFIC_SYSTEM = "organic-traffic"
COLD_OUTREACH_SYSTEM = "cold-outreach"
PRODUCT_QA_SYSTEM = "product-qa"
CREATIVE_STUDIO_SYSTEM = "creative-studio"
PAID_ADS_SYSTEM = "paid-ads"
DESIGN_MD_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000001")
PROJECT_MEMORY_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000002")
SCAN_REPORT_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000003")
VISIBILITY_AUDIT_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000004")
ANSWER_PAGE_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000005")
PROJECT_TASK_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000006")
WEEKLY_BRIEF_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000007")
RESEARCH_DEEP_DIVE_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000008")
PUBLIC_ARTICLE_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000009")
SITE_HEALTH_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000010")
EMAIL_SHORTLIST_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000011")
EMAIL_CAMPAIGN_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000012")
QA_SIGNUP_WALKTHROUGH_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000013")
CONTENT_DIAGRAM_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000014")
PRODUCT_CODE_MAP_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000015")
PRODUCT_DEEP_DIVE_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000016")
QA_PRODUCT_AUDIT_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000017")
GROWTH_ONBOARDING_PLAN_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000034")
GROWTH_ONBOARDING_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000035")
ORGANIC_AUDIT_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000020")
CREATIVE_CHARACTER_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000029")
CREATIVE_PRODUCT_DEMO_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000022")
PAID_ADS_ASSESSMENT_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000040")
PAID_ADS_LAUNCH_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000041")
PAID_ADS_MONITOR_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000042")
# Numbers below were used by built-ins that later left the catalog. Their rows still exist in
# deployed databases, and the boot-time sync refuses to bind a number to a different key, so a
# new built-in must take a fresh number above the highest ever used, never fill a gap.
RETIRED_BUILTIN_WORKFLOW_IDS = {
    UUID("00000000-0000-4000-8000-000000000018"): "strategy.prescribe",
    UUID("00000000-0000-4000-8000-000000000019"): "strategy.wildcards",
    UUID("00000000-0000-4000-8000-000000000021"): "creative.character_agent",
    UUID("00000000-0000-4000-8000-000000000026"): "creative.character_direct",
    # The paid ads keys moved from growth.paid_ads_* to ads.* on 2026-09-22; the published
    # rows keep their numbers, so the ads.* built-ins took fresh ones.
    UUID("00000000-0000-4000-8000-000000000037"): "growth.paid_ads_assessment",
    UUID("00000000-0000-4000-8000-000000000038"): "growth.paid_ads_launch",
    UUID("00000000-0000-4000-8000-000000000039"): "growth.paid_ads_monitor",
}


@dataclass(frozen=True)
class WorkflowSystem:
    id: str
    name: str
    display_order: int


WORKFLOW_SYSTEMS = (
    WorkflowSystem(
        id=START_HERE_SYSTEM,
        name="Start here",
        display_order=0,
    ),
    WorkflowSystem(
        id=ORGANIC_TRAFFIC_SYSTEM,
        name="Organic traffic system",
        display_order=1,
    ),
    WorkflowSystem(
        id=COLD_OUTREACH_SYSTEM,
        name="Cold outreach system",
        display_order=2,
    ),
    WorkflowSystem(
        id=PRODUCT_QA_SYSTEM,
        name="Product QA system",
        display_order=3,
    ),
    WorkflowSystem(
        id=CREATIVE_STUDIO_SYSTEM,
        name="Creative studio",
        display_order=4,
    ),
    WorkflowSystem(
        id=PAID_ADS_SYSTEM,
        name="Paid ads system",
        display_order=5,
    ),
)
WORKFLOW_SYSTEM_IDS = frozenset(item.id for item in WORKFLOW_SYSTEMS)


@dataclass(frozen=True)
class HumanReviewPolicy:
    reason: str
    review_label: str
    defer_label: str
    summary: str
    queue_clause: str
    revision_adapter: str | None = None

    def definition(self) -> dict[str, Any]:
        return {
            "eligible": True,
            "reason": self.reason,
            "review_label": self.review_label,
            "defer_label": self.defer_label,
            "summary": self.summary,
            "queue_clause": self.queue_clause,
            **({"revision_adapter": self.revision_adapter} if self.revision_adapter else {}),
        }


ANSWER_PAGE_REVIEW_POLICY = HumanReviewPolicy(
    reason="Produces customer-facing content.",
    review_label="Review draft",
    defer_label="Not now",
    summary=(
        "Your answer-page draft is ready. Review it before Tin marks the workflow complete; "
        "otherwise it stays safely on hold."
    ),
    queue_clause="Answer-page draft ready to finish",
)

PUBLIC_ARTICLE_REVIEW_POLICY = HumanReviewPolicy(
    reason="Produces a public-facing article draft.",
    review_label="Review article",
    defer_label="Not now",
    summary=(
        "Your public article draft is ready. Review the claims and copy before Tin marks the "
        "workflow complete; otherwise it stays safely on hold."
    ),
    queue_clause="Public article draft ready to finish",
    revision_adapter="content-revision.v1",
)

GROWTH_ONBOARDING_REVIEW_POLICY = HumanReviewPolicy(
    reason="Tin sets up only the systems the founder picked, with the tools they connected.",
    review_label="Set it up",
    defer_label="Not now",
    summary=(
        "The plan is ready. Say what Tin should take on, connect what it needs, then continue."
    ),
    queue_clause="Growth plan waiting for your pick",
)
PAID_ADS_LAUNCH_REVIEW_POLICY = HumanReviewPolicy(
    reason="Creates or changes things in the founder's Google Ads account.",
    review_label="Approve",
    defer_label="Not now",
    summary=(
        "The Google Ads step is ready: the exact campaign or the tracking setup Tin will "
        "carry out. Nothing happens in Google Ads until you approve it."
    ),
    queue_clause="Google Ads step ready for your approval",
)
EMAIL_CAMPAIGN_REVIEW_POLICY = HumanReviewPolicy(
    reason="Sends email to external recipients.",
    review_label="Approve & start",
    defer_label="Not now",
    summary=(
        "Your exact email campaign snapshot is ready. Nothing will be sent until you approve it."
    ),
    queue_clause="Email campaign ready to send",
)

CHARACTER_REVIEW_POLICY = HumanReviewPolicy(
    reason="Produces a brand character intended for public content.",
    review_label="Review character",
    defer_label="Not now",
    summary=(
        "Your character is ready. Look at it before Tin marks the workflow complete; "
        "otherwise it stays safely on hold."
    ),
    queue_clause="Character ready to finish",
)

DEMO_VIDEO_REVIEW_POLICY = HumanReviewPolicy(
    reason="Produces a public-facing product video.",
    review_label="Review video",
    defer_label="Not now",
    summary=(
        "Your demo video is ready. Watch it before Tin marks the workflow complete; "
        "otherwise it stays safely on hold."
    ),
    queue_clause="Demo video ready to finish",
)

CONTENT_DIAGRAM_REVIEW_POLICY = HumanReviewPolicy(
    reason="Produces a figure intended for public content.",
    review_label="Review diagram",
    defer_label="Not now",
    summary=(
        "Your diagram is ready. Review its meaning and labels before Tin marks the workflow "
        "complete; otherwise it stays safely on hold."
    ),
    queue_clause="Diagram ready to finish",
)


@dataclass(frozen=True)
class BuiltinWorkflow:
    id: UUID
    key: str
    title: str
    description: str
    executor: str
    version_label: str
    uses_system_wiki: bool = False
    review_policy: HumanReviewPolicy | None = None
    kind: str = "workflow"
    input_schema: dict[str, Any] | None = None
    procedure: CodexProcedureSource | None = None
    integration_requirements: tuple[IntegrationRequirement, ...] = ()
    model_route: ModelRoute | None = None
    schedule_modes: tuple[str, ...] = ("on_demand", "daily", "weekly")
    system: str | None = None
    presentation: WorkflowDiagram | None = None
    prerequisites: tuple[WorkflowPrerequisite, ...] = ()
    # Agents run it through the MCP; the product UI does not list it in the catalog.
    agent_only: bool = False

    @property
    def definition_path(self) -> str:
        return f"workflows/{self.key}.json"

    @property
    def definition(self) -> dict[str, Any]:
        definition, _files = self.definition_and_resource_files()
        return definition

    def definition_and_resource_files(self) -> tuple[dict[str, Any], dict[str, bytes]]:
        if (self.executor == CODEX_PROCEDURE_EXECUTOR) != (self.procedure is not None):
            raise ValueError(
                "codex.procedure workflows must declare exactly one procedure source package"
            )
        definition: dict[str, Any] = {
            "key": self.key,
            "version": self.version_label,
            "title": self.title,
            "description": self.description,
            "executor": self.executor,
            "kind": self.kind,
            "schedule_modes": list(self.schedule_modes),
            "input_schema": self.input_schema
            or {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project_id": {"type": "string", "format": "uuid"},
                },
                "required": ["project_id"],
            },
        }
        if self.review_policy is not None:
            definition["human_review"] = self.review_policy.definition()
        if self.system is not None:
            if self.system not in WORKFLOW_SYSTEM_IDS:
                raise ValueError(f"workflow {self.key} references unknown system {self.system}")
            definition["system"] = self.system
        if self.agent_only:
            definition["agent_only"] = True
        if self.presentation is not None:
            definition["presentation"] = {"flow": self.presentation.definition()}
        if self.integration_requirements:
            definition["integration_requirements"] = [
                requirement.definition() for requirement in self.integration_requirements
            ]
        if self.prerequisites:
            definition["prerequisites"] = [item.definition() for item in self.prerequisites]
        if self.model_route is not None:
            definition["model_route"] = {
                "key": self.model_route.key,
                "provider": self.model_route.provider.value,
                "model": self.model_route.model,
                "capabilities": sorted(item.value for item in self.model_route.capabilities),
            }
        resources: dict[str, bytes] = {}
        if self.key == style_capture.KEY:
            definition["style_policy"] = dict(style_capture.POLICY)
            definition["style_instructions"] = style_capture.INSTRUCTIONS
            definition["style_schema"] = style_capture.MODEL_SCHEMA
        if self.key == organic_system.KEY:
            definition["organic_system_policy"] = dict(organic_system.POLICY)
        if self.key == growth_onboarding.KEY:
            definition["growth_onboarding_policy"] = dict(growth_onboarding.POLICY)
        if self.key == growth_plan.KEY:
            definition["plan_policy"] = dict(growth_plan.POLICY)
            definition["plan_routes"] = growth_plan.route_definitions()
            definition["plan_contract_sha256"] = growth_plan.contract_digest()
            definition["output_path"] = GROWTH_ONBOARDING_PLAN_PATH
        if self.key == paid_ads.KEY:
            definition["paid_ads_policy"] = dict(paid_ads.POLICY)
            definition["paid_ads_routes"] = paid_ads.route_definitions()
            definition["paid_ads_contract_sha256"] = paid_ads.contract_digest()
        if self.key == paid_ads_launch.KEY:
            definition["paid_ads_launch_policy"] = dict(paid_ads_launch.POLICY)
            definition["paid_ads_launch_routes"] = paid_ads_launch.route_definitions()
            definition["paid_ads_launch_contract_sha256"] = paid_ads_launch.contract_digest()
        if self.key == paid_ads_monitor.KEY:
            definition["paid_ads_monitor_policy"] = dict(paid_ads_monitor.POLICY)
            definition["paid_ads_monitor_routes"] = paid_ads_monitor.route_definitions()
            definition["paid_ads_monitor_contract_sha256"] = paid_ads_monitor.contract_digest()
        if self.key == content_plan.KEY:
            definition["content_policy"] = dict(content_plan_editorial.POLICY)
            definition["content_instructions"] = content_plan_editorial.INSTRUCTIONS
            definition["content_schema"] = content_plan_editorial.MODEL_SCHEMA
        if self.key == AUDIT_KEY:
            definition["audit_policy"] = dict(AUDIT_POLICY)
            definition["audit_instructions"] = dict(AI_CONTRACT)
            definition["audit_schemas"] = dict(AI_SCHEMAS)
        if self.key == KEYWORD_KEY:
            definition["keyword_policy"] = dict(KEYWORD_POLICY)
            definition["keyword_instructions"] = dict(KEYWORD_INSTRUCTIONS)
            definition["keyword_schemas"] = dict(KEYWORD_SCHEMAS)
        if self.procedure is not None:
            procedure, resources = self.procedure.materialize(self.key)
            definition["procedure"] = procedure
            identity = procedure.get("identity", {})
            if (identity.get("create") or identity.get("reuse", "none") != "none") and not any(
                requirement.provider_key == GOOGLE_WORKSPACE_PROVIDER
                for requirement in self.integration_requirements
            ):
                raise ValueError(
                    "procedures that use a test identity require the Google Workspace mailbox"
                )
        if self.key == "content.public_article":
            definition["public_discovery"] = False
        from tin_lite.native_skill_pins import suite_for_workflow

        suite = suite_for_workflow(self.key)
        if suite is not None:
            definition["native_skill_suite"] = suite
        return definition, resources

    def definition_with_wiki(self, system_wiki: SystemWikiRef) -> dict[str, Any]:
        definition = self.definition
        if self.uses_system_wiki:
            definition["system_wiki"] = {
                "repo_id": system_wiki.repo_id,
                "path": system_wiki.path,
                "commit_sha": system_wiki.commit_sha,
            }
        return definition

    def definition_and_files_with_wiki(
        self, system_wiki: SystemWikiRef
    ) -> tuple[dict[str, Any], dict[str, bytes]]:
        definition, resources = self.definition_and_resource_files()
        if self.uses_system_wiki:
            definition["system_wiki"] = {
                "repo_id": system_wiki.repo_id,
                "path": system_wiki.path,
                "commit_sha": system_wiki.commit_sha,
            }
        return definition, resources


BUILTIN_WORKFLOWS = (
    BuiltinWorkflow(
        id=UUID("00000000-0000-4000-8000-000000000027"),
        key=organic_system.KEY,
        title="Run the organic traffic system",
        description=(
            "Audit your website and research buyer searches, then save an editable content "
            "plan and draft its next article for review. With GitHub connected, adapt the "
            "approved article into an unmerged PR; otherwise keep its Markdown in Tin. "
            "Optionally propose one technical fix. Never merges, publishes or sends outreach."
        ),
        executor=organic_system.KEY,
        version_label="0.2.0",
        system=ORGANIC_TRAFFIC_SYSTEM,
        schedule_modes=("on_demand",),
        input_schema=organic_system.INPUT_SCHEMA,
    ),
    BuiltinWorkflow(
        id=content_repository_delivery.WORKFLOW_ID,
        key=content_repository_delivery.KEY,
        title="Prepare article PR",
        description="Adapt an approved Tin article to the connected website repository's "
        "existing format and components. Preserve its copy, leave a reviewable GitHub PR "
        "unmerged, and keep the Markdown original in Tin.",
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.0.1",
        system=ORGANIC_TRAFFIC_SYSTEM,
        schedule_modes=("on_demand",),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "source_run_id": {
                    "type": "string",
                    "format": "uuid",
                    "title": "Approved article run",
                },
                "retry_run_id": {
                    "type": "string",
                    "default": "",
                    "pattern": r"^(|[0-9a-f-]{36})$",
                    "title": "Failed adaptation to retry",
                    "description": "Internal retry context. A fresh adaptation is a new "
                    "metered run; retrying a saved PR delivery does not use this input.",
                },
                "expected_repository": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 140,
                    "pattern": r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$",
                    "title": "Website repository",
                },
                "direction": {
                    "type": "string",
                    "default": "",
                    "maxLength": 2000,
                    "title": "Site instructions",
                    "description": "Optional site root or routing conventions. "
                    "Does not authorize changing the approved article.",
                    "x-tin-ui": {"control": "textarea", "order": 30},
                },
            },
            "required": ["project_id", "source_run_id", "expected_repository"],
        },
        integration_requirements=(
            IntegrationRequirement(
                provider_key=GITHUB_PROVIDER,
                capabilities=(
                    "contents.read",
                    "contents.write",
                    "pull_requests.read",
                    "pull_requests.write",
                ),
                required=True,
            ),
        ),
        procedure=CodexProcedureSource(
            root=Path(__file__).resolve().parents[2]
            / "codex_procedures"
            / content_repository_delivery.KEY,
            entry_skill="article-delivery",
            github_pull_request=GitHubPullRequestProcedure(
                receipt_path_template="content/deliveries/{run_id}.md",
                verification_commands=(content_repository_delivery.CHECK_COMMAND,),
                max_files=5,
                max_bytes=180_000,
            ),
        ),
    ),
    BuiltinWorkflow(
        id=UUID("00000000-0000-4000-8000-000000000031"),
        key=content_draft.KEY,
        title="Draft planned content",
        description="Check current coverage before drafting the next planned article "
        "in your style. "
        "Save useful copy for review, or explain why no draft is needed. "
        "Optional GitHub PR delivery follows article approval. "
        "Nothing is merged or published and the roadmap stays unchanged.",
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.5.0",
        system=ORGANIC_TRAFFIC_SYSTEM,
        schedule_modes=("on_demand",),
        review_policy=PUBLIC_ARTICLE_REVIEW_POLICY,
        prerequisites=(
            WorkflowPrerequisite(
                kind="artifact",
                level="required",
                path="content/plans/{program_id}/plan.json",
                producer=content_plan.KEY,
                reason="Choose an initialized content program to draft its next article.",
            ),
            WorkflowPrerequisite(
                kind="artifact",
                level="recommended",
                path=STYLE_PATH,
                producer=style_capture.KEY,
                reason="The writing guide shapes expression, not product facts.",
            ),
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "program_id": {"type": "string", "format": "uuid", "title": "Content program"},
                "delivery": {
                    "type": "string",
                    "enum": ["program", "draft_only"],
                    "default": "program",
                    "title": "After review",
                    "description": "Use the program's saved delivery settings, "
                    "or keep this run draft-only.",
                },
                "item_id": {
                    "type": "string",
                    "pattern": "^(|[a-zA-Z0-9_-]{1,64})$",
                    "default": "",
                    "title": "Planned article",
                    "description": "Leave empty to draft the next article in plan order. "
                    "Set only for an explicit selection.",
                },
                "rewrite": {
                    "type": "boolean",
                    "default": False,
                    "title": "Write a new draft of this article",
                    "description": "Requires an explicit item_id. Earlier drafts stay unchanged.",
                },
                "plan_revision": {
                    "type": "string",
                    "pattern": "^(|[0-9a-f]{40})$",
                    "default": "",
                    "title": "Selected plan revision",
                    "description": "Selection metadata; omitted means the current plan.",
                },
                "direction": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Anything to add?",
                    "x-tin-ui": {"control": "textarea", "order": 40},
                },
            },
            "required": ["project_id", "program_id"],
        },
        procedure=CodexProcedureSource(
            root=Path(__file__).resolve().parents[2] / "codex_procedures" / content_draft.KEY,
            entry_skill="planned-content",
            output_path_template=content_draft.PATH_TEMPLATE,
            output_validator=content_draft.EDITORIAL_VALIDATOR,
            output_max_bytes=80_000,
            project_skills=(
                ProjectSkillDependency(name="writing-style", path=STYLE_PATH, required=False),
            ),
        ),
    ),
    BuiltinWorkflow(
        id=UUID("00000000-0000-4000-8000-000000000030"),
        key=style_capture.KEY,
        title="Capture writing style",
        description=(
            "Use your coding agent to select writing samples, or add samples here. "
            "Save an editable voice guide for future content. Nothing is published."
        ),
        executor=style_capture.KEY,
        version_label="1.0.0",
        system=ORGANIC_TRAFFIC_SYSTEM,
        schedule_modes=("on_demand",),
        model_route=style_capture.ROUTE,
        prerequisites=(
            WorkflowPrerequisite(
                kind="artifact",
                level="required",
                path="{source_path}",
                reason="Capture reads a user-selected writing sample packet from project Files.",
            ),
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "source_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "title": "Selected samples",
                    "description": "A style source packet in project Files.",
                    "x-tin-ui": {"control": "text", "order": 10},
                },
                "direction": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Anything to change?",
                    "description": "Optional new direction. Existing preferences are preserved.",
                    "x-tin-ui": {"control": "textarea", "order": 20},
                },
            },
            "required": ["project_id", "source_path"],
        },
    ),
    BuiltinWorkflow(
        id=UUID("00000000-0000-4000-8000-000000000028"),
        key=technical_fix.KEY,
        title="Fix an audited technical issue",
        description=(
            "Recheck one missing-title or missing-description finding and propose a verified PR. "
            "Supports exact static HTML and bounded Python-wheel HTML templates. "
            "Lists unsupported pages separately. "
            "If no safe repair is available, explain why "
            "without a PR. Never merges or deploys; GitHub may run its configured PR checks."
        ),
        version_label="0.4.1",
        prerequisites=(
            WorkflowPrerequisite(
                kind="run",
                level="required",
                workflow=AUDIT_KEY,
                via_input="audit_run_id",
                reason="A technical fix repairs one finding from a successful, pinned audit.",
            ),
        ),
        executor="codex.procedure",
        system=ORGANIC_TRAFFIC_SYSTEM,
        schedule_modes=("on_demand",),
        input_schema=technical_fix.INPUT_SCHEMA,
        integration_requirements=(
            IntegrationRequirement(
                provider_key=GITHUB_PROVIDER,
                capabilities=(
                    "contents.read",
                    "contents.write",
                    "pull_requests.read",
                    "pull_requests.write",
                ),
                required=True,
            ),
        ),
        procedure=CodexProcedureSource(
            root=Path(__file__).parents[2] / "codex_procedures" / technical_fix.KEY,
            entry_skill="audit-title-repair",
            github_pull_request=GitHubPullRequestProcedure(
                receipt_path_template="reports/technical-fix/{run_id}/RESULT.md",
                verification_commands=(technical_fix.CHECK_COMMAND,),
                repair_policy=technical_fix.POLICY,
            ),
        ),
    ),
    BuiltinWorkflow(
        id=UUID("00000000-0000-4000-8000-000000000025"),
        key=content_plan.KEY,
        title="Plan upcoming content",
        description=(
            "Turn an audit and keyword research into an editable two-week to six-month roadmap. "
            "Save to My system to prepare weekly batches. Does not write articles or publish."
        ),
        executor=content_plan.KEY,
        version_label="0.6.0",
        prerequisites=(
            WorkflowPrerequisite(
                kind="run",
                level="required",
                workflow=AUDIT_KEY,
                via_input="audit_run_id",
                reason="Content planning consumes one exact successful organic audit publication.",
            ),
            WorkflowPrerequisite(
                kind="run",
                level="required",
                workflow=KEYWORD_KEY,
                via_input="keyword_run_id",
                reason="Content planning consumes one exact successful keyword plan publication.",
            ),
        ),
        system=ORGANIC_TRAFFIC_SYSTEM,
        schedule_modes=("on_demand", "weekly"),
        input_schema=content_plan.INPUT_SCHEMA,
        model_route=ModelRoute(
            key=content_plan_editorial.ROUTE_KEY,
            provider=ProviderName.OPENAI,
            model=content_plan_editorial.POLICY["model"],
            capabilities=frozenset({ModelCapability.TEXT, ModelCapability.JSON_SCHEMA}),
        ),
    ),
    BuiltinWorkflow(
        id=UUID("00000000-0000-4000-8000-000000000024"),
        key=KEYWORD_KEY,
        title="Plan keyword opportunities",
        description=(
            "Research buyer searches, competitor keywords, and a bounded sample of Google results. "
            "Get an evidence-backed keyword inventory for content planning. "
            "No audit or GitHub required; does not create a calendar, write articles, or publish."
        ),
        executor=KEYWORD_KEY,
        version_label="0.5.0",
        system=ORGANIC_TRAFFIC_SYSTEM,
        schedule_modes=("on_demand",),
        model_route=ModelRoute(
            key=KEYWORD_ROUTE_KEY,
            provider=ProviderName.OPENAI,
            model=KEYWORD_POLICY["model"],
            capabilities=frozenset({ModelCapability.TEXT, ModelCapability.JSON_SCHEMA}),
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "site_url": {
                    "type": "string",
                    "format": "uri",
                    "pattern": "^https://",
                    "minLength": 10,
                    "maxLength": 500,
                    "title": "Public website",
                    "description": "Exact HTTPS origin without a path or query.",
                    "x-tin-ui": {"control": "text", "order": 10},
                },
                "market": {
                    "type": "string",
                    "enum": list(MARKETS),
                    "title": "Buyer market",
                    "description": "Country for English keyword research.",
                    "x-tin-ui": {"control": "select", "order": 20},
                },
                "buyer_context": {
                    "type": "string",
                    "minLength": 20,
                    "maxLength": 2000,
                    "title": "Product and buyers",
                    "description": (
                        "What the product does, who needs it, and the problems they search for."
                    ),
                    "x-tin-ui": {"control": "textarea", "order": 30},
                },
                "seed_phrases": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 120},
                    "maxItems": 8,
                    "default": [],
                    "title": "Optional seed phrases",
                    "description": (
                        "Up to eight phrases. Leave empty to derive seeds from buyer context."
                    ),
                },
                "competitor_hosts": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 4, "maxLength": 253},
                    "maxItems": 3,
                    "default": [],
                    "title": "Optional competitor hosts",
                    "description": (
                        "Up to three public domains. Leave empty to discover search competitors."
                    ),
                },
                "audit_run_id": {
                    "type": "string",
                    "maxLength": 36,
                    "default": "",
                    "title": "Optional audit run ID",
                    "description": (
                        "A successful audit for this exact site and market in this project."
                    ),
                    "x-tin-ui": {"control": "text", "order": 60},
                },
                "use_search_console": {
                    "type": "boolean",
                    "default": True,
                    "title": "Use matching Search Console data",
                    "description": (
                        "Optional enrichment from this project's connected property, "
                        "if it matches the website."
                    ),
                    "x-tin-ui": {"control": "segmented", "order": 70},
                },
                "max_cost_usd": {
                    "type": "number",
                    "minimum": 5,
                    "maximum": 25,
                    "default": 10,
                    "title": "Maximum research spend (USD)",
                    "description": (
                        "Includes conservative provider and model reservations. "
                        "Also bounded by the server's enabled ceiling."
                    ),
                    "x-tin-ui": {"control": "number", "order": 80},
                },
            },
            "required": ["project_id", "site_url", "market", "buyer_context"],
        },
    ),
    BuiltinWorkflow(
        id=ORGANIC_AUDIT_WORKFLOW_ID,
        key=AUDIT_KEY,
        title="Audit organic visibility",
        description=(
            "Audit technical SEO and AI visibility (GEO). Check up to 100 public pages "
            "and see whether AI answers mention, cite, or recommend your business. "
            "Get a report, actionable findings, and supporting evidence. No GitHub required."
        ),
        executor=AUDIT_KEY,
        version_label="0.5.0",
        model_route=ModelRoute(
            key="organic.audit.visibility.v1",
            provider=ProviderName.OPENAI,
            model=AUDIT_POLICY["model"],
            capabilities=frozenset({ModelCapability.TEXT, ModelCapability.JSON_SCHEMA}),
        ),
        system=ORGANIC_TRAFFIC_SYSTEM,
        schedule_modes=("on_demand",),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "site_url": {
                    "type": "string",
                    "format": "uri",
                    "pattern": "^https://",
                    "minLength": 10,
                    "maxLength": 500,
                    "title": "Public website",
                    "description": (
                        "English marketing site. Use its exact HTTPS origin, "
                        "without a path or query."
                    ),
                    "x-tin-ui": {"control": "text", "order": 10},
                },
                "market": {
                    "type": "string",
                    "enum": list(MARKETS),
                    "title": "Buyer market",
                    "description": (
                        "Country for the English-language buyer questions; "
                        "not inferred from timezone."
                    ),
                    "x-tin-ui": {"control": "select", "order": 20},
                },
            },
            "required": ["project_id", "site_url", "market"],
        },
    ),
    BuiltinWorkflow(
        id=DESIGN_MD_WORKFLOW_ID,
        key=WORKFLOW_NAME,
        title="Generate project design",
        description="Analyze a project repository and publish its DESIGN.md.",
        executor=WORKFLOW_NAME,
        version_label="1.0.0",
    ),
    BuiltinWorkflow(
        id=PROJECT_MEMORY_WORKFLOW_ID,
        key=PROJECT_MEMORY_WORKFLOW_NAME,
        title="Garden project memory",
        description="Consolidate durable project outputs into the project wiki.",
        executor=PROJECT_MEMORY_WORKFLOW_NAME,
        version_label="1.1.0",
    ),
    BuiltinWorkflow(
        id=SCAN_REPORT_WORKFLOW_ID,
        key=SCAN_REPORT_WORKFLOW_NAME,
        title="Scan project",
        description=(
            "Review durable project knowledge against the system scanning guide and publish "
            "SCAN.md."
        ),
        executor=SCAN_REPORT_WORKFLOW_NAME,
        version_label="1.2.0",
        prerequisites=(
            WorkflowPrerequisite(
                kind="artifact",
                level="recommended",
                path=MEMORY_INDEX_PATH,
                producer=PROJECT_MEMORY_WORKFLOW_NAME,
                reason="Project memory gives the scan durable context instead of raw source runs.",
            ),
        ),
        uses_system_wiki=True,
    ),
    BuiltinWorkflow(
        id=SITE_HEALTH_WORKFLOW_ID,
        key=SITE_HEALTH_WORKFLOW_NAME,
        title="Improve site health",
        description=(
            "Inspect one public site against its selected GitHub repository, make one bounded "
            "mechanical improvement, and open a pull request for review. "
            "When no safe change is justified, save a no-change report without opening a PR."
        ),
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="2.2.2",
        system=ORGANIC_TRAFFIC_SYSTEM,
        presentation=WorkflowDiagram(
            nodes=(
                DiagramNode("inspect", "step", "inspect site + repo", "pinned evidence"),
                DiagramNode("change", "step", "make one fix", "bounded change"),
                DiagramNode("verify", "step", "run checks", "repository commands"),
                DiagramNode("pull_request", "surface", "GitHub pull request", "left unmerged"),
                DiagramNode("receipt", "receipt", "receipt", "PR linked in Files"),
            ),
            edges=(
                DiagramEdge("inspect", "change"),
                DiagramEdge("change", "verify"),
                DiagramEdge("verify", "pull_request"),
                DiagramEdge("pull_request", "receipt"),
            ),
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "site_url": {
                    "type": "string",
                    "format": "uri",
                    "pattern": "^https://",
                    "minLength": 1,
                    "maxLength": 500,
                    "title": "Public site URL",
                    "description": "The HTTPS page to inspect before proposing a repository fix.",
                    "x-tin-ui": {"control": "text", "order": 10},
                },
                "focus": {
                    "type": "string",
                    "enum": ["automatic", "accessibility", "technical_seo", "reliability"],
                    "default": "automatic",
                    "title": "Focus",
                    "x-tin-ui": {"control": "select", "order": 20},
                },
                "change_budget": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3,
                    "default": 1,
                    "title": "Maximum files",
                    "description": "A strict cap; the workflow should use fewer when possible.",
                    "x-tin-ui": {"control": "counter", "order": 30},
                },
                "context": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Optional context",
                    "description": (
                        "Known constraints or an issue to prioritize without widening scope."
                    ),
                    "x-tin-ui": {"control": "textarea", "order": 40},
                },
            },
            "required": ["project_id", "site_url"],
        },
        integration_requirements=(
            IntegrationRequirement(
                provider_key=GITHUB_PROVIDER,
                capabilities=(
                    "contents.read",
                    "contents.write",
                    "pull_requests.read",
                    "pull_requests.write",
                ),
                required=True,
            ),
        ),
        procedure=CodexProcedureSource(
            root=Path(__file__).parents[2] / "codex_procedures" / SITE_HEALTH_WORKFLOW_NAME,
            entry_skill="site-health-improvement",
            github_pull_request=GitHubPullRequestProcedure(
                receipt_path_template="reports/site-health/{run_id}.md",
                verification_commands=(content_repository_delivery.CHECK_COMMAND,),
                max_files=3,
                allow_no_change=True,
            ),
        ),
    ),
    BuiltinWorkflow(
        id=VISIBILITY_AUDIT_WORKFLOW_ID,
        key=VISIBILITY_AUDIT_WORKFLOW_NAME,
        title="Audit AI visibility",
        description=(
            "Measure whether Luna finds and recommends a chosen target across five target-blind "
            "buyer questions, then publish AI_VISIBILITY.md."
        ),
        executor=VISIBILITY_AUDIT_WORKFLOW_NAME,
        version_label="1.2.0",
        system=ORGANIC_TRAFFIC_SYSTEM,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "target": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "default": "this project",
                    "title": "Target",
                    "description": (
                        "Product, company, domain, or URL to audit. Keep “this project” to "
                        "derive it from durable project context."
                    ),
                    "x-tin-ui": {"control": "text", "order": 10},
                },
            },
            "required": ["project_id", "target"],
        },
    ),
    BuiltinWorkflow(
        id=ANSWER_PAGE_WORKFLOW_ID,
        key=ANSWER_PAGE_WORKFLOW_NAME,
        title="Draft an answer page",
        description=(
            "Create a public-facing Markdown content draft from the latest AI visibility "
            "findings; not for general advice or internal business questions."
        ),
        executor=ANSWER_PAGE_WORKFLOW_NAME,
        version_label="1.2.0",
        prerequisites=(
            WorkflowPrerequisite(
                kind="run",
                level="recommended",
                workflow=VISIBILITY_AUDIT_WORKFLOW_NAME,
                reason="The latest AI visibility audit supplies the questions the page answers.",
            ),
        ),
        system=ORGANIC_TRAFFIC_SYSTEM,
        review_policy=ANSWER_PAGE_REVIEW_POLICY,
    ),
    BuiltinWorkflow(
        id=WEEKLY_BRIEF_WORKFLOW_ID,
        key=WEEKLY_BRIEF_WORKFLOW_NAME,
        title="Create a weekly project brief",
        description=(
            "Summarize what moved, what needs attention, and the smallest useful next steps "
            "from this project's durable week of activity."
        ),
        executor=WEEKLY_BRIEF_WORKFLOW_NAME,
        version_label="1.1.0",
        presentation=WorkflowDiagram(
            nodes=(
                DiagramNode("collect", "step", "collect the week", "runs · files · activity"),
                DiagramNode("context", "store", "project state", "durable evidence"),
                DiagramNode("summarize", "step", "write the brief", "one bounded model call"),
                DiagramNode("publish", "receipt", "weekly brief", "dated file + activity"),
            ),
            edges=(
                DiagramEdge("collect", "context"),
                DiagramEdge("context", "summarize"),
                DiagramEdge("summarize", "publish"),
            ),
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "detail": {
                    "type": "string",
                    "enum": ["concise", "standard"],
                    "default": "concise",
                    "title": "Detail",
                    "x-tin-ui": {"control": "select", "order": 10},
                },
                "include_open_items": {
                    "type": "boolean",
                    "default": True,
                    "title": "Include open items",
                    "x-tin-ui": {"control": "segmented", "order": 20},
                },
                "focus": {
                    "type": "string",
                    "maxLength": 240,
                    "default": "",
                    "title": "Optional focus",
                    "description": "A topic to emphasize without excluding the rest of the week.",
                    "x-tin-ui": {"control": "text", "order": 30},
                },
            },
            "required": ["project_id"],
        },
    ),
    BuiltinWorkflow(
        id=PROJECT_TASK_WORKFLOW_ID,
        key=PROJECT_TASK_WORKFLOW_NAME,
        title="One-off project task",
        description=(
            "Use an isolated Codex task when the founder asks Tin to inspect, research, or change "
            "project files and no narrower registered workflow fits."
        ),
        executor=PROJECT_TASK_WORKFLOW_NAME,
        version_label="1.0.0",
        kind="task",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "instruction": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 8000,
                    "description": "The concrete one-off task to perform for this project.",
                },
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                    "description": "A short human-readable title for the task.",
                },
            },
            "required": ["project_id", "instruction", "title"],
        },
    ),
    BuiltinWorkflow(
        id=RESEARCH_DEEP_DIVE_WORKFLOW_ID,
        key=RESEARCH_DEEP_DIVE_WORKFLOW_NAME,
        title="Research a question deeply",
        description=(
            "Test a project question and its upstream assumptions against current, "
            "source-backed evidence."
        ),
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.1.0",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4000,
                    "title": "Research question",
                    "description": "The decision, claim, or uncertainty the report should resolve.",
                    "x-tin-ui": {"control": "textarea", "order": 10},
                },
                "depth": {
                    "type": "string",
                    "enum": ["focused", "standard", "extensive"],
                    "default": "standard",
                    "title": "Depth",
                    "x-tin-ui": {"control": "select", "order": 20},
                },
                "audience": {
                    "type": "string",
                    "maxLength": 240,
                    "default": "Project founders and decision-makers",
                    "title": "Audience",
                    "x-tin-ui": {"control": "text", "order": 30},
                },
                "known_assumptions": {
                    "type": "string",
                    "maxLength": 4000,
                    "default": "",
                    "title": "Known assumptions",
                    "description": "Claims to test, not facts the report should accept blindly.",
                    "x-tin-ui": {"control": "textarea", "order": 40},
                },
                "constraints": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Scope constraints",
                    "description": "Geography, date range, exclusions, or other hard boundaries.",
                    "x-tin-ui": {"control": "textarea", "order": 50},
                },
            },
            "required": ["project_id", "question"],
        },
        procedure=CodexProcedureSource(
            root=Path(__file__).parents[2] / "codex_procedures" / RESEARCH_DEEP_DIVE_WORKFLOW_NAME,
            entry_skill="research-deep-dive",
            output_path=RESEARCH_DEEP_DIVE_PATH,
            output_max_bytes=300_000,
        ),
    ),
    BuiltinWorkflow(
        id=PUBLIC_ARTICLE_WORKFLOW_ID,
        key=PUBLIC_ARTICLE_WORKFLOW_NAME,
        title="Draft a public article",
        description=(
            "Turn durable project evidence and original thinking into a rigorous, reviewable "
            "public article."
        ),
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.4.1",
        system=ORGANIC_TRAFFIC_SYSTEM,
        prerequisites=(
            WorkflowPrerequisite(
                kind="artifact",
                level="recommended",
                path=STYLE_PATH,
                producer=style_capture.KEY,
                reason="A captured writing style guide shapes expression; voice_notes still win.",
            ),
        ),
        review_policy=PUBLIC_ARTICLE_REVIEW_POLICY,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "brief": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4000,
                    "title": "Article brief",
                    "description": (
                        "The finding, argument, or original work the article should share."
                    ),
                    "x-tin-ui": {"control": "textarea", "order": 10},
                },
                "audience": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 240,
                    "default": "An informed general audience",
                    "title": "Audience",
                    "x-tin-ui": {"control": "text", "order": 20},
                },
                "goal": {
                    "type": "string",
                    "enum": ["explain", "argue", "share_findings", "announce"],
                    "default": "explain",
                    "title": "Goal",
                    "x-tin-ui": {"control": "select", "order": 30},
                },
                "length": {
                    "type": "string",
                    "enum": ["concise", "standard", "long"],
                    "default": "standard",
                    "title": "Length",
                    "x-tin-ui": {"control": "select", "order": 40},
                },
                "source_policy": {
                    "type": "string",
                    "enum": ["project_only", "project_and_web"],
                    "default": "project_and_web",
                    "title": "Sources",
                    "x-tin-ui": {"control": "select", "order": 50},
                },
                "voice_notes": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Voice notes",
                    "description": "Optional tone, vocabulary, or authorial constraints.",
                    "x-tin-ui": {"control": "textarea", "order": 60},
                },
            },
            "required": ["project_id", "brief"],
        },
        procedure=CodexProcedureSource(
            root=Path(__file__).parents[2] / "codex_procedures" / PUBLIC_ARTICLE_WORKFLOW_NAME,
            entry_skill="public-article",
            output_path_template="content/articles/{run_id}.md",
            output_validator="public-article.v2",
            output_max_bytes=300_000,
            project_skills=(
                ProjectSkillDependency(
                    name="writing-style",
                    path=".agents/skills/writing-style/SKILL.md",
                    required=False,
                ),
            ),
        ),
    ),
    BuiltinWorkflow(
        id=CONTENT_DIAGRAM_WORKFLOW_ID,
        key=CONTENT_DIAGRAM_WORKFLOW_NAME,
        title="Create a diagram",
        description=(
            "Turn a process or system into one clear diagram using approved brand guidance. "
            "Its source stays editable in project Files."
        ),
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="2.2.0",
        review_policy=CONTENT_DIAGRAM_REVIEW_POLICY,
        schedule_modes=("on_demand",),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "brief": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4000,
                    "title": "What should it show?",
                    "description": "Describe the process, system, or argument to make visible.",
                    "x-tin-ui": {"control": "textarea", "order": 10},
                },
                "slug": {
                    "type": "string",
                    "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    "minLength": 1,
                    "maxLength": 80,
                    "title": "Filename",
                    "description": "Lowercase words separated by hyphens.",
                    "x-tin-ui": {"control": "text", "order": 20},
                },
                "direction": {
                    "type": "string",
                    "enum": ["LR", "TD"],
                    "default": "LR",
                    "title": "Overall arrangement",
                    "description": "Groups inside a composition can use their own direction.",
                    "x-tin-ui": {"control": "select", "order": 30},
                },
                "context": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Optional context",
                    "description": "Facts or language the diagram should preserve.",
                    "x-tin-ui": {"control": "textarea", "order": 40},
                },
            },
            "required": ["project_id", "brief", "slug"],
        },
        procedure=CodexProcedureSource(
            root=Path(__file__).parents[2] / "codex_procedures" / CONTENT_DIAGRAM_WORKFLOW_NAME,
            entry_skill="content-diagram",
            output_path_template="diagrams/{slug}.mmd",
            output_media_type="text/vnd.mermaid",
            output_validator=TIN_DIAGRAM_BRANDED_VALIDATOR,
            output_max_bytes=64_000,
        ),
    ),
    BuiltinWorkflow(
        id=EMAIL_SHORTLIST_WORKFLOW_ID,
        key=EMAIL_SHORTLIST_WORKFLOW_NAME,
        title="Build an email outreach shortlist",
        description=(
            "Review the connected Gmail and Calendar history to create a bounded, "
            "evidence-backed outreach shortlist in project Files."
        ),
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.0.0",
        system=COLD_OUTREACH_SYSTEM,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "objective": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "title": "Outreach objective",
                    "description": "Who should be shortlisted and why now?",
                    "x-tin-ui": {"control": "textarea", "order": 10},
                },
                "lookback_days": {
                    "type": "integer",
                    "minimum": 7,
                    "maximum": 730,
                    "default": 365,
                    "title": "Lookback days",
                    "x-tin-ui": {"control": "counter", "order": 20},
                },
                "target_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                    "title": "Maximum candidates",
                    "x-tin-ui": {"control": "counter", "order": 30},
                },
                "selection_notes": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Selection notes",
                    "description": "Relationships, exclusions, or signals to prioritize.",
                    "x-tin-ui": {"control": "textarea", "order": 40},
                },
                "include_calendar": {
                    "type": "boolean",
                    "default": True,
                    "title": "Use Calendar context",
                    "x-tin-ui": {"control": "segmented", "order": 50},
                },
            },
            "required": ["project_id", "objective"],
        },
        integration_requirements=(
            IntegrationRequirement(
                provider_key=GOOGLE_WORKSPACE_PROVIDER,
                capabilities=("gmail.messages.read", "calendar.events.read"),
                required=True,
            ),
        ),
        procedure=CodexProcedureSource(
            root=Path(__file__).parents[2] / "codex_procedures" / EMAIL_SHORTLIST_WORKFLOW_NAME,
            entry_skill="email-shortlist",
            output_path=EMAIL_SHORTLIST_PATH,
            output_media_type="text/csv",
            output_validator=EMAIL_SHORTLIST_VALIDATOR,
            output_max_bytes=250_000,
        ),
    ),
    BuiltinWorkflow(
        id=EMAIL_CAMPAIGN_WORKFLOW_ID,
        key=EMAIL_CAMPAIGN_WORKFLOW_NAME,
        title="Run an email outreach campaign",
        description=(
            "Snapshot selected shortlist recipients and exact email copy for approval, then "
            "send with reply-aware follow-up timers."
        ),
        executor=EMAIL_CAMPAIGN_WORKFLOW_NAME,
        version_label="1.1.0",
        prerequisites=(
            WorkflowPrerequisite(
                kind="artifact",
                level="required",
                path=EMAIL_SHORTLIST_PATH,
                producer=EMAIL_SHORTLIST_WORKFLOW_NAME,
                reason="The campaign sends only to rows marked selected in the shortlist.",
            ),
        ),
        system=COLD_OUTREACH_SYSTEM,
        review_policy=EMAIL_CAMPAIGN_REVIEW_POLICY,
        schedule_modes=("on_demand",),
        presentation=WorkflowDiagram(
            nodes=(
                DiagramNode("snapshot", "store", "campaign snapshot", "recipients + exact copy"),
                DiagramNode("approval", "gate", "needs you", "approve the campaign"),
                DiagramNode("send", "surface", "Gmail", "paced initial sends"),
                DiagramNode("wait", "wait", "wait", "⧖ follow-up window"),
                DiagramNode("follow_up", "step", "check + follow up", "suppressed on reply"),
                DiagramNode("receipt", "receipt", "delivery ledger", "one receipt per touch"),
            ),
            edges=(
                DiagramEdge("snapshot", "approval"),
                DiagramEdge("approval", "send"),
                DiagramEdge("send", "wait"),
                DiagramEdge("wait", "follow_up"),
                DiagramEdge("follow_up", "receipt"),
            ),
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "subject": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "title": "Subject",
                    "description": "Use {{name}} for the recipient's name.",
                    "x-tin-ui": {"control": "text", "order": 10},
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 20000,
                    "title": "Email",
                    "description": "The exact initial message. Use {{name}} when useful.",
                    "x-tin-ui": {"control": "textarea", "order": 20},
                },
                "follow_up_body": {
                    "type": "string",
                    "maxLength": 20000,
                    "default": "",
                    "title": "Optional follow-up",
                    "description": "Sent only when Tin finds no reply.",
                    "x-tin-ui": {"control": "textarea", "order": 30},
                },
                "follow_up_delay_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "default": 4,
                    "title": "Follow-up delay",
                    "x-tin-ui": {"control": "counter", "order": 40},
                },
                "send_interval_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3600,
                    "default": 60,
                    "title": "Seconds between recipients",
                    "description": "Minimum pacing between initial sends in this campaign.",
                    "x-tin-ui": {"control": "counter", "order": 50},
                },
                "daily_send_cap": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 25,
                    "title": "Daily send cap",
                    "description": "Shared safety cap for this connected sender account.",
                    "x-tin-ui": {"control": "counter", "order": 60},
                },
                "send_window_start": {
                    "type": "string",
                    "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
                    "default": "09:00",
                    "title": "Send after",
                    "x-tin-ui": {"control": "text", "order": 70},
                },
                "send_window_end": {
                    "type": "string",
                    "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
                    "default": "17:00",
                    "title": "Stop sending at",
                    "x-tin-ui": {"control": "text", "order": 80},
                },
                "send_timezone": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100,
                    "default": "UTC",
                    "title": "Send timezone",
                    "description": "An IANA timezone such as Europe/Berlin.",
                    "x-tin-ui": {"control": "text", "order": 90},
                },
            },
            "required": ["project_id", "subject", "body"],
        },
        integration_requirements=(
            IntegrationRequirement(
                provider_key=GOOGLE_WORKSPACE_PROVIDER,
                capabilities=(
                    "gmail.messages.send",
                    "gmail.messages.read",
                    "gmail.history.read",
                ),
                required=True,
            ),
        ),
    ),
    BuiltinWorkflow(
        id=QA_SIGNUP_WALKTHROUGH_WORKFLOW_ID,
        key=QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME,
        title="Walk the signup as a new user",
        description=(
            "Sign up for your product as a stranger with a Tin-owned test account, verify the "
            "email, reach first activation, and report every break along the way."
        ),
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.3.0",
        system=PRODUCT_QA_SYSTEM,
        schedule_modes=("on_demand",),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "product_url": {
                    "type": "string",
                    "format": "uri",
                    "minLength": 8,
                    "maxLength": 2000,
                    "title": "Product URL",
                    "description": "The landing page a new user arrives at.",
                    "x-tin-ui": {"control": "text", "order": 10},
                },
                "notes": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Notes for the walkthrough",
                    "description": "Where the free trial lives, what first activation means, "
                    "or anything a stranger would not know.",
                    "x-tin-ui": {"control": "textarea", "order": 20},
                },
            },
            "required": ["project_id", "product_url"],
        },
        integration_requirements=(
            IntegrationRequirement(
                provider_key=GOOGLE_WORKSPACE_PROVIDER,
                capabilities=("gmail.messages.read",),
                required=True,
            ),
        ),
        procedure=CodexProcedureSource(
            root=Path(__file__).parents[2]
            / "codex_procedures"
            / QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME,
            entry_skill="signup-walkthrough",
            output_path_template=SIGNUP_WALKTHROUGH_PATH_TEMPLATE,
            output_validator=SIGNUP_WALKTHROUGH_VALIDATOR,
            output_max_bytes=200_000,
            sandbox=SandboxProfile(
                profile=BROWSER_SANDBOX_PROFILE,
                timeout_seconds=1800,
                egress=OPEN_SANDBOX_EGRESS,
            ),
            identity=True,
        ),
    ),
    BuiltinWorkflow(
        id=PRODUCT_CODE_MAP_WORKFLOW_ID,
        key=PRODUCT_CODE_MAP_WORKFLOW_NAME,
        title="Map the product from its code",
        description=(
            "Read the connected GitHub repository and write the Code map section of project "
            "memory: every user-facing surface with its exposure, plans and gating, "
            "integrations, and tech stack, each cited to file and line."
        ),
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.0.1",
        system=PRODUCT_QA_SYSTEM,
        schedule_modes=("on_demand", "weekly"),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "focus": {
                    "type": "string",
                    "maxLength": 500,
                    "default": "",
                    "title": "Focus",
                    "description": (
                        "An area, route, or module to read first and most deeply. Leave empty "
                        "to map evenly."
                    ),
                    "x-tin-ui": {"control": "text", "order": 10},
                },
                "depth": {
                    "type": "string",
                    "enum": ["focused", "standard", "extensive"],
                    "default": "standard",
                    "title": "Depth",
                    "x-tin-ui": {"control": "select", "order": 20},
                },
            },
            "required": ["project_id"],
        },
        integration_requirements=(
            IntegrationRequirement(
                provider_key=GITHUB_PROVIDER,
                capabilities=("contents.read",),
                required=True,
            ),
        ),
        procedure=CodexProcedureSource(
            root=Path(__file__).parents[2] / "codex_procedures" / PRODUCT_CODE_MAP_WORKFLOW_NAME,
            entry_skill="product-code-map",
            output_path=MEMORY_INDEX_PATH,
            output_validator=MEMORY_SECTION_VALIDATOR,
            output_max_bytes=100_000,
            output_section=OutputSection(
                parent=MEMORY_SECTION_PARENT, heading=CODE_MAP_SECTION, max_bytes=16_000
            ),
            github_workspace=GitHubRepositoryWorkspace(),
            sandbox=SandboxProfile(timeout_seconds=1800),
        ),
    ),
    BuiltinWorkflow(
        id=PRODUCT_DEEP_DIVE_WORKFLOW_ID,
        key=PRODUCT_DEEP_DIVE_WORKFLOW_NAME,
        title="Map what the product actually does",
        description=(
            "Read the docs, sign in and use your product with a Tin-owned account, reconcile "
            "with the code map, and write the Feature map section of project memory with "
            "evidence on every feature."
        ),
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.1.0",
        prerequisites=(
            WorkflowPrerequisite(
                kind="identity",
                level="required",
                reuse="active",
                host_input="product_url",
                producer=QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME,
                reason="The deep dive reuses the test account the signup walkthrough created.",
            ),
            WorkflowPrerequisite(
                kind="run",
                level="required",
                workflow=QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME,
                match=("product_host",),
                reason="The newest signup report gives the signup path, onboarding order and walls",
            ),
            WorkflowPrerequisite(
                kind="artifact",
                level="recommended",
                path=MEMORY_INDEX_PATH,
                section=CODE_MAP_SECTION,
                producer=PRODUCT_CODE_MAP_WORKFLOW_NAME,
                reason="The Code map adds the code lens; without it the map has docs and live only",
            ),
        ),
        system=PRODUCT_QA_SYSTEM,
        schedule_modes=("on_demand",),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "product_url": {
                    "type": "string",
                    "format": "uri",
                    "minLength": 8,
                    "maxLength": 2000,
                    "title": "Product URL",
                    "description": "The landing page a new user arrives at.",
                    "x-tin-ui": {"control": "text", "order": 10},
                },
                "docs_urls": {
                    "type": "string",
                    "maxLength": 4000,
                    "default": "",
                    "title": "Docs, pricing, and changelog URLs",
                    "description": (
                        "One URL per line. Leave empty to find them from the landing page."
                    ),
                    "x-tin-ui": {"control": "textarea", "order": 20},
                },
                "depth": {
                    "type": "string",
                    "enum": ["focused", "standard", "extensive"],
                    "default": "standard",
                    "title": "Depth",
                    "x-tin-ui": {"control": "select", "order": 30},
                },
                "notes": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Notes",
                    "description": (
                        "What the core action is, where gated areas live, or anything a "
                        "stranger would not know. Never credentials."
                    ),
                    "x-tin-ui": {"control": "textarea", "order": 40},
                },
            },
            "required": ["project_id", "product_url"],
        },
        integration_requirements=(
            IntegrationRequirement(
                provider_key=GOOGLE_WORKSPACE_PROVIDER,
                capabilities=("gmail.messages.read",),
                required=True,
            ),
        ),
        procedure=CodexProcedureSource(
            root=Path(__file__).parents[2] / "codex_procedures" / PRODUCT_DEEP_DIVE_WORKFLOW_NAME,
            entry_skill="product-deep-dive",
            output_path=MEMORY_INDEX_PATH,
            output_validator=MEMORY_SECTION_VALIDATOR,
            output_max_bytes=100_000,
            output_section=OutputSection(
                parent=MEMORY_SECTION_PARENT, heading=FEATURE_MAP_SECTION, max_bytes=24_000
            ),
            sandbox=SandboxProfile(
                profile=BROWSER_SANDBOX_PROFILE,
                timeout_seconds=3600,
                egress=OPEN_SANDBOX_EGRESS,
            ),
            identity=TestIdentityPolicy(create=True, reuse=IDENTITY_REUSE_ACTIVE),
        ),
    ),
    BuiltinWorkflow(
        id=QA_PRODUCT_AUDIT_WORKFLOW_ID,
        key=QA_PRODUCT_AUDIT_WORKFLOW_NAME,
        title="Audit the product feature by feature",
        description=(
            "Exercise every feature in the project's Feature map as a user with a Tin-owned "
            "account and report what works, what is broken, and what to improve, with "
            "reproducible evidence."
        ),
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.1.0",
        prerequisites=(
            WorkflowPrerequisite(
                kind="identity",
                level="required",
                reuse="active",
                host_input="product_url",
                producer=QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME,
                reason="The audit signs in with the test account the signup walkthrough created.",
            ),
            WorkflowPrerequisite(
                kind="run",
                level="required",
                workflow=QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME,
                match=("product_host",),
                reason="The newest signup report gives the signup path, onboarding order and walls",
            ),
            WorkflowPrerequisite(
                kind="artifact",
                level="required",
                path=MEMORY_INDEX_PATH,
                section=FEATURE_MAP_SECTION,
                producer=PRODUCT_DEEP_DIVE_WORKFLOW_NAME,
                reason="The Feature map is the audit checklist and scope names one of its areas.",
            ),
        ),
        system=PRODUCT_QA_SYSTEM,
        schedule_modes=("on_demand",),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "product_url": {
                    "type": "string",
                    "format": "uri",
                    "minLength": 8,
                    "maxLength": 2000,
                    "title": "Product URL",
                    "description": "The landing page a new user arrives at.",
                    "x-tin-ui": {"control": "text", "order": 10},
                },
                "scope": {
                    "type": "string",
                    "maxLength": 200,
                    "default": "all",
                    "title": "Scope",
                    "description": (
                        "`all`, or one area name exactly as it appears in the Feature map."
                    ),
                    "x-tin-ui": {"control": "text", "order": 20},
                },
                "depth": {
                    "type": "string",
                    "enum": ["focused", "standard", "extensive"],
                    "default": "standard",
                    "title": "Depth",
                    "x-tin-ui": {"control": "select", "order": 30},
                },
                "notes": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Notes",
                    "description": (
                        "Known problem areas, what to check first, or anything a stranger "
                        "would not know. Never credentials."
                    ),
                    "x-tin-ui": {"control": "textarea", "order": 40},
                },
            },
            "required": ["project_id", "product_url"],
        },
        integration_requirements=(
            IntegrationRequirement(
                provider_key=GOOGLE_WORKSPACE_PROVIDER,
                capabilities=("gmail.messages.read",),
                required=True,
            ),
        ),
        procedure=CodexProcedureSource(
            root=Path(__file__).parents[2] / "codex_procedures" / QA_PRODUCT_AUDIT_WORKFLOW_NAME,
            entry_skill="product-audit",
            output_path_template=PRODUCT_AUDIT_PATH_TEMPLATE,
            output_validator=PRODUCT_AUDIT_VALIDATOR,
            output_max_bytes=300_000,
            sandbox=SandboxProfile(
                profile=BROWSER_SANDBOX_PROFILE,
                timeout_seconds=3600,
                egress=OPEN_SANDBOX_EGRESS,
            ),
            identity=TestIdentityPolicy(create=True, reuse=IDENTITY_REUSE_ACTIVE),
        ),
    ),
    BuiltinWorkflow(
        id=CREATIVE_CHARACTER_WORKFLOW_ID,
        key=CREATIVE_CHARACTER_WORKFLOW_NAME,
        title="Design a brand character",
        description=(
            "Use when the founder has an explicit brand-design need. "
            "Design a vector mascot as an animatable SVG "
            "character in project Files (three mouth shapes, a blink, and a payoff "
            "expression), ready to narrate demo videos and appear in marketing. Tin reads the "
            "product page and project memory itself and asks one model for the drawing; about "
            "two minutes, no sandbox."
        ),
        executor=CREATIVE_CHARACTER_WORKFLOW_NAME,
        version_label="1.2.0",
        prerequisites=(
            WorkflowPrerequisite(
                kind="artifact",
                level="recommended",
                path=MEMORY_INDEX_PATH,
                producer=PROJECT_MEMORY_WORKFLOW_NAME,
                reason="Project memory grounds the character in the product's positioning.",
            ),
        ),
        system=CREATIVE_STUDIO_SYSTEM,
        review_policy=CHARACTER_REVIEW_POLICY,
        schedule_modes=("on_demand",),
        model_route=CHARACTER_MODEL_ROUTE,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "slug": {
                    "type": "string",
                    "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    "minLength": 1,
                    "maxLength": 80,
                    "title": "Character name",
                    "description": "Lowercase words separated by hyphens; the file name.",
                    "x-tin-ui": {"control": "text", "order": 10},
                },
                "brief": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Brief",
                    "description": (
                        "Who the character is: species or object, personality, what it "
                        "holds or wears. Leave empty to let Tin derive it from the product."
                    ),
                    "x-tin-ui": {"control": "textarea", "order": 20},
                },
                "product_url": {
                    "type": "string",
                    "maxLength": 500,
                    "default": "",
                    "title": "Product page",
                    "description": "A public HTTPS page Tin reads for audience, tone, and colors.",
                    "x-tin-ui": {"control": "text", "order": 30},
                },
                "notes": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Notes",
                    "description": "Colors to keep or avoid, references, things it must not be.",
                    "x-tin-ui": {"control": "textarea", "order": 40},
                },
            },
            "required": ["project_id", "slug"],
        },
    ),
    BuiltinWorkflow(
        id=CREATIVE_PRODUCT_DEMO_WORKFLOW_ID,
        key=CREATIVE_PRODUCT_DEMO_WORKFLOW_NAME,
        title="Make a product demo video",
        description=(
            "Capture the founder's live product at phone size and render a smooth 9:16 "
            "short-form demo video (TikTok, Reels, Shorts) with a pain hook, voiceover, "
            "word-synced captions, tap and scroll motion, and an optional narrating "
            "character from project Files."
        ),
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.1.1",
        prerequisites=(
            WorkflowPrerequisite(
                kind="artifact",
                level="required",
                path="characters/{character}.svg",
                producer=CREATIVE_CHARACTER_WORKFLOW_NAME,
                reason="The demo animates the named character; design it first or leave it empty.",
            ),
            WorkflowPrerequisite(
                kind="artifact",
                level="recommended",
                path=MEMORY_INDEX_PATH,
                section=FEATURE_MAP_SECTION,
                producer=PRODUCT_DEEP_DIVE_WORKFLOW_NAME,
                reason="Claims come from the Feature map; without it the script uses page text.",
            ),
        ),
        system=CREATIVE_STUDIO_SYSTEM,
        review_policy=DEMO_VIDEO_REVIEW_POLICY,
        schedule_modes=("on_demand",),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string", "format": "uuid"},
                "slug": {
                    "type": "string",
                    "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    "minLength": 1,
                    "maxLength": 80,
                    "title": "Video name",
                    "description": "Lowercase words separated by hyphens; the file name.",
                    "x-tin-ui": {"control": "text", "order": 10},
                },
                "product_url": {
                    "type": "string",
                    "format": "uri",
                    "minLength": 1,
                    "maxLength": 2048,
                    "title": "Product URL",
                    "description": "The public page the demo starts on.",
                    "x-tin-ui": {"control": "text", "order": 20},
                },
                "angle": {
                    "type": "string",
                    "maxLength": 1000,
                    "default": "",
                    "title": "Angle",
                    "description": (
                        "The pain or moment the hook should name and the one thing to "
                        "show. Leave empty to let Tin pick from project memory."
                    ),
                    "x-tin-ui": {"control": "textarea", "order": 30},
                },
                "character": {
                    "type": "string",
                    "pattern": "^(?:[a-z0-9]+(?:-[a-z0-9]+)*)?$",
                    "maxLength": 80,
                    "default": "",
                    "title": "Character",
                    "description": (
                        "Name of a character in project Files (characters/<name>.svg) to "
                        "narrate the video. Empty means captions only."
                    ),
                    "x-tin-ui": {"control": "text", "order": 40},
                },
                "voice": {
                    "type": "string",
                    "enum": list(STUDIO_VOICES),
                    "default": STUDIO_VOICES[0],
                    "title": "Voice",
                    "x-tin-ui": {"control": "select", "order": 50},
                },
                "backdrop": {
                    "type": "string",
                    "enum": ["mesh", "brand", "aurora", "paper", "bold"],
                    "default": "mesh",
                    "title": "Backdrop",
                    "x-tin-ui": {"control": "select", "order": 60},
                },
                "language": {
                    "type": "string",
                    "maxLength": 40,
                    "default": "English (US)",
                    "title": "Language",
                    "description": "Spoken language of the voiceover, for example English (US).",
                    "x-tin-ui": {"control": "text", "order": 70},
                },
                "notes": {
                    "type": "string",
                    "maxLength": 2000,
                    "default": "",
                    "title": "Notes",
                    "description": "Pages to visit, claims to avoid, a call to action to end on.",
                    "x-tin-ui": {"control": "textarea", "order": 80},
                },
            },
            "required": ["project_id", "slug", "product_url"],
        },
        procedure=CodexProcedureSource(
            root=Path(__file__).parents[2]
            / "codex_procedures"
            / CREATIVE_PRODUCT_DEMO_WORKFLOW_NAME,
            entry_skill="product-demo-video",
            output_path_template="demos/{slug}.mp4",
            output_media_type=DEMO_VIDEO_MEDIA_TYPE,
            output_validator=DEMO_VIDEO_VALIDATOR,
            output_max_bytes=MAX_DEMO_VIDEO_BYTES,
            sandbox=SandboxProfile(
                profile=STUDIO_SANDBOX_PROFILE,
                timeout_seconds=3600,
                egress=OPEN_SANDBOX_EGRESS,
            ),
        ),
    ),
    BuiltinWorkflow(
        id=GROWTH_ONBOARDING_WORKFLOW_ID,
        key=growth_onboarding.KEY,
        title="Start here: onboard this business",
        description=(
            "Part 1: your agent fills the form from the codebase and the founder's two answers, "
            "and Tin writes the plan: the marketing systems ranked, what is usable in Tin, and "
            "what Tin would run for each. Part 2: the founder says in their words what Tin "
            "takes on and connects what it needs; Tin then sets those systems up as schedules "
            "and first runs and tells them what runs, what to expect and where to watch."
        ),
        executor=growth_onboarding.KEY,
        version_label="1.1.0",
        system=START_HERE_SYSTEM,
        agent_only=True,
        review_policy=GROWTH_ONBOARDING_REVIEW_POLICY,
        schedule_modes=("on_demand",),
        input_schema=growth_onboarding.INPUT_SCHEMA,
    ),
    BuiltinWorkflow(
        id=GROWTH_ONBOARDING_PLAN_WORKFLOW_ID,
        key=GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME,
        title="Start here: plan how Tin grows this business",
        description=(
            "From the product URL when there is one, the systems and notes your agent fills "
            "from the codebase, "
            "and the founder's multiple-choice constraints, read the site and any memory and "
            "write one plan: the marketing systems ranked for this business, which are usable "
            "in Tin today, a proposed scope, and what Tin would run system by system. Run "
            "inside Start here: onboard this business to have Tin set the picks up."
        ),
        # An LLM flow: code owns the sequence, scoring, availability and rendering; models supply
        # judgment. It replaced a Codex procedure that spent most of four minutes typing the file.
        executor=growth_plan.KEY,
        version_label="3.1.0",
        system=START_HERE_SYSTEM,
        agent_only=True,
        schedule_modes=("on_demand",),
        input_schema=growth_onboarding.INPUT_SCHEMA,
    ),
    BuiltinWorkflow(
        id=PAID_ADS_ASSESSMENT_WORKFLOW_ID,
        key=paid_ads.KEY,
        title="Assess paid ads for this business",
        description=(
            "Decide whether Google Search ads fit: a verdict, the constraint that binds it, a "
            "scorecard and a rough campaign shape from Keyword Planner, DataForSEO, Search "
            "Console when connected and the site. Advisory only; nothing is created or spent "
            "on ads."
        ),
        # An LLM flow: code owns economics, scoring, the verdict and rendering; five bounded
        # model steps read evidence, label keywords, diagnose history and shape the campaign.
        executor=paid_ads.KEY,
        version_label="0.2.0",
        system=PAID_ADS_SYSTEM,
        schedule_modes=("on_demand",),
        input_schema=paid_ads.INPUT_SCHEMA,
        prerequisites=(
            WorkflowPrerequisite(
                kind="run",
                level="recommended",
                workflow=GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME,
                via_input="onboarding_run_id",
                reason="The Start here plan's answers prefill the business profile.",
            ),
            WorkflowPrerequisite(
                kind="run",
                level="recommended",
                workflow=KEYWORD_KEY,
                via_input="keyword_run_id",
                reason="A keyword plan supplies seeds and competitors the assessment reuses.",
            ),
            WorkflowPrerequisite(
                kind="run",
                level="recommended",
                workflow=AUDIT_KEY,
                via_input="audit_run_id",
                reason="An audit supplies landing-page facts the readiness score uses.",
            ),
        ),
        integration_requirements=(
            IntegrationRequirement(GSC_PROVIDER, ("search_analytics.read",), required=False),
            IntegrationRequirement(GITHUB_PROVIDER, ("contents.read",), required=False),
        ),
    ),
    BuiltinWorkflow(
        id=PAID_ADS_LAUNCH_WORKFLOW_ID,
        key=paid_ads_launch.KEY,
        title="Launch a Google Ads campaign",
        description=(
            "Turn an assessment's campaign shape into one live Google Search campaign in your "
            "own Ads account. Creates nothing until you approve the exact plan."
        ),
        # An LLM flow with one approval: code decides the structure, budget and bids; model
        # steps write the ads and the founder brief; the founder approves before any write.
        executor=paid_ads_launch.KEY,
        version_label="0.1.0",
        system=PAID_ADS_SYSTEM,
        review_policy=PAID_ADS_LAUNCH_REVIEW_POLICY,
        schedule_modes=("on_demand",),
        input_schema=paid_ads_launch.INPUT_SCHEMA,
        prerequisites=(
            WorkflowPrerequisite(
                kind="run",
                level="required",
                workflow=paid_ads.KEY,
                via_input="assessment_run_id",
                reason="The assessment's campaign shape and keywords are what gets launched.",
            ),
        ),
        integration_requirements=(
            IntegrationRequirement(ADS_PROVIDER, ("campaigns.write",), required=True),
            IntegrationRequirement(
                GITHUB_PROVIDER,
                ("contents.read", "contents.write", "pull_requests.write"),
                required=False,
            ),
        ),
    ),
    BuiltinWorkflow(
        id=PAID_ADS_MONITOR_WORKFLOW_ID,
        key=paid_ads_monitor.KEY,
        title="Check the Google Ads campaign",
        description=(
            "Read the launched campaign, add negatives from wasted search terms, pause "
            "disapproved ads and wasteful keywords on its own, and propose budget or bidding "
            "changes for your approval."
        ),
        executor=paid_ads_monitor.KEY,
        version_label="0.1.0",
        system=PAID_ADS_SYSTEM,
        schedule_modes=("on_demand", "daily", "weekly"),
        input_schema=paid_ads_monitor.INPUT_SCHEMA,
        prerequisites=(
            WorkflowPrerequisite(
                kind="run",
                level="required",
                workflow=paid_ads_launch.KEY,
                via_input="launch_run_id",
                reason="The monitor looks after the campaign a launch created.",
            ),
        ),
        integration_requirements=(
            IntegrationRequirement(ADS_PROVIDER, ("campaigns.write",), required=True),
        ),
    ),
)


# A published workflow keeps its executor. Each entry here is one reviewed exception: the key,
# the executor it was published with, and the executor that replaces it. Runs keep the executor
# they were created with; only runs created after the transition use the new one.
EXECUTOR_TRANSITIONS: dict[str, tuple[str, str]] = {
    growth_plan.KEY: (CODEX_PROCEDURE_EXECUTOR, growth_plan.KEY),
}


def executor_replaced_by(builtin_key: str, executor: str) -> str | None:
    """The published executor this built-in's executor is allowed to replace, if any."""
    previous, current = EXECUTOR_TRANSITIONS.get(builtin_key, (None, None))
    return previous if current == executor else None


PARENT_CHILD_KEYS: dict[str, tuple[str, ...]] = {
    organic_system.KEY: tuple(organic_system.STEPS.values()),
    growth_onboarding.KEY: tuple(growth_onboarding.STEPS.values()),
}


async def sync_builtin_workflows(
    *, database: Database, storage: CodeStorage, system_wiki: SystemWikiRef
) -> None:
    candidates = []
    for builtin in BUILTIN_WORKFLOWS:
        definition, resources = builtin.definition_and_files_with_wiki(system_wiki)
        canonical_bytes = (json.dumps(definition, indent=2, sort_keys=True) + "\n").encode()
        candidates.append(
            (builtin, definition, {builtin.definition_path: canonical_bytes, **resources})
        )
    candidates.extend(
        (package, package.definition, package.files) for package in await load_public_workflows()
    )
    if len({source.id for source, _, _ in candidates}) != len(candidates) or len(
        {source.key for source, _, _ in candidates}
    ) != len(candidates):
        raise RuntimeError("public workflow IDs and keys must be unique across the catalog")
    prepared = {}
    prerequisites: dict[str, tuple[WorkflowPrerequisite, ...]] = {}
    for builtin, definition, files in candidates:
        existing = await database.get_workflow(builtin.id)
        if existing and (existing.project_id is not None or existing.key != builtin.key):
            raise RuntimeError("built-in workflow ID belongs to a different workflow")
        if (
            existing
            and existing.current_commit_sha
            and (
                existing.executor
                not in {builtin.executor, executor_replaced_by(builtin.key, builtin.executor)}
                or existing.definition_repo_id != REGISTRY_REPO_ID
                or existing.definition_path != builtin.definition_path
            )
        ):
            raise RuntimeError("published workflow executor and source location cannot change")
        validate_input_schema(definition["input_schema"])
        if definition.get("system") is not None and definition["system"] not in WORKFLOW_SYSTEM_IDS:
            raise ValueError(f"workflow {builtin.key} references an unknown system")
        parse_integration_requirements(definition.get("integration_requirements"))
        prerequisites[builtin.key] = parse_workflow_prerequisites(
            definition.get("prerequisites"), input_schema=definition["input_schema"]
        )
        prepared[builtin.key] = (
            existing,
            definition,
            files,
        )
    try:
        validate_prerequisite_graph(prerequisites)
    except ValueError as exc:
        raise RuntimeError(f"built-in workflow prerequisites are invalid: {exc}") from exc
    for system in WORKFLOW_SYSTEMS:
        await database.upsert_workflow_system(
            system_id=system.id,
            name=system.name,
            display_order=system.display_order,
        )
    for builtin, _, _ in candidates:
        existing, definition, files = prepared[builtin.key]
        if builtin.key in PARENT_CHILD_KEYS:
            # The parent's immutable revision must contain all of its exact child
            # definitions and resources, including on first publication or upgrade.
            files = dict(files)
            for child_key in PARENT_CHILD_KEYS[builtin.key]:
                files.update(prepared[child_key][2])
        commit_sha = await storage.publish_workflow_files(
            repo_id=REGISTRY_REPO_ID,
            branch="main",
            files=files,
            commit_message=f"Publish {builtin.key} v{builtin.version_label}",
            known_commit_sha=existing.current_commit_sha if existing else None,
        )
        await database.upsert_registry_workflow(
            workflow_id=builtin.id,
            key=builtin.key,
            title=builtin.title,
            description=builtin.description,
            executor=builtin.executor,
            replaces_executor=executor_replaced_by(builtin.key, builtin.executor),
            definition_repo_id=REGISTRY_REPO_ID,
            definition_path=builtin.definition_path,
            current_commit_sha=commit_sha,
            version_label=builtin.version_label,
            definition=definition,
        )
