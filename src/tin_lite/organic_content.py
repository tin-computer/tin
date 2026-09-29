"""The fixed system's delivery intent, shared with review and admission.

No new publisher: approved copy still travels through content.deliver.
"""

from dataclasses import asdict
from uuid import UUID

from tin_lite import content_draft, organic_system


async def pin_destination(database, integrations, run):
    if run.input.get("content_delivery") != "auto":
        return {"mode": "draft_only", "reason": "draft_only_selected"}
    connection = await database.get_integration_connection(
        project_id=run.project_id, provider_key="infra.github"
    )
    if connection is None or connection.status != "connected":
        return {"mode": "draft_only", "reason": "github_not_connected"}
    repository = connection.configuration.get("selected_repository")
    if not repository:
        return {"mode": "draft_only", "reason": "github_repository_not_selected"}
    # Once configured, missing access is an actionable error, not silent draft-only delivery.
    binding = await integrations.github_repository_binding(
        project_id=run.project_id,
        expected_repository=run.input.get("expected_repository") or repository,
    )
    return {
        "mode": "github_pr",
        "binding": {**asdict(binding), "connection_id": str(binding.connection_id)},
        "system_run_id": str(run.id),
    }


async def draft_intent(database, *, parent_id, project_id, actor, selected):
    parent = await database.get_run(parent_id)
    prepared = await database.get_effect(f"traffic:{parent_id}:prepare")
    content = await database.get_effect(f"traffic:{parent_id}:step:content")
    if (
        not parent
        or parent.project_id != project_id
        or parent.executor != organic_system.KEY
        or parent.status.value != "running"
        or parent.started_by_clerk_user_id != actor
        or not prepared
        or prepared.status != "completed"
        or not organic_system.drafts_articles(prepared.result.get("policy"))
        or not content
        or content.status != "completed"
    ):
        raise ValueError("The organic system's draft authority is unavailable.")
    plan_run = await database.get_run(UUID(content.result["run_id"]))
    if (
        not plan_run
        or plan_run.project_id != project_id
        or str(plan_run.project_workflow_id) != selected["program_id"]
    ):
        raise ValueError("The draft does not belong to this system's content program.")
    return {**prepared.result["content_delivery"], "system_run_id": str(parent_id)}


async def intent_for(database, run):
    selected = await database.get_effect(content_draft.selection_key(run.id))
    return (
        (selected.result or {}).get("system_delivery")
        if selected and selected.status == "completed"
        else None
    )


async def project_review_progress(database, run):
    intent = await intent_for(database, run)
    if not intent or not intent.get("system_run_id"):
        return
    parent_id = UUID(intent["system_run_id"])
    parent = await database.get_run(parent_id)
    if not parent or parent.project_id != run.project_id or parent.status.value != "running":
        return
    await database.project_run_progress(
        run_id=parent_id,
        mode="steps",
        current=4,
        total=6,
        step="review",
        summary="Review the article to continue to its GitHub PR."
        if intent["mode"] == "github_pr"
        else "Review the article saved in Tin.",
    )


def check_destination(intent, binding):
    pinned = intent["binding"]
    if any(
        str(pinned[key]) != str(getattr(binding, key))
        for key in (
            "connection_id",
            "repository_id",
            "installation_id",
            "repository",
        )
    ):
        raise ValueError(
            "The connected repository changed after draft review was configured. "
            "The approved Markdown remains in Tin; delivery needs attention."
        )


def review_status(run, intent, result=None):
    result = result or {}
    return {
        "repository": intent["binding"]["repository"],
        "path": "Repository-adapted article",
        "status": result.get("status")
        or ("pending" if run.review_decision == "approved" else "awaiting_review"),
        "error": result.get("error"),
        "pull_request": result.get("pull_request"),
        "approval_label": "Approve & open PR",
        "system_run_id": intent["system_run_id"],
        "delivery_run_id": result.get("run_id"),
    }


async def delivery_status(database, run, intent):
    from tin_lite.content_delivery import choice_key
    from tin_lite.content_repository_delivery import recovery_key, saved_source, status_projection

    choice = await database.get_effect(choice_key(run.id))
    if choice and choice.status == "completed" and choice.result.get("mode") == "none":
        return None
    receipt = await database.get_effect(f"traffic:{intent['system_run_id']}:step:delivery")
    result = {}
    if receipt and receipt.status == "completed":
        step = receipt.result or {}
        if step.get("run_id"):
            child = await database.get_run(UUID(step["run_id"]))
            if child and child.project_id == run.project_id:
                source = await saved_source(database, child.id)
                publication = await database.get_effect(f"{child.id}:procedure_canonical_commit")
                recovery = await database.get_effect(recovery_key(child.id))
                result = status_projection(
                    child,
                    source,
                    publication.result if publication and publication.status == "completed" else {},
                    recovery.result if recovery and recovery.status == "completed" else {},
                )
                if child.status.value == "stopped" and not result.get("pull_request"):
                    result.update(status="failed", error="Article PR preparation was stopped.")
        elif step.get("status") in {"blocked", "failed"}:
            result = {
                "status": "failed",
                "error": "Article delivery needs attention. The Markdown draft remains in Tin.",
            }
    parent = await database.get_run(UUID(intent["system_run_id"]))
    if (
        parent
        and parent.status.value not in {"pending", "running"}
        and run.review_decision != "approved"
    ):
        # Rechecking a completed assessment is a separate user-directed run. Its
        # approval cannot promise continuation by an already-finished parent.
        return None
    if not result and parent and parent.status.value in {"failed", "stopped"}:
        result = {
            "status": "failed",
            "error": "The organic system stopped before PR delivery. "
            "The Markdown draft remains in Tin.",
        }
    return review_status(run, intent, result)
