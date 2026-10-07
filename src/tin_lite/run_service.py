from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from temporalio.exceptions import WorkflowAlreadyStartedError

from tin_lite import content_draft, content_plan, organic_system, technical_fix
from tin_lite.domain import PREREQUISITE_WAIT_MEMO, RunStatus, Workflow, WorkflowRun
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
from tin_lite.workflow_prerequisites import (
    PrerequisiteError,
    can_wait_for_prerequisites,
    evaluate_prerequisites,
    prerequisite_wait_run_ids,
)
from tin_lite.workflows import registered_workflow_implementations

logger = logging.getLogger(__name__)


class WorkflowExecutorUnavailableError(RuntimeError):
    pass


class ContentProgramNotSavedError(WorkflowInputError):
    """content.plan runs only as a saved program; carries the inputs the caller sent."""

    def __init__(self, inputs: dict[str, Any]) -> None:
        super().__init__(
            "Save the content program to My system before starting it: call "
            "create_project_workflow with workflow_id 'content.plan', these inputs and a weekly "
            "schedule. Saving the schedule starts its first run."
        )
        self.inputs = inputs


class TemporalStartError(RuntimeError):
    def __init__(self, run_id: UUID, *, uncertain: bool = False) -> None:
        super().__init__(
            "Start outcome is unconfirmed; retry the same request."
            if uncertain
            else "Temporal workflow did not start"
        )
        self.run_id = run_id


# Workflows retired for new work, with where the work goes instead.
RETIRED = {
    # website.change (source audit) runs the same repair with a founder decision per fix.
    technical_fix.KEY: (
        "organic.technical_fix is retired. Fix an audit's findings with website.change: "
        "call preflight_website_change (source audit), let the founder approve the changes, "
        "then start website.change with source audit."
    ),
    # Its triggers repeated the audit's orphan and competing-page checks, and nothing read
    # its page tree, URL rules or navigation; page decisions plan redirects and noindex.
    "organic.site_architecture": (
        "organic.site_architecture is retired. The organic audit reports orphaned, deep and "
        "competing pages, and page decisions (organic.content_efficacy) plan the redirects "
        "and noindex changes website.change makes with source planned."
    ),
    # website.change adds each new article to the site's own index.
    "content.blog_index": (
        "content.blog_index is retired. website.change adds each published article to the "
        "site's own index, and the organic audit reports posts nothing links to."
    ),
}
RETIRED_WEBSITE_SOURCES = {
    "blog_index": (
        "website.change no longer builds blog index plans: content.blog_index is retired. "
        "Use source audit or planned."
    ),
}


