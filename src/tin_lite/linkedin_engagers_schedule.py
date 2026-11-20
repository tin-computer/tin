"""Daily engagers reads for the copies members saved before engagers could run on a schedule.

Until 1.5.0 `social.linkedin_engagers` ran on demand only, so every saved copy has no schedule
and pins a definition that refuses one. `tin-lite schedule-linkedin-engagers` moves each active
copy without a schedule to the current definition with a daily schedule, then gives Temporal
the calendar the way a save does. The schedule runs as the member who saved the copy, with
their LinkedIn, at their own minute between 11:00 and 16:00 in the founder's time zone
(`linkedin_engagers.daily_time`). A copy someone paused or scheduled, one whose member left the
project, and one whose inputs no longer fit the definition are listed and left as they are.
Running it again changes nothing.
"""

from __future__ import annotations

import json
from typing import Any

from tin_lite import linkedin_engagers as engagers
from tin_lite import linkedin_requests as requests
from tin_lite.project_workflow_operations import sync_failed, sync_project_workflow


async def schedule_saved_copies(*, runtime: Any, settings: Any, apply: bool) -> list[dict]:
    """One line per saved engagers copy: scheduled, would schedule, or why it was left."""

    db = runtime.database
    workflow = await db.get_workflow(engagers.WORKFLOW_ID)
    if workflow is None or "daily" not in workflow.definition.get("schedule_modes", []):
        raise RuntimeError("Sync the built-in catalog first: engagers can't run daily yet.")
    rows = await db.pool.fetch(
        """SELECT id, project_id, created_by_clerk_user_id AS member, status, schedule,
                  settings_revision, input_schema = $2::jsonb AS fits
           FROM project_workflows
           WHERE workflow_id = $1 AND status <> 'archived'
           ORDER BY created_at""",
        workflow.id,
        json.dumps(workflow.definition["input_schema"]),
    )
    results = []
    for row in rows:
        line = {"project_workflow_id": str(row["id"]), "project_id": str(row["project_id"])}
        if row["schedule"] is not None:
            results.append({**line, "outcome": "already scheduled"})
            continue
        if row["status"] != "active":
            results.append({**line, "outcome": f"left as it is ({row['status']})"})
            continue
        if not row["fits"]:
            results.append({**line, "outcome": "left as it is (its inputs changed)"})
            continue
        if not await db.has_project_access(
            project_id=row["project_id"], clerk_user_id=row["member"]
        ):
            results.append({**line, "outcome": "left as it is (its member left the project)"})
            continue
        async with db.pool.acquire() as conn:
            timezone = await requests.founder_timezone(conn, row["project_id"])
        schedule = {
            "cadence": "daily",
            "local_time": engagers.daily_time(row["project_id"], row["member"]),
            "timezone": timezone,
        }
        line["schedule"] = f"daily {schedule['local_time']} {timezone}"
        if not apply:
            results.append({**line, "outcome": "would schedule"})
            continue
        configured = await db.schedule_saved_copy(
            project_workflow_id=row["id"],
            definition_commit_sha=workflow.current_commit_sha,
            input_schema=workflow.definition["input_schema"],
            schedule=schedule,
            expected_settings_revision=row["settings_revision"],
        )
        if configured is None:
            results.append({**line, "outcome": "left as it is (it changed while scheduling)"})
            continue
        try:
            await sync_project_workflow(runtime=runtime, settings=settings, configured=configured)
        except Exception as exc:
            # The copy is `failed` and dispatches nothing; saving it again from Tin resyncs it.
            await sync_failed(runtime=runtime, settings=settings, configured=configured, error=exc)
            results.append({**line, "outcome": f"schedule sync failed ({type(exc).__name__})"})
            continue
        results.append({**line, "outcome": "scheduled"})
    return results
