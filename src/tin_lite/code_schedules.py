"""Code occurrences on the existing Temporal Schedule dispatcher and run contract."""

import json
from datetime import UTC, datetime, timedelta

from tin_lite.billing_contracts import BillingError
from tin_lite.code_project_files import select as select_project_files
from tin_lite.domain import RunStatus
from tin_lite.private_workflows import require_private_execution
from tin_lite.schedules import (
    ScheduledWorkflowSkip,
    TemporalScheduleService,
    WorkflowSchedule,
    next_run_after,
)
from tin_lite.workflow_definitions import ensure_schedule_allowed, resolve_execution_contract
from tin_lite.workflow_inputs import normalize_workflow_inputs
from tin_lite.workflow_setup import code_readiness


def dispatched(run):
    if run.status in {RunStatus.FAILED, RunStatus.STOPPED, RunStatus.SUCCEEDED}:
        return {}
    return {
        "run_id": str(run.id),
        "executor": run.executor,
        "temporal_workflow_id": run.temporal_workflow_id,
    }


async def pause_for_issue(common, configured, message):
    db = common._db
    async with db.pool.acquire() as conn, conn.transaction():
        changed = await conn.fetchval(
            """UPDATE project_workflows SET status='paused', last_error=$3,
               next_run_at=NULL, updated_at=now()
               WHERE id=$1 AND settings_revision=$2 AND status='active' RETURNING id""",
            configured.id,
            configured.settings_revision,
            message[:1000],
        )
        if changed:
            await conn.execute(
                """INSERT INTO activity_events
                   (project_id,event_type,details,summary,audience,dedupe_key)
                   VALUES($1,'workflow_schedule_needs_attention',$2::jsonb,$3,'product',$4)
                   ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING""",
                configured.project_id,
                json.dumps(
                    {
                        "kind": "needs_you",
                        "project_workflow_id": str(configured.id),
                        "workflow_key": configured.workflow_key,
                    }
                ),
                f"{configured.name} schedule paused: {message}"[:1000],
                f"code-schedule:{configured.id}:issue:{configured.settings_revision}",
            )
    # Persist the stop before talking to Temporal. A retry can repair this projection gap.
    current = await db.get_project_workflow(configured.id)
    if current and current.status == "paused" and current.last_error:
        await TemporalScheduleService(client=common._temporal, settings=common._settings).pause(
            str(configured.id)
        )


async def dispatch_code_schedule(common, configured, workflow, payload):
    db = common._db
    key = f"schedule:{payload['occurrence_id']}"
    existing = await db.get_run_by_start_key(
        project_id=configured.project_id, start_idempotency_key=key
    )
    if existing is not None:
        if (
            existing.project_workflow_id != configured.id
            or existing.workflow_id != configured.workflow_id
            or existing.executor != "workflow.code"
        ):
            raise ValueError("Schedule occurrence belongs to another configuration.")
        # Activity-response loss must recover the accepted inputs, even after a future edit.
        return dispatched(existing)

    async def accepted_after_race():
        accepted = await db.get_run_by_start_key(
            project_id=configured.project_id, start_idempotency_key=key
        )
        if accepted is not None:
            if (
                accepted.project_workflow_id != configured.id
                or accepted.workflow_id != configured.workflow_id
                or accepted.executor != "workflow.code"
            ):
                raise ValueError("Schedule occurrence belongs to another configuration.")
            return dispatched(accepted)
        return None

    if configured.status != "active" or configured.schedule is None:
        if configured.status == "paused" and configured.last_error:
            await pause_for_issue(common, configured, configured.last_error)
        return {}
    scheduled_for = datetime.fromisoformat(payload["scheduled_for"])
    schedule = WorkflowSchedule.model_validate(configured.schedule)
    if (
        scheduled_for < datetime.now(UTC) - timedelta(hours=24)
        or (schedule.end_at and scheduled_for >= schedule.end_at)
        or (schedule.start_at and scheduled_for < schedule.start_at)
    ):
        await db.advance_project_workflow_schedule(
            project_workflow_id=configured.id,
            next_run_at=next_run_after(schedule),
            expected_settings_revision=configured.settings_revision,
        )
        return {}
    if await db.consume_project_workflow_skip(
        project_workflow_id=configured.id, scheduled_for=scheduled_for
    ):
        return {}
    try:
        workflow = await resolve_execution_contract(
            storage=common._storage,
            workflow=workflow,
            project_id=configured.project_id,
            revision=configured.definition_commit_sha,
            input_schema=configured.input_schema,
        )
        require_private_execution(common._settings, workflow, configured.project_id)
        ensure_schedule_allowed(workflow.definition, schedule)
        if not await db.has_project_access(
            project_id=configured.project_id, clerk_user_id=configured.created_by_clerk_user_id
        ):
            raise ValueError(
                "The schedule's author no longer has project access. "
                "A current member can save a new configuration."
            )
        inputs = normalize_workflow_inputs(
            schema=configured.input_schema,
            project_id=configured.project_id,
            inputs=configured.inputs,
        )
        readiness = await code_readiness(
            database=db,
            integrations=common._integrations,
            settings=common._settings,
            storage=common._storage,
            workflow=workflow,
            project_id=configured.project_id,
            inputs=inputs,
        )
        issues = readiness["issues"] + readiness["schedule_issues"]
        if issues:
            raise ValueError(issues[0])
        file_source = await select_project_files(
            database=db, storage=common._storage, project_id=configured.project_id
        )
        run, _created = await db.create_run(
            project_id=configured.project_id,
            workflow_id=configured.workflow_id,
            started_by_clerk_user_id=configured.created_by_clerk_user_id,
            start_idempotency_key=key,
            input_payload=inputs,
            project_workflow_id=configured.id,
            definition_commit_sha=configured.definition_commit_sha,
            pinned_definition=workflow.definition,
            trigger_source="schedule",
            scheduled_for=scheduled_for,
            schedule_settings_revision=configured.settings_revision,
            code_project_files_source=file_source,
        )
    except ScheduledWorkflowSkip:
        if (accepted := await accepted_after_race()) is not None:
            return accepted
        await db.advance_project_workflow_schedule(
            project_workflow_id=configured.id,
            next_run_at=next_run_after(schedule, scheduled_for),
            expected_settings_revision=configured.settings_revision,
        )
        return {}
    except BillingError as exc:
        if (accepted := await accepted_after_race()) is not None:
            return accepted
        await pause_for_issue(common, configured, str(exc))
        return {}
    except (LookupError, ValueError, RuntimeError) as exc:
        if (accepted := await accepted_after_race()) is not None:
            return accepted
        await pause_for_issue(common, configured, str(exc))
        return {}
    return dispatched(run)
