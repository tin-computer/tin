"""Native X voice capture with transient provider text and reviewable guide adoption."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import x_style
from tin_lite.model_providers import MessageRole, ModelMessage, ModelRequest
from tin_lite.model_usage import model_usage_scope
from tin_lite.project_files import credential_findings
from tin_lite.publication import OutputCheckpoint, OutputConflictError, PublicationPendingError

MODEL_TIMEOUT_SECONDS = 165
WAITING_FOR_APPROVAL = (
    "The X writing guide waits for your approval. The current guide is unchanged."
)


class XStyleActivities:
    def __init__(self, *, database, storage, router, x_connection):
        self.db = database
        self.storage = storage
        self.router = router
        self.x = x_connection

    async def active(self, run_id, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or run.executor != x_style.KEY or run.status.value not in {"pending", "running"}:
            raise ValueError("X voice capture is no longer active")
        return run

    async def progress(self, run_id, step, current, summary):
        await self.db.project_run_progress(
            run_id=run_id, mode="steps", step=step, current=current, total=3, summary=summary
        )

    @activity.defn(name="x_style_prepare")
    async def prepare(self, run_id: str):
        run = await self.active(run_id)
        x_style.validate_inputs(run.input or {})
        await self.db.mark_run_running(run.id)
        key = f"{run.id}:x_style_context"
        async with self.db.effect_lock(key, x_style.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            project = await self.db.get_project(run.project_id, conn=conn)
            repo = await self.storage.get_repo(project.state_repo_id)
            revision = await self.storage.head_sha(repo, project.canonical_branch)
            if not isinstance(revision, str) or len(revision) != 40:
                raise ValueError("Project files have no saved revision")
            existing = await self.storage.read_output_destination(
                repo_id=project.state_repo_id, revision=revision, path=x_style.GUIDE_PATH
            )
            guide = existing[1].decode("utf-8") if existing else ""
            if len(guide.encode()) > x_style.MAX_GUIDE_BYTES:
                raise ValueError("Current X writing guide is too large")
            definition = json.loads(
                await self.storage.read_canonical_artifact(
                    repo_id="registry/workflows",
                    commit_sha=run.definition_commit_sha,
                    path=f"workflows/{x_style.KEY}.json",
                )
            )
            if (
                definition.get("model_route") != x_style.route_definition()
                or definition.get("x_style_policy") != x_style.POLICY
                or definition.get("x_style_instructions") != x_style.INSTRUCTIONS
                or definition.get("x_style_schema") != x_style.MODEL_SCHEMA
            ):
                raise ValueError("Worker does not serve the selected X style contract")
            async with conn.transaction():
                await self.db.start_effect(conn, execution_key=key, operation=x_style.KEY)
                updated = await conn.fetchval(
                    """UPDATE workflow_runs SET expected_head_sha=$2
                       WHERE id=$1 AND executor='social.x_style' AND status='running'
                         AND (expected_head_sha IS NULL OR expected_head_sha=$2)
                       RETURNING id""",
                    run.id,
                    revision,
                )
                if not updated:
                    raise ValueError("X style source snapshot changed")
                await self.db.complete_effect(
                    conn,
                    execution_key=key,
                    result={"revision": revision, "existing_guide": guide},
                )
        await self.progress(run.id, "read", 1, "X writing sources are ready")

    async def _examples(self, run, context):
        inputs = run.input or {}
        examples = []
        if inputs.get("supplied_samples"):
            examples.extend(
                x_style.supplied_examples(inputs["supplied_samples"], source="supplied")
            )
        if inputs.get("source_path"):
            project = await self.db.get_project(run.project_id)
            entry = await self.storage.read_output_destination(
                repo_id=project.state_repo_id,
                revision=context["revision"],
                path=inputs["source_path"],
            )
            if entry is None:
                raise ValueError("The selected X sample file is missing at the pinned revision")
            body = entry[1].decode("utf-8")
            if credential_findings(body):
                raise ValueError("Remove credentials from the X sample file")
            examples.extend(x_style.supplied_examples(body, source=inputs["source_path"]))
        if examples:
            examples = examples[: x_style.MAX_SAMPLE_COUNT]
            for index, item in enumerate(examples, 1):
                item["id"] = f"s{index}"
            account = inputs.get("account_id") or "unbound"
            metadata = {
                "returned_count": len(examples),
                "excluded_count": 0,
                "selected_count": len(examples),
                "selected_ids": [],
                "oldest_date": "",
                "newest_date": "",
                "timeline_calls": 0,
            }
            return examples, metadata, account
        if inputs.get("preferences"):
            return (
                [],
                {
                    "returned_count": 0,
                    "excluded_count": 0,
                    "selected_count": 0,
                    "selected_ids": [],
                    "oldest_date": "",
                    "newest_date": "",
                    "timeline_calls": 0,
                },
                inputs.get("account_id") or "unbound",
            )
        connection = await self.x.connection(run.project_id, capability="x.posts.read")
        if connection.configuration.get("protected") is not False:
            raise ValueError("X voice sampling requires a confirmed public account")
        if inputs.get("account_id") and inputs["account_id"] != connection.external_account_id:
            raise ValueError("X account changed; reconnect or choose the current account")
        now = datetime.now(UTC)
        windows = (
            (now - timedelta(days=30), now),
            (now - timedelta(days=90), now - timedelta(days=30)),
            (now - timedelta(days=365), now - timedelta(days=90)),
        )
        pages = []
        for start, end in windows:
            page = await self.x.timeline(
                connection,
                limit=50,
                exclude_retweets=True,
                exclude_replies=False,
                start_time=start.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                end_time=end.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
            pages.append(page)
        selection = x_style.select_own_posts(pages, now=now)
        if not selection["examples"]:
            raise ValueError(
                "No usable own posts were found; supply writing samples or preferences"
            )
        return selection["examples"], selection["metadata"], connection.external_account_id

    @activity.defn(name="x_style_extract")
    async def extract(self, run_id: str):
        run = await self.active(run_id)
        context_receipt = await self.db.get_effect(f"{run.id}:x_style_context")
        if not context_receipt or context_receipt.status != "completed":
            raise ApplicationError("X style preparation is missing", non_retryable=True)
        context = context_receipt.result
        key = f"{run.id}:x_style_model"
        await self.progress(run.id, "extract", 1, "Learning your X voice")
        async with self.db.effect_lock(key, x_style.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            if saved:
                raise ApplicationError(
                    "The previous X sampling or model request is unconfirmed; "
                    "no replacement was purchased.",
                    non_retryable=True,
                )
            await self.active(run.id, conn=conn)
            # Receipt before any paid provider read. Retrying an uncertain read/model call never
            # silently rebuys it. Only IDs/counts and the derived guide enter the receipt.
            await self.db.start_effect(conn, execution_key=key, operation=x_style.KEY)
            try:
                examples, metadata, account = await self._examples(run, context)
                existing_guide = (
                    context["existing_guide"]
                    if x_style.guide_account(context["existing_guide"]) == account
                    else ""
                )
                packet = x_style.model_packet(
                    examples,
                    preferences=(run.input or {}).get("preferences", ""),
                    direction=(run.input or {}).get("direction", ""),
                    existing_guide=existing_guide,
                )
                with model_usage_scope(run_id=run.id, step="x_style:capture", conn=conn):
                    async with asyncio.timeout(180):
                        result = await self.router.generate(
                            x_style.ROUTE.key,
                            ModelRequest(
                                system=x_style.INSTRUCTIONS,
                                messages=(
                                    ModelMessage(role=MessageRole.USER, content=json.dumps(packet)),
                                ),
                                output_schema=x_style.MODEL_SCHEMA,
                                output_schema_name="x_writing_style",
                                max_output_tokens=x_style.POLICY["max_output_tokens"],
                            ),
                            timeout_seconds=MODEL_TIMEOUT_SECONDS,
                        )
                content = x_style.render_guide(
                    result.parsed,
                    account=account,
                    sample_ids={item["id"] for item in examples},
                    metadata=metadata,
                    existing_preferences=x_style.explicit_preferences(existing_guide),
                    new_preferences=(run.input or {}).get("preferences", ""),
                )
                # Long copied source passages are raw provider data, not a derived guide.
                guide_text = content.decode()
                if any(
                    item["text"] in guide_text
                    if len(item["text"]) < 60
                    else any(
                        item["text"][offset : offset + 60] in guide_text
                        for offset in range(0, len(item["text"]) - 59, 20)
                    )
                    for item in examples
                    if metadata["timeline_calls"]
                ):
                    raise ValueError("The proposed guide copied a source post")
                await self.db.complete_effect(
                    conn,
                    execution_key=key,
                    result={
                        "guide": guide_text,
                        "account_id": account,
                        "sampling": metadata,
                        "usage": asdict(result.usage),
                        "model": result.model,
                        "request_id": result.request_id,
                    },
                )
            except Exception:
                raise ApplicationError(
                    "X voice extraction could not be confirmed; no replacement was purchased. "
                    "The current guide is unchanged.",
                    non_retryable=True,
                ) from None

    @activity.defn(name="x_style_propose")
    async def propose(self, run_id: str) -> bool:
        run = await self.db.get_run(UUID(run_id))
        if (
            not run
            or run.executor != x_style.KEY
            or run.status.value not in {"pending", "running", "needs_input"}
        ):
            raise ApplicationError("X voice capture is no longer active", non_retryable=True)
        if not run.review_required:
            raise ApplicationError("X voice capture requires guide review", non_retryable=True)
        project = await self.db.get_project(run.project_id)
        key = f"{run.id}:x_style_proposal"
        async with self.db.effect_lock(key, x_style.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                proposal = saved.result
            else:
                model = (await self.db.get_effect(f"{run.id}:x_style_model")).result
                content = model["guide"].encode()
                path = x_style.proposal_path(run.id, run.created_at or datetime.now(UTC))
                await self.db.start_effect(conn, execution_key=key, operation=x_style.KEY)
                async with self.db.project_state_lock(conn, project.id):
                    head = await self.storage.head_sha(
                        await self.storage.get_repo(project.state_repo_id), project.canonical_branch
                    )
                    sha = await self.storage.create_canonical_commit(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        expected_head_sha=head,
                        artifact_path=path,
                        artifact=content,
                        execution_key=key,
                        run_id=str(run.id),
                        workflow_key=x_style.KEY,
                    )
                proposal = {"canonical_commit_sha": sha, "artifact_path": path}
                await self.db.complete_effect(conn, execution_key=key, result=proposal)
        await self.progress(run.id, "review", 2, "Your X writing guide is ready to review")
        sha, path = proposal["canonical_commit_sha"], proposal["artifact_path"]
        return await self.db.request_human_review(
            run_id=run.id,
            canonical_commit_sha=sha,
            artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{path}",
            artifact_path=path,
            artifact_title="Proposed X writing style guide",
            summary=(
                "Review this account-specific X guide. Approving saves it for future "
                "drafts; your current guide remains until then."
            ),
        )

    @activity.defn(name="x_style_record_approval")
    async def record_approval(self, run_id: str):
        run = await self.db.get_run(UUID(run_id))
        await self.db.record_human_review(
            run_id=run.id, decision="approved", summary="You approved the X writing style guide."
        )
        key = f"{run.id}:x_style_approval"
        async with self.db.effect_lock(key, x_style.KEY) as (conn, saved):
            if not saved or saved.status != "completed":
                project = await self.db.get_project(run.project_id)
                path = (await self.db.get_effect(f"{run.id}:x_style_proposal")).result[
                    "artifact_path"
                ]
                head = await self.storage.head_sha(
                    await self.storage.get_repo(project.state_repo_id), project.canonical_branch
                )
                content = await self.storage.read_canonical_artifact_if_exists(
                    repo_id=project.state_repo_id, commit_sha=head, path=path
                )
                expected_account = (await self.db.get_effect(f"{run.id}:x_style_model")).result[
                    "account_id"
                ]
                try:
                    approved_text = content.decode("utf-8") if content else ""
                except UnicodeDecodeError:
                    approved_text = ""
                if (
                    not content
                    or len(content) > x_style.MAX_GUIDE_BYTES
                    or x_style.guide_account(approved_text) != expected_account
                    or credential_findings(approved_text)
                ):
                    raise ApplicationError(
                        "The proposed X guide is missing or changed account identity. "
                        "The current guide is unchanged.",
                        non_retryable=True,
                    )
                await self.db.start_effect(conn, execution_key=key, operation=x_style.KEY)
                await self.db.complete_effect(
                    conn,
                    execution_key=key,
                    result={
                        "revision": head,
                        "artifact_path": path,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    },
                )
        await self.db.clear_x_style_review_projection(run.id)

    async def approved(self, run, project):
        receipt = await self.db.get_effect(f"{run.id}:x_style_approval")
        if run.review_decision != "approved" or not receipt or receipt.status != "completed":
            raise ApplicationError(WAITING_FOR_APPROVAL, non_retryable=True)
        approval = receipt.result
        content = await self.storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=approval["revision"],
            path=approval["artifact_path"],
        )
        if hashlib.sha256(content).hexdigest() != approval["sha256"]:
            raise ValueError("Approved X guide does not match its approval")
        return content

    @activity.defn(name="x_style_publish")
    async def publish(self, run_id: str):
        run = await self.db.get_run(UUID(run_id))
        if run and run.executor == x_style.KEY and run.status.value == "succeeded":
            return
        run = await self.active(run_id)
        project = await self.db.get_project(run.project_id)
        content = await self.approved(run, project)
        await self.progress(run.id, "save", 2, "Saving the X writing guide")
        checkpoint_key = f"{run.id}:x_style_artifact_persist"
        async with self.db.effect_lock(checkpoint_key, x_style.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                checkpoint = OutputCheckpoint.load(saved.result["checkpoint"], run=run)
                checkpoint.validate_content(content)
            else:
                await self.db.start_effect(
                    conn, execution_key=checkpoint_key, operation=x_style.KEY
                )
                await self.active(run.id, conn=conn)
                revision = await self.storage.stage_native_output(
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    run_id=str(run.id),
                    generation=run.generation,
                    path=x_style.GUIDE_PATH,
                    content=content,
                )
                checkpoint = OutputCheckpoint.create(
                    run=run,
                    revision=revision,
                    path=x_style.GUIDE_PATH,
                    media_type="text/markdown",
                    content=content,
                )
                await self.db.complete_effect(
                    conn, execution_key=checkpoint_key, result={"checkpoint": checkpoint.to_dict()}
                )
        key = f"{run.id}:x_style_publish"
        async with self.db.effect_lock(key, x_style.KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            intent = (saved.result or {}).get("publication") if saved else None
            await self.db.start_effect(conn, execution_key=key, operation=x_style.KEY)
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
                        workflow_key=x_style.KEY,
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
                            artifact_path=x_style.GUIDE_PATH,
                            artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{x_style.GUIDE_PATH}",
                            summary="X writing style captured. Future X drafts can use this guide.",
                            workflow_key=x_style.KEY,
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
                        "The X guide changed during capture. Your edits were kept; "
                        "compare the saved result.",
                        non_retryable=True,
                    ) from None
                raise

    @activity.defn(name="x_style_failure")
    async def failure(self, run_id: str):
        run = await self.db.get_run(UUID(run_id))
        if run and run.executor == x_style.KEY:
            await self.db.project_failure(
                run_id=run.id,
                error_message=(
                    "X voice capture could not confirm completion. Check the current guide "
                    "and any saved result before trying again."
                ),
            )
