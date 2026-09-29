"""Exact approved run outputs, selected by the consumer before admission.

Only a supported run's primary, single text artifact is accepted. The published
checkpoint and the immutable canonical bytes are the source, never the current
file at the same path. This contract remains for historical pinned definitions.
"""

from __future__ import annotations

import json
from uuid import UUID

from tin_lite.domain import RunStatus
from tin_lite.procedures import PROJECT_ARTIFACT_RESULT, load_pinned_codex_procedure
from tin_lite.publication import OutputCheckpoint
from tin_lite.workflow_code import output_path_allowed, validate_code_definition
from tin_lite.workflow_packages import load_workflow_source
from tin_lite.workflow_review_store import digest

TEXT_MEDIA_TYPES = frozenset({"text/markdown", "text/plain", "text/csv", "application/json"})
MAX_TOTAL_BYTES = 128_000
MAX_SERIALIZED_BYTES = 256_000


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _check_approval(run, workflow_key, published, command):
    """Return the exact approval basis, including a review command when one exists."""
    from tin_lite.content_editorial_judgment import no_draft

    if no_draft(published or {}):
        raise ValueError("An editorial assessment is not an approved source.")
    if command is None:
        return {"kind": "run_review"}
    from tin_lite.workflow_reviews import SUPPORTED

    if published is None or workflow_key not in SUPPORTED:
        raise ValueError("The source has an unsupported review command.")
    checkpoint = published["checkpoint"]
    artifact = {
        "run_id": str(run.id),
        "path": run.artifact_path,
        "revision": run.canonical_commit_sha,
        "sha256": checkpoint["sha256"],
        "assessment": no_draft(published),
    }
    expected = digest({"run": str(run.id), "version": run.review_version, "artifact": artifact})
    saved_artifact = _json(command["artifact"])
    if (
        command["action"] != "approve"
        or saved_artifact != artifact
        or command["review_token"] != expected
    ):
        raise ValueError("The source review does not approve its published version.")
    return {"kind": "exact_version", "review_token": expected, "artifact": artifact}


async def _producer(database, storage, project, run, workflow):
    if workflow.project_id is not None and (
        workflow.project_id != project.id or workflow.definition_repo_id != project.state_repo_id
    ):
        raise ValueError("The source workflow belongs to another project.")
    if not run.definition_commit_sha or not workflow.definition_repo_id:
        raise ValueError("The source has no pinned producer definition.")
    if run.executor == "workflow.code":
        source = await load_workflow_source(
            storage=storage,
            repo_id=workflow.definition_repo_id,
            commit_sha=run.definition_commit_sha,
            definition_path=workflow.definition_path,
        )
        definition = source.definition
        spec = validate_code_definition(definition)
        if not output_path_allowed(spec, run.artifact_path, run.created_at):
            raise ValueError("The source path differs from its pinned code output contract.")
        maximum, media_type = spec.max_bytes, spec.media_type
    elif run.executor == "codex.procedure":
        pinned = await load_pinned_codex_procedure(
            storage=storage,
            repo_id=workflow.definition_repo_id,
            commit_sha=run.definition_commit_sha,
            definition_path=workflow.definition_path,
        )
        spec = pinned.resolve_inputs(run.input, started_at=run.created_at, run_id=run.id)
        if (
            spec.result_kind != PROJECT_ARTIFACT_RESULT
            or spec.documents is not None
            or spec.output_section is not None
            or spec.output_path != run.artifact_path
        ):
            raise ValueError("The source is not one supported procedure text artifact.")
        definition = {"key": pinned.workflow_key, "executor": run.executor}
        maximum, media_type = spec.output_max_bytes, spec.output_media_type
    else:
        raise ValueError("This source executor has no approved evidence adapter.")
    if (
        definition.get("key") != workflow.key
        or definition.get("executor") != run.executor
        or workflow.executor != run.executor
        or media_type not in TEXT_MEDIA_TYPES
    ):
        raise ValueError("The pinned producer is not a supported text workflow.")
    return maximum, media_type


