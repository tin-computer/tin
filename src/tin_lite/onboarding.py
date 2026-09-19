"""Growth onboarding: the live Tin state the plan run reads.

A Codex procedure cannot call the MCP, so the switchboard computes which workflows this
project can consider, using the shared project prerequisite readiness and provider gates, and hands
that to the plan run as the `tin_state` input. Each blocked workflow also says who unblocks
it: `tin_operator` (service availability; never a founder credential task),
`connect_integration` (the founder connects a provider), or `prior_runs`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from tin_lite import organic_system
from tin_lite.billing_contracts import BillingError
from tin_lite.domain import (
    GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME,
    IntegrationConnection,
    Workflow,
)
from tin_lite.executor_gates import keyword_plan_gate, organic_audit_gate, organic_system_gate
from tin_lite.integrations import parse_integration_requirements, registered_integrations
from tin_lite.keyword_plan import KEY as KEYWORD_KEY
from tin_lite.organic_audit import AUDIT_KEY
from tin_lite.workflow_inputs import client_input_schema
from tin_lite.workflow_prerequisites import project_readiness

ONBOARDING_WORKFLOW_KEYS = frozenset({GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME, "growth.onboarding"})


def tin_state(
    *,
    settings: Any,
    workflows: list[Workflow],
    connections: list[IntegrationConnection],
    project_workflows: list[Any] = (),
    recent_runs: list[Any] = (),
    readiness: dict | None = None,
    billing_restrictions: dict | None = None,
) -> dict[str, Any]:
    connected = {item.provider_key for item in connections if item.status == "connected"}
    integrations = [
        {"provider_key": item.key, "name": item.name, "connected": item.key in connected}
        for item in registered_integrations()
    ]
    rows: list[dict[str, Any]] = []
    ordered = sorted(
        workflows,
        key=lambda item: (
            item.system_order if item.system_order is not None else 1_000_000,
            item.key,
        ),
    )
    for workflow in ordered:
        if workflow.project_id is not None or workflow.key in ONBOARDING_WORKFLOW_KEYS:
            continue
        definition = workflow.definition or {}
        requirements = parse_integration_requirements(definition.get("integration_requirements"))
        required_providers = sorted({item.provider_key for item in requirements if item.required})
        missing = [provider for provider in required_providers if provider not in connected]
        reason = _executor_reason(workflow.executor, settings)
        unblock: dict[str, str] | None = None
        if reason is not None:
            unblock = {"kind": "tin_operator", "detail": reason}
        elif missing:
            reason = f"Connect {', '.join(missing)} first."
            unblock = {
                "kind": "connect_integration",
                "detail": "The founder connects it from Integrations, or their agent starts "
                "the connection and hands the founder the authorization link.",
            }
        prerequisite_state = (readiness or {}).get(workflow.id)
        if reason is None and prerequisite_state and prerequisite_state["state"] == "blocked":
            reason = "Required project context is missing; see readiness.unmet."
            unblock = {"kind": "prior_runs", "detail": reason}
        billing = (billing_restrictions or {}).get(workflow.id)
        if billing:
            reason = billing["message"]
            unblock = {"kind": "tin_operator", "detail": reason}
        schema = definition.get("input_schema") or {}
        rows.append(
            {
                "key": workflow.key,
                "title": workflow.title,
                "description": workflow.description,
                "system": definition.get("system"),
                "kind": definition.get("kind", "workflow"),
                "schedule_modes": list(definition.get("schedule_modes") or ["on_demand"]),
                "runnable": reason is None,
                "reason": reason,
                "unblock": unblock,
                "readiness": prerequisite_state,
                "prerequisites": list(definition.get("prerequisites") or []),
                "billing": billing,
                "admission_note": (
                    "Exact inputs, saved configuration, delivery and funding are "
                    "validated when the run starts."
                ),
                "requires_integrations": required_providers,
                "input_schema": client_input_schema(definition),
                "required_inputs": [
                    key for key in schema.get("required", []) if key != "project_id"
                ],
                "optional_inputs": sorted(
                    key
                    for key in (schema.get("properties") or {})
                    if key != "project_id" and key not in schema.get("required", [])
                ),
            }
        )
    keys = {workflow.id: workflow.key for workflow in workflows}
    running = [
        {
            "key": getattr(item, "workflow_key", None) or keys.get(item.workflow_id, "?"),
            "name": item.name,
            "cadence": (item.schedule or {}).get("cadence") if item.schedule else "on_demand",
            "weekdays": (item.schedule or {}).get("weekdays", []) if item.schedule else [],
            "local_time": (item.schedule or {}).get("local_time") if item.schedule else None,
            "next_run_at": item.next_run_at.isoformat() if item.next_run_at else None,
            "status": item.status,
        }
        for item in project_workflows
        if item.status in {"active", "paused"}
    ]
    recent = [
        {
            "key": keys.get(run.workflow_id, "?"),
            "status": run.status.value if hasattr(run.status, "value") else str(run.status),
            "artifact_path": run.artifact_path,
            "finished_at": (
                run.finished_at.isoformat() if getattr(run, "finished_at", None) else None
            ),
        }
        for run in list(recent_runs)[:20]
        if keys.get(run.workflow_id) not in ONBOARDING_WORKFLOW_KEYS
    ]
    return {
        "integrations": integrations,
        "workflows": rows,
        "running": running,
        "recent_runs": recent,
    }


def _executor_reason(executor: str, settings: Any) -> str | None:
    if executor == AUDIT_KEY:
        return organic_audit_gate(settings)
    if executor == KEYWORD_KEY:
        return keyword_plan_gate(settings)
    if executor == organic_system.KEY:
        return organic_system_gate(settings)
    return None


async def onboarding_tin_state(
    *, database: Any, settings: Any, project_id: UUID, storage: Any = None
) -> dict[str, Any]:
    workflows = await database.list_workflows(project_id=project_id)
    connections = await database.list_integration_connections(project_id)
    project_workflows = await database.list_project_workflows(project_id=project_id)
    recent_runs = await database.list_runs(project_id=project_id, limit=20)
    readiness = await project_readiness(
        database=database, storage=storage, project_id=project_id, workflows=workflows
    )
    billing = await billing_restrictions(
        database=database, project_id=project_id, workflows=workflows
    )
    return tin_state(
        readiness=readiness,
        billing_restrictions=billing,
        settings=settings,
        workflows=workflows,
        connections=connections,
        project_workflows=project_workflows,
        recent_runs=recent_runs,
    )


async def billing_restrictions(*, database, project_id, workflows):
    """Report tariff availability only; admission still owns quotes and reservations."""
    billing = getattr(database, "billing", None)
    if billing is None:
        return {}
    enrolled = await database.pool.fetchval(
        (
            "SELECT EXISTS(SELECT 1 FROM billing_accounts b JOIN projects p "
            "ON p.workspace_id=b.workspace_id WHERE p.id=$1 AND b.run_billing_enabled)"
        ),
        project_id,
    )
    if not enrolled:
        return {}
    blocked = {}
    for workflow in workflows:
        try:
            billing.terms(workflow.definition, project_id)
        except BillingError as exc:
            blocked[workflow.id] = {"code": exc.code, "message": str(exc)}
    return blocked
