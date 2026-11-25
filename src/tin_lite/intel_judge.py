"""The judge child both intelligence workflows run: start it once, then read what it left.

`intel.world` starts `intel.world_judge` and `intel.daily` starts `intel.client_judge`, each an
Antigravity procedure on the parent's own registry revision, billed as the parent's child
(the content.plan precedent: content_plan_activities.start_research). The parent's history
holds only the child's identifiers; the judge's stories and brief are in Postgres and its
Markdown is in project Files.
"""

from __future__ import annotations

import json
import re
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


# A parent runs its judge at most twice: once, and once more when the first attempt died on a
# temporary provider failure (retry_child).
ATTEMPTS = (1, 2)
RETRY_NOTE = "The analyst's first attempt failed on a temporary provider error; retrying once"
# The relay's own codes for a provider that failed or never answered, not a refusal of the
# run's request (codex_api.record_relay_rejection). Budget, limit, bound and contract codes
# are never here: another child would be refused the same way, or should not be bought.
TRANSIENT_RELAY_REASONS = frozenset(
    {"stream_unconfirmed", "upstream_unreachable", "upstream_failed", "relay_timeout"}
)
# Retryable only for these upstream answers: rate limited or a server error.
UPSTREAM_REASONS = frozenset({"upstream_rejected", "rate_limited"})
NETWORK_FAILURES = frozenset(
    {
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
        "RemoteProtocolError",
        "ReadError",
        "ConnectError",
        "ReadTimeout",
        "ConnectTimeout",
    }
)
# A sandbox provider's server error or rate limit, as a failed child records it.
SANDBOX_TRANSIENT = re.compile(r"^SandboxException: (?:5\d\d|429)\b")
_IDS = ("run_id", "executor", "temporal_workflow_id", "attempt")


def start_key(parent_run_id, attempt: int = 1) -> str:
    """The child's start idempotency key, which billing also checks (billing.valid_child)."""
    base = f"intel:{parent_run_id}:judge"
    return base if attempt == 1 else f"{base}:{attempt}"


def start_keys(parent_run_id) -> frozenset[str]:
    return frozenset(start_key(parent_run_id, attempt) for attempt in ATTEMPTS)


def _saved_key(parent_run_id, attempt: int = 1) -> str:
    base = f"intel:{parent_run_id}:judge_child"
    return base if attempt == 1 else f"{base}:{attempt}"


async def saved_child(db, parent_run_id, attempt: int | None = None) -> dict | None:
    """One attempt's saved child, or with no attempt named the latest one started."""
    for number in reversed(ATTEMPTS) if attempt is None else (attempt,):
        receipt = await db.get_effect(_saved_key(parent_run_id, number))
        if receipt and receipt.status == "completed":
            return receipt.result
    return None


def _ids(saved: dict) -> dict:
    """What the parent's history holds: the child's identifiers and its attempt number."""
    return {key: saved[key] for key in _IDS if key in saved}


async def start_child(
    acts,
    run,
    *,
    key: str,
    scope: str,
    client_name: str | None = None,
    attempt: int = 1,
    record: dict | None = None,
) -> dict:
    """Prepare the judge's run once per attempt and return its identifiers for the parent's
    history. `record` is kept on the attempt's receipt only (why a retry was started)."""
    if attempt not in ATTEMPTS:
        raise ValueError(f"a judge has at most {len(ATTEMPTS)} attempts")
    saved = await saved_child(acts.db, run.id, attempt)
    if saved:
        return _ids(saved)
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
            start_idempotency_key=start_key(run.id, attempt),
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
    if attempt > 1:
        value = {**value, "attempt": attempt, **(record or {})}
    key_name = _saved_key(run.id, attempt)
    async with acts.db.effect_lock(key_name, PARENTS[key]) as (conn, receipt):
        if receipt and receipt.status == "completed":
            return _ids(receipt.result)
        await acts.db.start_effect(conn, execution_key=key_name, operation=PARENTS[key])
        await acts.db.complete_effect(conn, execution_key=key_name, result=value)
    return _ids(value)


