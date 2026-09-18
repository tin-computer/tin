from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from tin_lite import content_plan, content_plan_editorial, growth_plan, style_capture
from tin_lite.activities import TinActivities
from tin_lite.activity_lanes import (
    CODEX_ACTIVITIES,
    TRUSTED_ACTIVITIES,
    ActivityLaneInterceptor,
    trusted_task_queue,
)
from tin_lite.answer_page import AnswerPageDrafter
from tin_lite.catalog import sync_builtin_workflows
from tin_lite.character_design import MODEL_ROUTE as CHARACTER_MODEL_ROUTE
from tin_lite.character_design import CharacterDesigner
from tin_lite.character_design_activities import CharacterDesignActivities
from tin_lite.code_storage import CodeStorage
from tin_lite.codex_api_relay import CodexAPIRelay
from tin_lite.content_plan_activities import ContentPlanActivities
from tin_lite.db import Database
from tin_lite.e2b_runtime import E2BRuntime
from tin_lite.growth_onboarding_activities import GrowthOnboardingActivities
from tin_lite.growth_plan_activities import GrowthPlanActivities
from tin_lite.integrations import IntegrationService
from tin_lite.keyword_plan import POLICY as KEYWORD_POLICY
from tin_lite.keyword_plan import ROUTE_KEY as KEYWORD_ROUTE_KEY
from tin_lite.keyword_plan_activities import KeywordPlanActivities
from tin_lite.luna import LunaService, OpenAIResponsesClient, TinWorkflowApiClient
from tin_lite.memory import MemoryGardener
from tin_lite.model_providers import (
    ModelCapability,
    ModelRoute,
    ModelRouter,
    ProviderName,
    configured_model_router,
)
from tin_lite.model_usage import ModelUsageRecorder
from tin_lite.organic_audit_activities import OrganicAuditActivities
from tin_lite.organic_system_activities import OrganicSystemActivities
from tin_lite.output_resolution import OutputResolutionService
from tin_lite.project_files import ProjectFileService
from tin_lite.scan import ScanReporter
from tin_lite.settings import Settings
from tin_lite.site_health import SITE_HEALTH_MODEL_ROUTE, SiteHealthImprover
from tin_lite.skills import load_skill_suite
from tin_lite.studio import StudioService
from tin_lite.style_capture_activities import StyleCaptureActivities
from tin_lite.system_wiki import sync_system_wiki
from tin_lite.visibility import VisibilityAuditor
from tin_lite.weekly_brief import WeeklyBriefReporter
from tin_lite.worker_group import WorkerGroup
from tin_lite.workflows import registered_workflows

ROOT = Path(__file__).parents[2]


@dataclass(frozen=True)
class RuntimeServices:
    database: Database
    storage: CodeStorage
    sandboxes: E2BRuntime
    temporal: Client
    worker: WorkerGroup
    luna: LunaService | None
    model_router: ModelRouter
    integrations: IntegrationService
    project_files: ProjectFileService
    output_resolution: OutputResolutionService
    studio: StudioService
    codex_api: CodexAPIRelay | None = None