def retired_for_new_work(key: str, inputs: dict[str, Any] | None) -> str | None:
    """Why a new run of this workflow (or website.change source) is refused, if it is."""
    if key == organic_system.WEBSITE_KEY:
        return RETIRED_WEBSITE_SOURCES.get(str((inputs or {}).get("source") or ""))
    return RETIRED.get(key)


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
    _approval_delivery: bool = False,
    _x_publication: bool = False,
    _x_feedback: bool = False,
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
    retired = retired_for_new_work(workflow.key, input_payload)
    if (
        retired
        and existing is None
        and retry_of_run_id is None
        and project_workflow_id is None
        and _organic_parent_run_id is None
    ):
        # Retries, saved schedules and older organic system runs keep what they pinned.
        raise WorkflowInputError(retired)
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
    if workflow.executor == "connections.collect":
        from tin_lite.connection_collection import enabled

        if not enabled(settings, project_id):
            raise WorkflowExecutorUnavailableError(
                "Connection collection is unavailable on this project."
            )
    if workflow.executor == "social.x_revise" and not _x_feedback:
        raise WorkflowInputError("Read the X draft and use request_workflow_changes to revise it.")
    if workflow.executor == "social.x_revise" and not getattr(settings, "luna_api_key", None):
        raise WorkflowExecutorUnavailableError("X feedback requires the native model service.")
    schema = workflow.definition["input_schema"]
    normalized_inputs = normalize_workflow_inputs(
        schema=schema,
        project_id=project_id,
        inputs=input_payload,
    )
    if workflow.executor == "connections.collect":
        from tin_lite.connection_collection import CollectionInputs, cloud_ready

        normalized_inputs = CollectionInputs.model_validate(normalized_inputs).model_dump()
        # Cloud compute uses a separately qualified, explicitly funded contract.
        if normalized_inputs["execution"] != "local_only" and not cloud_ready(settings):
            raise WorkflowExecutorUnavailableError(
                "Cloud collection has not been qualified on this deployment. Choose local_only."
            )
        if not await runtime.database.has_project_access(
            project_id=project_id, clerk_user_id=started_by_clerk_user_id
        ):
            raise LookupError("project not found")
    if workflow.executor == "social.x_publish":
        from tin_lite.x_posts import approved_payload

        approval_id = normalized_inputs["approval_id"]
        if (
            not _x_publication
            or start_idempotency_key != f"x-publish:{approval_id}"
            or project_workflow_id
        ):
            raise WorkflowInputError("Preview the X post and explicitly confirm publication first.")
        try:
            await approved_payload(
                runtime.database, approval_id, project_id, started_by_clerk_user_id
            )
        except ValueError as exc:
            raise WorkflowInputError(str(exc)) from None
        if not await runtime.database.has_project_access(
            project_id=project_id, clerk_user_id=started_by_clerk_user_id
        ):
            raise LookupError("project not found")
    if workflow.executor == "social.x_draft" and existing is None:
        from tin_lite import x_draft

        try:
            x_draft.check_inputs(normalized_inputs)
        except ValueError as exc:
            raise WorkflowInputError(str(exc)) from None
        if not getattr(settings, "luna_api_key", None):
            raise WorkflowExecutorUnavailableError("X drafting requires the native model service.")
    if workflow.executor == "social.x_style" and existing is None:
        from tin_lite import x_style

        try:
            x_style.validate_inputs(normalized_inputs)
        except ValueError as exc:
            raise WorkflowInputError(str(exc)) from None
        if not getattr(settings, "luna_api_key", None):
            raise WorkflowExecutorUnavailableError(
                "X style capture requires the native model service."
            )
        if normalized_inputs.get("sample_source") == "connected" or not any(
            normalized_inputs.get(k) for k in ("supplied_samples", "source_path", "preferences")
        ):
            connection = await runtime.integrations.x.connection(
                project_id, capability="x.posts.read"
            )
            if connection.configuration.get("protected") is not False:
                raise WorkflowInputError(
                    "Connect a public X account or supply your own writing samples."
                )
            if normalized_inputs.get("account_id") not in {
                None,
                "",
                connection.external_account_id,
            }:
                raise WorkflowInputError(
                    "The selected X account differs from the connected account."
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
            # A parent-dispatched child starts without the wait memo, so it cannot wait.
            can_wait=can_wait_for_prerequisites(workflow) and not _prepare_only,
        )
        if evaluation.blocking:
            raise PrerequisiteError.from_evaluation(
                evaluation, workflow=workflow, inputs=normalized_inputs
            )
        if evaluation.results:
            prerequisite_evidence = evaluation.evidence(inputs=normalized_inputs)
    if workflow.executor == "style.capture" and existing is None:
        from tin_lite.style_capture import StyleSourceError, read_sources

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
        except StyleSourceError as exc:
            # Tin's own words: which file, and what is wrong with it.
            raise WorkflowInputError(str(exc)) from None
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
    if workflow.key == technical_fix.KEY and technical_fix.batches(
        technical_fix.definition_policy(workflow.definition)
    ):
        from tin_lite.technical_fix_sources import TechnicalFixError, TechnicalFixSources

        try:
            preview = await TechnicalFixSources(
                database=runtime.database,
                storage=runtime.storage,
                integrations=runtime.integrations,
                supported_checks=technical_fix.supported_checks(technical_fix.BATCH_POLICY),
                batch=True,
            ).batch(
                project_id=project_id,
                audit_run_id=UUID(normalized_inputs["audit_run_id"]),
                audit_revision=normalized_inputs["audit_revision"],
                expected_repository=normalized_inputs["expected_repository"],
                repository_serves_site=normalized_inputs["repository_serves_site"],
                finding_ids=normalized_inputs.get("finding_ids") or [],
                decisions=normalized_inputs.get("decisions") or [],
            )
        except TechnicalFixError as exc:
            raise WorkflowInputError(str(exc)) from exc
        if not preview["plan"]["repairs"]:
            waiting = len(preview["decisions_needed"])
            raise WorkflowInputError(
                "Nothing in this audit is ready to fix"
                + (
                    f": {waiting} finding{'s' if waiting != 1 else ''} wait for a decision. "
                    "Answer preflight_technical_fix's decisions_needed and pass them as decisions."
                    if waiting
                    else "; its findings are copy, manual steps or need no change."
                )
            )
    elif workflow.key == technical_fix.KEY:
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
            raise ContentProgramNotSavedError(normalized_inputs)
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

    if workflow.executor == "workflow.code" and existing is None:
        from tin_lite import code_article_sources, code_evidence, code_project_files
        from tin_lite.workflow_code import approved_article_input, evidence_specs

        try:
            create_arguments["code_project_files_source"] = await code_project_files.select(
                database=runtime.database,
                storage=runtime.storage,
                project_id=project_id,
            )
        except (ValueError, LookupError, RuntimeError) as exc:
            if not start_idempotency_key or not await runtime.database.get_run_by_start_key(
                project_id=project_id, start_idempotency_key=start_idempotency_key
            ):
                raise WorkflowInputError(str(exc)) from exc
        if approved_article_input(workflow.definition) is not None:
            try:
                create_arguments["approved_article_source"] = await code_article_sources.select(
                    database=runtime.database,
                    storage=runtime.storage,
                    project_id=project_id,
                    definition=workflow.definition,
                    inputs=normalized_inputs,
                )
            except (ValueError, LookupError) as exc:
                raise WorkflowInputError(str(exc)) from exc
        if evidence_specs(workflow.definition):
            try:
                create_arguments["approved_evidence_source"] = await code_evidence.select(
                    database=runtime.database,
                    storage=runtime.storage,
                    project_id=project_id,
                    definition=workflow.definition,
                    inputs=normalized_inputs,
                )
                code_evidence.bound_context(
                    create_arguments["approved_evidence_source"],
                    create_arguments.get("approved_article_source"),
                )
            except (ValueError, LookupError, UnicodeError) as exc:
                # Another identical start may have committed while this request
                # inspected the source. create_run will verify actor, inputs and
                # pinned definition before returning that existing run.
                if not start_idempotency_key or not await runtime.database.get_run_by_start_key(
                    project_id=project_id, start_idempotency_key=start_idempotency_key
                ):
                    raise WorkflowInputError(str(exc)) from exc

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
                # Internal: an approval's own start carries the founder's delivery setting.
                approval=_approval_delivery,
            )
        except (ValueError, LookupError, IntegrationError) as exc:
            raise WorkflowInputError(str(exc)) from exc
    if workflow.id == content_repository_delivery.WEBSITE_CHANGE_ID and existing is None:
        from tin_lite import website_change

        try:
            create_arguments["content_delivery_source"] = await website_change.select_source(
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
                project_id=project_id,
                inputs=normalized_inputs,
                retry_of_run_id=retry_of_run_id,
                # The organic traffic system's own draft is its next article; its delivery
                # step adapts articles only. Saved schedules draft every kind they pin.
                kinds=(content_plan.ARTICLE,)
                if _organic_parent_run_id is not None
                else content_draft.supported_kinds(workflow.definition),
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
        if workflow.key in {
            content_draft.KEY,
            content_repository_delivery.KEY,
            "website.change",
        } or (
            workflow.executor == "workflow.code"
            and (
                workflow.definition.get("code", {}).get("approved_article") is not None
                or workflow.definition.get("code", {}).get("evidence") is not None
            )
        ):
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
    durable_dispatch = paid or workflow.executor in {"social.x_publish", "social.x_revise"}
    if durable_dispatch:
        from temporalio.common import WorkflowIDReusePolicy

        temporal_options["id_reuse_policy"] = WorkflowIDReusePolicy.REJECT_DUPLICATE
    awaited = prerequisite_wait_run_ids(run.prerequisite_evidence)
    if awaited:
        # The workflow reads this memo to hold the run until these prerequisite runs finish.
        temporal_options["memo"] = {PREREQUISITE_WAIT_MEMO: awaited}
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
        if durable_dispatch:
            # The committed run/budget is the dispatch intent. A lost acknowledgment
            # cannot release funds or mark a possibly running external delivery failed.
            raise TemporalStartError(run.id, uncertain=True) from exc
        await runtime.database.project_failure(
            run_id=run.id,
            error_message="TemporalStartError: workflow did not start",
        )
        raise TemporalStartError(run.id) from exc
    if created and workflow.executor == "growth.onboarding":
        # Only a newly created, dispatched run replaces earlier unapproved ones; a replayed
        # start key returns its existing run and supersedes nothing.
        from tin_lite.growth_onboarding_control import supersede_earlier_onboarding

        try:
            await supersede_earlier_onboarding(runtime=runtime, run=run)
        except Exception:
            # The new run is already dispatched; an older one simply stays as it was.
            logger.exception("Could not supersede earlier onboarding runs for %s", run.id)
    return run
