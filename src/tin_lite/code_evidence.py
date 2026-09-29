"""Legacy approved-output inputs; new code workflows read project files directly."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from tin_lite import approved_evidence
from tin_lite.publication import OutputCheckpoint
from tin_lite.workflow_code import evidence_specs

OPERATION = "code_approved_evidence_v1"


def source_key(run_id):
    return f"code-evidence:{UUID(str(run_id))}:source"


def _bounded(snapshot):
    slots = snapshot.get("slots") if isinstance(snapshot, dict) else None
    if not isinstance(slots, dict):
        raise ValueError("The approved evidence snapshot is invalid.")
    total = 0
    for source in slots.values():
        if not isinstance(source, dict) or type(source.get("present")) is not bool:
            raise ValueError("The approved evidence slot is invalid.")
        if source["present"]:
            content = source.get("content")
            if not isinstance(content, str):
                raise ValueError("The approved evidence text is unavailable.")
            raw = content.encode("utf-8")
            if len(raw) != source.get("byte_count") or hashlib.sha256(
                raw
            ).hexdigest() != source.get("sha256"):
                raise ValueError("The approved evidence differs from its saved digest.")
            total += len(raw)
    if (
        total > approved_evidence.MAX_TOTAL_BYTES
        or len(json.dumps(snapshot, ensure_ascii=True, sort_keys=True).encode())
        > approved_evidence.MAX_SERIALIZED_BYTES
    ):
        raise ValueError("Selected evidence exceeds the shared context limit.")
    return total


def bound_context(snapshot, article_source=None):
    """Bound all source text when the legacy article and new slots coexist."""
    total = _bounded(snapshot)
    if article_source is not None:
        if not isinstance(article_source, dict) or not isinstance(
            article_source.get("article"), str
        ):
            raise ValueError("The approved article context is invalid.")
        total += len(article_source["article"].encode("utf-8"))
        style = article_source.get("style")
        if isinstance(style, dict) and style.get("content") is not None:
            if not isinstance(style["content"], str):
                raise ValueError("The approved writing style context is invalid.")
            total += len(style["content"].encode("utf-8"))
    if (
        total > approved_evidence.MAX_TOTAL_BYTES
        or len(
            json.dumps(
                {"evidence": snapshot["slots"], "approved_article": article_source},
                ensure_ascii=True,
                sort_keys=True,
            ).encode()
        )
        > approved_evidence.MAX_SERIALIZED_BYTES
    ):
        raise ValueError("Combined approved source context exceeds its size limit.")


async def select(*, database, storage, project_id, definition, inputs):
    slots = evidence_specs(definition)
    if not slots:
        raise ValueError("This workflow has no approved evidence slots.")
    selected = {}
    for slot in slots:
        source_run_id = inputs.get(slot.input)
        if source_run_id is None:
            if slot.required:
                raise ValueError(f"Choose an approved {slot.workflow_key} result for {slot.name}.")
            selected[slot.name] = {"present": False}
            continue
        selected[slot.name] = await approved_evidence.select_one(
            database=database,
            storage=storage,
            project_id=project_id,
            source_run_id=source_run_id,
            workflow_key=slot.workflow_key,
            max_bytes=slot.max_bytes,
        )
    snapshot = {"version": 1, "slots": selected}
    _bounded(snapshot)
    return snapshot


async def guard(database, conn, *, project_id, definition, inputs, snapshot, article_source=None):
    """Run under the project's admission row lock, before run and budget insertion."""
    slots = evidence_specs(definition)
    if not slots:
        if snapshot is not None:
            raise ValueError("This workflow does not declare approved evidence.")
        return
    if (
        snapshot is None
        or snapshot.get("version") != 1
        or set(snapshot.get("slots", {})) != {slot.name for slot in slots}
    ):
        raise ValueError("Select approved evidence before creating this run.")
    bound_context(snapshot, article_source)
    for slot in slots:
        source = snapshot["slots"][slot.name]
        selected = inputs.get(slot.input)
        if selected is None:
            if slot.required or source != {"present": False}:
                raise ValueError("The evidence selection changed before admission.")
            continue
        if not source["present"] or source.get("run_id") != str(UUID(str(selected))):
            raise ValueError("The evidence selection changed before admission.")
        if (
            source.get("workflow_key") != slot.workflow_key
            or source.get("byte_count", 0) > slot.max_bytes
        ):
            raise ValueError("The evidence differs from its declaration.")
        # Review writes can lock the source run independently of the project row.
        # Hold its version stable until this admission transaction commits.
        locked = await conn.fetchval(
            "SELECT id FROM workflow_runs WHERE id=$1 AND project_id=$2 FOR SHARE",
            UUID(source["run_id"]),
            project_id,
        )
        run = await database.get_run(locked, conn=conn) if locked is not None else None
        if (
            run is None
            or run.project_id != project_id
            or run.status.value != "succeeded"
            or not run.review_required
            or run.review_decision != "approved"
            or run.review_version != source.get("review_version")
            or run.definition_commit_sha != source.get("definition_commit_sha")
            or run.created_at.isoformat() != source.get("created_at")
            or run.artifact_path != source.get("path")
            or run.canonical_commit_sha != source.get("revision")
        ):
            raise ValueError("The selected output's approval or revision changed.")
        workflow = await database.get_workflow(run.workflow_id, conn=conn)
        if (
            workflow is None
            or workflow.key != slot.workflow_key
            or workflow.executor != run.executor
            or workflow.definition_repo_id != source.get("definition_repo_id")
            or workflow.definition_path != source.get("definition_path")
            or (workflow.project_id is not None and workflow.project_id != project_id)
        ):
            raise ValueError("The selected output's producer changed.")
        receipt = await database.get_effect(f"{run.id}:procedure_canonical_commit", conn=conn)
        published = (
            receipt.result
            if receipt
            and receipt.status == "completed"
            and receipt.operation == "procedure_canonical_commit"
            else None
        )
        if (
            not isinstance(published, dict)
            or published.get("canonical_commit_sha") != source["revision"]
            or published.get("artifact_path") != source["path"]
            or published.get("checkpoint") != source.get("publication_checkpoint")
        ):
            raise ValueError("The selected output's publication proof changed.")
        checkpoint = OutputCheckpoint.load(published["checkpoint"], run=run)
        if (
            checkpoint.companions
            or checkpoint.sha256 != source["sha256"]
            or checkpoint.byte_count != source["byte_count"]
            or checkpoint.media_type != source["media_type"]
        ):
            raise ValueError("The selected output's checkpoint changed.")
        command = await conn.fetchrow(
            "SELECT action, artifact, review_token FROM workflow_review_commands "
            "WHERE source_run_id=$1",
            run.id,
        )
        if approved_evidence._check_approval(run, workflow.key, published, command) != source.get(
            "approval_basis"
        ):
            raise ValueError("The selected output's review proof changed.")