async def build_runtime(settings: Settings) -> RuntimeServices:
    database = Database(settings.runtime_dsn)
    await database.connect()
    from tin_lite.billing import configure_billing

    try:
        await configure_billing(database, settings)
    except Exception:
        await database.close()
        raise
    storage = CodeStorage(
        organization=settings.code_storage_org,
        private_key=settings.code_storage_api_key.get_secret_value(),
    )
    system_wiki = await sync_system_wiki(
        storage=storage,
        source_root=ROOT / "system_wiki",
    )
    await sync_builtin_workflows(
        database=database,
        storage=storage,
        system_wiki=system_wiki,
    )
    sandboxes = E2BRuntime(
        api_key=settings.e2b_api_key.get_secret_value(),
        template=settings.e2b_template,
        browser_template=settings.e2b_browser_template,
        studio_template=settings.e2b_studio_template,
        isolated_template=settings.e2b_isolated_template,
        browser_api_template=settings.e2b_browser_api_template,
        studio_api_template=settings.e2b_studio_api_template,
        timeout_seconds=settings.sandbox_timeout_seconds,
        egress_allow_hosts=settings.egress_allow_hosts,
        usage_database=database,
        proxy_grant_dir=settings.proxy_grant_dir,
    )
    model_routes = (
        (
            SITE_HEALTH_MODEL_ROUTE,
            CHARACTER_MODEL_ROUTE,
            style_capture.ROUTE,
            *growth_plan.ROUTES,
            ModelRoute(
                key=content_plan.ROUTE_KEY,
                provider=ProviderName.OPENAI,
                model=content_plan.POLICY["model"],
                capabilities=frozenset({ModelCapability.TEXT, ModelCapability.JSON_SCHEMA}),
            ),
            ModelRoute(
                key=content_plan_editorial.ROUTE_KEY,
                provider=ProviderName.OPENAI,
                model=content_plan_editorial.POLICY["model"],
                capabilities=frozenset({ModelCapability.TEXT, ModelCapability.JSON_SCHEMA}),
            ),
            ModelRoute(
                key=KEYWORD_ROUTE_KEY,
                provider=ProviderName.OPENAI,
                model=KEYWORD_POLICY["model"],
                capabilities=frozenset({ModelCapability.TEXT, ModelCapability.JSON_SCHEMA}),
            ),
        )
        if settings.luna_api_key is not None
        else ()
    )
    from tin_lite.code_models import registered_routes as code_model_routes

    if settings.luna_api_key is not None:
        model_routes += code_model_routes()
    model_router = configured_model_router(
        settings, routes=model_routes, recorder=ModelUsageRecorder(database)
    )
    integrations = IntegrationService(database=database, settings=settings)
    project_files = ProjectFileService(database=database, storage=storage)
    temporal = await Client.connect(
        settings.temporal_endpoint,
        namespace=settings.temporal_namespace,
        api_key=settings.temporal_api_key.get_secret_value(),
        tls=True,
    )
    responses = None
    memory_gardener = None
    scan_reporter = None
    visibility_auditor = None
    answer_page_drafter = None
    weekly_brief_reporter = None
    site_health_improver = None
    if settings.luna_api_key is not None:
        responses = OpenAIResponsesClient(
            api_key=settings.luna_api_key.get_secret_value(),
            model=settings.luna_model,
            base_url=settings.luna_base_url,
            timeout_seconds=settings.luna_timeout_seconds,
        )
        memory_gardener = MemoryGardener(
            responses=responses,
            skill_suite=load_skill_suite(ROOT / "workflow_skills" / "project-memory"),
        )
        scan_reporter = ScanReporter(
            responses=responses,
            skill_suite=load_skill_suite(ROOT / "workflow_skills" / "scan-report"),
        )
        visibility_auditor = VisibilityAuditor(
            responses=responses,
            skill_suite=load_skill_suite(ROOT / "workflow_skills" / "visibility-audit"),
        )
        answer_page_drafter = AnswerPageDrafter(
            responses=responses,
            skill_suite=load_skill_suite(ROOT / "workflow_skills" / "answer-page"),
        )
        weekly_brief_reporter = WeeklyBriefReporter(
            responses=responses,
            skill_suite=load_skill_suite(ROOT / "workflow_skills" / "weekly-brief"),
        )
        site_health_improver = SiteHealthImprover(
            router=model_router,
            skill_suite=load_skill_suite(ROOT / "workflow_skills" / "site-health"),
        )
    activity_instance = TinActivities(
        database=database,
        storage=storage,
        sandboxes=sandboxes,
        settings=settings,
        memory_gardener=memory_gardener,
        scan_reporter=scan_reporter,
        visibility_auditor=visibility_auditor,
        answer_page_drafter=answer_page_drafter,
        weekly_brief_reporter=weekly_brief_reporter,
        site_health_improver=site_health_improver,
        integrations=integrations,
        temporal=temporal,
    )
    organic = OrganicAuditActivities(
        database=database, storage=storage, settings=settings, responses=responses
    )
    keywords = KeywordPlanActivities(
        database=database,
        storage=storage,
        settings=settings,
        router=model_router,
        integrations=integrations,
    )
    content_planner = ContentPlanActivities(
        database=database, storage=storage, settings=settings, router=model_router
    )
    character_designer = (
        CharacterDesigner(router=model_router) if settings.luna_api_key is not None else None
    )
    characters = CharacterDesignActivities(
        database=database, storage=storage, settings=settings, designer=character_designer
    )
    organic_system = OrganicSystemActivities(
        database=database,
        storage=storage,
        settings=settings,
        integrations=integrations,
    )
    style_activities = StyleCaptureActivities(
        database=database, storage=storage, router=model_router
    )
    plan_activities = GrowthPlanActivities(
        database=database,
        storage=storage,
        settings=settings,
        router=model_router,
        responses=responses,
    )
    onboarding = GrowthOnboardingActivities(
        database=database,
        storage=storage,
        settings=settings,
        integrations=integrations,
        temporal=temporal,
    )
    from tin_lite.code_activities import CodeActivities

    code = CodeActivities(common=activity_instance, model_router=model_router)
    activities = [
        code.execute,
        code.publish,
        code.review,
        code.approve,
        code.project,
        code.failure,
        activity_instance.deliver_content_draft,
        style_activities.prepare,
        style_activities.extract,
        style_activities.publish,
        style_activities.failure,
        plan_activities.prepare,
        plan_activities.write,
        plan_activities.publish,
        plan_activities.failure,
        organic_system.organic_system_prepare,
        organic_system.organic_system_step,
        organic_system.organic_system_step_failure,
        organic_system.organic_system_progress,
        organic_system.organic_system_finish,
        organic_system.organic_system_failure,
        onboarding.growth_onboarding_prepare,
        onboarding.growth_onboarding_step,
        onboarding.growth_onboarding_review,
        onboarding.growth_onboarding_approval,
        onboarding.growth_onboarding_setup,
        onboarding.growth_onboarding_report,
        onboarding.growth_onboarding_failure,
        characters.character_design,
        characters.character_review,
        characters.character_approval,
        characters.character_project,
        characters.character_failure,
        content_planner.content_plan_execute,
        content_planner.content_plan_failure,
        keywords.keyword_prepare,
        keywords.keyword_collect,
        keywords.keyword_sample_count,
        keywords.keyword_inspect,
        keywords.keyword_review,
        keywords.keyword_publish,
        keywords.keyword_project,
        keywords.keyword_failure,
        organic.organic_prepare,
        organic.organic_start_crawl,
        organic.organic_poll_crawl,
        organic.organic_end_crawl,
        organic.organic_prepare_panel,
        organic.organic_observe,
        organic.organic_brand_checks,
        organic.organic_publish,
        organic.organic_project,
        organic.organic_failure,
        activity_instance.dispatch_scheduled_workflow,
        activity_instance.create_design_sandbox,
        activity_instance.persist_design_artifact,
        activity_instance.commit_design_canonically,
        activity_instance.project_design_result,
        activity_instance.project_design_failure,
        activity_instance.garden_project_memory,
        activity_instance.project_memory_result,
        activity_instance.project_memory_failure,
        activity_instance.generate_scan_report,
        activity_instance.project_scan_result,
        activity_instance.project_scan_failure,
        activity_instance.draft_site_health_improvement,
        activity_instance.open_site_health_pull_request,
        activity_instance.project_site_health_result,
        activity_instance.project_site_health_failure,
        activity_instance.generate_visibility_audit,
        activity_instance.project_visibility_result,
        activity_instance.project_visibility_failure,
        activity_instance.draft_answer_page,
        activity_instance.request_answer_page_review,
        activity_instance.record_answer_page_approval,
        activity_instance.project_answer_page_result,
        activity_instance.project_answer_page_failure,
        activity_instance.generate_weekly_brief,
        activity_instance.project_weekly_brief_result,
        activity_instance.project_weekly_brief_failure,
        activity_instance.prepare_email_campaign,
        activity_instance.request_email_campaign_review,
        activity_instance.record_email_campaign_approval,
        activity_instance.list_email_campaign_recipients,
        activity_instance.reserve_email_campaign_delivery,
        activity_instance.send_email_campaign_recipient,
        activity_instance.email_campaign_follow_up_delay,
        activity_instance.check_email_campaign_reply,
        activity_instance.complete_email_campaign_recipient,
        activity_instance.fail_email_campaign_recipient,
        activity_instance.complete_email_campaign,
        activity_instance.project_email_campaign_failure,
        activity_instance.create_codex_procedure_sandbox,
        activity_instance.prepare_codex_procedure,
        activity_instance.resolve_codex_project,
        activity_instance.persist_codex_procedure_artifact,
        activity_instance.commit_codex_procedure_artifact,
        activity_instance.request_codex_procedure_review,
        activity_instance.receive_workflow_revision,
        activity_instance.record_codex_procedure_approval,
        activity_instance.project_codex_procedure_result,
        activity_instance.project_codex_procedure_failure,
        activity_instance.run_project_task_turn,
        activity_instance.apply_project_task_changes,
        activity_instance.record_project_task_stop,
        activity_instance.project_task_failure,
    ]
    by_name = {activity._Definition.must_from_callable(fn).name: fn for fn in activities}
    if set(by_name) != CODEX_ACTIVITIES | TRUSTED_ACTIVITIES:
        raise RuntimeError("Every registered activity needs an explicit worker lane")
    worker = WorkerGroup(
        Worker(
            temporal,
            task_queue=settings.task_queue,
            # Keep ALL registrations here for already-scheduled legacy activities.
            # Project child-workflow IDs serialize execution; this is only the
            # machine's parallel capacity across projects, not an account lock.
            max_concurrent_activities=4,
            workflows=registered_workflows(),
            activities=activities,
            interceptors=[ActivityLaneInterceptor()],
        ),
        Worker(
            temporal,
            task_queue=trusted_task_queue(settings.task_queue),
            max_concurrent_activities=4,
            activities=[by_name[name] for name in sorted(TRUSTED_ACTIVITIES)],
        ),
    )
    luna = None
    if responses is not None:
        luna = LunaService(
            responses=responses,
            workflow_api=TinWorkflowApiClient(base_url=settings.switchboard_public_url),
        )
    return RuntimeServices(
        database=database,
        storage=storage,
        sandboxes=sandboxes,
        temporal=temporal,
        worker=worker,
        luna=luna,
        model_router=model_router,
        integrations=integrations,
        project_files=project_files,
        output_resolution=OutputResolutionService(database=database, storage=storage),
        studio=StudioService(settings=settings, database=database),
        codex_api=CodexAPIRelay(database=database, api_key=settings.luna_api_key.get_secret_value())
        if settings.luna_api_key is not None
        else None,
    )
