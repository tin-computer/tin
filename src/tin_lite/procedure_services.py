"""Procedure access to the existing service gateway, authorized by a fenced run grant."""

from dataclasses import replace

from tin_lite.code_models import CodeModelError, CodeModels
from tin_lite.code_services import CodeServices
from tin_lite.domain import CODEX_PROCEDURE_EXECUTOR
from tin_lite.procedures import validate_codex_procedure_definition
from tin_lite.workflow_packages import load_workflow_source

SERVICE_PROVIDER = "tin.services"
SERVICE_CAPABILITY = "services.call"


class ProcedureServices:
    def __init__(self, *, database, storage, integrations, settings, managed=None):
        self.db, self.storage = database, storage
        self.integrations, self.settings = integrations, settings
        self.managed = managed  # Tests pass a ManagedServices with recorded responses.

    async def call(self, *, token, payload):
        async with self.db.pool.acquire() as conn:
            grant = await self.db.authorize_run_tool_grant(
                token=token, capability=SERVICE_CAPABILITY, conn=conn
            )
            if grant is None or grant.provider_key != SERVICE_PROVIDER:
                raise PermissionError("This run does not grant service access.")
            run = await self.db.get_run(grant.run_id, conn=conn)
            if run is None or run.executor != CODEX_PROCEDURE_EXECUTOR:
                raise PermissionError("This run does not grant procedure service access.")
            workflow = await self.db.get_workflow(run.workflow_id, conn=conn)
            project = await self.db.get_project(run.project_id, conn=conn)
            if (
                workflow is None
                or project is None
                or workflow.executor != CODEX_PROCEDURE_EXECUTOR
                or not run.definition_commit_sha
                or (
                    workflow.project_id is not None
                    and (
                        workflow.project_id != run.project_id
                        or workflow.definition_repo_id != project.state_repo_id
                    )
                )
            ):
                raise PermissionError("The procedure source is unavailable to this project.")
            source = await load_workflow_source(
                storage=self.storage,
                repo_id=workflow.definition_repo_id,
                commit_sha=run.definition_commit_sha,
                definition_path=workflow.definition_path,
            )
            if source.definition.get("key") != workflow.key:
                raise PermissionError("The procedure source identity changed.")
            spec = validate_codex_procedure_definition(source.definition)
            workflow = replace(
                workflow, definition=source.definition, current_commit_sha=run.definition_commit_sha
            )
            authority = CodeModels(database=self.db, router=None, settings=self.settings)

            async def authorize(**kwargs):
                # Check again under the gateway lock, including cached responses. A queued
                # request must not outlive its grant, generation, membership or private gate.
                current = await self.db.authorize_run_tool_grant(
                    token=token, capability=SERVICE_CAPABILITY, conn=conn
                )
                if current is None or current != grant:
                    raise CodeModelError("model_access_revoked")
                await authority.authorize(**kwargs)

            # Preserve code_service_call_v1 and its fingerprints/recovery semantics. Both
            # executors use the same provider checks, receipts and external usage records.
            return await CodeServices(
                database=self.db,
                integrations=self.integrations,
                authorize=authorize,
                settings=self.settings,
                managed=self.managed,
            ).call(conn=conn, run=run, workflow=workflow, spec=spec, payload=payload)
