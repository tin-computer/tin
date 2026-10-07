"""Secure setup transport. Secret values are intentionally absent from public MCP."""

import json
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from tin_lite.auth import require_user
from tin_lite.integrations import IntegrationError

router = APIRouter(prefix="/api/projects/{project_id}/connections")


async def setup_user(request: Request):
    """Browser sessions or an explicitly supplied OAuth token for the local helper."""
    try:
        return await require_user(request, None)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise
        context = await request.app.state.auth.authenticate_oauth_token(header[7:])
        if (
            context is None
            or "openid" not in context.scopes
            or (context.expires_at is not None and context.expires_at <= time.time())
        ):
            raise HTTPException(status_code=401, detail="authentication required") from None
        await request.app.state.runtime.database.record_tin_user(context.clerk_user_id)
        return context


USER = Depends(setup_user)


async def body(request, *, optional=False):
    # FastAPI's ordinary validation details echo bad inputs. This write-only endpoint
    # parses explicitly and emits fixed diagnostics, never a secret-bearing input.
    content_type = request.headers.get("content-type", "").split(";")[0]
    if content_type != "application/json" and not (optional and not content_type):
        raise HTTPException(status_code=415, detail="JSON required")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 300_000:
            raise HTTPException(status_code=413, detail="selected import is too large")
    if not raw and optional:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(status_code=422, detail="invalid setup request") from None


async def call(operation):
    try:
        return await operation
    except LookupError:
        raise HTTPException(status_code=404, detail="project not found") from None
    except IntegrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (ValueError, TypeError, KeyError):
        raise HTTPException(
            status_code=422, detail="Invalid setup fields; values were not saved."
        ) from None


@router.get("/secrets")
async def list_secrets(project_id: UUID, request: Request, user=USER):
    return await call(
        request.app.state.runtime.integrations.custom.secrets(project_id, user.clerk_user_id)
    )


@router.put("/secrets")
async def save_secrets(project_id: UUID, request: Request, user=USER):
    payload = await body(request)
    if set(payload) != {"entries"}:
        raise HTTPException(status_code=422, detail="selected entries required")
    return await call(
        request.app.state.runtime.integrations.custom.save_secrets(
            project_id, user.clerk_user_id, payload["entries"]
        )
    )


@router.delete("/secrets/{name}/{revision}")
async def delete_secret(project_id: UUID, name: str, revision: str, request: Request, user=USER):
    await call(
        request.app.state.runtime.integrations.custom.delete_secret(
            project_id, user.clerk_user_id, name, revision
        )
    )
    return {"deleted": True}


@router.put("/{provider_key}")
async def save_connection(project_id: UUID, provider_key: str, request: Request, user=USER):
    payload = await body(request)
    if set(payload) != {"configuration", "expected_revision"}:
        raise HTTPException(status_code=422, detail="configuration and expected revision required")
    result = await call(
        request.app.state.runtime.integrations.custom.save(
            project_id,
            user.clerk_user_id,
            provider_key,
            payload["configuration"],
            payload["expected_revision"],
        )
    )
    return {
        "connection_id": str(result.id),
        "key": result.provider_key,
        "configuration": result.configuration,
        "status": "saved",
    }
