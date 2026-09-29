"""One bounded native extraction; reuse the existing saved-output publication contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import style_capture as style
from tin_lite.model_providers import MessageRole, ModelMessage, ModelRequest
from tin_lite.model_usage import model_usage_scope
from tin_lite.publication import OutputCheckpoint, OutputConflictError, PublicationPendingError
from tin_lite.writing_style import STYLE_PATH

WAITING_FOR_APPROVAL = "The writing guide waits for your approval. The current guide is unchanged."
# The provider's own wait for the style model, just inside the step's 180-second budget. Without
# it the client stops at its 90-second default, before a 6,000-token guide can finish.
MODEL_TIMEOUT_SECONDS = 165


class StyleCaptureActivities:
    def __init__(self, *, database, storage, router):
        self.db, self.storage, self.router = database, storage, router

    async def active(self, run_id, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or run.executor != style.KEY or run.status.value not in {"pending", "running"}:
            raise ValueError("Style capture is no longer active.")
        return run

    @activity.defn(name="style_prepare")
    async def prepare(self, run_id: str):
        try:
            run = await self.active(run_id)
            await self.db.mark_run_running(run.id)
            key = f"{run.id}:style_context"
            async with self.db.effect_lock(key, style.KEY) as (conn, saved):
                if saved and saved.status == "completed":
                    return
                project = await self.db.get_project(run.project_id, conn=conn)
                try:
                    packet, revision = await style.read_sources(
                        self.storage, project, run.input["source_path"]
                    )
                    existing = await self.storage.read_output_destination(
                        repo_id=project.state_repo_id, revision=revision, path=STYLE_PATH
                    )
                    guide = existing[1].decode("utf-8") if existing else ""
                    if len(guide.encode()) > style.MAX_GUIDE_BYTES:
                        raise style.StyleSourceError(
                            f"The current writing guide {STYLE_PATH} is "
                            f"{len(guide.encode()):,} bytes; Tin reads at most "
                            f"{style.MAX_GUIDE_BYTES:,}. Shorten it in Files first."
                        )
                except style.StyleSourceError as exc:
                    # The failed receipt keeps the reason for the run's failure projection.
                    await self.db.start_effect(conn, execution_key=key, operation=style.KEY)
                    await self.db.fail_effect(conn, execution_key=key, error_message=str(exc))
                    raise
                definition = json.loads(
                    await self.storage.read_canonical_artifact(
                        repo_id="registry/workflows",
                        commit_sha=run.definition_commit_sha,
                        path=f"workflows/{style.KEY}.json",
                    )
                )
                if definition.get("model_route") != style.route_definition() or (
                    definition.get("style_policy") != style.POLICY
                ):
                    raise ValueError("This worker does not serve the selected style contract.")
                async with conn.transaction():
                    await self.db.start_effect(conn, execution_key=key, operation=style.KEY)
                    updated = await conn.fetchval(
                        """UPDATE workflow_runs SET expected_head_sha=$2
                           WHERE id=$1 AND executor='style.capture' AND status='running'
                             AND (expected_head_sha IS NULL OR expected_head_sha=$2)
                           RETURNING id""",
                        run.id,
                        revision,
                    )
                    if not updated:
                        raise ValueError("Style capture's source snapshot changed.")
                    await self.db.complete_effect(
                        conn,
                        execution_key=key,
                        result={
                            "packet": packet.model_dump(),
                            "revision": revision,
                            "existing_guide": guide,
                            "instructions": definition["style_instructions"],
                            "schema": definition["style_schema"],
                        },
                    )
            await self.progress(run.id, "read", 1, "Selected writing samples are ready")
        except style.StyleSourceError as exc:
            raise ApplicationError(str(exc), type="StyleSourceError", non_retryable=True) from None
        except ValueError:
            raise ApplicationError(
                "Style sources are invalid or unavailable. Check the selected sample packet.",
                non_retryable=True,
            ) from None

    async def progress(self, run_id, step, current, summary):
        await self.db.project_run_progress(
            run_id=run_id, mode="steps", step=step, current=current, total=3, summary=summary
        )

    @activity.defn(name="style_extract")
    async def extract(self, run_id: str):
        run = await self.active(run_id)
        key = f"{run.id}:style_model"
        context = (await self.db.get_effect(f"{run.id}:style_context")).result
        await self.progress(run.id, "extract", 1, "Extracting voice and editorial preferences")
        async with self.db.effect_lock(key, style.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            if saved:
                raise ApplicationError(
                    "The previous model request is unconfirmed; no replacement was purchased.",
                    non_retryable=True,
                )
            await self.active(run.id, conn=conn)
            await self.db.start_effect(conn, execution_key=key, operation=style.KEY)
            try:
                with model_usage_scope(run_id=run.id, step="style:capture", conn=conn):
                    async with asyncio.timeout(180):
                        result = await self.router.generate(
                            style.ROUTE.key,
                            ModelRequest(
                                system=context["instructions"],
                                messages=(
                                    ModelMessage(
                                        role=MessageRole.USER,
                                        content=json.dumps(
                                            {
                                                "samples": context["packet"],
                                                "existing_guide": context["existing_guide"],
                                                "direction": (run.input or {}).get("direction", ""),
                                            }
                                        ),
                                    ),
                                ),
                                output_schema=context["schema"],
                                output_schema_name="writing_style",
                                max_output_tokens=style.POLICY["max_output_tokens"],
                            ),
                            timeout_seconds=MODEL_TIMEOUT_SECONDS,
                        )
                # Receipt precedes semantic validation; a retry cannot buy a repair call.
                await self.db.complete_effect(
                    conn,
                    execution_key=key,
                    result={
                        "data": result.parsed,
                        "usage": asdict(result.usage),
                        "model": result.model,
                        "request_id": result.request_id,
                    },
                )
            except Exception:
                raise ApplicationError(
                    "The style model request could not be confirmed; no replacement was purchased.",
                    non_retryable=True,
                ) from None

    def rendered(self, run, context, model):
        try:
            return style.render_guide(
                model["data"],
                style.SourcePacket.model_validate(context["packet"]),
                source_path=run.input["source_path"],
                revision=context["revision"],
                direction=(run.input or {}).get("direction", ""),
                existing_preferences=style.explicit_preferences(context["existing_guide"]),
            )
        except (ValueError, TypeError):
            raise ApplicationError(
                "The extracted guide is invalid. The current guide is unchanged.",
                non_retryable=True,
            ) from None

    @activity.defn(name="style_propose")
    async def propose(self, run_id: str) -> bool:
        """Save the guide as a proposal and ask for review; the active guide stays unchanged."""
        run = await self.db.get_run(UUID(run_id))
        if not run or run.executor != style.KEY:
            raise ValueError("Style capture is no longer active.")
        if not run.review_required:
            return False  # Runs pinned before review publish exactly as they did.
        if run.status.value not in {"pending", "running", "needs_input"}:
            raise ApplicationError("Style capture is no longer active.", non_retryable=True)
        project = await self.db.get_project(run.project_id)
        key = f"{run.id}:style_proposal"
        async with self.db.effect_lock(key, style.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                proposal = saved.result
            else:
                context = (await self.db.get_effect(f"{run.id}:style_context")).result
                model = (await self.db.get_effect(f"{run.id}:style_model")).result
                content = self.rendered(run, context, model)
                path = style.proposal_path(run.id, run.created_at or datetime.now(UTC))
                await self.db.start_effect(conn, execution_key=key, operation=style.KEY)
                async with self.db.project_state_lock(conn, project.id):
                    head = await self.storage.head_sha(
                        await self.storage.get_repo(project.state_repo_id),
                        project.canonical_branch,
                    )
                    sha = await self.storage.create_canonical_commit(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        expected_head_sha=head,
                        artifact_path=path,
                        artifact=content,
                        execution_key=key,
                        run_id=str(run.id),
                        workflow_key=style.KEY,
                    )
                proposal = {"canonical_commit_sha": sha, "artifact_path": path}
                await self.db.complete_effect(conn, execution_key=key, result=proposal)
        await self.progress(run.id, "review", 2, "Your writing guide is ready to review")
        sha, path = proposal["canonical_commit_sha"], proposal["artifact_path"]
        return await self.db.request_human_review(
            run_id=run.id,
            canonical_commit_sha=sha,
            artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{path}",
            artifact_path=path,
            artifact_title="Proposed writing style guide",
            summary=(
                "Your writing style guide is ready. Approve it to save it for future drafts; "
                "until then your current guide stays in place."
            ),
        )

    @activity.defn(name="style_record_approval")
    async def record_approval(self, run_id: str):
        """Bind the approval to the proposal as it stands now, including edits made in Files."""
        run = await self.db.get_run(UUID(run_id))
        await self.db.record_human_review(
            run_id=run.id, decision="approved", summary="You approved the writing style guide."
        )
        key = f"{run.id}:style_approval"
        async with self.db.effect_lock(key, style.KEY) as (conn, saved):
            if not saved or saved.status != "completed":
                project = await self.db.get_project(run.project_id)
                path = (await self.db.get_effect(f"{run.id}:style_proposal")).result[
                    "artifact_path"
                ]
                head = await self.storage.head_sha(
                    await self.storage.get_repo(project.state_repo_id), project.canonical_branch
                )
                content = await self.storage.read_canonical_artifact_if_exists(
                    repo_id=project.state_repo_id, commit_sha=head, path=path
                )
                try:
                    if not content or len(content) > style.MAX_GUIDE_BYTES:
                        raise ValueError
                    content.decode("utf-8")
                except ValueError:
                    raise ApplicationError(
                        "The proposed guide was removed or is too large. "
                        "Your current guide is unchanged; start capture again.",
                        non_retryable=True,
                    ) from None
                await self.db.start_effect(conn, execution_key=key, operation=style.KEY)
                await self.db.complete_effect(
                    conn,
                    execution_key=key,
                    result={
                        "revision": head,
                        "artifact_path": path,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    },
                )
        # The proposal was only the review's document; saving the guide projects the result.
        await self.db.clear_style_review_projection(run.id)

    async def approved(self, run, project):
        receipt = await self.db.get_effect(f"{run.id}:style_approval")
        if run.review_decision != "approved" or not receipt or receipt.status != "completed":
            raise ApplicationError(WAITING_FOR_APPROVAL, non_retryable=True)
        approval = receipt.result
        content = await self.storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=approval["revision"],
            path=approval["artifact_path"],
        )
        if hashlib.sha256(content).hexdigest() != approval["sha256"]:
            raise ValueError("The approved guide does not match its approval.")
        return content

    @activity.defn(name="style_publish")
    async def publish(self, run_id: str):
        run = await self.db.get_run(UUID(run_id))
        if run and run.executor == style.KEY and run.status.value == "succeeded":
            return
        if run and run.review_required and run.review_decision != "approved":
            raise ApplicationError(WAITING_FOR_APPROVAL, non_retryable=True)
        run = await self.active(run_id)
        project = await self.db.get_project(run.project_id)
        if run.review_required:
            content = await self.approved(run, project)
        else:
            context = (await self.db.get_effect(f"{run.id}:style_context")).result
            model = (await self.db.get_effect(f"{run.id}:style_model")).result
            content = self.rendered(run, context, model)
        await self.progress(run.id, "save", 2, "Saving the editable writing guide")
        checkpoint_key = f"{run.id}:style_artifact_persist"
        async with self.db.effect_lock(checkpoint_key, style.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                checkpoint = OutputCheckpoint.load(saved.result["checkpoint"], run=run)
                checkpoint.validate_content(content)
            else:
                await self.db.start_effect(conn, execution_key=checkpoint_key, operation=style.KEY)
                await self.active(run.id, conn=conn)
                revision = await self.storage.stage_native_output(
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    run_id=str(run.id),
                    generation=run.generation,
                    path=STYLE_PATH,
                    content=content,
                )
                checkpoint = OutputCheckpoint.create(
                    run=run,
                    revision=revision,
                    path=STYLE_PATH,
                    media_type="text/markdown",
                    content=content,
                )
                await self.db.complete_effect(
                    conn, execution_key=checkpoint_key, result={"checkpoint": checkpoint.to_dict()}
                )
        key = f"{run.id}:style_publish"
        async with self.db.effect_lock(key, style.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            intent = (saved.result or {}).get("publication") if saved else None
            await self.db.start_effect(conn, execution_key=key, operation=style.KEY)
            await self.db.retain_procedure_output(
                conn, run_id=run.id, checkpoint=checkpoint.to_dict(), reason="publication_pending"
            )

            async def save_intent(value):
                await self.db.save_publication_intent(conn, execution_key=key, intent=value)

            async def validate():
                await self.active(run.id, conn=conn)

            try:
                async with self.db.project_state_lock(conn, project.id):
                    sha, _ = await self.storage.publish_procedure_output(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        checkpoint=checkpoint,
                        content=content,
                        execution_key=key,
                        workflow_key=style.KEY,
                        intent=intent,
                        legacy_attempt=False,
                        save_intent=save_intent,
                        validate_lease=validate,
                    )
                    async with conn.transaction():
                        await self.db._complete_readonly_report_projection(
                            conn,
                            execution_key=key,
                            run_id=run.id,
                            canonical_commit_sha=sha,
                            artifact_path=STYLE_PATH,
                            artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{STYLE_PATH}",
                            summary="Writing style captured. Future drafts can use this guide.",
                            workflow_key=style.KEY,
                        )
                        await conn.execute(
                            "UPDATE workflow_runs SET retained_output=NULL WHERE id=$1", run.id
                        )
            except (OutputConflictError, PublicationPendingError) as exc:
                conflict = isinstance(exc, OutputConflictError)
                await self.db.retain_procedure_output(
                    conn,
                    run_id=run.id,
                    checkpoint=checkpoint.to_dict(),
                    reason="output_conflict" if conflict else "reconciliation_pending",
                )
                if conflict:
                    raise ApplicationError(
                        "The writing guide changed during capture. "
                        "Your guide was kept; compare the saved result.",
                        non_retryable=True,
                    ) from None
                raise

    @activity.defn(name="style_failure")
    async def failure(self, run_id: str):
        run = await self.db.get_run(UUID(run_id))
        if run and run.executor == style.KEY:
            # A rejected source file names itself and its problem; keep those words.
            source = await self.db.get_effect(f"{run.id}:style_context")
            await self.db.project_failure(
                run_id=run.id,
                error_message=(
                    source.error_message
                    if source is not None and source.status == "failed" and source.error_message
                    else "Style capture could not confirm completion. "
                    "Check the current guide and any saved result before trying again."
                ),
            )
