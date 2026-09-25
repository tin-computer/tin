"""Draft progress is a projection of ordinary runs and their trusted selection receipts."""

from tin_lite.content_editorial_judgment import NO_DRAFT
from tin_lite.content_programs import decoded
from tin_lite.organic_audit import digest

ACTIVE = {"pending", "running", "paused"}
# A superseded copy keeps its outcome until a newer version of that item has output.
SETTLED = {"succeeded", "superseded"}


async def history(executor, *, project_id, program_id):
    # Prefer work in flight, then a saved article or completed assessment, then failure.
    # A failed rewrite must not hide a usable result. Assessments may supersede a draft's
    # relevance but never delete its artifact or resolve its pending approval. Legacy drafts have
    # the same item in their preparation receipt/input; no backfill or plan rewrite needed.
    # A superseded copy stays readable, so it ranks like any saved result: a failed or stopped
    # revision cannot hide it, and a newer version with output replaces it.
    # Delivery follows the approval-time choice when one exists, as ContentDelivery.intent does.
    rows = await executor.fetch(
        """
        SELECT DISTINCT ON (item_id) * FROM (
            SELECT run.id, run.status, run.created_at, run.canonical_commit_sha,
                   run.artifact_path, run.retained_output, run.output_resolution,
                   CASE WHEN choice.result IS NULL THEN selected.result->'delivery'
                        WHEN choice.result->'settings'->>'mode' IN ('github_pr','github_commit')
                        THEN choice.result END AS delivery_intent,
                   selected.result->'system_delivery' AS system_delivery,
                   parent.status AS system_status,
                   delivery.status AS delivery_status, delivery.result AS delivery_result,
                   delivery.error_message AS delivery_error,
                   COALESCE(published.result->'content_editorial',
                            persisted.result->'content_editorial') AS editorial,
                   COALESCE(selected.result->'item', prepared.result->'item') AS item,
                   COALESCE(selected.result->'item'->>'id', prepared.result->'item'->>'id',
                            NULLIF(run.input->>'item_id', '')) AS item_id
            FROM workflow_runs run JOIN workflows w ON w.id=run.workflow_id
            LEFT JOIN effect_receipts selected
              ON selected.execution_key='content-draft:' || run.id::text || ':selection'
             AND selected.operation='content_draft_selection_v1' AND selected.status='completed'
            LEFT JOIN effect_receipts choice
              ON choice.execution_key='content-draft:' || run.id::text || ':delivery:choice'
             AND choice.operation='content_draft_delivery_choice_v1' AND choice.status='completed'
            LEFT JOIN effect_receipts prepared
              ON prepared.execution_key='content-draft:' || run.id::text || ':prepare'
             AND prepared.operation='content.generate' AND prepared.status='completed'
            LEFT JOIN workflow_runs parent
              ON parent.id::text=selected.result->'system_delivery'->>'system_run_id'
             AND parent.project_id=run.project_id
            LEFT JOIN effect_receipts delivery
              ON delivery.execution_key='content-draft:' || run.id::text || ':delivery'
             AND delivery.operation='content_draft_delivery_v1'
            LEFT JOIN effect_receipts published
              ON published.execution_key=run.id::text || ':procedure_canonical_commit'
             AND published.operation='procedure_canonical_commit' AND published.status='completed'
             AND published.result->>'canonical_commit_sha'=run.canonical_commit_sha
            LEFT JOIN effect_receipts persisted
              ON persisted.execution_key=run.id::text || ':procedure_artifact_persist'
             AND persisted.operation='procedure_artifact_persist' AND persisted.status='completed'
            WHERE run.project_id=$1 AND w.key='content.generate' AND w.project_id IS NULL
              AND (run.status<>'superseded' OR run.canonical_commit_sha IS NOT NULL)
              AND run.input->>'program_id'=$2
        ) attempts WHERE item_id IS NOT NULL
        ORDER BY item_id,
            CASE WHEN status IN ('pending','running','paused') THEN 0
                 WHEN canonical_commit_sha IS NOT NULL OR retained_output IS NOT NULL THEN 1
                 ELSE 2 END,
            created_at DESC, id DESC
        """,
        project_id,
        str(program_id),
    )
    from tin_lite.content_repository_delivery import delivery_history

    repository_deliveries = await delivery_history(
        executor, project_id=project_id, source_ids=[str(row["id"]) for row in rows]
    )
    return {
        row["item_id"]: {
            "run_id": str(row["id"]),
            "status": row["status"],
            "stage": (
                "drafting"
                if row["status"] in ACTIVE
                else decoded(row["editorial"])["outcome"]
                if row["status"] in SETTLED
                and decoded(row["editorial"] or {}).get("outcome") in NO_DRAFT
                else "assessment_saved"
                if decoded(row["editorial"] or {}).get("outcome") in NO_DRAFT
                and decoded(row["output_resolution"] or {}).get("state") in {"applied", "kept"}
                else "awaiting_review"
                if row["status"] == "needs_input" and row["canonical_commit_sha"]
                else "drafted"
                if row["canonical_commit_sha"]
                or (
                    row["retained_output"]
                    and decoded(row["output_resolution"] or {}).get("state") in {"applied", "kept"}
                )
                else "saved_result"
                if row["retained_output"]
                else row["status"]
            ),
            "has_output": bool(row["canonical_commit_sha"] or row["retained_output"])
            and decoded(row["editorial"] or {}).get("outcome") not in NO_DRAFT,
            "assessment": decoded(row["editorial"] or {})
            if (
                row["status"] in SETTLED
                or decoded(row["output_resolution"] or {}).get("state") in {"applied", "kept"}
            )
            and decoded(row["editorial"] or {}).get("outcome") in NO_DRAFT
            else None,
            "artifact_path": row["artifact_path"],
            "output_source": "canonical" if row["canonical_commit_sha"] else "retained",
            "brief_sha256": digest(decoded(row["item"])) if row["item"] else None,
            "repository_delivery": repository_deliveries.get(str(row["id"])),
            "system_delivery": decoded(row["system_delivery"])
            if row["system_delivery"] and row["system_status"] in {"pending", "running"}
            else None,
            "delivery": (
                {
                    "status": row["delivery_status"]
                    or ("pending" if row["status"] == "succeeded" else "awaiting_review"),
                    "repository": decoded(row["delivery_intent"])["settings"]["repository"],
                    "path": decoded(row["delivery_intent"])["path"],
                    "pull_request": decoded(row["delivery_result"])
                    if row["delivery_result"]
                    else None,
                    "error": row["delivery_error"],
                }
                if row["delivery_intent"]
                and decoded(row["delivery_intent"])
                and decoded(row["editorial"] or {}).get("outcome") not in NO_DRAFT
                else None
            ),
        }
        for row in rows
    }


