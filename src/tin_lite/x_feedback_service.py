"""The same file-based X feedback boundary for the dashboard and MCP."""

import json
from uuid import UUID

from tin_lite import x_feedback, x_style
from tin_lite.workflow_review_store import ReviewConflict
from tin_lite.x_posts import MAX_DRAFT_BYTES, digest, validate_draft


async def supports(database, run, conn=None):
    definition = await database.get_workflow(run.workflow_id, conn=conn) if run else None
    return bool(
        definition and definition.project_id is None and definition.key in x_feedback.SOURCE_KEYS
    )


class XFeedback:
    def __init__(self, runtime, settings):
        self.runtime, self.settings = runtime, settings
        self.db, self.storage = runtime.database, runtime.storage

    async def source(self, run_id, actor, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or not await self.db.has_project_access(
            project_id=run.project_id, clerk_user_id=actor, conn=conn
        ):
            raise LookupError("Review not found.")
        if run.executor == x_feedback.KEY:
            return await self.source(run.input["source_run_id"], actor, conn)
        if not await supports(self.db, run, conn):
            raise ValueError("This run does not contain an X draft or writing guide.")
        if run.executor == "social.x_draft":
            receipt = await self.db.get_effect(f"x-draft:{run.id}:compose", conn=conn)
            if not receipt or not receipt.result.get("run_id"):
                raise ReviewConflict("The X drafting workflow has no saved post yet.")
            child = await self.source(receipt.result["run_id"], actor, conn)
            if child.project_id != run.project_id:
                raise ReviewConflict("The X draft does not belong to this project.")
            return child
        if not run.artifact_path or not run.canonical_commit_sha:
            raise ReviewConflict("There is no saved X draft to revise yet.")
        return run

    async def snapshot(self, run, post_id="", conn=None):
        project = await self.db.get_project(run.project_id, conn=conn)
        head = await self.storage.head_sha(
            await self.storage.get_repo(project.state_repo_id), project.canonical_branch
        )
        kind = "guide" if run.executor == x_style.KEY else "post"
        raw = await self.storage.read_bounded_project_file(
            repo_id=project.state_repo_id,
            commit_sha=head,
            path=run.artifact_path,
            max_bytes=x_style.MAX_GUIDE_BYTES if kind == "guide" else MAX_DRAFT_BYTES,
        )
        if raw is None:
            raise ReviewConflict("The saved X file is missing. Open Files before trying again.")
        if kind == "guide":
            if post_id:
                raise ValueError("Choose the writing guide without a post ID.")
            guide = raw.decode()
            account = x_style.guide_account(guide)
            source = await self.db.get_effect(f"{run.id}:x_style_model", conn=conn)
            if not source or source.result.get("account_id") != account:
                raise ReviewConflict("The writing guide changed account identity.")
            post = None
        else:
            draft = validate_draft(raw)
            if not post_id and len(draft["posts"]) != 1:
                raise ValueError("Choose which post to revise using its post_id.")
            post_id = post_id or draft["posts"][0]["id"]
            post = next((p for p in draft["posts"] if p["id"] == post_id), None)
            if post is None:
                raise ValueError("This draft does not contain that post.")
            account = draft["account_id"] or "unbound"
            guide_raw = await self.storage.read_bounded_project_file(
                repo_id=project.state_repo_id,
                commit_sha=head,
                path=x_style.GUIDE_PATH,
                max_bytes=x_style.MAX_GUIDE_BYTES,
            )
            guide = guide_raw.decode() if guide_raw else ""
            if guide and x_style.guide_account(guide) != account:
                raise ReviewConflict(
                    "The draft and current X writing guide belong to different accounts."
                )
        snapshot = {
            "source_run_id": str(run.id),
            "kind": kind,
            "path": run.artifact_path,
            "revision": head,
            "sha256": digest(raw),
            "guide_sha256": digest(guide.encode()),
            "post_id": post_id,
            "account_id": account,
        }
        # A token binds file contents, not unrelated project commits. Users always work
        # on the latest files; no revision selector is part of either client surface.
        token = digest({k: v for k, v in snapshot.items() if k != "revision"})
        return snapshot, token, {"guide": guide, "post": post, "raw": raw}

    async def view(self, run_id, actor, post_id="", conn=None):
        run = await self.source(run_id, actor, conn)
        snapshot, token, _ = await self.snapshot(run, post_id, conn)
        pending = await (conn or self.db.pool).fetchval(
            "SELECT id FROM workflow_runs WHERE project_id=$1 AND executor=$2 "
            "AND input->>'source_run_id'=$3 AND status IN ('pending','running','needs_input') "
            "ORDER BY created_at DESC LIMIT 1",
            run.project_id,
            x_feedback.KEY,
            str(run.id),
        )
        last = await (conn or self.db.pool).fetchrow(
            "SELECT id, result_summary FROM workflow_runs WHERE project_id=$1 AND executor=$2 "
            "AND input->>'source_run_id'=$3 AND status='succeeded' AND input->>'post_id'=$4 "
            "ORDER BY created_at DESC LIMIT 1",
            run.project_id,
            x_feedback.KEY,
            str(run.id),
            snapshot["post_id"],
        )
        guide = snapshot["kind"] == "guide"
        ready = (
            (run.status.value == "needs_input" and run.review_decision is None)
            if guide
            else run.status.value == "succeeded"
        )
        return {
            "run_id": str(run.id),
            "current_run_id": str(run.id),
            "version": 1,
            "status": run.status.value,
            "is_current": True,
            "versions": [],
            "artifact": {
                "run_id": str(run.id),
                "path": snapshot["path"],
                "revision": snapshot["revision"],
            },
            "review_token": token,
            "post_id": snapshot["post_id"],
            "x_feedback": True,
            "can_request_changes": ready and not pending,
            "can_approve": guide and ready and not pending,
            "pending_run_id": str(pending) if pending else None,
            "change_summary": last["result_summary"] if last else None,
            "feedback_hint": (
                "Tin carries clear writing preferences into your X guide. "
                "One-off changes stay with this draft."
            ),
        }

    async def request_changes(
        self,
        *,
        run_id,
        actor,
        feedback,
        request_id,
        token,
        post_id="",
        billing_quote_id=None,
        trigger_client=None,
        trigger_source="manual",
        oauth_client_id=None,
    ):
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 8000:
            raise ValueError("Describe the changes in 1–8,000 characters.")
        run = await self.source(run_id, actor)
        start_key = f"x-feedback:{UUID(str(request_id))}"
        existing = await self.db.get_run_by_start_key(
            project_id=run.project_id, start_idempotency_key=start_key
        )
        if existing:
            if (
                existing.executor != x_feedback.KEY
                or existing.started_by_clerk_user_id != actor
                or existing.input["source_run_id"] != str(run.id)
                or existing.input["feedback"] != feedback
                or existing.input["review_token"] != token
                or (post_id and existing.input["post_id"] != post_id)
            ):
                raise ReviewConflict("This request ID belongs to different feedback.")
            return existing
        view = await self.view(run.id, actor, post_id)
        if not view["can_request_changes"] or token != view["review_token"]:
            raise ReviewConflict(
                "The draft changed or is already being revised. Read it again first."
            )
        snapshot, current_token, _ = await self.snapshot(run, post_id)
        if token != current_token:
            raise ReviewConflict("The draft changed. Read it again first.")
        workflow = await self.db.get_workflow(x_feedback.WORKFLOW_ID)
        if workflow is None:
            raise ValueError("X feedback is not installed yet.")
        from tin_lite.run_service import start_workflow_run

        return await start_workflow_run(
            runtime=self.runtime,
            settings=self.settings,
            workflow=workflow,
            project_id=run.project_id,
            started_by_clerk_user_id=actor,
            start_idempotency_key=start_key,
            billing_quote_id=billing_quote_id,
            input_payload={
                "source_run_id": str(run.id),
                "post_id": snapshot["post_id"],
                "feedback": feedback,
                "review_token": token,
                "snapshot": json.dumps(snapshot),
            },
            trigger_client=trigger_client,
            trigger_source=trigger_source,
            started_by_oauth_client_id=oauth_client_id,
            _x_feedback=True,
        )

    async def approve(self, *, run_id, actor, token):
        from uuid import NAMESPACE_URL, uuid5

        from tin_lite.workflow_review_store import insert_command

        run = await self.source(run_id, actor)
        if run.executor != x_style.KEY:
            raise ReviewConflict("Preview and explicitly confirm an X post to publish it.")
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT id FROM projects WHERE id=$1 FOR UPDATE", run.project_id)
            run = await self.db.get_run(run.id, conn=conn)
            if run.review_decision == "approved":
                return run
            view = await self.view(run.id, actor, conn=conn)
            if not token or token != view["review_token"] or not view["can_approve"]:
                raise ReviewConflict(
                    "Read the current guide and finish any pending revision before approving."
                )
            snapshot, current_token, _ = await self.snapshot(run, conn=conn)
            if token != current_token:
                raise ReviewConflict("The guide changed. Read it again before approving.")
            key = f"{run.id}:x_style_approval"
            await self.db.start_effect(conn, execution_key=key, operation=x_style.KEY)
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={
                    "revision": snapshot["revision"],
                    "artifact_path": snapshot["path"],
                    "sha256": snapshot["sha256"],
                },
            )
            command_id = uuid5(NAMESPACE_URL, f"tin:x-guide-approval:{run.id}:{token}")
            await insert_command(
                conn,
                {
                    "id": command_id,
                    "project_id": run.project_id,
                    "request_id": command_id,
                    "actor_clerk_user_id": actor,
                    "source_run_id": run.id,
                    "root_run_id": run.id,
                    "artifact_run_id": run.id,
                    "coordinator_run_id": run.id,
                    "action": "approve",
                    "request_digest": token,
                    "review_token": token,
                    "artifact": {"path": snapshot["path"], "revision": snapshot["revision"]},
                },
            )
            await conn.execute(
                "UPDATE workflow_runs SET review_decision='approved', reviewed_at=now(), "
                "reviewed_by_clerk_user_id=$2, status='running' WHERE id=$1",
                run.id,
                actor,
            )
            await conn.execute(
                "UPDATE run_decisions SET status='applied', applied_at=now(), "
                "applied_by_clerk_user_id=$2, response=jsonb_build_object('action','approved') "
                "WHERE run_id=$1 AND status='pending'",
                run.id,
                actor,
            )
        return await self.db.get_run(run.id)


