"""Thin content-program HTTP surface. MCP calls the same underlying services."""

from dataclasses import asdict
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from tin_lite.auth import AuthContext, require_user
from tin_lite.content_programs import ContentPrograms
from tin_lite.failures import public_error_text
from tin_lite.workflow_prerequisites import PrerequisiteError

router = APIRouter(prefix="/api/projects/{project_id}/content-programs")
USER = Depends(require_user)


class EditPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    plan: dict


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    batch_ids: list[str] = Field(min_length=1, max_length=27)
    instruction: str = Field(min_length=1, max_length=4000)
    context_paths: list[str] = Field(default_factory=list, max_length=8)


class ResolveRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["apply", "discard"]
    expected_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


async def service(request, project_id, user):
    runtime = request.app.state.runtime
    if not await runtime.database.has_project_access(
        project_id=project_id, clerk_user_id=user.clerk_user_id
    ):
        raise HTTPException(status_code=404, detail="project not found")
    return ContentPrograms(database=runtime.database, storage=runtime.storage)


async def request_revision_run(*, runtime, settings, project_id, program_id, request, actor):
    from tin_lite.run_service import start_workflow_run

    programs = ContentPrograms(database=runtime.database, storage=runtime.storage)
    configured = await programs.configured(project_id, program_id)
    revision = await programs.begin_revision(
        project_id=project_id,
        program_id=program_id,
        request_id=request.request_id,
        expected_revision=request.expected_revision,
        batch_ids=request.batch_ids,
        instruction=request.instruction,
        context_paths=request.context_paths,
        actor=actor,
    )
    if revision["status"] != "pending":
        return {
            "revision_id": str(revision["id"]),
            "status": revision["status"],
            "run_id": str(revision["run_id"]) if revision["run_id"] else None,
        }
    workflow = await runtime.database.get_workflow(configured.workflow_id)
    run = await start_workflow_run(
        runtime=runtime,
        settings=settings,
        workflow=workflow,
        project_id=project_id,
        started_by_clerk_user_id=actor,
        start_idempotency_key=f"content-amendment:{request.request_id}",
        project_workflow_id=program_id,
        definition_commit_sha=configured.definition_commit_sha,
        input_schema=configured.input_schema,
        input_payload={**configured.inputs, "amendment_id": str(request.request_id)},
    )
    return {"revision_id": str(revision["id"]), "run_id": str(run.id), "status": run.status.value}


async def stop_content_run(*, runtime, project_id, program_id, run_id, actor):
    programs = ContentPrograms(database=runtime.database, storage=runtime.storage)
    await programs.configured(project_id, program_id)
    run = await runtime.database.get_run(run_id)
    if not run or run.project_id != project_id or run.project_workflow_id != program_id:
        raise LookupError("Content plan run not found.")
    stopped = await runtime.database._stop_paid_report(
        run_id=run_id, project_id=project_id, actor=actor, workflow_key="content.plan"
    )
    await runtime.temporal.get_workflow_handle(run.temporal_workflow_id).signal("stop")
    return {"id": str(stopped.id), "status": stopped.status.value}


@router.post("/{program_id}/runs/{run_id}/stop")
async def stop(
    project_id: UUID, program_id: UUID, run_id: UUID, request: Request, user: AuthContext = USER
):
    await service(request, project_id, user)
    try:
        return await stop_content_run(
            runtime=request.app.state.runtime,
            project_id=project_id,
            program_id=program_id,
            run_id=run_id,
            actor=user.clerk_user_id,
        )
    except (LookupError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=public_error_text(exc)) from exc


@router.get("/sources")
async def sources(project_id: UUID, request: Request, offset: int = 0, user: AuthContext = USER):
    programs = await service(request, project_id, user)
    if not 0 <= offset <= 10000:
        raise HTTPException(status_code=422, detail="invalid source offset")
    rows = await programs.db.pool.fetch(
        """SELECT id, executor, created_at, canonical_commit_sha,
        input->>'site_url' AS site_url, input->>'market' AS market FROM workflow_runs
        WHERE project_id = $1 AND executor IN ('organic.audit','organic.keyword_plan')
          AND status = 'succeeded' AND canonical_commit_sha IS NOT NULL
        ORDER BY created_at DESC, id DESC LIMIT 51 OFFSET $2""",
        project_id,
        offset,
    )
    return {
        "sources": [dict(row) for row in rows[:50]],
        "next_offset": offset + 50 if len(rows) > 50 else None,
    }


@router.get("/{program_id}")
async def facts(project_id: UUID, program_id: UUID, request: Request, user: AuthContext = USER):
    programs = await service(request, project_id, user)
    try:
        await programs.configured(project_id, program_id)
        return await programs.facts(program_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=public_error_text(exc)) from exc


@router.get("/{program_id}/plan")
async def read(project_id: UUID, program_id: UUID, request: Request, user: AuthContext = USER):
    programs = await service(request, project_id, user)
    try:
        return await programs.read(project_id=project_id, program_id=program_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "The working plan is missing or invalid. Open Files to repair it; "
                "no earlier plan will be used."
            ),
        ) from exc


@router.put("/{program_id}/plan")
async def save(
    project_id: UUID,
    program_id: UUID,
    payload: EditPlan,
    request: Request,
    user: AuthContext = USER,
):
    programs = await service(request, project_id, user)
    try:
        return asdict(
            await programs.save(
                project_id=project_id,
                program_id=program_id,
                request_id=payload.request_id,
                expected_revision=payload.expected_revision,
                proposed=payload.plan,
                actor=user.clerk_user_id,
            )
        )
    except (LookupError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=public_error_text(exc)) from exc


@router.post("/{program_id}/revisions")
async def revise(
    project_id: UUID,
    program_id: UUID,
    payload: RevisionRequest,
    request: Request,
    user: AuthContext = USER,
):
    await service(request, project_id, user)
    try:
        return await request_revision_run(
            runtime=request.app.state.runtime,
            settings=request.app.state.settings,
            project_id=project_id,
            program_id=program_id,
            request=payload,
            actor=user.clerk_user_id,
        )
    except PrerequisiteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.diagnostic()) from exc
    except (LookupError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=public_error_text(exc)) from exc


@router.post("/{program_id}/revisions/{revision_id}")
async def resolve(
    project_id: UUID,
    program_id: UUID,
    revision_id: UUID,
    payload: ResolveRevision,
    request: Request,
    user: AuthContext = USER,
):
    programs = await service(request, project_id, user)
    try:
        return await programs.resolve(
            project_id=project_id,
            program_id=program_id,
            revision_id=revision_id,
            action=payload.action,
            actor=user.clerk_user_id,
            expected_revision=payload.expected_revision,
        )
    except (LookupError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=public_error_text(exc)) from exc
