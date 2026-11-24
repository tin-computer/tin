"""RB2B's webhook, and the Integrations card that hands a project its address.

`POST /webhooks/rb2b/{token}` is what RB2B calls. RB2B disconnects an address that answers
anything but 200 within 15 seconds, and sends no signature, so the endpoint always answers 200
with an empty body (an unknown token too, which tells a prober nothing). It keeps the payload
and answers; the visitor's run starts after, in the background, so nothing slow sits between
RB2B and its 200. A run that can't start then is tried again by the main process's sweep of
waiting visits (`waiting_visits_loop`) and with the project's next visitor; the card says how
many wait. The webhook intake (a web-only process, see infra) answers while the main process
restarts for a deploy.

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
# How long one run start may take, in the background.
START_SECONDS = 8
# How long keeping the payload may take before the webhook answers RB2B anyway, well inside
# its 15 seconds.
RECEIVE_SECONDS = 8
# How often the main process starts visits still waiting for their run.
SWEEP_SECONDS = 60
# Visitors whose run couldn't start, tried again with the next visitor: a few at a time.
RETRY_WAITING = 3
# Company-only visits started per project in a day: a runaway guard well above real traffic,
# since each is Tin-funded model and data work. Over it, a visit is kept and never started.
COMPANY_RUNS_PER_DAY = 200
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
            """SELECT count(*) FILTER (WHERE kind IN ('person', 'company') AND run_id IS NULL
                                         AND NOT no_start) AS waiting,
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

    async def receive(self, token: str, body: bytes) -> tuple[UUID, UUID | None] | None:
        """Keep what RB2B posted; the project and visit whose run is to start, or None.
        Never raises: RB2B only hears 200."""

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
        if kind not in ("person", "company"):
            return None
        return project_id, visit_id

    async def start_waiting(self, project_id: UUID, visit_id: UUID | None = None) -> None:
        """Start this visit's run, and a few of the project's visits still waiting for one."""

        hook = await self.db.pool.fetchrow(
            "SELECT created_by FROM rb2b_hooks WHERE project_id = $1", project_id
        )
        if hook is None:
            return
        waiting = await self.db.pool.fetch(
            """SELECT id FROM rb2b_visits WHERE project_id = $1 AND kind IN ('person', 'company')
                 AND run_id IS NULL AND NOT no_start AND ($2::uuid IS NULL OR id <> $2)
                 AND received_at > now() - interval '7 days'
               -- Visits that never failed to start go first, newest first: one that keeps
               -- failing never holds up the rest.
               ORDER BY start_error IS NOT NULL, received_at DESC LIMIT $3""",
            project_id,
            visit_id,
            RETRY_WAITING,
        )
        for waiting_id in [visit_id, *(row["id"] for row in waiting)]:
            if waiting_id is not None and await self._may_start(project_id, waiting_id):
                await self.start(project_id, waiting_id, hook["created_by"])

    async def _may_start(self, project_id: UUID, visit_id: UUID) -> bool:
        """A company visit starts only if it takes one of the project's runs for the day. The
        count and the claim happen under one per-project lock, so two visits at once can't both
        take the last run; over the limit, the visit is kept and marked never to start."""

        async with self.db.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT kind, claimed_at FROM rb2b_visits WHERE id = $1", visit_id
            )
            if row is None or row["kind"] != "company":
                return True
            if row["claimed_at"] is not None:
                return True  # claimed before; its start is being retried
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"rb2b-company-runs:{project_id}",
            )
            claimed = await conn.fetchval(
                """SELECT count(*) FROM rb2b_visits
                   WHERE project_id = $1 AND kind = 'company'
                     AND claimed_at > now() - interval '1 day'""",
                project_id,
            )
            if claimed >= COMPANY_RUNS_PER_DAY:
                await conn.execute("UPDATE rb2b_visits SET no_start = true WHERE id = $1", visit_id)
                return False
            await conn.execute(
                "UPDATE rb2b_visits SET claimed_at = clock_timestamp() WHERE id = $1", visit_id
            )
            return True

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
                    # Tin binds the project itself; the input names only the visit.
                    input_payload={"visit_id": str(visit_id)},
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


# Run starts under way after their webhook answered; held so they aren't collected early.
_starting: set[asyncio.Task] = set()


async def _start_quietly(hooks: Hooks, project_id: UUID, visit_id: UUID | None) -> None:
    try:
        await hooks.start_waiting(project_id, visit_id)
    except Exception:  # noqa: BLE001 - the sweep of waiting visits tries again
        logger.exception("RB2B run start failed")


async def settled() -> None:
    """Wait for run starts under way (tests, and nothing else, need to)."""

    await asyncio.gather(*list(_starting), return_exceptions=True)


@router.post("/webhooks/rb2b/{token}", include_in_schema=False)
async def rb2b_webhook(token: str, request: Request) -> Response:
    hooks = _hooks(request)
    try:
        async with asyncio.timeout(RECEIVE_SECONDS):
            started = await hooks.receive(token, await request.body())
    except Exception:  # noqa: BLE001 - RB2B disconnects on anything but 200
        logger.exception("RB2B webhook failed")
        started = None
    if started is not None:
        task = asyncio.create_task(_start_quietly(hooks, *started), name="rb2b-start")
        _starting.add(task)
        task.add_done_callback(_starting.discard)
    return Response(status_code=200)


async def waiting_visits_loop(runtime, settings) -> None:
    """Start visits still waiting for their run: one the webhook kept while its start failed,
    or one the webhook intake kept while this process restarted."""

    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            await sweep_waiting(Hooks(runtime, settings))
        except Exception:  # noqa: BLE001 - a sweep that fails runs again next time
            logger.exception("RB2B waiting visits sweep failed")


async def sweep_waiting(hooks: Hooks) -> None:
    """Start a few waiting visits of each project with some, received at least half a minute
    ago (a newer one is the webhook's own to start)."""

    rows = await hooks.db.pool.fetch(
        """SELECT DISTINCT v.project_id FROM rb2b_visits v
           JOIN rb2b_hooks h ON h.project_id = v.project_id
           WHERE v.kind IN ('person', 'company') AND v.run_id IS NULL
             AND NOT v.no_start AND v.received_at > now() - interval '7 days'
             AND v.received_at < now() - interval '30 seconds'"""
    )
    for row in rows:
        await hooks.start_waiting(row["project_id"])


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
