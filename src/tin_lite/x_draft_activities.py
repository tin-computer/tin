"""Trusted, idempotent dispatch of X voice setup followed by one composition run."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import x_draft, x_style
from tin_lite.integrations import X_PROVIDER
from tin_lite.workflow_packages import decode_workflow_source


class XDraftActivities:
    def __init__(self, *, database, storage, integrations, settings):
        self.db, self.storage, self.integrations, self.settings = (
            database,
            storage,
            integrations,
            settings,
        )

    async def active(self, run_id, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or run.executor != x_draft.KEY or run.status.value not in {"pending", "running"}:
            raise ApplicationError("X drafting is no longer active", non_retryable=True)
        if not await self.db.has_project_access(
            project_id=run.project_id, clerk_user_id=run.started_by_clerk_user_id
        ):
            raise ApplicationError("Project access is no longer available", non_retryable=True)
        return run

    async def saved(self, run_id, step, *, conn=None):
        receipt = await self.db.get_effect(f"x-draft:{run_id}:{step}", conn=conn)
        return receipt.result if receipt and receipt.status == "completed" else None

    async def guide(self, project):
        repo = await self.storage.get_repo(project.state_repo_id)
        revision = await self.storage.head_sha(repo, project.canonical_branch)
        entry = await self.storage.read_output_destination(
            repo_id=project.state_repo_id, revision=revision, path=x_style.GUIDE_PATH
        )
        if entry is None:
            return ""
        if len(entry[1]) > x_style.MAX_GUIDE_BYTES:
            raise ValueError("The X writing guide exceeds its saved-file limit")
        return entry[1].decode("utf-8")

    @activity.defn(name="x_draft_prepare")
    async def prepare(self, run_id: str):
        run = await self.active(run_id)
        await self.db.mark_run_running(run.id)
        key = f"x-draft:{run.id}:prepare"
        async with self.db.effect_lock(key, x_draft.KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return
            definition = json.loads(
                await self.storage.read_canonical_artifact(
                    repo_id="registry/workflows",
                    commit_sha=run.definition_commit_sha,
                    path=f"workflows/{x_draft.KEY}.json",
                )
            )
            if definition.get("x_draft_policy") != x_draft.POLICY:
                raise ApplicationError("Unsupported X drafting recipe", non_retryable=True)
            definitions = {}
            for step, child_key in x_draft.STEPS.items():
                template = await self.db.get_registry_workflow(child_key)
                if template is None:
                    raise ValueError("X drafting child is unavailable")
                source = await self.storage.read_canonical_artifact(
                    repo_id="registry/workflows",
                    commit_sha=run.definition_commit_sha,
                    path=template.definition_path,
                )
                child = (
                    decode_workflow_source(
                        source, definition_path=template.definition_path
                    ).definition
                    if step == "compose"
                    else json.loads(source)
                )
                expected = x_style.KEY if step == "style" else "workflow.code"
                if child.get("key") != child_key or child.get("executor") != expected:
                    raise ValueError("X drafting child contract changed")
                definitions[step] = child
            project = await self.db.get_project(run.project_id, conn=conn)
            connection = await self.db.get_integration_connection(
                project_id=run.project_id, provider_key=X_PROVIDER
            )
            connection = connection if connection and connection.status == "connected" else None
            account = connection.external_account_id if connection else ""
            guide = await self.guide(project)
            inputs = run.input or {}
            style_inputs = None
            if x_draft.matching_guide(guide, account):
                voice = "existing_guide"
                account = account or (x_style.guide_account(guide) or "")
                if account == "unbound":
                    account = ""
            else:
                supplied = any(
                    inputs.get(k, "").strip() for k in ("supplied_samples", "source_path")
                )
                can_sample = bool(
                    connection
                    and connection.configuration.get("protected") is False
                    and "x.posts.read" in connection.configuration.get("granted_capabilities", [])
                )
                if supplied or can_sample or inputs.get("preferences", "").strip():
                    source = "supplied" if supplied or not can_sample else "connected"
                    voice = "supplied_samples" if source == "supplied" else "connected_account"
                    style_inputs = {
                        "sample_source": source,
                        "account_id": account,
                        "preferences": inputs.get("preferences", ""),
                    }
                    if source == "supplied":
                        style_inputs.update(
                            {k: inputs.get(k, "") for k in ("supplied_samples", "source_path")}
                        )
                    x_style.validate_inputs(style_inputs)
                else:
                    voice = "project_context"
            await self.db.start_effect(conn, execution_key=key, operation=x_draft.KEY)
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={
                    "definitions": definitions,
                    "definition_revision": run.definition_commit_sha,
                    "voice": voice,
                    "account_id": account,
                    "connection_id": str(connection.id) if connection else None,
                    "style_inputs": style_inputs,
                },
            )

    async def check_account(self, run, prepared):
        connection = await self.db.get_integration_connection(
            project_id=run.project_id, provider_key=X_PROVIDER
        )
        current = connection if connection and connection.status == "connected" else None
        if prepared["connection_id"] and (
            current is None
            or str(current.id) != prepared["connection_id"]
            or current.external_account_id != prepared["account_id"]
        ):
            raise ApplicationError(
                "The X account changed. Start a new draft for the current account.",
                non_retryable=True,
            )
        if (
            not prepared["connection_id"]
            and current
            and current.external_account_id != prepared["account_id"]
        ):
            raise ApplicationError(
                "An X account was connected during drafting. Start again for that account.",
                non_retryable=True,
            )

    @activity.defn(name="x_draft_step")
    async def step(self, payload: dict[str, str]):
        from tin_lite.run_service import start_workflow_run

        run_id, step = payload["run_id"], payload["step"]
        if step not in x_draft.STEPS:
            raise ValueError("Unknown X drafting step")
        run = await self.active(run_id)
        key = f"x-draft:{run.id}:{step}"
        async with self.db.effect_lock(key, x_draft.KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result
            await self.active(run.id, conn=conn)
            prepared = await self.saved(run.id, "prepare", conn=conn)
            if not prepared:
                raise ValueError("X drafting preparation is missing")
            await self.check_account(run, prepared)
            inputs = prepared["style_inputs"]
            if step == "compose":
                style = await self.saved(run.id, "style", conn=conn)
                if not style:
                    raise ValueError("X voice selection has not finished")
                if style.get("run_id"):
                    child = await self.db.get_run(UUID(style["run_id"]), conn=conn)
                    if (
                        not child
                        or child.project_id != run.project_id
                        or child.workflow_name != x_style.KEY
                        or child.status.value != "succeeded"
                    ):
                        raise ValueError("The X voice guide must finish review before drafting")
                if prepared["voice"] != "project_context":
                    project = await self.db.get_project(run.project_id, conn=conn)
                    if not x_draft.matching_guide(
                        await self.guide(project), prepared["account_id"]
                    ):
                        raise ValueError(
                            "The selected X guide changed or is missing. Start a new draft."
                        )
                inputs = {
                    name: run.input[name] for name in x_draft.COMPOSE_FIELDS if name in run.input
                }
                inputs["account_id"] = prepared["account_id"]
            if inputs is None:
                result = {"status": "skipped"}
            else:
                template = await self.db.get_registry_workflow(x_draft.STEPS[step])
                definition = prepared["definitions"][step]
                if template is None or template.executor != definition["executor"]:
                    raise ValueError("X drafting child executor changed")
                child = await start_workflow_run(
                    runtime=SimpleNamespace(
                        database=self.db, storage=self.storage, integrations=self.integrations
                    ),
                    settings=self.settings,
                    workflow=replace(template, definition=definition),
                    project_id=run.project_id,
                    started_by_clerk_user_id=run.started_by_clerk_user_id,
                    start_idempotency_key=key,
                    input_payload=inputs,
                    definition_commit_sha=prepared["definition_revision"],
                    input_schema=definition["input_schema"],
                    trigger_source=run.trigger_source,
                    trigger_client=run.trigger_client,
                    started_by_oauth_client_id=run.started_by_oauth_client_id,
                    _prepare_only=True,
                    _billing_parent_run_id=run.id if getattr(self.db, "billing", None) else None,
                )
                result = {
                    "run_id": str(child.id),
                    "executor": child.executor,
                    "temporal_workflow_id": child.temporal_workflow_id,
                }
            await self.db.project_run_progress(
                run_id=run.id,
                mode="steps",
                step=step,
                current=1 if step == "style" else 2,
                total=3,
                summary=(
                    "Learning your X voice. Review the guide in Decisions to continue drafting."
                    if step == "style" and inputs is not None
                    else "Drafting from project context; no personal X voice is available."
                    if prepared["voice"] == "project_context"
                    else "Drafting with your saved X voice."
                ),
            )
            await self.db.start_effect(conn, execution_key=key, operation=x_draft.KEY)
            await self.db.complete_effect(conn, execution_key=key, result=result)
        return result

    @activity.defn(name="x_draft_finish")
    async def finish(self, run_id: str):
        key = f"x-draft:{run_id}:complete"
        async with self.db.effect_lock(key, x_draft.KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return
            run = await self.active(run_id, conn=conn)
            saved = await self.saved(run.id, "compose", conn=conn)
            child = await self.db.get_run(UUID(saved["run_id"]), conn=conn) if saved else None
            template = await self.db.get_registry_workflow(x_draft.STEPS["compose"])
            if (
                not child
                or child.project_id != run.project_id
                or template is None
                or child.workflow_id != template.id
                or child.executor != "workflow.code"
                or child.status.value != "succeeded"
                or not child.canonical_commit_sha
                or not (child.artifact_path or "").startswith("social/x-drafts/")
                or not child.artifact_path.endswith(".json")
            ):
                raise ValueError("The X composition run has no completed draft")
            prepared = await self.saved(run.id, "prepare", conn=conn)
            summary = (
                "Your X draft is ready. Personal voice was unavailable; it uses project context."
                if prepared["voice"] == "project_context"
                else "Your X draft is ready to review and edit."
            )
            async with conn.transaction():
                await self.db.start_effect(conn, execution_key=key, operation=x_draft.KEY)
                await self.db._complete_readonly_report_projection(
                    conn,
                    execution_key=key,
                    run_id=run.id,
                    canonical_commit_sha=child.canonical_commit_sha,
                    artifact_path=child.artifact_path,
                    artifact_ref=child.artifact_ref,
                    summary=summary,
                    workflow_key=x_draft.KEY,
                )

    @activity.defn(name="x_draft_failure")
    async def failure(self, run_id: str):
        run = await self.db.get_run(UUID(run_id))
        if run and run.executor == x_draft.KEY:
            await self.db.project_failure(
                run_id=run.id,
                error_message=(
                    "X drafting could not finish. Check the voice or composition run in Activity. "
                    "Saved guides and drafts remain in Files."
                ),
            )
