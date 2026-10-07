"""Bounded extension transport; secret validation errors never reflect request inputs."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from tin_lite.connection_collection import CollectionError, LeaseCommand
from tin_lite.connection_collection_store import CollectionStore
from tin_lite.project_connections_api import USER, body

router = APIRouter()


def store(request):
    return CollectionStore(request.app.state.runtime.database, request.app.state.settings)


def bearer(request):
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer "):
        raise HTTPException(401, "extension authentication required")
    return value[7:]


def extension_origin(request):
    allowed = {
        f"chrome-extension://{key}" for key in request.app.state.settings.linkedin_extension_ids
    }
    identity = request.headers.get("x-tin-extension-id")
    if identity not in request.app.state.settings.linkedin_extension_ids or (
        request.headers.get("origin") is not None and request.headers.get("origin") not in allowed
    ):
        raise HTTPException(403, "extension origin unavailable")


async def safe(operation):
    try:
        return await operation
    except CollectionError as exc:
        raise HTTPException(409, exc.code) from None
    except (ValueError, TypeError, KeyError):
        raise HTTPException(422, "invalid collection request") from None


@router.post("/api/projects/{project_id}/connection-collection/pairing")
async def pairing(project_id: UUID, request: Request, user=USER):
    return await safe(store(request).grant(project_id, user.clerk_user_id))


@router.post("/api/connection-extension/pair")
async def pair(request: Request):
    extension_origin(request)
    value = await body(request)
    if set(value) != {"grant", "token_hash", "actor"}:
        raise HTTPException(422, "invalid pairing request")
    return await safe(store(request).pair(value["grant"], value["token_hash"], value["actor"]))


@router.get("/api/projects/{project_id}/connection-extension/pending")
async def pending(project_id: UUID, request: Request):
    extension_origin(request)
    return await safe(store(request).pending(project_id, bearer(request)))


@router.post("/api/projects/{project_id}/connection-extension/{run_id}/{operation}")
async def command(project_id: UUID, run_id: UUID, operation: str, request: Request):
    extension_origin(request)
    value = await body(request)
    service, token = store(request), bearer(request)
    if operation in {"heartbeat", "session", "source", "pause"}:
        try:
            LeaseCommand.model_validate({k: value.get(k) for k in ("generation", "lease")})
        except ValueError:
            raise HTTPException(422, "invalid lease") from None
    if operation == "stop" and not value:
        return await safe(service.cancel_device(run_id, project_id, token))
    if operation == "resume" and set(value) == {"actor_key"}:
        return await safe(service.resume_device(run_id, project_id, token, value["actor_key"]))
    if operation == "session" and set(value) == {"generation", "lease", "session"}:
        return await safe(
            service.cloud_session(
                run_id,
                project_id,
                token,
                **value,
                cipher=request.app.state.runtime.integrations._cipher,
            )
        )
    if operation == "source" and set(value) == {"generation", "lease", "source"}:
        return await safe(service.cloud_source(run_id, project_id, token, **value))
    if operation == "claim" and set(value) == {"actor_key"}:
        return await safe(service.claim(run_id, project_id, token, value["actor_key"]))
    if operation == "heartbeat" and set(value) == {"generation", "lease"}:
        return await safe(service.heartbeat(run_id, project_id, token, **value))
    if operation == "page":
        return await safe(service.page(run_id, project_id, token, value))
    if operation == "pause" and set(value) == {"generation", "lease", "reason"}:
        return await safe(service.stop_attempt(run_id, project_id, token, **value))
    raise HTTPException(422, "invalid collection operation")


@router.post("/api/projects/{project_id}/connection-collection/{run_id}/resume")
async def resume(project_id: UUID, run_id: UUID, request: Request, user=USER):
    await safe(store(request).resume(run_id, project_id, user.clerk_user_id))
    return {"resumed": True}
