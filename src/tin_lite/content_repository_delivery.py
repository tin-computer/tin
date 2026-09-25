"""Repository adaptation of an approved article, using the ordinary procedure runner.

The article is immutable input, not an invitation to draft again. Admission, usage,
execution isolation, checkpoints and GitHub effects remain the existing contracts.
"""

import hashlib
import json
import re
from dataclasses import asdict
from uuid import UUID

from tin_lite import content_draft
from tin_lite.content_delivery import DRAFT_WORKFLOW_ID, ContentDelivery, article_body
from tin_lite.domain import RunStatus
from tin_lite.integrations import GitHubRepositoryBinding

KEY = "content.deliver"
WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000036")
OPERATION = "content_repository_delivery_source_v1"
CHECK_COMMAND = "git diff --check"
RECOVERY_OPERATION = "content_repository_delivery_recovery_v1"


def recovery_key(run_id):
    return f"content-delivery:{UUID(str(run_id))}:recovery"


def status_projection(run, source, publication, recovery):
    pr = (
        recovery
        if recovery.get("url")
        else (
            {
                "url": publication["external_url"],
                "number": publication.get("pull_request_number"),
                "repository": source["binding"]["repository"],
            }
            if publication.get("external_url")
            else None
        )
    )
    return {
        "status": "completed"
        if pr
        else "failed"
        if run.status == RunStatus.FAILED
        else "started"
        if run.status == RunStatus.RUNNING
        else "pending",
        "repository": source["binding"]["repository"],
        "path": "Repository-adapted article",
        "source_run_id": source["source_run_id"],
        "run_id": str(run.id),
        "error": run.error_message if not pr else None,
        "pull_request": pr,
        "approval_label": None,
    }


def source_key(run_id):
    return f"content-delivery:{UUID(str(run_id))}:source"


def binding_from(source):
    return GitHubRepositoryBinding(
        **{**source["binding"], "connection_id": UUID(source["binding"]["connection_id"])}
    )


async def discover(database, project_id):
    rows = await database.pool.fetch(
        "SELECT r.id, COALESCE(p.result->'item'->>'title', 'Approved article') AS title "
        "FROM workflow_runs r LEFT JOIN effect_receipts p "
        "ON p.execution_key='content-draft:' || r.id::text || ':prepare' AND p.status='completed' "
        "WHERE r.project_id=$1 AND r.workflow_id=$2 "
        "AND r.status='succeeded' AND r.review_decision='approved' "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT 100",
        project_id,
        DRAFT_WORKFLOW_ID,
    )
    connection = await database.get_integration_connection(
        project_id=project_id, provider_key="infra.github"
    )
    return {
        "articles": [{"run_id": str(row["id"]), "title": row["title"]} for row in rows],
        "repository": connection.configuration.get("selected_repository")
        if connection and connection.status == "connected"
        else None,
    }


