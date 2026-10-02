"""Bounded, receipted X revisions. Draft and remembered preferences commit together."""

import asyncio
import hashlib
import json
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import x_feedback, x_style
from tin_lite.domain import result_line
from tin_lite.model_providers import MessageRole, ModelMessage, ModelRequest
from tin_lite.model_usage import model_usage_scope
from tin_lite.project_files import ProjectFileMutation
from tin_lite.x_feedback_service import XFeedback
from tin_lite.x_posts import validate_draft


class XFeedbackActivities:
    def __init__(self, *, database, storage, router):
        from types import SimpleNamespace

        self.db, self.storage, self.router = database, storage, router
        self.service = XFeedback(SimpleNamespace(database=database, storage=storage), None)

    async def active(self, run_id, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if (
            not run
            or run.executor != x_feedback.KEY
            or run.status.value not in {"pending", "running"}
        ):
            raise ValueError("The X revision is no longer active.")
        return run

    async def sources(self, run, conn=None):
        source = await self.service.source(
            run.input["source_run_id"], run.started_by_clerk_user_id, conn
        )
        snapshot, token, packet = await self.service.snapshot(source, run.input["post_id"], conn)
        if token != run.input["review_token"]:
            raise ValueError("The X draft or writing guide changed. Your newer edits were kept.")
        if snapshot["kind"] == "guide" and (
            source.status.value != "needs_input" or source.review_decision is not None
        ):
            raise ValueError("The guide is already approved or no longer awaiting review.")
        return source, snapshot, packet

    async def references(self, run):
        """The guide revision's reference files, exactly as pinned when the founder sent them."""
        pinned = json.loads(run.input.get("references") or "[]")
        if not pinned:
            return []
        project = await self.db.get_project(run.project_id)
        references = []
        for ref in pinned:
            raw = await self.storage.read_bounded_project_file(
                repo_id=project.state_repo_id,
                commit_sha=ref["revision"],
                path=ref["path"],
                max_bytes=x_feedback.MAX_REFERENCE_BYTES,
            )
            if (
                raw is None
                or len(raw) != ref["bytes"]
                or hashlib.sha256(raw).hexdigest() != ref["sha256"]
            ):
                raise ApplicationError(
                    f"{ref['path']} does not match the file you sent.", non_retryable=True
                )
            references.append({"path": ref["path"], "text": raw.decode("utf-8")})
        return references

    @activity.defn(name="x_feedback_generate")
    async def generate(self, run_id: str):
        run = await self.active(run_id)
        await self.db.mark_run_running(run.id)
        key = f"{run.id}:x_feedback_model"
        async with self.db.effect_lock(key, x_feedback.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            if saved:
                raise ApplicationError(
                    "The revision call could not be confirmed; no replacement was purchased.",
                    non_retryable=True,
                )
            _, snapshot, packet = await self.sources(run, conn)
            definition = json.loads(
                await self.storage.read_canonical_artifact(
                    repo_id="registry/workflows",
                    commit_sha=run.definition_commit_sha,
                    path=f"workflows/{x_feedback.KEY}.json",
                )
            )
            route = x_feedback.ROUTE
            if definition.get("model_route") != {
                "key": route.key,
                "provider": route.provider.value,
                "model": route.model,
                "capabilities": sorted(c.value for c in route.capabilities),
            }:
                raise ApplicationError(
                    "Worker does not serve the selected revision model.", non_retryable=True
                )
            if definition.get("x_feedback_contract") != {
                "policy": x_feedback.POLICY,
                "instructions": x_feedback.INSTRUCTIONS,
                "schema": x_feedback.SCHEMA,
            }:
                raise ApplicationError(
                    "Worker does not serve this X revision contract.", non_retryable=True
                )
            model_packet = x_feedback.model_packet(
                kind=snapshot["kind"],
                post=packet["post"],
                guide=packet["guide"],
                feedback=run.input["feedback"],
                references=await self.references(run),
            )
            content = json.dumps(model_packet, ensure_ascii=False)
            if len(content.encode()) > x_feedback.POLICY["max_input_bytes"]:
                raise ApplicationError(
                    "The X draft's supporting material is too large to revise.", non_retryable=True
                )
            await self.db.start_effect(conn, execution_key=key, operation=x_feedback.KEY)
            try:
                with model_usage_scope(run_id=run.id, step="x_feedback:revise", conn=conn):
                    async with asyncio.timeout(180):
                        result = await self.router.generate(
                            x_feedback.ROUTE.key,
                            ModelRequest(
                                system=x_feedback.INSTRUCTIONS,
                                messages=(ModelMessage(role=MessageRole.USER, content=content),),
                                output_schema=x_feedback.SCHEMA,
                                output_schema_name="x_revision",
                                max_output_tokens=x_feedback.POLICY["max_output_tokens"],
                            ),
                            timeout_seconds=165,
                        )
                revised = x_feedback.validate_result(
                    result.parsed,
                    feedback=run.input["feedback"],
                    guide=packet["guide"],
                    account=snapshot["account_id"],
                    kind=snapshot["kind"],
                )
                documents = {}
                if snapshot["kind"] == "guide":
                    documents[snapshot["path"]] = x_feedback.remember(
                        revised["text"], revised["preferences"], account=snapshot["account_id"]
                    )
                else:
                    draft = validate_draft(packet["raw"])
                    post = next(p for p in draft["posts"] if p["id"] == snapshot["post_id"])
                    post["text"] = revised["text"]
                    # Readiness, supporting facts and attachments are deliberately preserved.
                    documents[snapshot["path"]] = json.dumps(
                        validate_draft(draft), ensure_ascii=False, indent=2
                    )
                    guide = x_feedback.remember(
                        packet["guide"], revised["preferences"], account=snapshot["account_id"]
                    )
                    if guide != packet["guide"]:
                        documents[x_style.GUIDE_PATH] = guide
                remembered = revised["preferences"]
                summary = revised["summary"]
                if remembered:
                    label = (
                        " Added to the proposed guide: "
                        if snapshot["kind"] == "guide"
                        else " Remembered for future X posts: "
                    )
                    summary += label + " ".join(remembered)
                await self.db.complete_effect(
                    conn,
                    execution_key=key,
                    result={
                        "documents": documents,
                        "summary": summary,
                        "remembered": remembered,
                    },
                )
            except Exception:
                raise ApplicationError(
                    "The X revision could not be validated. No files were changed and no "
                    "replacement call was purchased.",
                    non_retryable=True,
                ) from None

    @activity.defn(name="x_feedback_publish")
    async def publish(self, run_id: str):
        run = await self.db.get_run(UUID(run_id))
        if run and run.executor == x_feedback.KEY and run.status.value == "succeeded":
            return
        run = await self.active(run_id)
        project = await self.db.get_project(run.project_id)
        model = (await self.db.get_effect(f"{run.id}:x_feedback_model")).result
        snapshot = json.loads(run.input["snapshot"])
        key = f"{run.id}:x_feedback_commit"
        async with self.db.effect_lock(key, x_feedback.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            intent = (saved.result or {}).get("publication") if saved else None
            await self.db.start_effect(conn, execution_key=key, operation=x_feedback.KEY)
            async with self.db.project_state_lock(conn, project.id):
                if not intent:
                    _, current, _ = await self.sources(run, conn)
                    intent = {"head": current["revision"]}
                    await self.db.save_publication_intent(conn, execution_key=key, intent=intent)
                # Same project/source lock order as admission and guide approval. The file
                # CAS also protects edits by clients that do not use these row locks.
                async with conn.transaction():
                    available = await conn.fetchval(
                        "SELECT true FROM projects WHERE id=$1 AND deleted_at IS NULL FOR UPDATE",
                        project.id,
                    )
                    member = await conn.fetchval(
                        "SELECT true FROM project_memberships WHERE project_id=$1 "
                        "AND clerk_user_id=$2",
                        project.id,
                        run.started_by_clerk_user_id,
                    )
                    if not available or not member:
                        raise ApplicationError(
                            "Project access changed; revision was not applied.", non_retryable=True
                        )
                    await self.active(run.id, conn=conn)
                    source = await conn.fetchrow(
                        "SELECT status, review_decision FROM workflow_runs WHERE id=$1 AND "
                        "project_id=$2 FOR UPDATE",
                        UUID(snapshot["source_run_id"]),
                        project.id,
                    )
                    if snapshot["kind"] == "guide" and (
                        source["status"] != "needs_input" or source["review_decision"] is not None
                    ):
                        raise ApplicationError(
                            "The guide was already approved. The revision was not applied.",
                            non_retryable=True,
                        )
                    sha, _ = await self.storage.commit_project_changes(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        expected_head_sha=intent["head"],
                        request_id=str(run.id),
                        message="Revise X writing from feedback",
                        changes=tuple(
                            ProjectFileMutation(operation="upsert", path=p, content=t.encode())
                            for p, t in model["documents"].items()
                        ),
                    )
                    path = snapshot["path"]
                    ref = f"code.storage://{project.state_repo_id}@{sha}/{path}"
                    # The file is committed; a summary longer than the run's one-line result
                    # must not fail the projection and leave the revision unrecorded.
                    line = result_line(model["summary"])
                    if snapshot["kind"] == "guide":
                        await conn.execute(
                            "UPDATE workflow_runs SET canonical_commit_sha=$2, "
                            "artifact_ref=$3, result_summary=$4 WHERE id=$1",
                            UUID(snapshot["source_run_id"]),
                            sha,
                            ref,
                            line,
                        )
                    await self.db._complete_readonly_report_projection(
                        conn,
                        execution_key=key,
                        run_id=run.id,
                        canonical_commit_sha=sha,
                        artifact_path=path,
                        artifact_ref=ref,
                        summary=line,
                        workflow_key=x_feedback.KEY,
                    )

    @activity.defn(name="x_feedback_failure")
    async def failure(self, run_id: str):
        run = await self.db.get_run(UUID(run_id))
        if run and run.executor == x_feedback.KEY:
            await self.db.project_failure(
                run_id=run.id,
                error_message="The X revision did not finish. Read the current file before "
                "requesting another revision; newer edits were kept.",
            )
