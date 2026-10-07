from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from tin_lite import (
    content_plan,
    content_plan_editorial,
    growth_plan,
    paid_ads,
    paid_ads_launch,
    payment_recovery,
    style_capture,
    x_feedback,
)
from tin_lite.activities import TinActivities
from tin_lite.activity_lanes import (
    CODEX_ACTIVITIES,
    TRUSTED_ACTIVITIES,
    ActivityLaneInterceptor,
    trusted_task_queue,
    workflow_runner,
)
from tin_lite.answer_page import AnswerPageDrafter
from tin_lite.awesome_submit_activities import AwesomeSubmitActivities
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
from tin_lite.paid_ads_activities import PaidAdsActivities
from tin_lite.paid_ads_launch_activities import PaidAdsLaunchActivities
from tin_lite.paid_ads_monitor_activities import PaidAdsMonitorActivities
from tin_lite.payment_recovery_activities import PaymentRecoveryActivities
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
from tin_lite.x_draft_activities import XDraftActivities
from tin_lite.x_feedback_activities import XFeedbackActivities
from tin_lite.x_publish_activities import XPublishActivities
from tin_lite.x_style_activities import XStyleActivities

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
            x_feedback.ROUTE,
            *growth_plan.ROUTES,
            *paid_ads.ROUTES,
            *paid_ads_launch.ROUTES,
            *payment_recovery.ROUTES,
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
    from tin_lite.ai_answers_activities import AIAnswersActivities

    # The organic audit asks its buyer questions on six AI engines through it (v13).
    ai_answers = AIAnswersActivities(database=database, settings=settings)
    organic = OrganicAuditActivities(
        database=database,
        storage=storage,
        settings=settings,
        responses=responses,
        integrations=integrations,
    )
    keywords = KeywordPlanActivities(
        database=database,
        storage=storage,
        settings=settings,
        router=model_router,
        integrations=integrations,
    )
    content_planner = ContentPlanActivities(
        database=database,
        storage=storage,
        settings=settings,
        router=model_router,
        integrations=integrations,
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
        temporal=temporal,
    )
    style_activities = StyleCaptureActivities(
        database=database, storage=storage, router=model_router
    )
    x_draft_activities = XDraftActivities(
        database=database, storage=storage, integrations=integrations, settings=settings
    )
    x_feedback_activities = XFeedbackActivities(
        database=database, storage=storage, router=model_router
    )
    x_style_activities = XStyleActivities(
        database=database, storage=storage, router=model_router, x_connection=integrations.x
    )
    x_publish_activities = XPublishActivities(
        database=database, storage=storage, integrations=integrations
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
    from tin_lite.gak import client_from_settings

    paid_ads_activities = PaidAdsActivities(
        database=database,
        storage=storage,
        settings=settings,
        router=model_router,
        integrations=integrations,
        gak=client_from_settings(settings),
    )
    paid_ads_launch_activities = PaidAdsLaunchActivities(
        database=database,
        storage=storage,
        settings=settings,
        router=model_router,
        integrations=integrations,
    )
    paid_ads_monitor_activities = PaidAdsMonitorActivities(
        database=database,
        storage=storage,
        settings=settings,
        router=model_router,
        integrations=integrations,
    )
    awesome_submit_activities = AwesomeSubmitActivities(
        database=database, storage=storage, integrations=integrations
    )
    from tin_lite.activity_lanes import COLLECTION_ACTIVITIES, collection_task_queue

    payment_recovery_activities = PaymentRecoveryActivities(
        database=database,
        storage=storage,
        settings=settings,
        router=model_router,
        integrations=integrations,
    )
    from tin_lite.code_activities import CodeActivities
    from tin_lite.connection_collection_activities import CollectionActivities
    from tin_lite.linkedin_cloud import LinkedInCloud

    collection = CollectionActivities(
        database=database,
        storage=storage,
        settings=settings,
        cloud=LinkedInCloud(database, settings),
        cipher=integrations._cipher,
    )
    code = CodeActivities(common=activity_instance, model_router=model_router)
    activities = [
        collection.prepare,
        collection.poll,
        collection.publish,
        collection.failure,
        code.execute,
        code.publish,
        code.review,
        code.approve,
        code.project,
        code.failure,
        activity_instance.deliver_content_draft,
        style_activities.prepare,
        style_activities.extract,
        style_activities.propose,
        style_activities.record_approval,
        style_activities.publish,
        style_activities.failure,
        x_draft_activities.prepare,
        x_draft_activities.step,
        x_draft_activities.finish,
        x_draft_activities.failure,
        x_feedback_activities.generate,
        x_feedback_activities.publish,
        x_feedback_activities.failure,
        x_style_activities.prepare,
        x_style_activities.extract,
        x_style_activities.propose,
        x_style_activities.record_approval,
        x_style_activities.publish,
        x_style_activities.failure,
        x_publish_activities.execute,
        x_publish_activities.failure,
        plan_activities.prepare,
        plan_activities.write,
        plan_activities.publish,
        plan_activities.failure,
        paid_ads_activities.prepare,
        paid_ads_activities.gather,
        paid_ads_activities.research,
        paid_ads_activities.assess,
        paid_ads_activities.publish,
        paid_ads_activities.project,
        paid_ads_activities.failure,
        paid_ads_launch_activities.prepare,
        paid_ads_launch_activities.gather,
        paid_ads_launch_activities.draft,
        paid_ads_launch_activities.settle_setup,
        paid_ads_launch_activities.request_review,
        paid_ads_launch_activities.record_approval,
        paid_ads_launch_activities.apply,
        paid_ads_launch_activities.publish,
        paid_ads_launch_activities.failure,
        paid_ads_monitor_activities.prepare,
        paid_ads_monitor_activities.read,
        paid_ads_monitor_activities.decide,
        paid_ads_monitor_activities.apply,
        paid_ads_monitor_activities.propose,
        paid_ads_monitor_activities.publish,
        paid_ads_monitor_activities.failure,
        awesome_submit_activities.prepare,
        awesome_submit_activities.draft,
        awesome_submit_activities.request_review,
        awesome_submit_activities.record_approval,
        awesome_submit_activities.apply,
        awesome_submit_activities.publish,
        awesome_submit_activities.failure,
        payment_recovery_activities.prepare,
        payment_recovery_activities.gather,
        payment_recovery_activities.draft,
        payment_recovery_activities.request_review,
        payment_recovery_activities.record_approval,
        payment_recovery_activities.apply,
        payment_recovery_activities.publish,
        payment_recovery_activities.expire,
        payment_recovery_activities.failure,
        organic_system.organic_system_prepare,
        organic_system.organic_system_step,
        organic_system.organic_system_step_failure,
        organic_system.organic_system_progress,
        organic_system.organic_system_finish,
        organic_system.organic_system_failure,
        organic_system.organic_system_weekly_articles,
        organic_system.organic_system_refresh,
        organic_system.organic_system_measurement,
        organic_system.organic_system_weekly_articles_failure,
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
        content_planner.content_plan_research,
        content_planner.content_plan_execute,
        content_planner.content_plan_failure,
        keywords.keyword_prepare,
        keywords.keyword_collect,
        keywords.keyword_sample_count,
        keywords.keyword_inspect,
        keywords.keyword_inspect_batch,
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
        organic.organic_prepare_ai_engines,
        organic.organic_publish,
        organic.organic_project,
        organic.organic_failure,
        ai_answers.ai_answers_measure,
        activity_instance.dispatch_scheduled_workflow,
        activity_instance.prerequisite_wait,
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
    if set(by_name) != CODEX_ACTIVITIES | TRUSTED_ACTIVITIES | COLLECTION_ACTIVITIES:
        raise RuntimeError("Every registered activity needs an explicit worker lane")
    graceful_shutdown = timedelta(seconds=settings.worker_graceful_shutdown_seconds)
    worker = WorkerGroup(
        Worker(
            temporal,
            task_queue=collection_task_queue(settings.task_queue),
            max_concurrent_activities=2,
            graceful_shutdown_timeout=graceful_shutdown,
            activities=[by_name[name] for name in sorted(COLLECTION_ACTIVITIES)],
        ),
        Worker(
            temporal,
            task_queue=settings.task_queue,
            # Keep ALL registrations here for already-scheduled legacy activities.
            # Project child-workflow IDs serialize execution; this is only the
            # machine's parallel capacity across projects, not an account lock.
            max_concurrent_activities=4,
            graceful_shutdown_timeout=graceful_shutdown,
            workflows=registered_workflows(),
            workflow_runner=workflow_runner(),
            activities=[fn for name, fn in by_name.items() if name not in COLLECTION_ACTIVITIES],
            interceptors=[ActivityLaneInterceptor()],
        ),
        Worker(
            temporal,
            task_queue=trusted_task_queue(settings.task_queue),
            max_concurrent_activities=4,
            graceful_shutdown_timeout=graceful_shutdown,
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
