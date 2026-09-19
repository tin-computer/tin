"""Record the founder's onboarding picks in the plan and gate approval on them.

The growth onboarding plan is a markdown file with three checklists: the systems Tin runs, the
control the founder keeps, and the connections they made or declined. Their agent used to tick
those by rewriting the whole file; this module records the picks structurally, writes the ticks
into the file so it stays the record, and refuses to approve a plan with nothing ticked. Both the
MCP tools and the HTTP approve route go through here.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tin_lite.domain import RunStatus
from tin_lite.growth_onboarding import (
    KEY,
    PLAN_PATH,
    apply_plan_picks,
    current_plan_text,
    plan_readiness,
)
from tin_lite.organic_audit import digest
from tin_lite.project_files import StaleProjectRevisionError

SystemPick = str  # a system id from the plan, or "suggested" for the set Tin marked
ControlChoice = Literal["pull_request", "review_in_tin"]
ConnectionDecision = Literal["connected", "not_now"]


class OnboardingPickError(ValueError):
    """A pick Tin cannot record; `code` is stable, the message says what to do."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConnectionPick(BaseModel):
    """One line of the plan's Connections list: the founder connected it, or declined for now."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        pattern=r"^[a-z]+\.[a-z_]+$",
        description="Integration key as listed in the plan, e.g. infra.github or analytics.gsc.",
    )
    decision: ConnectionDecision = Field(
        description="connected once get_integration confirms it; not_now when the founder declines."
    )
    reason: str = Field(
        default="",
        max_length=200,
        description="The founder's reason for not_now, in their words; empty for connected.",
    )


async def onboarding_run(*, database: Any, run_id: UUID, require_waiting: bool = True) -> Any:
    """The onboarding run waiting for picks, or the error that says why it is not."""
    run = await database.get_run(run_id)
    if run is None:
        raise LookupError("run not found")
    if run.executor != KEY:
        raise OnboardingPickError(
            "wrong_workflow",
            f"Run {run_id} is {run.executor}, not {KEY}; picks apply to the plan run.",
        )
    if require_waiting and run.status is not RunStatus.NEEDS_INPUT:
        raise OnboardingPickError(
            "not_waiting",
            f"Run {run_id} is {run.status.value}; picks are recorded only while it waits for you.",
        )
    return run


async def record_onboarding_picks(
    *,
    runtime: Any,
    run: Any,
    request_id: UUID,
    systems: list[str],
    control: str,
    connections: list[ConnectionPick],
    actor_clerk_user_id: str,
    client_id: str | None,
    settings: Any = None,
) -> dict[str, Any]:
    """Tick the picks into the plan at HEAD as a member-authored commit; replay-safe."""
    database = runtime.database
    key = f"onboarding:{run.id}:picks:{request_id}"
    fingerprint = digest(
        {
            "actor": actor_clerk_user_id,
            "client": client_id,
            "systems": systems,
            "control": control,
            "connections": [pick.model_dump() for pick in connections],
        }
    )
    async with database.effect_lock(f"onboarding:{run.id}:control", KEY) as (conn, _):
        async with database.effect_lock(key, KEY, conn=conn) as (_, receipt):
            if receipt and receipt.result:
                if receipt.result["digest"] != fingerprint:
                    raise OnboardingPickError(
                        "request_conflict",
                        "Reuse request_id only for the original picks and caller.",
                    )
                if receipt.status == "completed":
                    return {**receipt.result["response"], "replayed": True}
            await onboarding_run(database=database, run_id=run.id)
            if await database.get_effect(f"onboarding:{run.id}:approved_plan", conn=conn):
                raise OnboardingPickError(
                    "already_approved",
                    (
                        "This run's approved plan is sealed; start a new onboarding run "
                        "to change its setup."
                    ),
                )
            if receipt and receipt.result:
                intent = receipt.result["intent"]
                project = await database.get_project(run.project_id)
            else:
                project = await runtime.database.get_project(run.project_id)
                if project is None:
                    raise LookupError("project not found")
                connected = {
                    item.provider_key: item
                    for item in await runtime.integrations.list_connections(project.id)
                }
                for pick in connections:
                    if pick.decision != "connected":
                        continue
                    row = connected.get(pick.provider)
                    state = row.status if row is not None else "none"
                    if state != "connected":
                        raise OnboardingPickError(
                            "not_connected",
                            f"{pick.provider} is not connected (status: {state}). Call "
                            "start_integration_connection, have the founder finish it in the "
                            "browser, confirm with get_integration, then record it again; "
                            "or record it as not_now with their reason.",
                        )
                text, head = await current_plan_text(storage=runtime.storage, project=project)
                original_text = text
                # Older plans may omit useful optional access now exposed by the MCP.
                # Accept only server-recommended providers, and record the explicit decision.
                from tin_lite.growth_onboarding import picked_actions, plan_block, suggested_systems
                from tin_lite.onboarding_experience import access_needs

                block = plan_block(text) or {}
                selected = suggested_systems(block) if systems == ["suggested"] else systems
                current_connections = plan_readiness(text)["connections"]
                recommendations = await access_needs(
                    database=database,
                    project_id=project.id,
                    inputs=run.input or {},
                    actions=picked_actions(block, selected),
                    connections=current_connections,
                )
                for need in recommendations:
                    provider = need["provider"]
                    if provider in current_connections or not any(
                        p.provider == provider for p in connections
                    ):
                        continue
                    line = f"- [ ] {provider} — {need['name']}, recommended: {need['benefit']}\n"
                    # A separate checklist section works even when a legacy heading has a suffix.
                    text += f"\n## Connections — additional access\n{line}"
                try:
                    updated = apply_plan_picks(
                        text,
                        systems=systems,
                        control=control,
                        connections={
                            pick.provider: (pick.decision, pick.reason) for pick in connections
                        },
                    )
                except ValueError as exc:
                    raise OnboardingPickError("unknown_pick", str(exc)) from exc
                intent = {"head": head, "text": updated, "changed": updated != original_text}
                await database.start_effect(conn, execution_key=key, operation=KEY)
                await database.save_effect_progress(
                    conn, execution_key=key, result={"digest": fingerprint, "intent": intent}
                )
            head, updated = intent["head"], intent["text"]
            if not intent["changed"]:
                revision, replayed = head, False
            else:
                try:
                    result = await runtime.project_files.commit(
                        project=project,
                        actor_clerk_user_id=actor_clerk_user_id,
                        client_id=client_id,
                        request_id=request_id,
                        expected_revision=head,
                        message=(
                            f"Onboarding picks: systems {', '.join(systems)}; control {control}"
                        ),
                        changes=[{"operation": "upsert", "path": PLAN_PATH, "content": updated}],
                    )
                except StaleProjectRevisionError as exc:
                    raise OnboardingPickError(
                        "stale_revision",
                        "The plan changed while the picks were being recorded; call the "
                        "tool again with a new request_id.",
                    ) from exc
                revision, replayed = result.revision, result.replayed
            readiness = plan_readiness(updated)
            result_view = {
                "run_id": str(run.id),
                "project_id": str(project.id),
                "revision": revision,
                "replayed": replayed,
                "systems": readiness["systems"],
                "offered": readiness["offered"],
                "control": readiness["control"],
                "connections": readiness["connections"],
                "next": "approve_workflow_run",
                "note": (
                    f"Recorded in {PLAN_PATH}. Now call approve_workflow_run with this run_id; "
                    "Tin then sets those systems up, skipping what was declined."
                ),
            }

            if settings is not None:
                from tin_lite.onboarding_experience import onboarding_experience

                result_view.update(
                    await onboarding_experience(
                        database=database,
                        storage=runtime.storage,
                        settings=settings,
                        project_id=run.project_id,
                        run=run,
                        plan_text=updated,
                    )
                )

            await runtime.database.complete_effect(
                conn, execution_key=key, result={"digest": fingerprint, "response": result_view}
            )
            return result_view


async def ensure_onboarding_approvable(*, runtime: Any, run: Any) -> None:
    """Validate and seal the exact approved plan in Postgres before signaling Temporal."""
    key = f"onboarding:{run.id}:approved_plan"
    async with runtime.database.effect_lock(f"onboarding:{run.id}:control", KEY) as (conn, _):
        async with runtime.database.effect_lock(key, KEY, conn=conn) as (_, receipt):
            if receipt and receipt.status == "completed":
                return
            await onboarding_run(database=runtime.database, run_id=run.id)
            project = await runtime.database.get_project(run.project_id)
            if project is None:
                raise LookupError("project not found")
            try:
                text, head = await current_plan_text(storage=runtime.storage, project=project)
            except Exception as exc:
                raise OnboardingPickError(
                    "plan_missing",
                    f"{PLAN_PATH} could not be read at the project's current revision.",
                ) from exc
            readiness = plan_readiness(text)
            if not readiness["systems"]:
                offered = ", ".join(readiness["offered"]) or "none"
                raise OnboardingPickError(
                    "no_systems",
                    f"No system is ticked in {PLAN_PATH}. Call record_onboarding_picks(run_id, "
                    f"request_id, systems, control, connections) with systems from {offered} "
                    '(or ["suggested"]), then approve_workflow_run again.',
                )
            if readiness["control"] is None:
                raise OnboardingPickError(
                    "no_control",
                    f"No control is ticked in {PLAN_PATH}. Call record_onboarding_picks "
                    "with control set to pull_request or review_in_tin, "
                    "then approve_workflow_run again.",
                )
            from tin_lite.growth_onboarding import plan_block

            if plan_block(text) is None:
                raise OnboardingPickError(
                    "invalid_plan", "The plan has no executable onboarding block."
                )
            from tin_lite.onboarding_experience import validate_plan

            issues = await validate_plan(
                database=runtime.database,
                project_id=run.project_id,
                text=text,
                systems=readiness["systems"],
                timezone=(run.input or {}).get("timezone") or "UTC",
            )
            if issues:
                summary = "; ".join(
                    f"{item.get('workflow_key', item.get('system', 'plan'))}: {item['reason']}"
                    for item in issues
                )
                raise OnboardingPickError(
                    "invalid_plan",
                    f"Setup has not started. Correct the plan before approval: {summary}",
                )
            await runtime.database.start_effect(conn, execution_key=key, operation=KEY)
            await runtime.database.complete_effect(
                conn, execution_key=key, result={"text": text, "revision": head}
            )
