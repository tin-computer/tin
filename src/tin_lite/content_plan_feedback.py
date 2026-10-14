"""Request changes on a content plan that waits for approval (content.plan 1.1.0).

The founder's feedback becomes an ordinary plan revision (ContentPrograms.begin_revision and a
revision run) over every week that has not started; nothing has started before the first plan
is approved. When that run finishes, Tin applies the revision to the working plan and points
the waiting plan's review at the revised PLAN.md, so the same Decisions card shows what the
founder now approves; approval binds that copy. It works like a revised brand or style proposal
(capture_revisions), and the plan's own revision contract stays as it was: base check, held
batches, exact apply. Approving waits while a revision runs; a failed or conflicting revision
names why and leaves the plan the founder last read.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

from tin_lite.content_plan import KEY, paths
from tin_lite.content_programs import ContentPrograms, decoded
from tin_lite.organic_audit import digest
from tin_lite.workflow_review_store import ReviewConflict

OPERATION = "content_plan_feedback_v1"
FEEDBACK_HINT = (
    "Tin re-plans every week from your feedback, then shows the revised plan here for you to "
    "approve. It runs the planning agent again: up to $6 and about 10 to 20 minutes."
)
ACTIVE = ("pending", "running")


def feedback_key(request_id):
    return f"content-plan-feedback:{request_id}"


async def waiting_plan(database, run):
    """Whether `run` is a program's first plan, waiting in Decisions for approval."""
    if (
        run is None
        or run.executor != KEY
        or run.status.value != "needs_input"
        or not run.review_required
        or run.review_decision is not None
        or run.project_workflow_id is None
    ):
        return False
    initial = await database.pool.fetchval(
        "SELECT initial_run_id FROM content_programs WHERE project_workflow_id=$1",
        run.project_workflow_id,
    )
    return initial == run.id


async def requests(database, run_id):
    """The feedback requests on one waiting plan, oldest first."""
    rows = await database.pool.fetch(
        "SELECT execution_key, status, result FROM effect_receipts WHERE operation=$1 "
        "AND result->>'plan_run_id'=$2 ORDER BY created_at, execution_key",
        OPERATION,
        str(run_id),
    )
    return [{"status": row["status"], **(decoded(row["result"]) or {})} for row in rows]


async def revising(database, run):
    """The revision run working on this plan's feedback now, or None."""
    for entry in reversed(await requests(database, run.id)):
        if entry.get("outcome") or not entry.get("revision_run_id"):
            continue
        revision_run = await database.get_run(UUID(entry["revision_run_id"]))
        if revision_run and revision_run.status.value in ACTIVE:
            return revision_run
    return None


async def approval_conflict(database, run):
    """Why the founder can't approve this plan right now, or None."""
    if not await waiting_plan(database, run):
        return None
    if await revising(database, run):
        return (
            "Tin is revising this plan from your feedback. Approve the revised plan when it is "
            "ready; approving now would use the older copy."
        )
    return None


def review_token(run):
    return digest(
        {"run": str(run.id), "revision": run.canonical_commit_sha, "path": run.artifact_path}
    )


