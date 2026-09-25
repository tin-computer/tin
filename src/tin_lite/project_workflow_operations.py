"""Shared dashboard/MCP operations for saved configurations and their Temporal clock."""

from types import SimpleNamespace

from tin_lite.code_schedules import pause_for_issue
from tin_lite.schedules import TemporalScheduleService, WorkflowSchedule, next_run_after
from tin_lite.workflow_setup import prepare_workflow


async def schedule_issue(runtime, settings, configured):
    if not getattr(configured, "workflow_key", "").startswith("custom."):
        return None
    workflow = await runtime.database.get_workflow(configured.workflow_id)
    if workflow is None or workflow.executor != "workflow.code":
        return None
    try:
        setup = await prepare_workflow(
            runtime=runtime,
            settings=settings,
            project_id=configured.project_id,
            actor=configured.created_by_clerk_user_id,
            project_workflow_id=configured.id,
        )
    except LookupError:
        return "The schedule's author no longer has project access. Save a new configuration."
    except ValueError as exc:
        return str(exc)
    return next(iter(setup["issues"] + setup["schedule_issues"]), None)


def next_unskipped_run(schedule, configured):
    next_run = next_run_after(schedule)
    # An armed skip-once still holds, so the occurrence after it is the next run.
    if next_run is not None and next_run == getattr(configured, "skip_scheduled_for", None):
        return next_run_after(schedule, next_run)
    return next_run


async def sync_project_workflow(
    *, runtime, settings, configured, previous_schedule=None, paused=None
):
    db = runtime.database
    service = TemporalScheduleService(client=runtime.temporal, settings=settings)
    schedule_paused = configured.status == "paused" if paused is None else paused
    if configured.schedule is None:
        if previous_schedule is not None or configured.temporal_schedule_id is not None:
            await service.delete(str(configured.id))
        return await db.project_workflow_synced(
            project_workflow_id=configured.id,
            temporal_schedule_id=None,
            next_run_at=None,
        )
    issue = await schedule_issue(runtime, settings, configured)
    schedule = WorkflowSchedule.model_validate(configured.schedule)
    schedule_id = service.schedule_id(str(configured.id))
    if previous_schedule is None:
        await service.create(
            project_workflow_id=str(configured.id),
            schedule=schedule,
            paused=schedule_paused or bool(issue),
        )
    else:
        await service.update(
            project_workflow_id=str(configured.id),
            schedule=schedule,
            paused=schedule_paused or bool(issue),
        )
    result = await db.project_workflow_synced(
        project_workflow_id=configured.id,
        temporal_schedule_id=schedule_id,
        next_run_at=None if schedule_paused or issue else next_unskipped_run(schedule, configured),
        paused=schedule_paused and not issue,
    )
    if issue:
        await pause_for_issue(
            SimpleNamespace(_db=db, _temporal=runtime.temporal, _settings=settings), result, issue
        )
        result = await db.get_project_workflow(configured.id)
    return result


async def set_schedule_paused(*, runtime, settings, configured, paused):
    if configured.schedule is None or configured.status not in {"active", "paused"}:
        raise ValueError("Only an active or paused schedule can change pause state.")
    if not paused:
        issue = await schedule_issue(runtime, settings, configured)
        if issue:
            raise ValueError(issue)
    service = TemporalScheduleService(client=runtime.temporal, settings=settings)
    # A persisted pause blocks code dispatch even when the Temporal request needs retry.
    if paused:
        result = await runtime.database.set_project_workflow_paused(
            project_workflow_id=configured.id, project_id=configured.project_id, paused=True
        )
        await service.pause(str(configured.id))
        return result
    await service.resume(str(configured.id))
    return await runtime.database.project_workflow_synced(
        project_workflow_id=configured.id,
        temporal_schedule_id=service.schedule_id(str(configured.id)),
        next_run_at=next_unskipped_run(
            WorkflowSchedule.model_validate(configured.schedule), configured
        ),
    )


async def archive_configuration(
    *, runtime, settings, configured, actor, expected_settings_revision
):
    from tin_lite.domain import StaleSettingsRevisionError

    if configured.settings_revision != expected_settings_revision:
        raise StaleSettingsRevisionError("workflow settings changed after this editor was opened")
    if configured.status != "archived":
        await runtime.database.archive_project_workflow(
            project_workflow_id=configured.id,
            project_id=configured.project_id,
            expected_settings_revision=expected_settings_revision,
            clerk_user_id=actor,
            workflow_key=configured.workflow_key,
            workflow_title=configured.name,
        )
    if configured.temporal_schedule_id is not None:
        await TemporalScheduleService(client=runtime.temporal, settings=settings).delete(
            str(configured.id)
        )