def check_existing(progress, *, rewrite=False, item=None):
    if not progress:
        return
    run_id = progress["run_id"]
    if progress.get("assessment"):
        if item and digest(item) != progress["brief_sha256"]:
            return
        if not rewrite:
            raise ValueError(
                f"This brief already has an editorial assessment. Open run {run_id}, "
                "revise the brief, or explicitly recheck it."
            )
    if progress["stage"] == "drafting":
        raise ValueError(
            f"Already drafting this article. Open run {run_id}; no duplicate was started."
        )
    if progress["stage"] == "saved_result":
        raise ValueError(f"This article has a saved result to resolve first. Open run {run_id}.")
    if progress["has_output"] and not rewrite:
        raise ValueError(
            f"This article already has a draft. Open run {run_id}, "
            "or explicitly choose to rewrite it.",
        )


def next_item(items):
    """A manual start may work ahead of a date, but never past an unfinished/held item."""
    for item in items:
        if item["readiness"] == "deferred":
            continue
        progress = item.get("draft") or {}
        if progress.get("stage") == "already_covered" and not item.get("brief_changed"):
            continue
        if progress.get("stage") in {"awaiting_review", "drafted"}:
            continue
        return item
    return None


async def guard_selection(conn, *, project_id, inputs, selection):
    """Recheck under create_run's program/project locks, before a run/budget is committed."""
    from uuid import UUID

    program_id = UUID(inputs["program_id"])
    configured = await conn.fetchrow(
        "SELECT c.status FROM project_workflows c JOIN workflows w ON w.id=c.workflow_id "
        "WHERE c.id=$1 AND c.project_id=$2 AND w.key='content.plan' AND c.status<>'archived'",
        program_id,
        project_id,
    )
    if not configured or configured["status"] != "active":
        raise ValueError(
            "Resume this content program before drafting, or choose an active program."
        )
    if selection["program_id"] != str(program_id) or selection["input_sha256"] != digest(inputs):
        raise ValueError("Draft selection does not match this request.")
    if inputs.get("item_id") and inputs["item_id"] != selection["item"]["id"]:
        raise ValueError("Draft selection does not match the requested article.")
    held = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM content_plan_revisions WHERE project_workflow_id=$1 "
        "AND project_id=$2 AND status IN ('pending','applying') AND batch_ids ? $3)",
        program_id,
        project_id,
        selection["batch_id"],
    )
    if held:
        raise ValueError("Finish the pending revision for this batch before drafting.")
    prior = await history(conn, project_id=project_id, program_id=program_id)
    if selection["mode"] == "next":
        for progress in prior.values():
            if progress["stage"] == "drafting":
                check_existing(progress)
    check_existing(
        prior.get(selection["item"]["id"]),
        rewrite=inputs.get("rewrite", False),
        item=selection["item"],
    )
