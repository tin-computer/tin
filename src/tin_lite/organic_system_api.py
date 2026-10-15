"""Membership-gated controls for organic-system runs."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from tin_lite.auth import AuthContext, require_user
from tin_lite.failures import public_error_text

router = APIRouter(prefix="/api/projects/{project_id}/organic-system")
USER = Depends(require_user)


async def require_access(request, project_id, user):
    if not await request.app.state.runtime.database.has_project_access(
        project_id=project_id, clerk_user_id=user.clerk_user_id
    ):
        raise HTTPException(status_code=404, detail="project not found")


@router.get("/runs/{run_id}")
async def system_run(project_id: UUID, run_id: UUID, request: Request, user: AuthContext = USER):
    from tin_lite.organic_system import system_facts

    await require_access(request, project_id, user)
    try:
        return await system_facts(
            database=request.app.state.runtime.database, project_id=project_id, run_id=run_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.post("/runs/{run_id}/stop")
async def stop_recipe(project_id: UUID, run_id: UUID, request: Request, user: AuthContext = USER):
    from tin_lite.db import SideEffectConflictError
    from tin_lite.organic_system_control import stop_system

    await require_access(request, project_id, user)
    run = await request.app.state.runtime.database.get_run(run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        return await stop_system(
            runtime=request.app.state.runtime, run_id=run_id, actor=user.clerk_user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (ValueError, SideEffectConflictError) as exc:
        raise HTTPException(status_code=409, detail=public_error_text(exc)) from exc
