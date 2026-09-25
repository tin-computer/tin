from __future__ import annotations

from typing import Any
from uuid import UUID

from temporalio.exceptions import WorkflowAlreadyStartedError

from tin_lite import content_draft, content_plan, organic_system, technical_fix
from tin_lite.domain import RunStatus, Workflow, WorkflowRun
from tin_lite.executor_gates import (
    google_ads_gate,
    keyword_plan_gate,
    organic_audit_gate,
    organic_system_gate,
    paid_ads_gate,
)
from tin_lite.integrations import (
    IntegrationError,
    load_pinned_integration_requirements,
)
from tin_lite.keyword_plan import KEY as KEYWORD_KEY
from tin_lite.keyword_plan import check_inputs as check_keyword_inputs
from tin_lite.organic_audit import AUDIT_KEY, public_site
from tin_lite.paid_ads import KEY as PAID_ADS_KEY
from tin_lite.paid_ads import check_inputs as check_paid_ads_inputs
from tin_lite.paid_ads_launch import KEY as PAID_ADS_LAUNCH_KEY
from tin_lite.paid_ads_launch import check_inputs as check_paid_ads_launch_inputs
from tin_lite.paid_ads_monitor import KEY as PAID_ADS_MONITOR_KEY
from tin_lite.paid_ads_monitor import check_inputs as check_paid_ads_monitor_inputs
from tin_lite.runtime import RuntimeServices
from tin_lite.settings import Settings
from tin_lite.workflow_definitions import resolve_execution_contract
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs
from tin_lite.workflow_prerequisites import PrerequisiteError, evaluate_prerequisites
from tin_lite.workflows import registered_workflow_implementations


class WorkflowExecutorUnavailableError(RuntimeError):
    pass


class TemporalStartError(RuntimeError):
    def __init__(self, run_id: UUID, *, uncertain: bool = False) -> None:
        super().__init__(
            "Start outcome is unconfirmed; retry the same request."
            if uncertain
            else "Temporal workflow did not start"
        )
        self.run_id = run_id