async def select_one(*, database, storage, project_id, source_run_id, workflow_key, max_bytes):
    """Validate one selected result and return its bounded bytes plus provenance."""
    run = await database.get_run(UUID(str(source_run_id)))
    if run is None or run.project_id != project_id:
        raise ValueError("Choose an approved output from this project.")
    workflow = await database.get_workflow(run.workflow_id)
    if workflow is None or workflow.key != workflow_key:
        raise ValueError(f"Choose an approved {workflow_key} result.")
    if (
        run.status != RunStatus.SUCCEEDED
        or not run.review_required
        or run.review_decision != "approved"
        or not run.artifact_path
        or not run.canonical_commit_sha
    ):
        raise ValueError("Read and approve the completed source before using it.")
    project = await database.get_project(project_id)
    if project is None:
        raise LookupError("project not found")
    declared_maximum, media_type = await _producer(database, storage, project, run, workflow)
    receipt = await database.get_effect(f"{run.id}:procedure_canonical_commit")
    published = (
        receipt.result
        if receipt
        and receipt.status == "completed"
        and receipt.operation == "procedure_canonical_commit"
        else None
    )
    if (
        not isinstance(published, dict)
        or published.get("canonical_commit_sha") != run.canonical_commit_sha
        or published.get("artifact_path") != run.artifact_path
        or not isinstance(published.get("checkpoint"), dict)
    ):
        raise ValueError("The source publication proof is unavailable.")
    checkpoint = OutputCheckpoint.load(published["checkpoint"], run=run)
    if (
        checkpoint.companions
        or checkpoint.artifact_path != run.artifact_path
        or checkpoint.media_type != media_type
        or checkpoint.byte_count > min(max_bytes, declared_maximum)
    ):
        raise ValueError("The source output differs from its single-text contract or size limit.")
    command = await database.pool.fetchrow(
        "SELECT action, artifact, review_token FROM workflow_review_commands "
        "WHERE source_run_id=$1",
        run.id,
    )
    # The generic review gate writes run.review_decision; article revision reviews
    # additionally write one token-bound command. Both paths approve the saved copy.
    approval_basis = _check_approval(run, workflow.key, published, command)
    raw = await storage.read_canonical_artifact(
        repo_id=project.state_repo_id, commit_sha=run.canonical_commit_sha, path=run.artifact_path
    )
    checkpoint.validate_content(raw)
    if len(raw) > max_bytes:
        raise ValueError("The source exceeds this workflow's evidence size limit.")
    content = raw.decode("utf-8")
    if "\x00" in content:
        raise ValueError("The approved evidence is not ordinary text.")
    return {
        "present": True,
        "run_id": str(run.id),
        "workflow_key": workflow.key,
        "definition_commit_sha": run.definition_commit_sha,
        "definition_repo_id": workflow.definition_repo_id,
        "definition_path": workflow.definition_path,
        "path": run.artifact_path,
        "revision": run.canonical_commit_sha,
        "sha256": checkpoint.sha256,
        "media_type": media_type,
        "byte_count": len(raw),
        "title": run.artifact_title or workflow.title,
        "created_at": run.created_at.isoformat(),
        "content": content,
        "review_version": run.review_version,
        "approval_basis": approval_basis,
        "publication_checkpoint": checkpoint.to_dict(),
    }


def candidate_metadata(source):
    return {
        "run_id": source["run_id"],
        "title": source["title"],
        "workflow_key": source["workflow_key"],
        "artifact_path": source["path"],
        "revision": source["revision"],
        "created_at": source["created_at"],
    }


def source_metadata(snapshot):
    """A run projection: provenance only, never source text or internal checkpoint."""
    return {
        name: (
            {"present": False}
            if not source["present"]
            else {"present": True, **candidate_metadata(source), "sha256": source["sha256"]}
        )
        for name, source in snapshot["slots"].items()
    }
