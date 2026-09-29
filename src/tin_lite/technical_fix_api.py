"""Membership-gated technical-fix preparation and organic-system controls."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from tin_lite.auth import AuthContext, require_user
from tin_lite.technical_fix_sources import TechnicalFixError, TechnicalFixSources

router = APIRouter(prefix="/api/projects/{project_id}/technical-fixes")
system_router = APIRouter(prefix="/api/projects/{project_id}/organic-system")
USER = Depends(require_user)


class TechnicalFixSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audit_run_id: UUID
    audit_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    finding_id: str = Field(pattern=r"^oa_[0-9a-f]{20}$")
    expected_repository: str = Field(min_length=3, max_length=140)
    repository_serves_site: StrictBool


async def service(request, project_id, user):
    runtime = request.app.state.runtime
    if not await runtime.database.has_project_access(
        project_id=project_id, clerk_user_id=user.clerk_user_id
    ):
        raise HTTPException(status_code=404, detail="project not found")
    return TechnicalFixSources(
        database=runtime.database, storage=runtime.storage, integrations=runtime.integrations
    )


@system_router.get("/runs/{run_id}")
async def system_run(project_id: UUID, run_id: UUID, request: Request, user: AuthContext = USER):
    from tin_lite.organic_system import system_facts

    await service(request, project_id, user)
    try:
        return await system_facts(
            database=request.app.state.runtime.database, project_id=project_id, run_id=run_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


async def stop_control(request, project_id, run_id, user, control):
    from tin_lite.db import SideEffectConflictError

    await service(request, project_id, user)
    run = await request.app.state.runtime.database.get_run(run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        return await control(
            runtime=request.app.state.runtime, run_id=run_id, actor=user.clerk_user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (ValueError, SideEffectConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/runs/{run_id}/live")
async def live_check(
    project_id: UUID, run_id: UUID, request: Request, check: bool = False, user: AuthContext = USER
):
    """After the fix's PR merges, whether the live site still shows the finding. `check`
    asks GitHub and the site again, at most every ten minutes per run."""
    from tin_lite.technical_fix_live import live_service

    await service(request, project_id, user)
    run = await request.app.state.runtime.database.get_run(run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(status_code=404, detail="run not found")
    return {"live_check": await live_service(request.app.state.runtime).view(run, check=check)}


@router.post("/runs/{run_id}/stop")
async def stop_fix(project_id: UUID, run_id: UUID, request: Request, user: AuthContext = USER):
    from tin_lite.organic_system_control import stop_technical

    return await stop_control(request, project_id, run_id, user, stop_technical)


@system_router.post("/runs/{run_id}/stop")
async def stop_recipe(project_id: UUID, run_id: UUID, request: Request, user: AuthContext = USER):
    from tin_lite.organic_system_control import stop_system

    return await stop_control(request, project_id, run_id, user, stop_system)


def http_error(exc):
    return HTTPException(
        status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
    )


@router.get("/sources")
async def sources(project_id: UUID, request: Request, offset: int = 0, user: AuthContext = USER):
    preparation = await service(request, project_id, user)
    try:
        return await preparation.list_sources(project_id=project_id, offset=offset)
    except TechnicalFixError as exc:
        raise http_error(exc) from exc


@router.get("/sources/{audit_run_id}")
async def inspect(project_id: UUID, audit_run_id: UUID, request: Request, user: AuthContext = USER):
    preparation = await service(request, project_id, user)
    try:
        return await preparation.inspect(project_id=project_id, audit_run_id=audit_run_id)
    except TechnicalFixError as exc:
        raise http_error(exc) from exc


@router.post("/preflight")
async def preflight(
    project_id: UUID, payload: TechnicalFixSelection, request: Request, user: AuthContext = USER
):
    preparation = await service(request, project_id, user)
    try:
        return await preparation.preflight(project_id=project_id, **payload.model_dump())
    except TechnicalFixError as exc:
        raise http_error(exc) from exc