async def guard_admission(conn, *, project_id, inputs, actor):
    """Called under ordinary admission's project row lock, before budget insertion."""
    source = await conn.fetchrow(
        "SELECT r.*, w.key FROM workflow_runs r JOIN workflows w ON w.id=r.workflow_id "
        "WHERE r.id=$1 AND r.project_id=$2 FOR UPDATE OF r",
        UUID(inputs["source_run_id"]),
        project_id,
    )
    member = await conn.fetchval(
        "SELECT true FROM project_memberships WHERE project_id=$1 AND clerk_user_id=$2",
        project_id,
        actor,
    )
    if not source or not member or source["key"] not in x_feedback.SOURCE_KEYS - {x_feedback.KEY}:
        raise LookupError("Review not found.")
    guide = source["executor"] == x_style.KEY
    if (
        guide
        and (source["status"] != "needs_input" or source["review_decision"] is not None)
        or not guide
        and source["status"] != "succeeded"
    ):
        raise ReviewConflict("This X file is no longer available for feedback.")
    pending = await conn.fetchval(
        "SELECT id FROM workflow_runs WHERE project_id=$1 AND executor=$2 "
        "AND input->>'source_run_id'=$3 AND status IN ('pending','running','needs_input') LIMIT 1",
        project_id,
        x_feedback.KEY,
        inputs["source_run_id"],
    )
    if pending:
        raise ReviewConflict("This X file is already being revised.")