async def select_source(*, database, storage, integrations, project_id, inputs):
    run = await database.get_run(UUID(inputs["source_run_id"]))
    if not run or run.project_id != project_id or run.workflow_id != DRAFT_WORKFLOW_ID:
        raise ValueError("Choose an article draft from this project.")
    if run.status != RunStatus.SUCCEEDED or run.review_decision != "approved":
        raise ValueError("Read and approve this article in Tin before preparing its PR.")
    selected = await database.get_effect(content_draft.selection_key(run.id))
    # The approval-time choice, else the pinned intent: the one the exact publisher uses.
    if await ContentDelivery(database=database).intent(run):
        raise ValueError(
            "This article already has automatic delivery. Use its existing delivery action."
        )
    prepared = await database.get_effect(content_draft.receipt_key(run.id))
    publication = await database.get_effect(f"{run.id}:procedure_canonical_commit")
    if (
        not prepared
        or prepared.status != "completed"
        or not prepared.result
        or not publication
        or publication.status != "completed"
        or not publication.result
        or publication.result.get("canonical_commit_sha") != run.canonical_commit_sha
        or publication.result.get("artifact_path") != run.artifact_path
        or run.artifact_path != content_draft.PATH_TEMPLATE.format(run_id=run.id)
    ):
        raise ValueError("The approved article's publication proof is unavailable.")
    project = await database.get_project(project_id)
    raw = await storage.read_canonical_artifact(
        repo_id=project.state_repo_id, commit_sha=run.canonical_commit_sha, path=run.artifact_path
    )
    if len(raw) > 80_000:
        raise ValueError("The approved article exceeds its size limit.")
    article, title = article_body(raw, prepared.result)
    binding = await integrations.github_repository_binding(
        project_id=project_id, expected_repository=inputs["expected_repository"]
    )
    system_delivery = (selected.result or {}).get("system_delivery") if selected else None
    if system_delivery and system_delivery["mode"] == "github_pr":
        from tin_lite.organic_content import check_destination

        check_destination(system_delivery, binding)
    return {
        "source_run_id": str(run.id),
        "source_revision": run.canonical_commit_sha,
        "source_path": run.artifact_path,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "article": article,
        "article_sha256": hashlib.sha256(article.encode()).hexdigest(),
        "title": title,
        "item": prepared.result["item"],
        "program_id": prepared.result["program_id"],
        "due_date": prepared.result["due_date"],
        "binding": {**asdict(binding), "connection_id": str(binding.connection_id)},
    }


async def guard_source(conn, *, project_id, inputs, source):
    """Called under create_run's project lock, in the run/budget/receipt transaction."""
    approved = await conn.fetchval(
        "SELECT id FROM workflow_runs WHERE id=$1 AND project_id=$2 AND workflow_id=$3 "
        "AND status='succeeded' AND review_decision='approved' AND canonical_commit_sha=$4",
        UUID(source["source_run_id"]),
        project_id,
        DRAFT_WORKFLOW_ID,
        source["source_revision"],
    )
    if not approved or inputs["source_run_id"] != source["source_run_id"]:
        raise ValueError("The selected article is not approved for delivery.")
    duplicate = await conn.fetchval(
        "SELECT r.id FROM workflow_runs r JOIN effect_receipts s "
        "ON s.execution_key='content-delivery:' || r.id::text || ':source' "
        "AND s.operation=$4 AND s.status='completed' "
        "WHERE r.project_id=$1 AND r.workflow_id=$2 "
        "AND s.result->>'source_run_id'=$3 "
        "AND s.result->'binding'->>'repository_id'=$5 "
        "ORDER BY r.created_at DESC LIMIT 1",
        project_id,
        WORKFLOW_ID,
        source["source_run_id"],
        OPERATION,
        str(source["binding"]["repository_id"]),
    )
    if duplicate and inputs.get("retry_run_id") == str(duplicate):
        prior = await conn.fetchrow("SELECT status FROM workflow_runs WHERE id=$1", duplicate)
        external = await conn.fetchrow(
            "SELECT status FROM integration_call_receipts WHERE execution_key=$1",
            f"{duplicate}:procedure_pull_request",
        )
        checkpoint = await conn.fetchval(
            "SELECT true FROM effect_receipts WHERE execution_key=$1 AND status='completed'",
            f"{duplicate}:procedure_artifact_persist",
        )
        if prior["status"] != "failed" or external or checkpoint:
            raise ValueError("Reconcile the previous delivery before purchasing a new adaptation.")
        return
    if inputs.get("retry_run_id"):
        raise ValueError("Choose the latest failed adaptation for this article and repository.")
    if duplicate:
        raise ValueError(
            f"This article already has a delivery attempt. Open run {duplicate}; "
            "retry its saved delivery rather than drafting or paying again."
        )


async def saved_source(database, run_id):
    receipt = await database.get_effect(source_key(run_id))
    if not receipt or receipt.status != "completed" or not receipt.result:
        raise ValueError("The delivery's approved source binding is unavailable.")
    return receipt.result


