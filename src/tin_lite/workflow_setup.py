"""Shared code-workflow setup facts; never a paid provider probe or spending approval."""

from tin_lite.billing_contracts import usd
from tin_lite.code_models import model_terms
from tin_lite.integrations import IntegrationError, parse_integration_requirements
from tin_lite.private_workflows import require_private_execution
from tin_lite.workflow_code import approved_article_input, evidence_specs, validate_code_definition
from tin_lite.workflow_costs import configured_terms
from tin_lite.workflow_definitions import resolve_execution_contract
from tin_lite.workflow_inputs import normalize_workflow_inputs
from tin_lite.workflow_prerequisites import evaluate_prerequisites


async def code_readiness(
    *, database, integrations, settings, storage, workflow, project_id, inputs
):
    spec = validate_code_definition(workflow.definition)
    issues, schedule_issues, connections = [], [], []
    try:
        require_private_execution(settings, workflow, project_id)
    except ValueError as exc:
        issues.append(str(exc))
    for requirement in parse_integration_requirements(
        workflow.definition.get("integration_requirements")
    ):
        ready = False
        if integrations is not None:
            try:
                await integrations.ensure_requirements(
                    project_id=project_id, requirements=(requirement,)
                )
                ready = True
            except IntegrationError:
                pass
        connections.append(
            {
                "provider_key": requirement.provider_key,
                "capabilities": list(requirement.capabilities),
                "ready": ready,
            }
        )
        if requirement.required and not ready:
            issues.append(
                f"Connect {requirement.provider_key} with the declared permissions in Integrations."
            )
    evaluation = await evaluate_prerequisites(
        database=database,
        storage=storage,
        project_id=project_id,
        workflow=workflow,
        normalized_inputs=inputs,
    )
    if evaluation.blocking:
        issues.append(
            "Complete this workflow's required earlier work or project files before running."
        )
    evidence_snapshot = None
    article_source = None
    if evidence_specs(workflow.definition):
        from tin_lite import code_evidence

        try:
            evidence_snapshot = await code_evidence.select(
                database=database,
                storage=storage,
                project_id=project_id,
                definition=workflow.definition,
                inputs=inputs,
            )
        except (LookupError, ValueError) as exc:
            issues.append(str(exc))
    if approved_article_input(workflow.definition) is not None:
        from tin_lite import code_article_sources

        try:
            article_source = await code_article_sources.select(
                database=database,
                storage=storage,
                project_id=project_id,
                definition=workflow.definition,
                inputs=inputs,
            )
        except (LookupError, ValueError) as exc:
            issues.append(str(exc))
    if evidence_snapshot is not None:
        from tin_lite.code_evidence import bound_context

        try:
            bound_context(evidence_snapshot, article_source)
        except ValueError as exc:
            issues.append(str(exc))
    terms = (
        configured_terms(model_terms(workflow.definition), workflow.definition, inputs)
        if spec.model_routes
        else None
    )
    estimate = {
        "estimated_usd": usd(terms["maximum_nanos"]) if terms else "0.00",
        "approval_required": False,
        "basis": "conservative_configured_bound" if terms else "included_bounded_compute",
        "policy_id": terms["estimate"]["id"] if terms else "bounded-code-v1",
        "external_provider_cost": "unknown" if spec.services else "not_applicable",
    }
    if spec.model_routes:
        if not getattr(settings, "luna_api_key", None):
            issues.append("The declared managed model service is unavailable.")
        billing = database.billing
        if billing is not None:
            async with database.pool.acquire() as conn:
                account = await conn.fetchrow(
                    "SELECT a.* FROM billing_accounts a JOIN projects p "
                    "ON p.workspace_id=a.workspace_id WHERE p.id=$1",
                    project_id,
                )
                policy = await conn.fetchrow(
                    "SELECT * FROM billing_project_policies WHERE project_id=$1", project_id
                )
            if not account or not account["run_billing_enabled"] or account["status"] != "active":
                issues.append("Managed model workflows need an enabled credit account.")
            elif not getattr(settings, "billing_test_enabled", False):
                issues.append("New paid runs are paused.")
            elif account["balance_nanos"] <= 0:
                issues.append("Add Tin credits before running this model workflow.")
            if not policy or (policy["schedule_max_nanos"] or 0) < terms["maximum_nanos"]:
                schedule_issues.append(
                    "Set a sufficient standing schedule limit in project billing settings."
                )
    return {
        "connections": connections,
        "issues": issues,
        "schedule_issues": schedule_issues,
        "can_run": not issues,
        "can_schedule": not issues
        and not schedule_issues
        and bool(set(workflow.definition["schedule_modes"]) & {"daily", "weekly"}),
        "estimate": estimate,
    }


async def prepare_workflow(
    *, runtime, settings, project_id, actor, workflow_id=None, project_workflow_id=None, inputs=None
):
    db = runtime.database
    if not await db.has_project_access(project_id=project_id, clerk_user_id=actor):
        raise LookupError("project not found")
    if bool(workflow_id) == bool(project_workflow_id):
        raise ValueError("Choose a workflow or a saved configuration.")
    configured = None
    if project_workflow_id:
        configured = await db.get_project_workflow(project_workflow_id)
        if not configured or configured.project_id != project_id or configured.status == "archived":
            raise LookupError("saved workflow not found")
        workflow_id = configured.workflow_id
        inputs = configured.inputs if inputs is None else inputs
    workflow = await db.get_workflow(workflow_id)
    if not workflow or workflow.project_id not in (None, project_id):
        raise LookupError("workflow not found")
    workflow = await resolve_execution_contract(
        storage=runtime.storage,
        workflow=workflow,
        project_id=project_id,
        revision=configured.definition_commit_sha if configured else None,
        input_schema=configured.input_schema if configured else None,
    )
    if workflow.executor != "workflow.code":
        raise ValueError("This setup readout is for code workflows.")
    normalized = normalize_workflow_inputs(
        schema=workflow.definition["input_schema"],
        project_id=project_id,
        inputs=inputs or {},
    )
    result = await code_readiness(
        database=db,
        integrations=getattr(runtime, "integrations", None),
        settings=settings,
        storage=runtime.storage,
        workflow=workflow,
        project_id=project_id,
        inputs=normalized,
    )
    if configured and not await db.has_project_access(
        project_id=project_id, clerk_user_id=configured.created_by_clerk_user_id
    ):
        result["schedule_issues"].append(
            "The schedule's author no longer has access. Save a new configuration."
        )
        result["can_schedule"] = False
    return {
        **result,
        "workflow_id": str(workflow.id),
        "definition_revision": workflow.current_commit_sha,
        "input_schema": workflow.definition["input_schema"],
        "schedule_modes": workflow.definition["schedule_modes"],
        "inputs": normalized,
        "checks_are_advisory": True,
    }
