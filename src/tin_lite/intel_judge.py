"""The judge child both intelligence workflows run: start it once, then read what it left.

`intel.world` starts `intel.world_judge` and `intel.daily` starts `intel.client_judge`, each an
Antigravity procedure on the parent's own registry revision, billed as the parent's child
(the content.plan precedent: content_plan_activities.start_research). The parent's history
holds only the child's identifiers; the judge's stories and brief are in Postgres and its
Markdown is in project Files.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

from tin_lite import intel_store

WORLD_JUDGE_KEY = "intel.world_judge"
CLIENT_JUDGE_KEY = "intel.client_judge"
PARENTS = {WORLD_JUDGE_KEY: "intel.world", CLIENT_JUDGE_KEY: "intel.daily"}
SCOPES = {WORLD_JUDGE_KEY: "world", CLIENT_JUDGE_KEY: "client"}

# Both judges' run input. Scope, day and project are also re-derived from the run on every
# write (intel_run_tools), never trusted from a request.
JUDGE_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "parent_run_id": {"type": "string", "format": "uuid"},
        "scope": {"type": "string", "enum": ["world", "client"]},
        "day": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        "client_name": {"type": "string", "minLength": 1, "maxLength": 100},
    },
    "required": ["project_id", "parent_run_id", "scope", "day"],
}


def start_key(parent_run_id) -> str:
    """The child's start idempotency key, which billing also checks (billing.valid_child)."""
    return f"intel:{parent_run_id}:judge"


def _saved_key(parent_run_id) -> str:
    return f"intel:{parent_run_id}:judge_child"


async def saved_child(db, parent_run_id) -> dict | None:
    receipt = await db.get_effect(_saved_key(parent_run_id))
    return receipt.result if receipt and receipt.status == "completed" else None


async def start_child(acts, run, *, key: str, scope: str, client_name: str | None = None) -> dict:
    """Prepare the judge's run once and return its identifiers for the parent's history."""
    saved = await saved_child(acts.db, run.id)
    if saved:
        return saved
    if SCOPES.get(key) != scope:
        raise ValueError(f"{key} is not the {scope} judge")
    fetch = await intel_store.get_fetch(acts.db.pool, run.id)
    if fetch is None:
        raise ValueError("The day's fetch was never prepared; start the run again.")
    from tin_lite.run_service import (
        PrerequisiteError,
        WorkflowExecutorUnavailableError,
        WorkflowInputError,
        start_workflow_run,
    )

    definition = json.loads(
        await acts.storage.read_canonical_artifact(
            repo_id="registry/workflows",
            commit_sha=run.definition_commit_sha,
            path=f"workflows/{key}.json",
        )
    )
    template = await acts.db.get_registry_workflow(key)
    if template is None or template.executor != definition.get("executor"):
        raise ValueError("The intelligence judge is not available in this release.")
    template = replace(template, definition=definition)
    payload = {
        "parent_run_id": str(run.id),
        "scope": scope,
        "day": fetch["day"].isoformat(),
    }
    if client_name:
        payload["client_name"] = client_name[:100]
    try:
        child = await start_workflow_run(
            runtime=SimpleNamespace(
                database=acts.db, storage=acts.storage, integrations=acts.integrations
            ),
            settings=acts.settings,
            workflow=template,
            project_id=run.project_id,
            started_by_clerk_user_id=run.started_by_clerk_user_id,
            start_idempotency_key=start_key(run.id),
            input_payload=payload,
            definition_commit_sha=run.definition_commit_sha,
            input_schema=definition["input_schema"],
            trigger_source=run.trigger_source,
            trigger_client=run.trigger_client,
            started_by_oauth_client_id=run.started_by_oauth_client_id,
            _prepare_only=True,
            _billing_parent_run_id=run.id if getattr(acts.db, "billing", None) else None,
        )
    except (
        PrerequisiteError,
        WorkflowExecutorUnavailableError,
        WorkflowInputError,
        LookupError,
    ) as exc:
        raise ValueError(f"The intelligence judge could not start: {exc}"[:500]) from None
    value = {
        "run_id": str(child.id),
        "executor": child.executor,
        "temporal_workflow_id": child.temporal_workflow_id,
    }
    key_name = _saved_key(run.id)
    async with acts.db.effect_lock(key_name, PARENTS[key]) as (conn, receipt):
        if receipt and receipt.status == "completed":
            return receipt.result
        await acts.db.start_effect(conn, execution_key=key_name, operation=PARENTS[key])
        await acts.db.complete_effect(conn, execution_key=key_name, result=value)
    return value


async def collect(acts, run) -> dict:
    """What the judge left: its run's status and reason, the stories it wrote and its brief."""
    saved = await saved_child(acts.db, run.id)
    if not saved:
        return {"child_status": "missing", "stories": 0, "brief": False, "reason": ""}
    child = await acts.db.get_run(UUID(saved["run_id"]))
    status = child.status.value if child else "missing"
    stories = await acts.db.pool.fetchval(
        "SELECT count(*) FROM intel_story_days WHERE run_id = $1", UUID(saved["run_id"])
    )
    brief = await acts.db.pool.fetchval(
        "SELECT source FROM intel_briefs WHERE run_id = $1", UUID(saved["run_id"])
    )
    return {
        "child_status": status,
        "child_run_id": saved["run_id"],
        "stories": int(stories or 0),
        "brief": brief == "judge",
        "reason": ((child.error_message if child else "") or "")[:500],
    }


async def check_parent(db, run, scope: str) -> dict:
    """A judge runs only as its parent's child: the parent is an active intel run in the same
    project whose fetch is loaded, and the run's input names it. Returns the parent's fetch."""
    parent_id = (run.input or {}).get("parent_run_id")
    try:
        parent = await db.get_run(UUID(str(parent_id)))
    except (TypeError, ValueError):
        parent = None
    expected = PARENTS[WORLD_JUDGE_KEY if scope == "world" else CLIENT_JUDGE_KEY]
    if (
        parent is None
        or parent.executor != expected
        or parent.project_id != run.project_id
        or parent.status.value not in {"pending", "running"}
        or (run.input or {}).get("scope") != scope
    ):
        from tin_lite.run_service import WorkflowInputError

        raise WorkflowInputError(
            f"The intelligence judge runs only inside {expected}; start {expected} instead."
        )
    fetch = await intel_store.get_fetch(db.pool, parent.id)
    if fetch is None or fetch["day"].isoformat() != (run.input or {}).get("day"):
        from tin_lite.run_service import WorkflowInputError

        raise WorkflowInputError("The intelligence judge's day does not match its parent's.")
    return fetch