async def start_workflow_run(
    *,
    runtime: RuntimeServices,
    settings: Settings,
    workflow: Workflow,
    project_id: UUID,
    started_by_clerk_user_id: str,
    start_idempotency_key: str | None = None,
    input_payload: dict[str, Any] | None = None,
    project_workflow_id: UUID | None = None,
    definition_commit_sha: str | None = None,
    input_schema: dict[str, Any] | None = None,
    trigger_source: str = "manual",
    trigger_client: str | None = None,
    started_by_oauth_client_id: str | None = None,
    retry_of_run_id: UUID | None = None,
    payment_card=None,
    _prepare_only: bool = False,
    billing_quote_id: UUID | None = None,
    _billing_parent_run_id: UUID | None = None,
    _review_transition: dict[str, Any] | None = None,
    _organic_parent_run_id: UUID | None = None,
) -> WorkflowRun:
    implementation = registered_workflow_implementations().get(workflow.executor)
    if implementation is None:
        raise WorkflowExecutorUnavailableError("workflow executor is not installed")
    if workflow.project_id is not None:
        project = await runtime.database.get_project(project_id)
        if (
            workflow.project_id != project_id
            or project is None
            or workflow.definition_repo_id != project.state_repo_id
            or not await runtime.database.has_project_access(
                project_id=project_id, clerk_user_id=started_by_clerk_user_id
            )
        ):
            raise LookupError("workflow not found")
    existing: WorkflowRun | None = None
    if start_idempotency_key is not None:
        existing = await runtime.database.get_run_by_start_key(
            project_id=project_id, start_idempotency_key=start_idempotency_key
        )
        if existing is not None:
            if existing.workflow_id != workflow.id:
                raise WorkflowInputError("start request belongs to a different workflow")
            # A lost acknowledgement followed by catalog activation must recover the
            # already-selected revision. create_run still checks actor, inputs and lineage.
            if definition_commit_sha is None:
                definition_commit_sha = existing.definition_commit_sha
    workflow = await resolve_execution_contract(
        storage=getattr(runtime, "storage", None),
        workflow=workflow,
        project_id=project_id,
        revision=definition_commit_sha,
        input_schema=input_schema,
    )
    if existing is None:
        from tin_lite.codex_api import api_enabled

        if workflow.executor in {
            "content.design_md",
            "project.task",
            "codex.procedure",
        } and not api_enabled(settings, project_id):
            raise WorkflowExecutorUnavailableError(
                "This workflow requires protected Codex execution. "
                "Configure the protected API route before starting this workflow."
            )
    if (
        existing is None
        and workflow.executor == "codex.procedure"
        and workflow.definition.get("procedure", {}).get("sandbox", {}).get("profile") == "studio"
        and getattr(settings, "fal_key", None) is None
    ):
        raise WorkflowExecutorUnavailableError(
            "Studio voice is not configured. No video run or model purchase was started."
        )
    if workflow.project_id is not None:
        from tin_lite.private_workflows import require_private_execution

        require_private_execution(settings, workflow, project_id)
    schema = workflow.definition["input_schema"]
    normalized_inputs = normalize_workflow_inputs(
        schema=schema,
        project_id=project_id,
        inputs=input_payload,
    )
    if existing is None and workflow.executor == "workflow.code":
        from tin_lite.workflow_code import validate_code_definition

        if validate_code_definition(workflow.definition).model_routes and not getattr(
            settings, "luna_api_key", None
        ):
            raise WorkflowExecutorUnavailableError(
                "The declared managed model service is unavailable."
            )
    from tin_lite.run_payment_card import prepare as prepare_payment_card

    run_card = prepare_payment_card(
        payment_card, workflow=workflow, integrations=getattr(runtime, "integrations", None)
    )
    prerequisite_evidence: dict[str, Any] | None = None
    if existing is None and _review_transition is None:
        # Admission asks the project's durable facts, never a caller claim, whether the
        # earlier work this workflow builds on exists. A replayed start keeps its original
        # admission; re-checking it could reject a run that already happened.
        evaluation = await evaluate_prerequisites(
            database=runtime.database,
            storage=getattr(runtime, "storage", None),
            project_id=project_id,
            workflow=workflow,
            normalized_inputs=normalized_inputs,
        )
        if evaluation.blocking:
            raise PrerequisiteError.from_evaluation(
                evaluation, workflow=workflow, inputs=normalized_inputs
            )
        if evaluation.results:
            prerequisite_evidence = evaluation.evidence(inputs=normalized_inputs)
    if workflow.executor == "style.capture" and existing is None:
        from tin_lite.style_capture import read_sources

        if not getattr(settings, "luna_api_key", None):
            raise WorkflowExecutorUnavailableError(
                "Style capture requires the native model service."
            )
        project = await runtime.database.get_project(project_id)
        if project is None or not await runtime.database.has_project_access(
            project_id=project_id, clerk_user_id=started_by_clerk_user_id
        ):
            raise LookupError("project not found")
        try:
            await read_sources(runtime.storage, project, normalized_inputs["source_path"])
        except ValueError:
            raise WorkflowInputError(
                "Choose a valid style source packet in this project's Files before starting."
            ) from None
    if workflow.key == "brand.capture" and workflow.project_id is None and existing is None:
        from tin_lite.brand_capture import BrandCaptureSources

        if not await runtime.database.has_project_access(
            project_id=project_id, clerk_user_id=started_by_clerk_user_id
        ):
            raise LookupError("project not found")
        try:
            await BrandCaptureSources(database=runtime.database, storage=runtime.storage).inspect(
                project_id, normalized_inputs
            )
        except ValueError as exc:
            raise WorkflowInputError(str(exc)) from exc
    if workflow.key == technical_fix.KEY:
        from tin_lite.technical_fix_sources import TechnicalFixError, TechnicalFixSources

        try:
            await TechnicalFixSources(
                database=runtime.database,
                storage=runtime.storage,
                integrations=runtime.integrations,
                supported_checks=technical_fix.supported_checks(
                    technical_fix.definition_policy(workflow.definition)
                ),
            ).preflight(
                project_id=project_id,
                audit_run_id=UUID(normalized_inputs["audit_run_id"]),
                **{
                    key: normalized_inputs[key]
                    for key in (
                        "audit_revision",
                        "finding_id",
                        "expected_repository",
                        "repository_serves_site",
                    )
                },
            )
        except TechnicalFixError as exc:
            raise WorkflowInputError(str(exc)) from exc
    if workflow.executor == organic_system.KEY:
        try:
            organic_system.check_inputs(normalized_inputs)
        except (KeyError, ValueError, TypeError) as exc:
            raise WorkflowInputError(
                "Choose a valid site, market, buyer context and start date."
            ) from exc
        organic_system_reason = organic_system_gate(
            settings, keyword_max_cost_usd=normalized_inputs["keyword_max_cost_usd"]
        )
        if organic_system_reason is not None:
            raise WorkflowExecutorUnavailableError(organic_system_reason)
        if normalized_inputs["technical_fix"]:
            await runtime.integrations.github_repository_binding(
                project_id=project_id,
                expected_repository=normalized_inputs["expected_repository"],
            )
    if workflow.executor == KEYWORD_KEY:
        try:
            check_keyword_inputs(normalized_inputs)
        except (ValueError, TypeError) as exc:
            raise WorkflowInputError(str(exc)) from exc
        keyword_reason = keyword_plan_gate(settings)
        if keyword_reason is not None:
            raise WorkflowExecutorUnavailableError(keyword_reason)
    if workflow.executor == PAID_ADS_KEY:
        try:
            check_paid_ads_inputs(normalized_inputs)
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkflowInputError(str(exc) or "Invalid paid ads inputs.") from exc
        paid_ads_reason = paid_ads_gate(settings)
        if paid_ads_reason is not None:
            raise WorkflowExecutorUnavailableError(paid_ads_reason)
    if workflow.executor in {PAID_ADS_LAUNCH_KEY, PAID_ADS_MONITOR_KEY}:
        check = (
            check_paid_ads_launch_inputs
            if workflow.executor == PAID_ADS_LAUNCH_KEY
            else check_paid_ads_monitor_inputs
        )
        try:
            check(normalized_inputs)
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkflowInputError(str(exc) or "Invalid Google Ads inputs.") from exc
        ads_reason = google_ads_gate(settings)
        if ads_reason is not None:
            raise WorkflowExecutorUnavailableError(ads_reason)
    if workflow.executor == content_plan.KEY:
        try:
            content_plan.check_inputs(normalized_inputs)
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkflowInputError(
                "Choose valid research runs, a start date and duration."
            ) from exc
        if project_workflow_id is None:
            raise WorkflowInputError("Save the content program to My system before starting it.")
    if workflow.executor == AUDIT_KEY:
        try:
            public_site(normalized_inputs["site_url"])
        except ValueError as exc:
            raise WorkflowInputError(str(exc)) from exc
        audit_reason = organic_audit_gate(settings)
        if audit_reason is not None:
            raise WorkflowExecutorUnavailableError(audit_reason)
    create_arguments: dict[str, Any] = {
        "project_id": project_id,
        "workflow_id": workflow.id,
        "started_by_clerk_user_id": started_by_clerk_user_id,
        "start_idempotency_key": start_idempotency_key,
        "definition_commit_sha": workflow.current_commit_sha,
        "pinned_definition": workflow.definition,
    }
    from tin_lite import content_repository_delivery

    if workflow.id == content_repository_delivery.WORKFLOW_ID and existing is None:
        try:
            create_arguments[
                "content_delivery_source"
            ] = await content_repository_delivery.select_source(
                database=runtime.database,
                storage=runtime.storage,
                integrations=runtime.integrations,
                project_id=project_id,
                inputs=normalized_inputs,
            )
        except (ValueError, LookupError, IntegrationError) as exc:
            raise WorkflowInputError(str(exc)) from exc
    if project_workflow_id is not None:
        create_arguments["project_workflow_id"] = project_workflow_id
    if trigger_source != "manual":
        create_arguments["trigger_source"] = trigger_source
    if trigger_client is not None:
        create_arguments["trigger_client"] = trigger_client
    if started_by_oauth_client_id is not None:
        create_arguments["started_by_oauth_client_id"] = started_by_oauth_client_id
    if retry_of_run_id is not None:
        create_arguments["retry_of_run_id"] = retry_of_run_id
    if run_card is not None:
        create_arguments["payment_card"] = run_card
    if normalized_inputs:
        create_arguments["input_payload"] = normalized_inputs
    if billing_quote_id is not None:
        create_arguments["billing_quote_id"] = billing_quote_id
    if _billing_parent_run_id is not None:
        create_arguments["billing_parent_run_id"] = _billing_parent_run_id
    if prerequisite_evidence is not None:
        create_arguments["prerequisite_evidence"] = prerequisite_evidence
    if _review_transition is not None:
        create_arguments["review_transition"] = _review_transition
        if _review_transition.get("draft_selection"):
            create_arguments["draft_selection"] = _review_transition["draft_selection"]
    if workflow.key == content_draft.KEY and existing is None and _review_transition is None:
        from tin_lite.content_draft_sources import ContentDraftSources

        try:
            create_arguments["draft_selection"] = await ContentDraftSources(
                database=runtime.database, storage=runtime.storage
            ).choose(
                project_id=project_id, inputs=normalized_inputs, retry_of_run_id=retry_of_run_id
            )
            if _organic_parent_run_id is not None:
                from tin_lite.organic_content import draft_intent

                create_arguments["draft_selection"]["system_delivery"] = await draft_intent(
                    runtime.database,
                    parent_id=_organic_parent_run_id,
                    project_id=project_id,
                    actor=started_by_clerk_user_id,
                    selected=create_arguments["draft_selection"],
                )
            # Only definitions that declare this input may acquire delivery. Historical
            # saved versions and approvals remain draft-only, even after configuration.
            if workflow.project_id is None and normalized_inputs.get("delivery") == "program":
                from tin_lite.content_delivery import ContentDelivery

                create_arguments["draft_selection"]["delivery"] = await ContentDelivery(
                    database=runtime.database,
                    storage=runtime.storage,
                    integrations=runtime.integrations,
                ).pin(project_id=project_id, selected=create_arguments["draft_selection"])
        except (ValueError, LookupError, KeyError, IntegrationError) as exc:
            # A concurrent identical request may already have selected this article.
            # Let create_run validate that request's actor, inputs and lineage as usual.
            if not start_idempotency_key or not await runtime.database.get_run_by_start_key(
                project_id=project_id, start_idempotency_key=start_idempotency_key
            ):
                raise WorkflowInputError(str(exc)) from exc
    try:
        run, created = await runtime.database.create_run(**create_arguments)
    except ValueError as exc:
        if workflow.key in {content_draft.KEY, content_repository_delivery.KEY}:
            raise WorkflowInputError(str(exc)) from exc
        raise
    if not created and run.status != RunStatus.PENDING:
        return run
    if created:
        try:
            if run.definition_commit_sha is None:
                raise RuntimeError("workflow run did not pin a definition commit")
            requirements = await load_pinned_integration_requirements(
                storage=getattr(runtime, "storage", None),
                workflow=workflow,
                commit_sha=run.definition_commit_sha,
            )
            if requirements:
                await runtime.integrations.ensure_requirements(
                    project_id=project_id,
                    requirements=requirements,
                )
        except IntegrationError as exc:
            await runtime.database.project_failure(
                run_id=run.id,
                error_message=f"Integration unavailable: {exc}",
            )
            raise
        except Exception as exc:
            await runtime.database.project_failure(
                run_id=run.id,
                error_message=(
                    "IntegrationPreflightError: workflow requirements could not be resolved"
                ),
            )
            raise RuntimeError("workflow integration requirements could not be resolved") from exc
    if _prepare_only:
        # A Tin-owned parent dispatches this ordinary pinned run as a Temporal child.
        # This switch is internal Python API, never part of a dashboard/MCP input schema.
        return run
    paid = bool(getattr(runtime.database, "billing", None)) and bool(
        await runtime.database.pool.fetchval(
            """SELECT EXISTS(SELECT 1 FROM billing_run_budgets WHERE run_id=$1)
               OR EXISTS(SELECT 1 FROM effect_receipts
                         WHERE operation='included_workflow_v1'
                           AND execution_key=$2 AND status='completed')""",
            run.id,
            f"billing-included:{run.id}",
        )
    )
    temporal_options = {}
    if paid:
        from temporalio.common import WorkflowIDReusePolicy

        temporal_options["id_reuse_policy"] = WorkflowIDReusePolicy.REJECT_DUPLICATE
    try:
        await runtime.temporal.start_workflow(
            implementation.run,
            str(run.id),
            id=run.temporal_workflow_id,
            task_queue=settings.task_queue,
            **temporal_options,
        )
    except WorkflowAlreadyStartedError:
        return run
    except Exception as exc:
        if paid:
            # The committed run/budget is the dispatch intent. A lost acknowledgment
            # cannot release funds or mark a possibly running execution failed.
            raise TemporalStartError(run.id, uncertain=True) from exc
        await runtime.database.project_failure(
            run_id=run.id,
            error_message="TemporalStartError: workflow did not start",
        )
        raise TemporalStartError(run.id) from exc
    return run