class PlanFeedback:
    def __init__(self, runtime, settings):
        self.runtime, self.settings = runtime, settings
        self.db, self.storage = runtime.database, runtime.storage

    async def view(self, run_id, actor):
        run = await self.db.get_run(run_id)
        if run is None or not await self.db.has_project_access(
            project_id=run.project_id, clerk_user_id=actor
        ):
            raise LookupError("Review not found.")
        waiting = await waiting_plan(self.db, run)
        history = await requests(self.db, run.id)
        active = await revising(self.db, run) if waiting else None
        applied = [entry for entry in history if entry.get("outcome") == "applied"]
        last = history[-1] if history else {}
        conflict = None
        if not active and last.get("outcome") in {"conflict", "failed"}:
            conflict = last["reason"]
        elif not active and last.get("revision_run_id") and not last.get("outcome"):
            revision_run = await self.db.get_run(UUID(last["revision_run_id"]))
            if revision_run and revision_run.status.value in {"failed", "stopped"}:
                conflict = (
                    "The last revision did not finish, so this is still the plan you read "
                    "before. Request changes again, or approve this one."
                )
        project = await self.db.get_project(run.project_id)
        content = None
        if run.artifact_path and run.canonical_commit_sha:
            content = await self.storage.read_canonical_artifact_if_exists(
                repo_id=project.state_repo_id,
                commit_sha=run.canonical_commit_sha,
                path=run.artifact_path,
            )
        artifact = (
            {
                "run_id": str(run.id),
                "path": run.artifact_path,
                "revision": run.canonical_commit_sha,
                "sha256": hashlib.sha256(content).hexdigest(),
                "assessment": False,
            }
            if content is not None
            else None
        )
        ready = waiting and artifact is not None and active is None
        return {
            "run_id": str(run.id),
            "project_id": str(run.project_id),
            "root_run_id": str(run.id),
            "current_run_id": str(run.id),
            # The plan is revised in place, so its card never moves to another run.
            "version": 1,
            "status": run.status.value,
            "artifact": artifact,
            "review_token": review_token(run) if artifact else None,
            "can_request_changes": ready,
            "can_approve": ready,
            "is_current": True,
            "versions": [],
            "feedback": last.get("feedback"),
            "feedback_hint": FEEDBACK_HINT,
            "submit_label": "Revise plan",
            "pending_run_id": str(active.id) if active else None,
            "conflict": conflict,
            "proposal_revisions": {
                "count": len(applied),
                "latest_by": "Tin, from your feedback",
                "latest_by_you": False,
                "latest_at": applied[-1].get("applied_at"),
            }
            if applied
            else None,
            "review_url": f"/document/{run.id}?project={run.project_id}&return=decisions",
        }

    async def request_changes(self, *, run_id, actor, feedback, request_id, token, **_):
        from tin_lite.content_program_api import request_revision_run

        run = await self.db.get_run(run_id)
        if run is None or not await self.db.has_project_access(
            project_id=run.project_id, clerk_user_id=actor
        ):
            raise LookupError("Review not found.")
        if not isinstance(feedback, str) or not 1 <= len(feedback.strip()) <= 4000:
            raise ValueError("Describe the changes in 1–4,000 characters.")
        key = feedback_key(request_id)
        async with self.db.effect_lock(key, OPERATION) as (conn, receipt):
            if receipt is not None:
                if (receipt.result or {}).get("plan_run_id") != str(run.id) or (
                    receipt.result or {}
                ).get("feedback") != feedback:
                    raise ReviewConflict("This request ID already belongs to different feedback.")
                if receipt.status == "completed":
                    return await self.db.get_run(run.id)
            else:
                if not await waiting_plan(self.db, run):
                    raise ReviewConflict("This plan is no longer waiting for your review.")
                if token != review_token(run):
                    raise ReviewConflict("This plan changed. Open the current review first.")
                if await revising(self.db, run):
                    raise ReviewConflict("Tin is already revising this plan from your feedback.")
                await self.db.start_effect(conn, execution_key=key, operation=OPERATION)
                await self.db.save_effect_progress(
                    conn,
                    execution_key=key,
                    result={"plan_run_id": str(run.id), "feedback": feedback, "actor": actor},
                )
        programs = ContentPrograms(database=self.db, storage=self.storage)
        program_id = run.project_workflow_id
        await self.release_stale(programs, run, actor)
        current = await programs.read(project_id=run.project_id, program_id=program_id)
        facts = await programs.facts(program_id)
        started = {row["batch_id"] for row in facts["batches"]}
        started |= set((facts["pending_revision"] or {}).get("batch_ids", []))
        weeks = [b["id"] for b in current["plan"]["batches"] if b["id"] not in started]
        if not weeks:
            raise ReviewConflict("Every week of this plan has started; nothing is left to revise.")
        started_run = await request_revision_run(
            runtime=self.runtime,
            settings=self.settings,
            project_id=run.project_id,
            program_id=program_id,
            request=SimpleNamespace(
                request_id=request_id,
                expected_revision=current["revision"],
                batch_ids=weeks,
                instruction=feedback,
                context_paths=[],
            ),
            actor=actor,
        )
        async with self.db.effect_lock(key, OPERATION) as (conn, receipt):
            if receipt.status != "completed":
                await self.db.complete_effect(
                    conn,
                    execution_key=key,
                    result={
                        "plan_run_id": str(run.id),
                        "feedback": feedback,
                        "actor": actor,
                        "revision_id": str(request_id),
                        "revision_run_id": started_run.get("run_id"),
                    },
                )
        return await self.db.get_run(run.id)

    async def release_stale(self, programs, run, actor):
        """Discard a pending revision whose run ended without a preview to apply."""
        pending = (await programs.facts(run.project_workflow_id))["pending_revision"]
        if not pending or pending["status"] != "pending":
            return
        # The revision row learns its run when that run starts executing; find it by input.
        started = await self.db.pool.fetch(
            "SELECT status FROM workflow_runs WHERE project_workflow_id=$1 "
            "AND input->>'amendment_id'=$2",
            run.project_workflow_id,
            str(pending["id"]),
        )
        if any(row["status"] not in {"failed", "stopped"} for row in started):
            raise ReviewConflict(
                "A revision of this plan is already pending. Apply or discard it first."
            )
        await programs.resolve(
            project_id=run.project_id,
            program_id=run.project_workflow_id,
            revision_id=pending["id"],
            action="discard",
            actor=actor,
        )