async def saved_source(database, run, spec):
    receipt = await database.get_effect(source_key(run.id))
    if (
        receipt is None
        or receipt.status != "completed"
        or receipt.operation != OPERATION
        or not isinstance(receipt.result, dict)
        or receipt.result.get("version") != 1
    ):
        raise ValueError("The run's approved evidence snapshot is unavailable.")
    snapshot = receipt.result
    if set(snapshot.get("slots", {})) != {slot.name for slot in spec.evidence}:
        raise ValueError("The run's approved evidence slots differ from its pinned definition.")
    _bounded(snapshot)
    for slot in spec.evidence:
        source = snapshot["slots"][slot.name]
        chosen = (run.input or {}).get(slot.input)
        if chosen is None:
            if slot.required or source != {"present": False}:
                raise ValueError("The run's optional evidence differs from its input.")
        elif (
            not source["present"]
            or source.get("run_id") != str(UUID(str(chosen)))
            or source.get("workflow_key") != slot.workflow_key
            or source.get("byte_count", 0) > slot.max_bytes
        ):
            raise ValueError("The run's evidence differs from its input.")
    return {
        name: {
            key: value
            for key, value in source.items()
            if key not in {"publication_checkpoint", "definition_repo_id", "definition_path"}
        }
        for name, source in snapshot["slots"].items()
    }
