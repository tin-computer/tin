"""Project-bound onboarding handoff facts shared by MCP clients.

Read projections and receipts only. These fields describe access and delivery; reading
them never connects an account, starts work, or grants publishing authority.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from tin_lite.growth_onboarding import (
    CONTENT_DRAFT_KEYS,
    current_plan_text,
    expectation,
    picked_actions,
    plan_block,
    plan_connections,
    plan_readiness,
    ui_links,
)
from tin_lite.integrations import registered_integrations
from tin_lite.product_urls import dashboard_url
from tin_lite.schedules import WorkflowSchedule
from tin_lite.workflow_definitions import ensure_schedule_allowed
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs

_PLAN_SYSTEM_SHAPE = Draft202012Validator(
    {
        "type": "object",
        "required": ["workflows"],
        "properties": {
            "workflows": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["key"],
                    "properties": {
                        "key": {"type": "string", "minLength": 1},
                        "inputs": {"type": "object"},
                        "mode": {"type": "string"},
                        "weekdays": {"type": "array", "items": {"type": "string"}},
                        "weekday": {"type": "string"},
                        "local_time": {"type": "string"},
                    },
                },
            }
        },
    }
)


def delivery_destination(settings: Any, project_id: UUID) -> dict[str, Any]:
    links = ui_links(dashboard_url(settings), project_id)
    return {
        "channel": "tin",
        "supported_channels": ["tin"],
        "notifications_enabled": False,
        "description": (
            "Open Tin to receive reports in Files and review drafts in Decisions. "
            "Email and Slack notifications are not available for these results."
        ),
        "reports_url": links["files"],
        "review_url": links["decisions"],
        "schedule_url": links["my_system"],
    }


def result_links(settings: Any, run: Any) -> list[dict[str, Any]]:
    """Only advertise an individual result when an artifact actually exists."""
    if not getattr(run, "artifact_path", None) or not getattr(run, "canonical_commit_sha", None):
        return []
    review = run.status.value == "needs_input" and run.review_required
    return [
        {
            "run_id": str(run.id),
            "title": getattr(run, "workflow_name", "Result"),
            "kind": "review" if review else "report",
            "url": f"{dashboard_url(settings)}/document/{run.id}?project={run.project_id}",
            "artifact_path": run.artifact_path,
            "revision": run.canonical_commit_sha,
        }
    ]


async def validate_plan(
    *, database: Any, project_id: UUID, text: str, systems: list[str], timezone: str
) -> list[dict[str, Any]]:
    """Validate exact proposed inputs and schedules before any setup side effects."""
    block = plan_block(text)
    if block is None:
        return [
            {
                "code": "invalid_plan",
                "reason": "The plan needs a valid tin-plan block.",
                "next_action": "Correct the plan before approval.",
            }
        ]
    issues = []
    for row in block["systems"]:
        if not isinstance(row, dict) or row.get("id") not in systems:
            continue
        for error in _PLAN_SYSTEM_SHAPE.iter_errors(row):
            field = ".".join(str(part) for part in error.absolute_path) or "system"
            issues.append(
                {
                    "system": row["id"],
                    "code": "invalid_plan",
                    "reason": f"{field} must satisfy {error.validator}: {error.validator_value}.",
                    "next_action": "Correct this system in the plan before approval.",
                }
            )
    if issues:
        return issues
    actions = picked_actions(block, systems)
    for system in systems:
        if not any(a["system"] == system for a in actions):
            # A shared workflow may be deduplicated into another selected system.
            rows = [s for s in block["systems"] if isinstance(s, dict) and s.get("id") == system]
            if not rows or not any(
                isinstance(w, dict) and isinstance(w.get("key"), str)
                for w in rows[0].get("workflows", [])
            ):
                issues.append(
                    {
                        "system": system,
                        "code": "empty_system",
                        "reason": "The selected system has no executable workflows.",
                        "next_action": "Correct the system in the plan before approval.",
                    }
                )
    for action in actions:
        template = await database.get_registry_workflow(action["key"])
        code, reason = "invalid_inputs", ""
        if template is None:
            code, reason = "workflow_unavailable", "This workflow is not registered."
        elif template.definition.get("kind") == "task":
            code, reason = (
                "unsupported_workflow",
                "One-off tasks cannot be installed by onboarding.",
            )
        else:
            try:
                normalize_workflow_inputs(
                    schema=template.definition["input_schema"],
                    project_id=project_id,
                    inputs=action["inputs"],
                )
                if action["mode"] != "once":
                    code = "invalid_schedule"
                    schedule = WorkflowSchedule(
                        cadence=action["mode"],
                        weekdays=action["weekdays"],
                        local_time=action["local_time"],
                        timezone=timezone,
                    )
                    ensure_schedule_allowed(template.definition, schedule)
            except WorkflowInputError as exc:
                cause = exc.__cause__
                if isinstance(cause, ValidationError):
                    field = ".".join(str(p) for p in cause.absolute_path) or "inputs"
                    reason = f"{field} must satisfy {cause.validator}: {cause.validator_value}."
                else:
                    reason = "Inputs do not match the workflow schema; inspect get_workflow."
            except ValueError:
                reason = "Use a supported cadence, weekday, HH:MM time and IANA timezone."
        if reason:
            issues.append(
                {
                    "system": action["system"],
                    "workflow_key": action["key"],
                    "code": code,
                    "reason": reason,
                    "next_action": "Correct this workflow in the plan before approval.",
                }
            )
    return issues


async def access_needs(
    *,
    database: Any,
    project_id: UUID,
    inputs: dict[str, Any],
    actions: list[dict[str, Any]],
    connections: dict[str, Any],
    initial: bool = False,
) -> list[dict[str, Any]]:
    live = {c.provider_key: c for c in await database.list_integration_connections(project_id)}
    required: dict[str, list[str]] = {}
    for action in actions:
        template = await database.get_registry_workflow(action["key"])
        for need in template.definition.get("integration_requirements", []) if template else []:
            if need.get("required", True):
                required.setdefault(need["provider_key"], []).append(action["key"])
    keys = {a["key"] for a in actions}
    relevant = set(required) | (set(connections) - {"workspace.google"})
    if initial or inputs.get("product_url"):
        relevant.add("analytics.gsc")
    if initial or keys & CONTENT_DRAFT_KEYS or inputs.get("system_repository"):
        relevant.add("infra.github")
    benefits = {
        "analytics.gsc": (
            "Use real search queries and impressions to prioritize research and audits.",
            "Select the verified property for the product's website.",
        ),
        "infra.github": (
            "Inspect the website code and deliver approved content or fixes "
            "as unmerged pull requests.",
            "Confirm the website is on GitHub and select the repository that serves it.",
        ),
        "workspace.google": (
            "Run the selected signup test or research mailbox relationships; "
            "email sends need separate approval.",
            "Confirm the selected workflow needs a Google mailbox before connecting it.",
        ),
    }
    needs = []
    for provider in registered_integrations():
        if provider.key not in relevant:
            continue
        connection = live.get(provider.key)
        connected = connection is not None and connection.status == "connected"
        decision = connections.get(provider.key, {})
        benefit, selection = benefits[provider.key]
        needs.append(
            {
                "provider": provider.key,
                "name": provider.name,
                "status": "connected" if connected else "not_connected",
                "decision": decision.get("state", "open"),
                "reason": decision.get("note", ""),
                "requirement": "required" if provider.key in required else "recommended",
                "applicability": "Confirm this service is used by the business before connecting.",
                "required_by": required.get(provider.key, []),
                "benefit": benefit,
                "permissions": provider.access_label,
                "resource_selection": selection,
                "estimated_setup": "about one minute in the browser",
                "connect": {
                    "name": "start_integration_connection",
                    "arguments": {
                        "project_id": str(project_id),
                        "provider_key": provider.key,
                    },
                },
            }
        )
    return needs


async def onboarding_experience(
    *,
    database: Any,
    storage: Any,
    settings: Any,
    project_id: UUID,
    run: Any = None,
    plan_text: str | None = None,
) -> dict[str, Any]:
    """Additive v1 contract; legacy prose and links remain available to old clients."""
    destination = delivery_destination(settings, project_id)
    view: dict[str, Any] = {
        "onboarding_contract_version": 1,
        "setup_status": "not_started",
        "access_needs": [],
        "first_deliverables": [],
        "delivery_destination": destination,
        "result_links": [],
        "incomplete_setup": [],
    }
    inputs = (run.input or {}) if run else {}
    actions, connections, setup = [], {}, None
    if run:
        receipt = await database.get_effect(f"onboarding:{run.id}:setup")
        setup = receipt.result if receipt and receipt.status == "completed" else None
    if setup:
        actions, connections = setup["actions"], setup.get("connections", {})
        view["setup_status"] = "complete"
        configured = {
            str(w.id): w for w in await database.list_project_workflows(project_id=project_id)
        }
        for action in actions:
            problem = action.get("status") in {"blocked", "skipped", "declined"}
            code = action.get("status")
            reason = action.get("reason", "")
            if action.get("first_run_status") == "blocked":
                problem, code = True, "first_run_blocked"
            if action.get("status") == "scheduled":
                saved = configured.get(action.get("project_workflow_id"))
                if saved is None or saved.status != "active":
                    problem, code = True, "schedule_inactive"
                    reason = "The saved schedule is missing or inactive."
            if problem:
                view["incomplete_setup"].append(
                    {
                        "system": action.get("system"),
                        "workflow_key": action["key"],
                        "code": code,
                        "reason": reason,
                        "next_action": (
                            "Left out by your choice; enable it only if you change that decision."
                            if code == "declined"
                            else "Review the blocked work in My system before restarting it."
                        ),
                    }
                )
            if action.get("delivery_error"):
                view["incomplete_setup"].append(
                    {
                        "workflow_key": action["key"],
                        "code": "delivery_not_configured",
                        "reason": action["delivery_error"],
                        "next_action": "Configure delivery; drafts remain in Tin.",
                    }
                )
    elif run and run.status.value == "needs_input":
        project = await database.get_project(project_id)
        try:
            if plan_text is None:
                text, _ = await current_plan_text(storage=storage, project=project)
            else:
                text = plan_text
        except Exception:  # Keep durable run state readable when artifact storage is unavailable.
            view["incomplete_setup"].append(
                {
                    "code": "plan_unavailable",
                    "reason": "The plan could not be read.",
                    "next_action": "Retry get_run.",
                }
            )
        else:
            readiness = plan_readiness(text)
            systems = readiness["systems"] or readiness["suggested"]
            actions = picked_actions(plan_block(text) or {}, systems)
            connections = plan_connections(text)
            view["incomplete_setup"] = await validate_plan(
                database=database,
                project_id=project_id,
                text=text,
                systems=systems,
                timezone=inputs.get("timezone") or "UTC",
            )
        view["setup_status"] = "waiting_for_selection"
    elif run:
        view["setup_status"] = (
            "working" if run.status.value in {"pending", "running"} else "unknown"
        )
    view["access_needs"] = await access_needs(
        database=database,
        project_id=project_id,
        inputs=inputs,
        actions=actions,
        connections=connections,
        initial=run is None,
    )
    for action in actions:
        template = await database.get_registry_workflow(action["key"])
        child = await database.get_run(UUID(action["run_id"])) if action.get("run_id") else None
        if child is not None and child.project_id != project_id:
            raise ValueError("Onboarding child is outside this project.")
        links = result_links(settings, child) if child else []
        if action.get("run_id") and child is None:
            view["incomplete_setup"].append(
                {
                    "workflow_key": action["key"],
                    "code": "first_run_unavailable",
                    "reason": "The recorded first run is unavailable.",
                    "next_action": "Check the run before starting any replacement work.",
                }
            )
        if child and child.status.value in {"failed", "stopped"}:
            view["incomplete_setup"].append(
                {
                    "workflow_key": action["key"],
                    "run_id": str(child.id),
                    "code": "first_run_failed",
                    "reason": "The first run did not finish successfully.",
                    "next_action": "Read get_run for this run before deciding whether to retry.",
                }
            )
        status = child.status.value if child else action.get("status", "proposed")
        saved = configured.get(action.get("project_workflow_id")) if setup else None
        if setup:
            # A cleared or paused schedule is current state, not missing receipt data.
            schedule = getattr(saved, "schedule", None)
            next_run = (
                getattr(saved, "next_run_at", None)
                if saved and saved.status == "active" and schedule
                else None
            )
            if not child and action.get("status") == "scheduled":
                status = (
                    "schedule_inactive"
                    if saved is None or saved.status != "active"
                    else "scheduled"
                    if schedule
                    else "on_demand"
                )
        else:
            schedule = (
                {
                    "cadence": action["mode"],
                    "weekdays": action.get("weekdays", []),
                    "local_time": action.get("local_time"),
                    "timezone": inputs.get("timezone", "UTC"),
                }
                if action["mode"] != "once"
                else None
            )
            next_run = None
        next_action = "Check get_run after the estimated time"
        if links:
            next_action = "Review the result"
        elif status == "proposed":
            next_action = "Choose this system and review its access needs before setup"
        elif status == "on_demand":
            next_action = "Start the saved workflow when you need a result"
        elif status in {"blocked", "skipped", "declined", "failed", "stopped", "schedule_inactive"}:
            next_action = "Read incomplete_setup for the next action"
        view["first_deliverables"].append(
            {
                "system": action.get("system"),
                "workflow_key": action["key"],
                "title": template.title if template else action["key"],
                "purpose": expectation(action["key"])["watch"],
                "estimated_time": expectation(action["key"])["first"],
                "timing_basis": "after admission; estimates are not guarantees",
                "status": status,
                "run_id": str(child.id) if child else None,
                "schedule": schedule,
                "next_run_at": next_run.isoformat() if next_run else None,
                "result_links": links,
                "destination": "Decisions"
                if template and template.definition.get("human_review")
                else "Files",
                "next_action": next_action,
            }
        )
        view["result_links"].extend(links)
    if not run:
        view["first_deliverables"] = [
            {
                "title": "A growth plan for your business",
                "status": "not_started",
                "estimated_time": "three to six minutes after starting",
                "purpose": "Choose a first experiment, its access needs and success criteria.",
                "destination": "Decisions",
                "result_links": [],
            }
        ]
    if run:
        view["result_links"] = result_links(settings, run) + view["result_links"]
    if setup and view["incomplete_setup"]:
        view["setup_status"] = "partial"
    if not inputs.get("system_analytics"):
        view["measurement_needs"] = [
            {
                "status": "unknown",
                "benefit": "Connect acquisition work to signups and activation.",
                "next_action": (
                    "Identify the analytics tool and supply aggregate conversion counts; "
                    "no analytics access is assumed."
                ),
            }
        ]
    view["connection_batch"] = {
        "name": "start_integration_connections",
        "arguments": {
            "project_id": str(project_id),
            "providers": [
                n["provider"]
                for n in view["access_needs"]
                if n["status"] != "connected" and n["decision"] != "declined"
            ],
        },
        "requires_founder_choice": True,
    }
    return view