async def finish(database, storage, revision_run_id):
    """After a revision run: apply a feedback revision and re-point the waiting plan's review.

    Idempotent; a retried activity reapplies nothing. When the founder edited the plan while
    Tin revised it, or decided on the plan meanwhile, the revision is discarded and the review
    names why; the plan they last read stays as it was.
    """
    revision_run = await database.get_run(UUID(str(revision_run_id)))
    amendment = (revision_run.input or {}).get("amendment_id") if revision_run else None
    if not amendment or revision_run.status.value != "succeeded":
        return None
    key = feedback_key(amendment)
    receipt = await database.get_effect(key)
    if receipt is None or receipt.status != "completed" or not receipt.result:
        return None
    entry = receipt.result
    if entry.get("outcome"):
        return entry
    plan_run = await database.get_run(UUID(entry["plan_run_id"]))
    programs = ContentPrograms(database=database, storage=storage)
    program_id = plan_run.project_workflow_id
    outcome = {}
    if not await waiting_plan(database, plan_run):
        # The founder approved or declined the plan meanwhile: the revision stays a preview
        # (apply or discard it on the program) and the decided plan is left alone.
        outcome = {"outcome": "decided", "reason": "The plan was decided first."}
    else:
        current = await programs.read(project_id=plan_run.project_id, program_id=program_id)
        try:
            applied = await programs.resolve(
                project_id=plan_run.project_id,
                program_id=program_id,
                revision_id=UUID(amendment),
                action="apply",
                actor=entry["actor"],
                expected_revision=current["revision"],
            )
        except ValueError:
            await programs.resolve(
                project_id=plan_run.project_id,
                program_id=program_id,
                revision_id=UUID(amendment),
                action="discard",
                actor=entry["actor"],
            )
            outcome = {
                "outcome": "conflict",
                "reason": "The plan was edited while Tin revised it, so Tin kept your edits and "
                "left this copy as it was. Request changes again to revise the edited plan.",
            }
        else:
            project = await database.get_project(plan_run.project_id)
            path = paths(str(revision_run.id))["PLAN.md"]
            explanation = None
            raw = await storage.read_canonical_artifact_if_exists(
                repo_id=project.state_repo_id,
                commit_sha=applied["revision"],
                path=paths(str(revision_run.id))["evidence.json"],
            )
            mapping = ((json.loads(raw) if raw else {}).get("editorial") or {}).get("targets")
            if mapping is not None:
                from tin_lite.content_plan_targets import review_line

                revised = await programs.read(
                    project_id=plan_run.project_id,
                    program_id=program_id,
                    revision=applied["revision"],
                )
                planned = sum(len(batch["items"]) for batch in revised["plan"]["batches"])
                explanation = "Revised from your feedback. " + review_line(mapping, planned)
            await database.repoint_review_artifact(
                run_id=plan_run.id,
                canonical_commit_sha=applied["revision"],
                artifact_path=path,
                artifact_ref=f"code.storage://{project.state_repo_id}@{applied['revision']}/{path}",
                explanation=explanation,
            )
            outcome = {
                "outcome": "applied",
                "applied_revision": applied["revision"],
                "artifact_path": path,
                "applied_at": revision_run.finished_at.isoformat()
                if revision_run.finished_at
                else None,
            }
    async with database.effect_lock(key, OPERATION) as (conn, _):
        await conn.execute(
            "UPDATE effect_receipts SET result = result || $2::jsonb WHERE execution_key=$1",
            key,
            json.dumps(outcome),
        )
    return {**entry, **outcome}
