"""Public, cached read of the workflow Registry for the website.

tin.computer lists what Tin can run today. It reads this endpoint instead of keeping its
own copy of the catalog, so the site and the product cannot drift apart.

The response is deliberately small: identity and presentation fields of active Registry
workflows (``project_id IS NULL``), never definitions, input schemas, prompts, commit
SHAs or anything project-scoped. It needs no sign-in, so it is served from a short
in-process cache and marked cacheable for shared caches; a burst of requests costs at
most one database read per ``CACHE_SECONDS``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, Request, Response

from tin_lite.domain import WorkflowStatus
from tin_lite.public_workflows import PUBLIC_WORKFLOWS

router = APIRouter()

# How long one process reuses a Registry read, and what shared caches may do with it.
CACHE_SECONDS = 60
CACHE_CONTROL = "public, max-age=300, s-maxage=900, stale-while-revalidate=86400"

_COMMUNITY_KEYS = frozenset(item.key for item in PUBLIC_WORKFLOWS)


@dataclass(frozen=True)
class _Snapshot:
    body: bytes
    etag: str
    expires_at: float


def _catalog(workflows: list[Any]) -> dict[str, Any]:
    systems: dict[str, dict[str, Any]] = {}
    items = []
    for workflow in workflows:
        # The Registry query excludes archived rows; paused ones are not runnable either.
        if workflow.project_id is not None or workflow.status != WorkflowStatus.ACTIVE:
            continue
        if workflow.system_id and workflow.system_name:
            systems.setdefault(
                workflow.system_id,
                {
                    "id": workflow.system_id,
                    "name": workflow.system_name,
                    "order": workflow.system_order,
                },
            )
        items.append(
            {
                "key": workflow.key,
                "title": workflow.title,
                "description": workflow.description,
                "system": workflow.system_id if workflow.system_name else None,
                # Start here runs through the MCP only; the dashboard catalog hides it.
                "agent_only": bool((workflow.definition or {}).get("agent_only")),
                "source": "community" if workflow.key in _COMMUNITY_KEYS else "built-in",
            }
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "count": len(items),
        "systems": sorted(
            systems.values(), key=lambda s: (s["order"] is None, s["order"] or 0, s["name"])
        ),
        "workflows": items,
    }


async def _snapshot(request: Request) -> _Snapshot:
    state = request.app.state
    cached: _Snapshot | None = getattr(state, "public_catalog_snapshot", None)
    now = time.monotonic()
    if cached is not None and cached.expires_at > now:
        return cached
    lock = getattr(state, "public_catalog_lock", None)
    if lock is None:
        lock = state.public_catalog_lock = asyncio.Lock()
    async with lock:
        cached = getattr(state, "public_catalog_snapshot", None)
        if cached is not None and cached.expires_at > time.monotonic():
            return cached
        workflows = await state.runtime.database.list_workflows(project_id=None)
        body = json.dumps(_catalog(workflows), separators=(",", ":")).encode()
        # The ETag covers the catalog, not the generation time, so an unchanged
        # Registry keeps answering 304 across cache refreshes.
        stable = json.loads(body)
        stable.pop("generated_at")
        digest = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
        snapshot = _Snapshot(
            body=body,
            etag=f'"{digest[:32]}"',
            expires_at=time.monotonic() + CACHE_SECONDS,
        )
        state.public_catalog_snapshot = snapshot
        return snapshot


@router.get("/api/public/workflows")
async def public_workflows(
    request: Request,
    if_none_match: str | None = Header(default=None),
) -> Response:
    snapshot = await _snapshot(request)
    headers = {
        "Cache-Control": CACHE_CONTROL,
        "ETag": snapshot.etag,
        "X-Tin-Read-Source": "postgres",
    }
    if if_none_match and snapshot.etag in {tag.strip() for tag in if_none_match.split(",")}:
        return Response(status_code=304, headers=headers)
    return Response(content=snapshot.body, media_type="application/json", headers=headers)
