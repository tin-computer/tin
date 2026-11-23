"""RB2B's webhook, and the Integrations card that hands a project its address.

`POST /webhooks/rb2b/{token}` is what RB2B calls. RB2B disconnects an address that answers
anything but 200 within 15 seconds, and sends no signature, so the endpoint always answers 200
with an empty body (an unknown token too, which tells a prober nothing), keeps the payload, and
starts the visitor's run. A run that can't start now is tried again with the project's next
visitor; the card says how many wait.

Members of the project set the address up, see it again, replace it and remove it under
`/api/projects/{project_id}/rb2b`. Runs start as the member who set it up.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from tin_lite import rb2b
from tin_lite.auth import AuthContext, require_user

logger = logging.getLogger(__name__)
router = APIRouter()
USER = Depends(require_user)
# How long the webhook waits for a run to start before answering RB2B anyway.
START_SECONDS = 8
# Visitors whose run couldn't start, tried again with the next visitor: a few at a time.
RETRY_WAITING = 3
GONE = "The member who set up RB2B has left the project. Set it up again to keep reading visitors."


class Hooks:
    def __init__(self, runtime, settings):
        self.runtime, self.settings = runtime, settings
        self.db = runtime.database

    @property
    def _cipher(self):
        cipher = getattr(getattr(self.runtime, "integrations", None), "_cipher", None)
        if cipher is None:
            raise HTTPException(503, "RB2B isn't available on this deployment yet.")
        return cipher

    async def _member(self, project_id: UUID, viewer: str) -> None:
        if not await self.db.has_project_access(project_id=project_id, clerk_user_id=viewer):
            raise HTTPException(404, "project not found")

    async def status(self, project_id: UUID, viewer: str) -> dict[str, Any]:
        await self._member(project_id, viewer)
        hook = await self.db.pool.fetchrow(
            "SELECT * FROM rb2b_hooks WHERE project_id = $1", project_id
        )
        if hook is None:
            return {"set_up": False}
        token = self._cipher.decrypt(bytes(hook["token_ciphertext"]), context=f"rb2b:{project_id}")
        counts = await self.db.pool.fetchrow(
            """SELECT count(*) FILTER (WHERE kind = 'person' AND run_id IS NULL) AS waiting,
                      count(*) FILTER (WHERE kind = 'person') AS people,
                      count(*) FILTER (WHERE kind = 'company') AS companies,
                      (SELECT start_error FROM rb2b_visits w WHERE w.project_id = $1
                         AND w.run_id IS NULL AND w.start_error IS NOT NULL
                       ORDER BY w.received_at DESC LIMIT 1) AS start_error
               FROM rb2b_visits WHERE project_id = $1""",
            project_id,
        )
        return {
            "set_up": True,
            "url": rb2b.hook_url(self.settings.switchboard_public_url, token),
            "set_up_by_you": hook["created_by"] == viewer,
            "set_up_at": hook["created_at"],
            "last_received_at": hook["last_received_at"],
            "last_test_at": hook["last_test_at"],
            "people": counts["people"],
            "companies": counts["companies"],
            "waiting": counts["waiting"],
            "start_error": counts["start_error"],
        }

    async def set_up(self, project_id: UUID, viewer: str) -> dict[str, Any]:
        """A new address for the project; one set up before stops working."""

        await self._member(project_id, viewer)
        token = rb2b.new_token()
        sealed = self._cipher.encrypt(token, context=f"rb2b:{project_id}")
        await self.db.pool.execute(
            """INSERT INTO rb2b_hooks (project_id, token_hash, token_ciphertext, created_by)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (project_id) DO UPDATE SET token_hash = EXCLUDED.token_hash,
                   token_ciphertext = EXCLUDED.token_ciphertext,
                   created_by = EXCLUDED.created_by, created_at = now()""",
            project_id,
            rb2b.token_hash(token),
            sealed,
            viewer,
        )
        return await self.status(project_id, viewer)

    async def remove(self, project_id: UUID, viewer: str) -> None:
        await self._member(project_id, viewer)
        await self.db.pool.execute("DELETE FROM rb2b_hooks WHERE project_id = $1", project_id)

    async def receive(self, token: str, body: bytes) -> None:
        """Keep what RB2B posted and start its run. Never raises: RB2B only hears 200."""

        if not token or len(token) > 200 or len(body) > rb2b.MAX_BODY:
            return
        hook = await self.db.pool.fetchrow(
            "SELECT project_id, created_by FROM rb2b_hooks WHERE token_hash = $1",
            rb2b.token_hash(token),
        )
        if hook is None:
            return
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        found = rb2b.lead(payload)
        kind = rb2b.kind(found)
        project_id = hook["project_id"]
        visit_id = await self.db.pool.fetchval(
            """INSERT INTO rb2b_visits (id, project_id, dedupe_key, kind, payload)
               VALUES ($1, $2, $3, $4, $5::jsonb)
               ON CONFLICT (project_id, dedupe_key) DO NOTHING
               RETURNING id""",
            uuid4(),
            project_id,
            rb2b.dedupe_key(found),
            kind,
            json.dumps(payload),
        )
        await self.db.pool.execute(
            """UPDATE rb2b_hooks SET last_received_at = now(),
                   last_test_at = CASE WHEN $2 THEN now() ELSE last_test_at END
               WHERE project_id = $1""",
            project_id,
            kind == "test",
        )
        if kind != "person":
            return
        waiting = await self.db.pool.fetch(
            """SELECT id FROM rb2b_visits WHERE project_id = $1 AND kind = 'person'
                 AND run_id IS NULL AND id <> $2 AND received_at > now() - interval '7 days'
               ORDER BY received_at LIMIT $3""",
            project_id,
            visit_id,
            RETRY_WAITING,
        )
        for waiting_id in [visit_id, *(row["id"] for row in waiting)]:
            if waiting_id is not None:
                await self.start(project_id, waiting_id, hook["created_by"])

    async def start(self, project_id: UUID, visit_id: UUID, member: str) -> None:
        from tin_lite.failures import public_error_text
        from tin_lite.run_service import start_workflow_run

        error = None
        try:
            workflow = await self.db.get_workflow(rb2b.WORKFLOW_ID)
            if workflow is None:
                raise RuntimeError("Reading RB2B's visitors isn't available right now.")
            if not await self.db.has_project_access(project_id=project_id, clerk_user_id=member):
                raise PermissionError(GONE)
            async with asyncio.timeout(START_SECONDS):
                run = await start_workflow_run(
                    runtime=self.runtime,
                    settings=self.settings,
                    workflow=workflow,
                    project_id=project_id,
                    started_by_clerk_user_id=member,
                    start_idempotency_key=f"rb2b:{visit_id}",
                    input_payload={"project_id": str(project_id), "visit_id": str(visit_id)},
                    trigger_source="api",
                    _rb2b=True,
                )
        except PermissionError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - kept with its reason; the next visit retries
            logger.warning("RB2B visit %s didn't start: %s", visit_id, type(exc).__name__)
            error = public_error_text(exc, "Tin couldn't start reading this visitor yet.")
        if error is None:
            await self.db.pool.execute(
                "UPDATE rb2b_visits SET run_id = $2, start_error = NULL WHERE id = $1",
                visit_id,
                run.id,
            )
        else:
            await self.db.pool.execute(
                "UPDATE rb2b_visits SET start_error = $2 WHERE id = $1", visit_id, error[:500]
            )


def _hooks(request: Request) -> Hooks:
    return Hooks(request.app.state.runtime, request.app.state.settings)


@router.post("/webhooks/rb2b/{token}", include_in_schema=False)
async def rb2b_webhook(token: str, request: Request) -> Response:
    try:
        await _hooks(request).receive(token, await request.body())
    except Exception:  # noqa: BLE001 - RB2B disconnects on anything but 200
        logger.exception("RB2B webhook failed")
    return Response(status_code=200)


@router.get("/api/projects/{project_id}/rb2b")
async def rb2b_status(project_id: UUID, request: Request, user: AuthContext = USER):
    return await _hooks(request).status(project_id, user.clerk_user_id)


@router.post("/api/projects/{project_id}/rb2b")
async def rb2b_set_up(project_id: UUID, request: Request, user: AuthContext = USER):
    return await _hooks(request).set_up(project_id, user.clerk_user_id)


@router.delete("/api/projects/{project_id}/rb2b", status_code=204)
async def rb2b_remove(project_id: UUID, request: Request, user: AuthContext = USER):
    await _hooks(request).remove(project_id, user.clerk_user_id)
    return Response(status_code=204)
