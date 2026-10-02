"""Retain bounded, incomplete text separately from publishable procedure checkpoints."""

import hashlib
from dataclasses import replace

from tin_lite.domain import StaleGenerationError
from tin_lite.procedures import (
    ANALYTICS_BRIEF_VALIDATOR,
    MEMORY_SECTION_VALIDATOR,
    settle_procedure_artifact,
    validate_procedure_artifact,
)
from tin_lite.publication import OutputCheckpoint, PublicationPendingError

OPERATION = "procedure_interrupted_output"


def eligible(spec):
    return (
        spec.result_kind == "project.artifact"
        and spec.sandbox.profile in {"default", "isolated"}
        and spec.output_media_type == "text/markdown"
        and spec.output_validator in {None, MEMORY_SECTION_VALIDATOR, ANALYTICS_BRIEF_VALIDATOR}
        and not spec.identity.enabled
        and spec.companion_path is None
        and 0 < spec.output_max_bytes <= 1_000_000
    )


def _partial(spec):
    # A partial analytics brief has no finished evidence block yet and is never published,
    # so only its size and encoding are checked.
    if spec.output_validator == ANALYTICS_BRIEF_VALIDATOR:
        return replace(spec, output_validator=None)
    return spec


async def retain(*, db, conn, storage, run, project, spec, base, content=None):
    """With no bytes, reconcile a previous write only. Never run or publish a draft."""
    if not eligible(spec):
        return
    key = f"{run.id}:{OPERATION}:{run.generation}"
    branch = f"interrupted-procedures/{run.id}/{run.generation}"
    async with db.effect_lock(key, OPERATION, conn=conn) as (conn, existing):
        if existing is None and content is None:
            return
        await _lease(db, conn, run)
        intent = existing.result if existing else None
        if content is not None:
            # A section-owning draft keeps only its own section, as a finished output does.
            content = settle_procedure_artifact(content, spec=spec, base=base)
            validate_procedure_artifact(content, spec=_partial(spec), base=base)
            if content == base:
                return
            candidate = {
                "version": 1,
                "run_id": str(run.id),
                "project_id": str(run.project_id),
                "generation": run.generation,
                "definition_commit_sha": run.definition_commit_sha,
                "source_base_sha": run.expected_head_sha,
                "artifact_path": spec.output_path,
                "media_type": spec.output_media_type,
                "sha256": hashlib.sha256(content).hexdigest(),
                "byte_count": len(content),
            }
            if intent and any(intent.get(k) != v for k, v in candidate.items()):
                raise ValueError("interrupted output differs from its saved intent")
            if not intent:
                intent = candidate
                await db.start_effect(conn, execution_key=key, operation=OPERATION)
                await db.save_effect_progress(conn, execution_key=key, result=intent)
        revision = intent.get(
            "ephemeral_commit_sha"
        ) or await storage.procedure_checkpoint_revision(
            repo_id=project.state_repo_id, branch=branch
        )
        if revision is None:
            if content is None:
                return  # No acknowledged or discoverable write; no bytes can be invented.
            repo = await storage.get_repo(project.state_repo_id)
            await _lease(db, conn, run)
            try:
                result = (
                    await repo.create_commit(
                        target_branch=branch,
                        base_branch=project.canonical_branch,
                        ephemeral=True,
                        ephemeral_base=False,
                        commit_message=f"Interrupted procedure {run.id}",
                        author={"name": "Tin Switchboard", "email": "switchboard@tin.local"},
                        ttl=300,
                    )
                    .add_file(spec.output_path, content)
                    .send()
                )
                revision = result["commit_sha"]
            except Exception:
                raise PublicationPendingError(
                    "interrupted output write requires recovery"
                ) from None
        checkpoint = OutputCheckpoint.load({**intent, "ephemeral_commit_sha": revision}, run=run)
        if (
            checkpoint.artifact_path != spec.output_path
            or checkpoint.media_type != spec.output_media_type
        ):
            raise ValueError("interrupted output does not match its pinned contract")
        saved = await storage.read_procedure_checkpoint(
            repo_id=project.state_repo_id, revision=revision, path=spec.output_path
        )
        checkpoint.validate_content(saved)
        validate_procedure_artifact(saved, spec=_partial(spec), base=base)
        # Serialize projection with stop/replacement. No canonical file is written.
        async with conn.transaction():
            await conn.fetchval("SELECT id FROM workflow_runs WHERE id=$1 FOR UPDATE", run.id)
            await _lease(db, conn, run)
            await db.retain_procedure_output(
                conn,
                run_id=run.id,
                checkpoint=checkpoint.to_dict(),
                reason="execution_interrupted",
                only_if_missing=True,
            )
            await db.complete_effect(conn, execution_key=key, result=checkpoint.to_dict())


async def _lease(db, conn, run):
    if not await db.validate_lease(
        project_id=run.project_id,
        thread_id=run.thread_id,
        generation=run.generation,
        lease_owner=run.lease_owner,
        fencing_token=run.fencing_token,
        sandbox_id=run.sandbox_id,
        conn=conn,
    ):
        raise StaleGenerationError("interrupted output no longer owns its run lease")
