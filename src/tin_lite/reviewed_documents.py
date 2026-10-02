"""Apply a declared document pair through Tin's existing review and write receipts."""

import hashlib
from uuid import NAMESPACE_URL, uuid4, uuid5

from tin_lite.capture_revisions import (
    EDITED,
    STALE,
    latest_revision,
    revised_checkpoint,
    revisions,
    summary,
)
from tin_lite.domain import RunStatus
from tin_lite.procedure_documents import resolve_run_path, validate_document
from tin_lite.procedures import validate_codex_procedure_definition
from tin_lite.publication import OutputCheckpoint, OutputConflictError
from tin_lite.workflow_packages import load_workflow_source
from tin_lite.workflow_review_store import ReviewConflict, accept_approval, digest, unpack


async def document_spec(database, storage, run):
    if run is None or run.executor != "codex.procedure":
        return None
    workflow = await database.get_workflow(run.workflow_id)
    if workflow is None or workflow.project_id is not None:
        return None
    if workflow.current_commit_sha == run.definition_commit_sha:
        definition = workflow.definition
    else:
        source = await load_workflow_source(
            storage=storage,
            repo_id=workflow.definition_repo_id,
            commit_sha=run.definition_commit_sha,
            definition_path=workflow.definition_path,
        )
        definition = source.definition
    if not (definition.get("procedure", {}).get("output", {}).get("apply_on_approval")):
        return None
    return validate_codex_procedure_definition(definition)


def entry_identity(entry):
    if entry is None:
        return None
    return {
        "mode": entry[0],
        "sha256": hashlib.sha256(entry[1]).hexdigest(),
        "bytes": len(entry[1]),
    }


