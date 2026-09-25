"""Delete a project: tombstone it, stop and close its work, then purge what it owned.

The project row and every billing row stay (the ledger is append-only); `deleted_at` hides
the project from every access gate. Three phases: one transaction that fences the project
(tombstone, stopped runs, archived schedules, no members), external cleanup (Temporal,
sandboxes, provider revocation, billing release, the state repository), then one transaction
that purges the rest. A crash after phase 1 leaves a hidden, stopped project and a started
receipt; retrying with the same request_id repeats the later phases, all of which tolerate
already-done work.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from temporalio.service import RPCError, RPCStatusCode

from tin_lite import analytics
from tin_lite.domain import Project, StoppedRunHandle
from tin_lite.projects import is_personal_project, personal_project_id
from tin_lite.schedules import TemporalScheduleService

logger = logging.getLogger(__name__)

OPERATION = "project_delete"
EXTERNAL_TIMEOUT_SECONDS = 15


class ProjectDeletionPending(RuntimeError):
    """The project is hidden and stopped, but external cleanup did not finish.

    Retry with the same request_id; every remaining step tolerates already-done work.
    """


def receipt_key(project_id: UUID, request_id: UUID) -> str:
    return f"project-delete:{project_id}:{request_id}"


def _authorize(project: Project, actor: str, *, is_member: bool) -> None:
    """LookupError for anyone but the creator; ValueError for the personal project.

    Access is checked first so a stranger never learns that a personal project exists.
    A deleted project has no members, so a repeat delete authorizes through the creator or
    the identity that deleted it.
    """
    creator = project.created_by_clerk_user_id
    if creator is not None:
        allowed = creator == actor
    else:
        allowed = is_member or project.deleted_by_clerk_user_id == actor
    if not allowed:
        raise LookupError("project not found")
    if is_personal_project(project.name) or project.id == personal_project_id(actor):
        raise ValueError("the personal project cannot be deleted")


async def deletable_project(runtime: Any, *, project_id: UUID, actor: str) -> Project:
    """The project the actor may delete, tombstoned or not; LookupError otherwise."""
    db = runtime.database
    project = await db.get_project(project_id)
    if project is None:
        raise LookupError("project not found")
    async with db.pool.acquire() as conn:
        is_member = await db.has_project_membership(
            conn, project_id=project_id, clerk_user_id=actor
        )
    _authorize(project, actor, is_member=is_member)
    return project


async def delete_project(
    runtime: Any, *, project_id: UUID, actor: str, request_id: UUID
) -> dict[str, Any]:
    db = runtime.database
    key = receipt_key(project_id, request_id)
    async with db.effect_lock(key, OPERATION) as (conn, receipt):
        if receipt is not None and receipt.status == "completed":
            return dict(receipt.result or {})
        async with db.project_state_lock(conn, project_id):
            # Phase 1: fence the project in one transaction.
            async with conn.transaction():
                row = await db.get_project_for_deletion(conn, project_id)
                if row is None:
                    raise LookupError("project not found")
                project = await db.get_project(project_id, conn=conn)
                assert project is not None
                is_member = await db.has_project_membership(
                    conn, project_id=project_id, clerk_user_id=actor
                )
                _authorize(project, actor, is_member=is_member)
                await db.start_effect(conn, execution_key=key, operation=OPERATION)
                deleted_at = await db.tombstone_project(conn, project_id=project_id, actor=actor)
                stopped = await db.stop_runs_for_project(conn, project_id=project_id)
                # A retry finds its runs already stopped; close them again with any new ones.
                earlier = await db.runs_stopped_by_deletion(conn, project_id=project_id)
                closing = stopped + [handle for handle in earlier if handle not in stopped]
                scheduled = await db.archive_project_workflows_for_deletion(
                    conn, project_id=project_id
                )
                await db.revoke_project_access(conn, project_id=project_id)
                roots = await db.billing_root_runs_for_project(conn, project_id=project_id)
            # Committed: no gate opens the project and nothing new can be admitted.

            # Phase 2: external cleanup, outside any transaction.
            providers = [
                connection.provider_key
                for connection in await db.list_integration_connections(project_id)
            ]
            failures = await _close_temporal_and_sandboxes(runtime, closing, scheduled)
            if failures:
                message = "; ".join(failures)[:2000]
                async with conn.transaction():
                    await db.fail_effect(conn, execution_key=key, error_message=message)
                raise ProjectDeletionPending(
                    f"project cleanup is pending; retry with the same request_id ({message})"
                )
            disconnected = 0
            for provider_key in providers:
                if await runtime.integrations.disconnect(
                    project_id=project_id, provider_key=provider_key
                ):
                    disconnected += 1
            billing = getattr(db, "billing", None)
            if billing is not None:
                for root in roots:
                    try:
                        await billing.settle(root)
                    except Exception:  # noqa: BLE001 — the reconciliation loop retries it
                        logger.exception("billing settle failed for %s", root)
            try:
                repo_deleted = bool(await runtime.storage.delete_repo(row["state_repo_id"]))
            except Exception as exc:  # noqa: BLE001
                async with conn.transaction():
                    await db.fail_effect(conn, execution_key=key, error_message=str(exc)[:2000])
                raise ProjectDeletionPending(
                    "project storage cleanup is pending; retry with the same request_id"
                ) from exc

            # Phase 3: purge and complete the receipt in one transaction.
            result = {
                "project_id": str(project_id),
                "name": row["name"],
                "deleted_at": deleted_at.isoformat(),
                "stopped_runs": len(stopped),
                "removed_schedules": len(scheduled),
                "disconnected": disconnected,
                "repo_deleted": repo_deleted,
            }
            async with conn.transaction():
                await db.purge_project_data(conn, project_id=project_id)
                await db.complete_effect(conn, execution_key=key, result=result)
    analytics.capture(
        "project_deleted",
        distinct_id=project_id,
        project_id=project_id,
        properties={
            "clerk_user_id": actor,
            "stopped_runs": len(stopped),
            "removed_schedules": len(scheduled),
        },
    )
    return result


async def _close_temporal_and_sandboxes(
    runtime: Any, stopped: list[StoppedRunHandle], scheduled: list[UUID]
) -> list[str]:
    """Terminate every execution and delete every schedule; return the failures by name."""
    schedules = TemporalScheduleService(
        client=runtime.temporal, settings=getattr(runtime, "settings", None)
    )
    labels: list[str] = []
    calls = []
    for handle in stopped:
        labels.append(f"terminate {handle.temporal_workflow_id}")
        calls.append(_terminate(runtime, handle.temporal_workflow_id))
        if handle.sandbox_id is not None:
            labels.append(f"kill sandbox {handle.sandbox_id}")
            calls.append(_bounded(runtime.sandboxes.kill(handle.sandbox_id)))
    for project_workflow_id in scheduled:
        labels.append(f"terminate tin-scheduled-dispatch:{project_workflow_id}")
        calls.append(_terminate(runtime, f"tin-scheduled-dispatch:{project_workflow_id}"))
        labels.append(f"delete schedule {project_workflow_id}")
        calls.append(_bounded(schedules.delete(str(project_workflow_id))))
    outcomes = await asyncio.gather(*calls, return_exceptions=True)
    failures = []
    for label, outcome in zip(labels, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning("project deletion: %s failed: %r", label, outcome)
            failures.append(label)
    return failures


async def _terminate(runtime: Any, workflow_id: str) -> None:
    # Terminate, not cancel: cancellation runs each executor's failure activity, which would
    # write a failed status and new activity into a project being purged. Termination closes
    # the execution server-side; a never-started or already-closed execution is NOT_FOUND.
    try:
        async with asyncio.timeout(EXTERNAL_TIMEOUT_SECONDS):
            await runtime.temporal.get_workflow_handle(workflow_id).terminate("project deleted")
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise


async def _bounded(call: Any) -> None:
    async with asyncio.timeout(EXTERNAL_TIMEOUT_SECONDS):
        await call
