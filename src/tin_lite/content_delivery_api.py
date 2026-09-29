"""HTTP and MCP share these delivery settings and durable retry operations."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from tin_lite.auth import AuthContext, require_user
from tin_lite.content_delivery import WORKFLOW, ContentDelivery, DeliverySettings
from tin_lite.domain import RunStatus
from tin_lite.integrations import IntegrationError
from tin_lite.project_files import ProjectFileError

router = APIRouter(prefix="/api/projects/{project_id}")
USER = Depends(require_user)


class SaveDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    settings: DeliverySettings


def delivery_service(runtime):
    return ContentDelivery(
        database=runtime.database,
        storage=getattr(runtime, "storage", None),
        integrations=getattr(runtime, "integrations", None),
    )


def page_url_service(runtime, settings=None):
    from tin_lite.page_urls import PageUrls

    return PageUrls(
        database=runtime.database,
        storage=getattr(runtime, "storage", None),
        integrations=getattr(runtime, "integrations", None),
        settings=settings,
    )


def adapt_on_approval(settings, run):
    """Whether an approval's repository pick for this run starts content.deliver."""
    from tin_lite.content_delivery import adaptable

    return adaptable(settings, run)


async def delivery_cost(*, runtime, settings, run, actor, repository):
    """The configured cost preview of adapting this page, or None where billing is off."""
    from tin_lite.billing import BillingService
    from tin_lite.billing_contracts import BillingError
    from tin_lite.content_repository_delivery import WORKFLOW_ID

    try:
        preview = await BillingService(database=runtime.database, settings=settings).quote(
            runtime=runtime,
            project_id=run.project_id,
            actor=actor,
            workflow_id=WORKFLOW_ID,
            inputs={"source_run_id": str(run.id), "expected_repository": repository},
            preview_only=True,
        )
    except (BillingError, LookupError, ValueError):
        return None
    if not preview.get("enabled"):
        return None
    return {
        "estimated_usd": preview["estimated_usd"],
        "maximum_usd": preview["maximum_usd"],
        "notice": preview.get("notice"),
    }


async def publish_preview(*, runtime, settings, run, actor):
    """What Publish does for a page Tin adapts to the site, before the founder presses it.

    `adapt` is False where approval keeps today's choices: no selected repository, no
    Codex API execution, or a run that is not an answer page or public article. `mode` is
    the delivery the founder saved (commit to main, else a pull request); `footer` is the
    card's one line, with the configured cost preview when billing is on.
    """
    from tin_lite.content_delivery import about_usd, publish_sentence

    connection = await runtime.database.get_integration_connection(
        project_id=run.project_id, provider_key="infra.github"
    )
    repository = (
        connection.configuration.get("selected_repository")
        if connection and connection.status == "connected"
        else None
    )
    if not repository or not adapt_on_approval(settings, run):
        return {"adapt": False}
    mode = await delivery_service(runtime).saved_mode(run)
    cost = await delivery_cost(
        runtime=runtime, settings=settings, run=run, actor=actor, repository=repository
    )
    sentence = publish_sentence(mode)
    about = about_usd(cost["estimated_usd"]) if cost else None
    return {
        "adapt": True,
        "label": "Publish",
        "mode": mode,
        "repository": repository,
        "sentence": sentence,
        "cost": cost,
        "footer": f"{sentence} · about {about}" if about else sentence,
    }


async def retry_delivery(*, runtime, settings, project_id, run_id):
    service = delivery_service(runtime)
    run = await runtime.database.get_run(run_id)
    if not run or run.project_id != project_id:
        raise LookupError("Draft not found.")
    from tin_lite import content_repository_delivery

    if run.workflow_id != content_repository_delivery.WORKFLOW_ID:
        status = await service.status(run)
        if not status or run.status != RunStatus.SUCCEEDED or run.review_decision != "approved":
            raise ValueError("This run has no approved GitHub delivery to retry.")
        if status.get("adapter") and status.get("run_id") and status["status"] != "completed":
            # The approval's adaptation exists: retrying means delivering its saved patch.
            # With no adaptation yet (a refused start), the page's own delivery retries it.
            run = await runtime.database.get_run(UUID(status["run_id"]))
    if run.workflow_id == content_repository_delivery.WORKFLOW_ID:
        status = await content_repository_delivery.retry_status(runtime.database, run)
    if status["status"] == "completed":
        return status
    # One durable operation per draft. An ambiguous HTTP retry attaches to it.
    try:
        await runtime.temporal.start_workflow(
            WORKFLOW,
            str(run.id),
            id=f"content-delivery:{run.id}",
            task_queue=settings.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        pass
    return {**status, "status": "pending"}


async def authorized(request, project_id, user):
    runtime = request.app.state.runtime
    if not await runtime.database.has_project_access(
        project_id=project_id, clerk_user_id=user.clerk_user_id
    ):
        raise HTTPException(status_code=404, detail="project not found")
    return delivery_service(runtime)


@router.get("/content-programs/{program_id}/delivery")
async def get_settings(
    project_id: UUID, program_id: UUID, request: Request, user: AuthContext = USER
):
    service = await authorized(request, project_id, user)
    try:
        return await service.settings(project_id=project_id, program_id=program_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/content-drafts/delivery-sources")
async def delivery_sources(project_id: UUID, request: Request, user: AuthContext = USER):
    await authorized(request, project_id, user)
    from tin_lite.content_repository_delivery import discover

    return await discover(request.app.state.runtime.database, project_id)


@router.put("/content-programs/{program_id}/delivery")
async def save_settings(
    project_id: UUID,
    program_id: UUID,
    payload: SaveDelivery,
    request: Request,
    user: AuthContext = USER,
):
    service = await authorized(request, project_id, user)
    try:
        return await service.save_settings(
            project_id=project_id,
            program_id=program_id,
            settings=payload.settings,
            request_id=payload.request_id,
            expected_revision=payload.expected_revision,
            actor=user.clerk_user_id,
        )
    except (LookupError, ValueError, ProjectFileError, IntegrationError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/content-drafts/{run_id}/delivery")
async def status(project_id: UUID, run_id: UUID, request: Request, user: AuthContext = USER):
    service = await authorized(request, project_id, user)
    run = await service.db.get_run(run_id)
    if not run or run.project_id != project_id:
        raise HTTPException(status_code=404, detail="draft not found")
    return {"delivery": await service.status(run)}


@router.get("/content-drafts/{run_id}/publish-preview")
async def preview(project_id: UUID, run_id: UUID, request: Request, user: AuthContext = USER):
    """The approval card's Publish line for a page Tin adapts to the site."""
    service = await authorized(request, project_id, user)
    run = await service.db.get_run(run_id)
    if not run or run.project_id != project_id:
        raise HTTPException(status_code=404, detail="draft not found")
    return await publish_preview(
        runtime=request.app.state.runtime,
        settings=request.app.state.settings,
        run=run,
        actor=user.clerk_user_id,
    )


@router.post("/content-drafts/{run_id}/delivery/retry", status_code=202)
async def retry(project_id: UUID, run_id: UUID, request: Request, user: AuthContext = USER):
    await authorized(request, project_id, user)
    try:
        return await retry_delivery(
            runtime=request.app.state.runtime,
            settings=request.app.state.settings,
            project_id=project_id,
            run_id=run_id,
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