class ReviewedDocuments:
    def __init__(self, *, database, storage):
        self.db, self.storage = database, storage

    async def source(self, run_id, actor):
        run = await self.db.get_run(run_id)
        if not run or not await self.db.has_project_access(
            project_id=run.project_id, clerk_user_id=actor
        ):
            raise LookupError("Review not found.")
        spec = await document_spec(self.db, self.storage, run)
        if not spec or not spec.documents or not run.review_required:
            raise ReviewConflict("This run does not apply reviewed documents.")
        return run, spec, await self.db.get_project(run.project_id)

    async def artifact(self, run, spec, project):
        receipt = await self.db.get_effect(f"{run.id}:procedure_canonical_commit")
        if not receipt or receipt.status != "completed" or not receipt.result:
            raise ReviewConflict("The document pair is not ready for review.")
        checkpoint = OutputCheckpoint.load(receipt.result["checkpoint"], run=run)
        pair = spec.documents.resolve(run.id, run.created_at)
        primary = resolve_run_path(spec.output_path_template, run.id, run.created_at)
        # A pair the founder's agent revised is reviewed at its latest revision commit.
        revised = await latest_revision(self.db, run.id) if spec.documents.agent_revision else None
        saved = revised["revision"] if revised else receipt.result.get("canonical_commit_sha")
        if (
            [p.artifact_path for p in checkpoint.files] != [primary, pair.companion_path]
            or run.artifact_path != primary
            or saved != run.canonical_commit_sha
        ):
            raise ReviewConflict("The saved pair does not match the reviewed contract.")
        if revised:
            checkpoint = revised_checkpoint(checkpoint, revised, run)
        files = []
        for item, destination, maximum in zip(
            checkpoint.files,
            pair.destinations,
            (spec.output_max_bytes, pair.companion_max_bytes),
            strict=True,
        ):
            raw = await self.storage.read_canonical_artifact(
                repo_id=project.state_repo_id,
                commit_sha=run.canonical_commit_sha,
                path=item.artifact_path,
            )
            item.validate_content(raw)
            validate_document(raw, maximum)
            before = await self.storage.read_output_destination(
                repo_id=project.state_repo_id,
                revision=checkpoint.source_base_sha,
                path=destination,
            )
            files.append(
                {
                    "path": item.artifact_path,
                    "sha256": item.sha256,
                    "destination": destination,
                    "expected": entry_identity(before),
                    "change": "new"
                    if before is None
                    else "unchanged"
                    if before[1] == raw
                    else "updated",
                }
            )
        result = {
            "run_id": str(run.id),
            "path": primary,
            "revision": run.canonical_commit_sha,
            "sha256": checkpoint.sha256,
            "checkpoint": checkpoint.to_dict(),
            "files": files,
        }
        if revised:  # Unrevised pairs keep the exact shape older approvals recorded.
            result["proposal_revision"] = revised["number"]
        return result

    async def check_current(self, project, artifact, *, revisable=False):
        repo = await self.storage.get_repo(project.state_repo_id)
        revision = await self.storage.head_sha(repo, project.canonical_branch)
        for item in artifact["files"]:
            current = await self.storage.read_output_destination(
                repo_id=project.state_repo_id,
                revision=revision,
                path=item["destination"],
            )
            if entry_identity(current) != item["expected"]:
                raise ReviewConflict(
                    f"{item['destination']} changed during review; no documents were applied."
                )
            proposal = await self.storage.read_output_destination(
                repo_id=project.state_repo_id,
                revision=revision,
                path=item["path"],
            )
            if proposal is None or hashlib.sha256(proposal[1]).hexdigest() != item["sha256"]:
                raise ReviewConflict(
                    EDITED
                    if revisable
                    else "A proposed document was edited. Use Files or start a new capture."
                )

    async def view(self, run_id, actor):
        run, spec, project = await self.source(run_id, actor)
        artifact = await self.artifact(run, spec, project)
        conflict = None
        revisable = spec.documents.agent_revision
        if run.review_decision is None:
            try:
                await self.check_current(project, artifact, revisable=revisable)
            except (ReviewConflict, OutputConflictError) as exc:
                conflict = str(exc)
        result = {
            "run_id": str(run.id),
            "project_id": str(run.project_id),
            "root_run_id": str(run.id),
            "current_run_id": str(run.id),
            "version": run.review_version,
            "status": run.status.value,
            "artifact": artifact,
            "review_token": digest({"run": str(run.id), "artifact": artifact}),
            "can_request_changes": False,
            "can_approve": run.status == RunStatus.NEEDS_INPUT
            and run.review_decision is None
            and not conflict,
            "is_current": True,
            "versions": [],
            "feedback": None,
            "change_summary": None,
            "documents": artifact["files"],
            "conflict": conflict,
            "review_url": f"/document/{run.id}?project={run.project_id}&return=decisions",
        }
        if revisable:
            result["proposal_revisions"] = summary(await revisions(self.db, run.id), actor)
        if spec.output_validator == "brand-design-capture.v1":
            from tin_lite.brand_contract import tokens

            raw = await self.storage.read_canonical_artifact(
                repo_id=project.state_repo_id,
                commit_sha=run.canonical_commit_sha,
                path=run.artifact_path,
            )
            result["palette_preview"] = tokens(raw.decode())["light"]
        return result

    async def approve(self, *, run_id, actor, token):
        run, spec, project = await self.source(run_id, actor)
        existing = await self.db.pool.fetchrow(
            "SELECT * FROM workflow_review_commands WHERE source_run_id=$1",
            run.id,
        )
        if existing:
            if existing["action"] == "approve" and token == existing["review_token"]:
                return run
            raise ReviewConflict("This run already has a different review decision.")
        artifact = await self.artifact(run, spec, project)
        expected = digest({"run": str(run.id), "artifact": artifact})
        if token != expected:
            raise ReviewConflict(
                STALE
                if token is not None and "proposal_revision" in artifact
                else "Read both documents before approving this exact pair."
            )
        await self.check_current(project, artifact, revisable=spec.documents.agent_revision)
        command = {
            "id": uuid4(),
            "project_id": run.project_id,
            "request_id": uuid5(NAMESPACE_URL, f"tin:documents:{run.id}:{expected}"),
            "actor_clerk_user_id": actor,
            "source_run_id": run.id,
            "root_run_id": run.id,
            "artifact_run_id": run.id,
            "coordinator_run_id": run.id,
            "action": "approve",
            "request_digest": digest({"approve": str(run.id), "token": expected}),
            "review_token": expected,
            "artifact": artifact,
        }
        await accept_approval(self.db, command)
        return await self.db.get_run(run.id)

    async def apply(self, run_id):
        key = f"{run_id}:procedure_document_apply"
        async with self.db.effect_lock(key, "procedure_document_apply") as (conn, saved):
            if saved and saved.status == "completed":
                return
            row = await conn.fetchrow(
                "SELECT * FROM workflow_review_commands "
                "WHERE source_run_id=$1 AND action='approve'",
                run_id,
            )
            if not row:
                raise ReviewConflict(
                    "Document application requires an accepted exact-pair approval."
                )
            command = unpack(row)
            run = await self.db.get_run(run_id)
            spec = await document_spec(self.db, self.storage, run)
            if spec is None:
                raise ReviewConflict("The reviewed document contract is unavailable.")
            project = await self.db.get_project(run.project_id)
            artifact = command["artifact"]
            expected = await self.artifact(run, spec, project)
            if (
                expected != artifact
                or digest({"run": str(run.id), "artifact": artifact}) != command["review_token"]
            ):
                raise ReviewConflict("The approved document pair no longer matches its proof.")
            checkpoint = OutputCheckpoint.load(artifact["checkpoint"], run=run)
            intent = (saved.result or {}).get("publication") if saved else None

            async def authority():
                current = await self.db.get_run(run.id)
                if (
                    current.status != RunStatus.RUNNING
                    or current.review_decision != "approved"
                    or not await self.db.has_project_access(
                        project_id=run.project_id, clerk_user_id=command["actor_clerk_user_id"]
                    )
                ):
                    raise ReviewConflict("Document application is no longer authorized.")
                await self.check_current(project, artifact)

            async def save_intent(value):
                await self.db.save_publication_intent(conn, execution_key=key, intent=value)

            await self.db.start_effect(
                conn, execution_key=key, operation="procedure_document_apply"
            )
            async with self.db.project_state_lock(conn, project.id):
                revision, changed = await self.storage.apply_reviewed_documents(
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    checkpoint=checkpoint,
                    proposal_revision=artifact["revision"],
                    destinations=spec.documents.destinations,
                    execution_key=key,
                    intent=intent,
                    save_intent=save_intent,
                    validate_authority=authority,
                )
                async with conn.transaction():
                    await self.db.complete_effect(
                        conn,
                        execution_key=key,
                        result={
                            "revision": revision,
                            "changed": changed,
                            "command_id": str(command["id"]),
                            "artifact": artifact,
                            "actor": command["actor_clerk_user_id"],
                        },
                    )
                    await self.db.add_activity(
                        run_id=run.id,
                        event_type="procedure_documents_applied",
                        summary="The approved project documents are now in use.",
                        audience="product",
                        details={"revision": revision},
                        dedupe_key=f"{key}:applied",
                    )