async def retry_status(database, run):
    source = await saved_source(database, run.id)
    recovery = await database.get_effect(recovery_key(run.id))
    publication = await database.get_effect(f"{run.id}:procedure_canonical_commit")
    if (
        publication
        and publication.status == "completed"
        and (publication.result or {}).get("external_url")
    ):
        status = status_projection(run, source, publication.result, {})
        return status
    checkpoint = await database.get_effect(f"{run.id}:procedure_artifact_persist")
    if (
        not checkpoint
        or checkpoint.status != "completed"
        or not (checkpoint.result or {}).get("ephemeral_commit_sha")
    ):
        raise ValueError(
            "This attempt has no saved repository patch to deliver. "
            "No model call will be purchased by Retry delivery."
        )
    if run.status not in {RunStatus.FAILED, RunStatus.SUCCEEDED}:
        raise ValueError("Wait for this delivery attempt to finish before retrying delivery.")
    return {
        "status": recovery.status if recovery else "pending",
        "repository": source["binding"]["repository"],
        "run_id": str(run.id),
        "pull_request": recovery.result if recovery else None,
    }


async def recover_delivery(*, database, storage, integrations, run):
    """Member-requested retry of a frozen patch, without a new model run or approval."""
    await retry_status(database, run)
    key = recovery_key(run.id)
    async with database.effect_lock(key, RECOVERY_OPERATION) as (conn, existing):
        if existing and existing.status == "completed":
            return existing.result
        await database.start_effect(conn, execution_key=key, operation=RECOVERY_OPERATION)
        try:
            from tin_lite.integrations import GitHubFileChange
            from tin_lite.procedures import (
                load_pinned_codex_procedure,
                validate_procedure_pull_request,
            )

            workflow = await database.get_workflow(run.workflow_id)
            spec = await load_pinned_codex_procedure(
                storage=storage,
                repo_id=workflow.definition_repo_id,
                commit_sha=run.definition_commit_sha,
                definition_path=workflow.definition_path,
            )
            project = await database.get_project(run.project_id)
            persisted = await database.get_effect(f"{run.id}:procedure_artifact_persist")
            from tin_lite.procedures import procedure_checkpoint_path

            checkpoint = await storage.read_procedure_checkpoint(
                repo_id=project.state_repo_id,
                revision=persisted.result["ephemeral_commit_sha"],
                path=procedure_checkpoint_path(run.id),
            )
            source = await saved_source(database, run.id)
            approved = await database.get_run(UUID(source["source_run_id"]))
            if (
                not approved
                or approved.project_id != run.project_id
                or approved.status != RunStatus.SUCCEEDED
                or approved.review_decision != "approved"
                or approved.canonical_commit_sha != source["source_revision"]
            ):
                raise ValueError("The saved delivery no longer has an approved source.")
            manifest = validate_procedure_pull_request(checkpoint, spec=spec)
            proof = validate_copy(manifest, source)
            binding = binding_from(source)
            result = await integrations.github_create_pull_request(
                project_id=run.project_id,
                run_id=run.id,
                execution_key=f"{run.id}:procedure_pull_request",
                title=manifest["title"],
                body=manifest["body"],
                files=tuple(GitHubFileChange(**item) for item in manifest["files"]),
                base_branch=binding.default_branch,
                expected_base_sha=binding.head_sha,
                expected_binding=binding,
                allow_unrelated_base_advance=True,
            )
            result = {**asdict(result), **proof}
            async with conn.transaction():
                await database.complete_effect(conn, execution_key=key, result=result)
                await database.add_activity(
                    conn=conn,
                    run_id=run.id,
                    event_type="content_delivery_recovered",
                    audience="product",
                    summary=f"Article delivery confirmed as PR #{result['number']}.",
                    details={
                        "kind": "runs",
                        "external_url": result["url"],
                        "external_label": f"Review PR #{result['number']}",
                        "source_run_id": source["source_run_id"],
                    },
                    dedupe_key=f"{key}:ready",
                )
            return result
        except Exception:
            await database.fail_effect(
                conn,
                execution_key=key,
                error_message="Delivery could not be confirmed. "
                "The saved patch and article are unchanged.",
            )
            raise


