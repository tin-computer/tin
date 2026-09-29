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


def page_url_service(runtime):
    from tin_lite.page_urls import PageUrls

    return PageUrls(
        database=runtime.database,
        storage=getattr(runtime, "storage", None),
        integrations=getattr(runtime, "integrations", None),
    )


async def retry_delivery(*, runtime, settings, project_id, run_id):
    service = delivery_service(runtime)
    run = await runtime.database.get_run(run_id)
    if not run or run.project_id != project_id:
        raise LookupError("Draft not found.")
    from tin_lite import content_repository_delivery

    if run.workflow_id == content_repository_delivery.WORKFLOW_ID:
        status = await content_repository_delivery.retry_status(runtime.database, run)
    else:
        status = await service.status(run)
        if not status or run.status != RunStatus.SUCCEEDED or run.review_decision != "approved":
            raise ValueError("This run has no approved GitHub delivery to retry.")
    if status["status"] == "completed":
        return status
    # One durable operation per draft. An ambiguous HTTP retry attaches to it.
    try:
        await runtime.temporal.start_workflow(
            WORKFLOW,
            str(run_id),
            id=f"content-delivery:{run_id}",
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