async def transient_cause(db, child) -> str | None:
    """Why a failed judge child failed, when the cause was a temporary provider failure and
    one more child may help: a relay code for a provider that failed or never answered (its
    attempt receipt), a network failure, a sandbox server error, or failures.py's transient
    kind. None for everything else: founder-fixable, budget and limits, bounds and refusals,
    Tin's own errors, a stopped or still running child."""
    if child is None or child.status.value != "failed":
        return None
    if child.error_kind == "needs_you":
        return None
    if child.error_kind == "transient":
        return "transient"
    from tin_lite.codex_api import ATTEMPT

    rows = await db.pool.fetch(
        """SELECT result FROM effect_receipts WHERE operation = $1 AND result->>'run_id' = $2
           ORDER BY created_at""",
        ATTEMPT,
        str(child.id),
    )
    for row in rows:
        result = row["result"]
        result = json.loads(result) if isinstance(result, str) else (result or {})
        rejection = result.get("relay_rejection")
        if isinstance(rejection, dict):
            reason = rejection.get("reason")
            upstream = rejection.get("upstream_status")
            if reason in TRANSIENT_RELAY_REASONS:
                return reason
            if reason in UPSTREAM_REASONS and (
                upstream == 429 or (type(upstream) is int and upstream >= 500)
            ):
                return reason
            # The first rejection is the cause; later ones are its consequences.
            return None
        if result.get("failure_type") in NETWORK_FAILURES:
            return "network"
    detail = await db.pool.fetchval(
        "SELECT error_detail FROM workflow_runs WHERE id = $1", child.id
    )
    if SANDBOX_TRANSIENT.match(detail or ""):
        return "sandbox"
    return None


async def retry_child(
    acts, run, *, key: str, scope: str, client_name: str | None = None, progress=None
) -> dict:
    """At most one more judge for this parent, when its first child failed on a temporary
    provider failure. The first attempt keeps its own receipts and charge (an uncertain one
    stays uncertain); its stories and brief stay too, and the new child reads them as today's
    stories. Returns the new child's identifiers, or {} when no retry is due."""
    saved = await saved_child(acts.db, run.id, 2)
    if saved:
        return _ids(saved)
    first = await saved_child(acts.db, run.id, 1)
    if not first:
        return {}
    child = await acts.db.get_run(UUID(first["run_id"]))
    cause = await transient_cause(acts.db, child)
    if cause is None:
        return {}
    if progress is not None:
        await progress(RETRY_NOTE)
    return await start_child(
        acts,
        run,
        key=key,
        scope=scope,
        client_name=client_name,
        attempt=2,
        record={"retry_of": first["run_id"], "cause": cause},
    )


async def collect(acts, run) -> dict:
    """What the judge left: the latest attempt's status and reason, and the stories and judge
    brief every attempt wrote for this parent (a retry keeps what the first attempt wrote)."""
    attempts = [
        found for number in ATTEMPTS if (found := await saved_child(acts.db, run.id, number))
    ]
    if not attempts:
        return {"child_status": "missing", "stories": 0, "brief": False, "reason": ""}
    saved = attempts[-1]
    child = await acts.db.get_run(UUID(saved["run_id"]))
    status = child.status.value if child else "missing"
    stories = await acts.db.pool.fetchval(
        "SELECT count(*) FROM intel_story_days WHERE run_id = ANY($1::uuid[])",
        [UUID(found["run_id"]) for found in attempts],
    )
    brief = await acts.db.pool.fetchval(
        "SELECT 1 FROM intel_briefs WHERE parent_run_id = $1 AND source = 'judge' LIMIT 1",
        run.id,
    )
    return {
        "child_status": status,
        "child_run_id": saved["run_id"],
        "attempts": len(attempts),
        "stories": int(stories or 0),
        "brief": bool(brief),
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