def validate_copy(manifest, source):
    """Prove lossless article storage, not that a site's renderer displays every byte.

    A Markdown site stores the reviewed body directly. Component-based sites retain
    it as a JSON string literal consumed by their existing Markdown renderer. The
    adapter may not introduce a renderer dependency or rewrite the prose as JSX.
    Actual rendering/build checks are separate and must never be implied by this proof.
    """
    article = source["article"]
    if hashlib.sha256(article.encode()).hexdigest() != source["article_sha256"]:
        raise ValueError("The approved article proof is inconsistent.")
    if (
        manifest["repository"] != source["binding"]["repository"]
        or manifest["head_sha"] != source["binding"]["head_sha"]
    ):
        raise ValueError("The repository differs from the pinned delivery source.")
    matches = []
    for item in manifest["files"]:
        path, text = item["path"], item["content"]
        if path.rsplit("/", 1)[-1] in {
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
        }:
            raise ValueError("Article delivery cannot change dependencies.")
        if path.endswith((".md", ".mdx")):
            body = re.sub(r"\A---\r?\n.*?\r?\n---\r?\n", "", text, count=1, flags=re.S)
            if body.lstrip("\n") == article:
                matches.append(path)
        elif path.endswith((".tsx", ".jsx", ".js", ".ts", ".astro", ".vue", ".svelte", ".json")):
            # Decode complete JSON string tokens, not fragment matches in escaped
            # prose. This proves source storage, not runtime consumption/rendering.
            for token in re.findall(
                r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"', text
            ):
                if json.loads(token) == article:
                    matches.append(path)
                    break
    if len(matches) != 1:
        raise ValueError(
            "Keep the approved article unchanged in exactly one Markdown file "
            "or JSON string consumed by the site's Markdown renderer."
        )
    return {
        "article_path": matches[0],
        "article_sha256": source["article_sha256"],
        "copy_check": "exact_source_preserved",
        "build_check": "not_verified_by_tin",
    }


async def delivery_history(executor, *, project_id, source_ids):
    if not source_ids:
        return {}
    rows = await executor.fetch(
        "SELECT DISTINCT ON (s.result->>'source_run_id') r.id, r.status, r.error_message, "
        "r.progress_summary, s.result->>'source_run_id' AS source_id, "
        "s.result->'binding'->>'repository' AS repository, p.result AS publication, "
        "recovery.result AS recovery, checkpoint.result AS checkpoint "
        "FROM workflow_runs r JOIN effect_receipts s "
        "ON s.execution_key='content-delivery:' || r.id::text || ':source' "
        "AND s.operation=$3 AND s.status='completed' "
        "LEFT JOIN effect_receipts p "
        "ON p.execution_key=r.id::text || ':procedure_canonical_commit' "
        "AND p.status='completed' "
        "LEFT JOIN effect_receipts recovery "
        "ON recovery.execution_key='content-delivery:' || r.id::text || ':recovery' "
        "AND recovery.status='completed' "
        "LEFT JOIN effect_receipts checkpoint "
        "ON checkpoint.execution_key=r.id::text || ':procedure_artifact_persist' "
        "AND checkpoint.status='completed' WHERE r.project_id=$1 AND r.workflow_id=$2 "
        "AND s.result->>'source_run_id'=ANY($4::text[]) "
        "ORDER BY s.result->>'source_run_id', r.created_at DESC, r.id DESC",
        project_id,
        WORKFLOW_ID,
        OPERATION,
        list(source_ids),
    )
    from tin_lite.content_programs import decoded

    return {
        row["source_id"]: {
            "run_id": str(row["id"]),
            "status": row["status"],
            "repository": row["repository"],
            "summary": row["progress_summary"],
            "error": row["error_message"],
            "pull_request_url": decoded(row["publication"] or {}).get("external_url")
            or decoded(row["recovery"] or {}).get("url"),
            "has_checkpoint": bool(decoded(row["checkpoint"] or {}).get("ephemeral_commit_sha")),
        }
        for row in rows
    }
