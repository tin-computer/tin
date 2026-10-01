"""Trusted orchestration for code workflows; authored Python runs only in E2B."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite.code_models import CodeModelError, CodeModels
from tin_lite.code_project_files import CodeProjectFileError, CodeProjectFiles
from tin_lite.code_project_files import saved_source as saved_project_files
from tin_lite.code_services import CodeServiceError, CodeServices
from tin_lite.e2b_runtime import CodeExecutionError
from tin_lite.procedures import SandboxProfile
from tin_lite.publication import OutputCheckpoint, OutputConflictError, PublicationPendingError
from tin_lite.workflow_code import (
    EXECUTOR,
    code_result_path,
    load_code_package,
    output_path_allowed,
    validate_code_result,
)
from tin_lite.workflow_inputs import normalize_workflow_inputs


class CodeActivities:
    def __init__(self, *, common, model_router=None):
        self.common = common
        self.db, self.storage, self.sandboxes = common._db, common._storage, common._sandboxes
        self.models = CodeModels(database=self.db, router=model_router, settings=common._settings)
        self.services = CodeServices(
            database=self.db,
            integrations=common._integrations,
            authorize=self.models.authorize,
            settings=common._settings,
        )
        self.project_files = CodeProjectFiles(database=self.db, storage=self.storage)

    async def selected(self, run_id):
        run = await self.common._require_run(UUID(str(run_id)))
        if run.executor != EXECUTOR or not run.definition_commit_sha:
            raise ValueError("not a pinned code run")
        workflow = await self.db.get_workflow(run.workflow_id)
        project = await self.db.get_project(run.project_id)
        if workflow is None or project is None or workflow.executor != EXECUTOR:
            raise ValueError("code workflow is unavailable")
        if workflow.project_id is not None and (
            workflow.project_id != run.project_id
            or workflow.definition_repo_id != project.state_repo_id
        ):
            raise ValueError("code source does not belong to the project")
        definition, spec, files = await load_code_package(
            storage=self.storage,
            repo_id=workflow.definition_repo_id,
            commit_sha=run.definition_commit_sha,
            definition_path=workflow.definition_path,
        )
        if definition["key"] != workflow.key:
            raise ValueError("code source identity changed")
        workflow = replace(
            workflow, definition=definition, current_commit_sha=run.definition_commit_sha
        )
        return run, workflow, project, spec, files

    @activity.defn(name="execute_code_workflow")
    async def execute(self, run_id: str):
        await self.common._await_with_heartbeats(
            self._execute(run_id), details={"stage": "code_execution"}
        )

    async def _execute(self, run_id: str):
        # Reuse the established run output receipt/checkpoint namespace. It contains
        # execution metadata, not an authored state file or a Codex/model attempt.
        key = f"{run_id}:procedure_artifact_persist"
        async with self.db.effect_lock(key, "procedure_artifact_persist") as (conn, saved):
            if saved and saved.status == "completed":
                run = await self.common._require_run(UUID(run_id))
                if run.sandbox_id:
                    await self.sandboxes.kill(run.sandbox_id)
                return
            sandbox_id = None
            stage = "preparation"
            try:
                run, workflow, project, spec, files = await self.selected(run_id)
                if run.status.value not in {"pending", "running"}:
                    raise ValueError("code run is no longer active")
                await self.common._check_private_attempt(run, workflow)
                inputs = normalize_workflow_inputs(
                    schema=workflow.definition["input_schema"],
                    project_id=run.project_id,
                    inputs={k: v for k, v in (run.input or {}).items() if k != "project_id"},
                )
                context = {"run_id": run_id, "created_at": run.created_at.isoformat()}
                if spec.approved_article_input is not None:
                    from tin_lite.code_article_sources import saved_source

                    context["approved_article"] = await saved_source(self.db, run, spec)
                if spec.evidence:
                    from tin_lite.code_evidence import bound_context
                    from tin_lite.code_evidence import saved_source as saved_evidence

                    context["evidence"] = await saved_evidence(self.db, run, spec)
                    bound_context(
                        {"version": 1, "slots": context["evidence"]},
                        context.get("approved_article"),
                    )
                file_source = await saved_project_files(self.db, run, project)
                branch = f"procedures/{run.id}/{run.generation}"
                revision = await self.storage.procedure_checkpoint_revision(
                    repo_id=project.state_repo_id, branch=branch
                )
                await self.db.start_effect(
                    conn, execution_key=key, operation="procedure_artifact_persist"
                )
                if revision is None:
                    base = run.expected_head_sha or await self.storage.head_sha(
                        await self.storage.get_repo(project.state_repo_id), project.canonical_branch
                    )
                    if base is None:
                        raise ValueError("project has no canonical revision")
                    # Pure computation may repeat after loss; its files, inputs and timestamp
                    # remain pinned. Managed model calls recover their own durable results.
                    if run.sandbox_id:
                        # A worker can die while its previous controller is still running.
                        # Pure retry starts only after that physical sandbox is gone.
                        await self.sandboxes.kill(run.sandbox_id)
                    stage = "sandbox creation"
                    sandbox_id = await self.sandboxes.create(
                        execution_key=f"{run_id}:code_sandbox",
                        run_id=run_id,
                        profile=SandboxProfile(
                            profile="isolated", timeout_seconds=spec.timeout_seconds
                        ),
                        code_only=True,
                    )
                    run = await self.db.attach_sandbox(
                        run_id=run.id,
                        sandbox_id=sandbox_id,
                        lease_owner=f"{run_id}:code_sandbox",
                        expected_head_sha=base,
                        ephemeral_branch=branch,
                        require_active=True,
                    )
                    await self.db.mark_run_running(run.id)
                    packet = {
                        "files": {p: raw.decode("utf-8") for p, raw in files.items()},
                        "entrypoint": spec.entrypoint,
                        "timeout_seconds": spec.timeout_seconds,
                        "context": context,
                        "inputs": inputs,
                    }
                    options = {}
                    if spec.model_routes or spec.services or file_source is not None:
                        packet["model_client"] = True

                        async def generate(payload):
                            if isinstance(payload, dict) and payload.get("kind") == "file":
                                return await self.project_files.call(
                                    conn=conn,
                                    run=run,
                                    project=project,
                                    source=file_source,
                                    payload=payload,
                                )
                            if (
                                isinstance(payload, dict)
                                and set(payload) == {"kind", "payload"}
                                and payload["kind"] == "service"
                            ):
                                return await self.services.call(
                                    conn=conn,
                                    run=run,
                                    workflow=workflow,
                                    spec=spec,
                                    payload=payload["payload"],
                                )
                            return await self.models.generate(
                                conn=conn, run=run, workflow=workflow, spec=spec, payload=payload
                            )

                        options["model_call"] = generate
                    stage = "execution"
                    raw = await self.common._await_with_heartbeats(
                        self.sandboxes.run_code_and_kill(
                            sandbox_id=sandbox_id, packet=packet, **options
                        ),
                        details={"stage": "code_execution"},
                        procedure_run=run,
                    )
                    content = validate_code_result(raw, spec, created_at=run.created_at)
                    path = code_result_path(raw)
                    if path != spec.output_path:
                        # A named output is chosen by the code; record it before the write so
                        # recovery reads the same file instead of guessing.
                        await self.db.save_effect_progress(
                            conn, execution_key=key, result={"artifact_path": path}
                        )
                    await self.common._validate_procedure_publication_lease(run)
                    stage = "saving the result checkpoint"
                    revision = await self.storage.stage_native_output(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        run_id=run_id,
                        generation=run.generation,
                        path=path,
                        content=content,
                        executor=EXECUTOR,
                    )
                else:
                    stage = "recovering the saved result"
                    path = ((saved.result or {}) if saved else {}).get(
                        "artifact_path", spec.output_path
                    )
                    content = await self.storage.read_procedure_checkpoint(
                        repo_id=project.state_repo_id, revision=revision, path=path
                    )
                    validate_code_result(
                        json.dumps({"path": path, "content": content.decode("utf-8")}).encode(),
                        spec,
                        created_at=run.created_at,
                    )
                    if run.sandbox_id:
                        await self.sandboxes.kill(run.sandbox_id)
                checkpoint = OutputCheckpoint.create(
                    run=run,
                    revision=revision,
                    path=path,
                    media_type=spec.media_type,
                    content=content,
                )
                stage = "recording the saved result"
                await self.db.complete_procedure_persist(
                    conn,
                    execution_key=key,
                    run_id=run.id,
                    result={
                        "checkpoint": checkpoint.to_dict(),
                        "ephemeral_commit_sha": revision,
                        "sandbox_killed": True,
                        "summary": f"{workflow.title} is ready.",
                    },
                    sandbox_killed=True,
                    sandbox_stage="code_workflow",
                )
            except CodeModelError as exc:
                await self.db.fail_effect(
                    conn, execution_key=key, error_message=f"{exc.code}: {exc}"
                )
                raise ApplicationError(f"{exc.code}: {exc}", non_retryable=True) from None
            except CodeServiceError as exc:
                await self.db.fail_effect(conn, execution_key=key, error_message=str(exc))
                raise ApplicationError(str(exc), non_retryable=True) from None
            except CodeProjectFileError as exc:
                # Only a revoked lease escapes the file bridge; say so, not "failed validation".
                message = (
                    f"{exc.code}: The run no longer has permission to read project files."
                    if exc.code == "file_access_revoked"
                    else f"{exc.code}: A project file request was refused."
                )
                await self.db.fail_effect(conn, execution_key=key, error_message=message)
                raise ApplicationError(message, non_retryable=True) from None
            except CodeExecutionError as exc:
                if exc.deterministic:
                    # The package ran and exited without a valid result. A fresh sandbox runs
                    # the same code on the same inputs, so another attempt repeats the failure.
                    await self.db.fail_effect(conn, execution_key=key, error_message=str(exc))
                    raise ApplicationError(str(exc), non_retryable=True) from None
                await self._retain_failure(conn, key, str(exc))
                raise
            except (ValueError, UnicodeError):
                await self.db.fail_effect(
                    conn,
                    execution_key=key,
                    error_message="The code package, inputs or result failed validation.",
                )
                raise ApplicationError(
                    "The code package, inputs or result failed validation.", non_retryable=True
                ) from None
            except BaseException as exc:
                # Retain the failed stage without exposing arbitrary sandbox/provider text.
                # Temporal still retries; completed model/service receipts are reused.
                await self._retain_failure(
                    conn, key, f"Code workflow failed during {stage} ({type(exc).__name__})."
                )
                raise
            finally:
                if sandbox_id:
                    await self.sandboxes.kill(sandbox_id)

    async def _retain_failure(self, conn, key, reason):
        current = await self.db.get_effect(key, conn=conn)
        if current and current.status == "started":
            await self.db.save_effect_progress(
                conn,
                execution_key=key,
                result={**(current.result or {}), "failure_reason": reason},
            )

    @activity.defn(name="publish_code_workflow")
    async def publish(self, run_id: str):
        key = f"{run_id}:procedure_canonical_commit"
        async with self.db.effect_lock(key, "procedure_canonical_commit") as (conn, saved):
            if saved and saved.status == "completed":
                await self.db.release_lease(UUID(run_id))
                return
            run, workflow, project, spec, _files = await self.selected(run_id)
            persisted = await self.db.get_effect(f"{run_id}:procedure_artifact_persist")
            if not persisted or persisted.status != "completed":
                raise ValueError("code result is not saved")
            checkpoint = OutputCheckpoint.load(persisted.result["checkpoint"], run=run)
            path = checkpoint.artifact_path
            if (
                not output_path_allowed(spec, path, run.created_at)
                or checkpoint.media_type != spec.media_type
            ):
                raise ValueError("saved output differs from its contract")
            content = await self.storage.read_procedure_checkpoint(
                repo_id=project.state_repo_id,
                revision=checkpoint.ephemeral_commit_sha,
                path=path,
            )
            checkpoint.validate_content(content)
            validate_code_result(
                json.dumps({"path": path, "content": content.decode("utf-8")}).encode(),
                spec,
                created_at=run.created_at,
            )
            intent = (saved.result or {}).get("publication") if saved else None
            if intent is None:
                await self.common._validate_procedure_publication_lease(run)
            await self.db.start_effect(
                conn, execution_key=key, operation="procedure_canonical_commit"
            )

            async def save_intent(value):
                await self.db.save_publication_intent(conn, execution_key=key, intent=value)

            async def validate():
                await self.common._validate_procedure_publication_lease(run)

            try:
                async with self.db.project_state_lock(conn, project.id):
                    sha, changed = await self.storage.publish_procedure_output(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        checkpoint=checkpoint,
                        content=content,
                        execution_key=key,
                        workflow_key=workflow.key,
                        intent=intent,
                        legacy_attempt=False,
                        save_intent=save_intent,
                        validate_lease=validate,
                    )
                    await self.db.complete_procedure_publication(
                        conn,
                        run_id=run.id,
                        execution_key=key,
                        result={
                            "canonical_commit_sha": sha,
                            "artifact_path": path,
                            "checkpoint": checkpoint.to_dict(),
                            "changed": changed,
                            "summary": persisted.result["summary"],
                        },
                        artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{path}",
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
                        "The output file changed. The generated result remains saved.",
                        non_retryable=True,
                    ) from None
                raise
            await self.db.release_lease(run.id)

    @activity.defn(name="review_code_workflow")
    async def review(self, run_id: str) -> bool:
        return await self.common.request_codex_procedure_review(run_id)

    @activity.defn(name="approve_code_workflow")
    async def approve(self, run_id: str):
        await self.common.record_codex_procedure_approval(run_id)

    @activity.defn(name="project_code_workflow")
    async def project(self, run_id: str):
        await self.common.project_codex_procedure_result(run_id)

    @activity.defn(name="fail_code_workflow")
    async def failure(self, run_id: str):
        receipt = await self.db.get_effect(f"{run_id}:procedure_artifact_persist")
        reason = (receipt.result or {}).get("failure_reason") if receipt else None
        await self.common.project_codex_procedure_failure(
            {
                "run_id": run_id,
                "reason": reason or "The code workflow could not confirm completion.",
            }
        )
