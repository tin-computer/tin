"""What a member reads and decides about connection requests, for HTTP and MCP alike (#450).

A member sees and decides only requests their own LinkedIn would send; turning sending on takes
their own tick of the consent box, so it happens on the LinkedIn card, never over MCP.
Approving a batch, or choosing people in People, hands it to the member's
`social.linkedin_requests` run on the project, which sends their requests in one line; a new
run starts when none still has requests waiting. A member who turned on `auto_approve` lets
Tin approve the people their own LinkedIn reads propose, right after each read
(`ConnectionRequests.auto_approve`), as they would in Decisions.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from tin_lite import linkedin_requests as requests
from tin_lite.connection_collection import PROVIDER, enabled
from tin_lite.connection_collection_connection import permission

logger = logging.getLogger(__name__)

CONSENT_TEXT = (
    "Send the connection requests I approve from Tin's servers, with my LinkedIn session: one "
    "at a time, a few each weekday. This works while my computer is off. Tin stops at the "
    "first warning from LinkedIn, and I can turn it off here anytime."
)
RISK_TEXT = (
    "LinkedIn's terms don't allow automated requests. Few, paced requests that stop at the "
    "first warning lower the risk to your account; they don't remove it."
)


# The namespace of the request ID an automatic approval decides with, one per LinkedIn read.
AUTO_APPROVE = UUID("5d0f3c1e-8a47-4b2f-9e61-2c7a4f8d9b30")


class RequestsRefused(Exception):
    """A request Tin refuses, with its status and the sentence the member reads."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


STATUS = {
    "not_found": 404,
    "conflict": 409,
    "sending_off": 409,
    "consent_required": 422,
    "invalid": 422,
}


def _refused(error: requests.RequestError) -> RequestsRefused:
    return RequestsRefused(STATUS.get(error.code, 422), error.message)


class ConnectionRequests:
    def __init__(self, runtime, settings):
        self.runtime, self.settings = runtime, settings
        self.db = runtime.database

    async def _project(self, project_id: UUID, member: str):
        if not await self.db.has_project_access(project_id=project_id, clerk_user_id=member):
            raise RequestsRefused(404, "project not found")
        project = await self.db.get_project(project_id)
        if project is None:
            raise RequestsRefused(404, "project not found")
        return project

    async def _reading(self, project_id: UUID, member: str) -> str | None:
        """How the member's own LinkedIn reading runs: "cloud", "browser" or None. Sending uses
        the session cloud reading saves, so it needs cloud."""

        configuration = await self.db.pool.fetchval(
            """SELECT configuration FROM integration_connections
               WHERE project_id = $1 AND provider_key = $2 AND status = 'connected'
                 AND connected_by_clerk_user_id = $3""",
            project_id,
            PROVIDER,
            member,
        )
        if configuration is None:
            return None
        return (
            "browser"
            if permission(configuration).get("mode", "local_only") == "local_only"
            else "cloud"
        )

    async def state(self, project_id: UUID, member: str) -> dict[str, Any]:
        """The member's sending, this week's numbers, and the batch waiting for them."""

        await self._project(project_id, member)
        now = datetime.now(UTC)
        async with self.db.pool.acquire() as conn:
            sending = await requests.sending(conn, project_id, member)
            week = await requests.week(conn, project_id, member, now)
            batches = await requests.open_batches(conn, project_id, member, now)
            # When one more request, chosen now in People, would go.
            next_open = (await requests.plan(conn, project_id, member, ["next"], now) or [None])[0]
        offered = enabled(self.settings, project_id)
        reading = await self._reading(project_id, member) if offered else None
        on = sending["on"] or (not sending["set"] and reading == "cloud")
        return {
            "offered": offered,
            "reading": reading == "cloud",
            "browser_only": reading == "browser",
            "sending": {
                "on": on,
                **{
                    key: sending[key]
                    for key in (
                        "paused",
                        "held",
                        "paused_reason",
                        "paused_until",
                        "daily_cap",
                        "weekly_cap",
                        "start_minute",
                        "end_minute",
                        "timezone",
                        "auto_approve",
                    )
                },
            },
            "pause_text": requests.PAUSES.get(sending["paused_reason"] or "")
            if sending["paused_reason"] != "limit"
            else "LinkedIn's weekly limit for invitations",
            "consent": {
                "version": requests.CONSENT_VERSION,
                "text": CONSENT_TEXT,
                "risk": RISK_TEXT,
            },
            "week": week,
            "next_open": next_open if on else None,
            "batches": batches,
        }

    async def set_sending(
        self,
        project_id: UUID,
        member: str,
        *,
        enabled_: bool,
        consent_version: int | None = None,
        limits: dict[str, int] | None = None,
        auto_approve: bool | None = None,
    ) -> dict[str, Any]:
        await self._project(project_id, member)
        if enabled_ and await self._reading(project_id, member) != "cloud":
            raise RequestsRefused(409, "Turn on cloud reading on the LinkedIn card first.")
        try:
            async with self.db.pool.acquire() as conn:
                await requests.set_sending(
                    conn,
                    project_id,
                    member,
                    enabled=enabled_,
                    consent_version=consent_version,
                    limits=limits,
                    auto_approve=auto_approve,
                )
        except requests.RequestError as error:
            raise _refused(error) from None
        return await self.state(project_id, member)

    async def _refusal(self, error, project_id: UUID, member: str) -> RequestsRefused:
        """Sending is off only for a member who turned it off or reads in this browser only;
        the second needs cloud reading, not the switch."""

        if error.code == "sending_off" and await self._reading(project_id, member) != "cloud":
            return RequestsRefused(409, "Turn on cloud reading on the LinkedIn card first.")
        return _refused(error)

    async def _on_by_default(self, project_id: UUID, member: str) -> None:
        """Sending is on for a member who never set it, once their LinkedIn reading runs in the
        cloud. Their first approval records it, so a pause at LinkedIn's first warning has a
        setting to hold; a member who turned sending off stays off."""

        async with self.db.pool.acquire() as conn:
            if (await requests.sending(conn, project_id, member))["set"]:
                return
        if await self._reading(project_id, member) != "cloud":
            return
        async with self.db.pool.acquire() as conn:
            await requests.set_sending(
                conn, project_id, member, enabled=True, consent_version=requests.CONSENT_VERSION
            )

    async def resume(self, project_id: UUID, member: str) -> dict[str, Any]:
        await self._project(project_id, member)
        async with self.db.pool.acquire() as conn:
            await requests.resume(conn, project_id, member)
        return await self.state(project_id, member)

    async def decide(
        self,
        project_id: UUID,
        batch_id: UUID,
        member: str,
        *,
        request_id: UUID,
        approve: bool,
        people: list[UUID] | None = None,
        trigger_source: str = "manual",
        oauth_client_id: str | None = None,
    ) -> dict[str, Any]:
        """Approve the chosen people, who join the member's line to be sent, or discard the
        batch."""

        await self._project(project_id, member)
        if approve:
            await self._on_by_default(project_id, member)
        try:
            async with self.db.pool.acquire() as conn:
                decided = await requests.decide(
                    conn,
                    project_id,
                    batch_id,
                    member,
                    request_id=request_id,
                    approve=approve,
                    people=people,
                )
        except requests.RequestError as error:
            raise await self._refusal(error, project_id, member) from None
        if decided["status"] == "approved":
            try:
                run_id = await self._sending(
                    project_id, batch_id, member, trigger_source, oauth_client_id
                )
            except RequestsRefused:
                await self._reopen(project_id, batch_id)
                raise
            except Exception:
                logger.exception("connection requests: the sending run did not start")
                await self._reopen(project_id, batch_id)
                raise RequestsRefused(
                    503, "Tin couldn't start sending, so nothing was approved. Try again shortly."
                ) from None
            decided["run_id"] = str(run_id)
        return decided

    async def auto_approve(self, project_id: UUID, member: str, *, run_id: UUID) -> int:
        """Approve the people the member's own LinkedIn read just proposed, when the member
        turned on `auto_approve` and sending is on and not paused until they resume it. Tin
        decides as the member would in Decisions, as the member, so their caps, window, wait
        and pauses apply as usual. Only people who still fit and wait to be asked are approved;
        the rest of the batch is declined, as unticked people are.

        The read's run keys the decision: a retried read repeats the same approval and starts
        no second run. Returns how many people the approval queued. A refusal leaves the batch
        in Decisions, where the member reads why."""

        request_id = uuid5(AUTO_APPROVE, f"auto-approve:{run_id}")
        people = None
        async with self.db.pool.acquire() as conn:
            batch_id = await conn.fetchval(
                """SELECT id FROM linkedin_request_batches
                   WHERE project_id = $1 AND member = $2 AND decision_request_id = $3""",
                project_id,
                member,
                request_id,
            )
            if batch_id is None:
                sending = await requests.sending(conn, project_id, member)
                if not (sending["on"] and sending["auto_approve"]) or sending["held"]:
                    return 0
                batch_id = await conn.fetchval(
                    """SELECT id FROM linkedin_request_batches
                       WHERE project_id = $1 AND member = $2 AND status = 'open'""",
                    project_id,
                    member,
                )
                if batch_id is None:
                    return 0
                people = await requests.auto_approvable(conn, batch_id, member)
                if not people:
                    return 0
        try:
            decided = await self.decide(
                project_id,
                batch_id,
                member,
                request_id=request_id,
                approve=True,
                people=people,
                trigger_source="auto_approve",
            )
        except RequestsRefused as error:
            logger.warning("connection requests: automatic approval refused: %s", error.message)
            return 0
        if decided["status"] != "approved":
            return 0
        return await self.db.pool.fetchval(
            """SELECT count(*) FROM linkedin_requests
               WHERE batch_id = $1 AND status <> 'declined'""",
            batch_id,
        )

    async def send(
        self,
        project_id: UUID,
        member: str,
        *,
        request_id: UUID,
        people: list[UUID],
        trigger_source: str = "manual",
        oauth_client_id: str | None = None,
    ) -> dict[str, Any]:
        """Queue requests to the people the member chose in People and start sending them.
        Choosing is the approval; if sending can't start, nothing stays approved."""

        await self._project(project_id, member)
        await self._on_by_default(project_id, member)
        try:
            async with self.db.pool.acquire() as conn:
                picked = await requests.pick(
                    conn, project_id, member, people, request_id=request_id
                )
        except requests.RequestError as error:
            raise await self._refusal(error, project_id, member) from None
        batch_id = picked["batch_id"]
        try:
            await self._sending(project_id, batch_id, member, trigger_source, oauth_client_id)
        except RequestsRefused:
            await self._unpick(project_id, batch_id)
            raise
        except Exception:
            logger.exception("connection requests: the sending run did not start")
            await self._unpick(project_id, batch_id)
            raise RequestsRefused(
                503, "Tin couldn't start sending, so nothing was queued. Try again shortly."
            ) from None
        async with self.db.pool.acquire() as conn:
            ids = [
                row["id"]
                for row in await conn.fetch(
                    "SELECT id FROM linkedin_requests WHERE batch_id = $1 ORDER BY queued_at, id",
                    batch_id,
                )
            ]
            slots = await requests.plan(conn, project_id, member, ids, datetime.now(UTC))
        return {
            **picked,
            "first_send": slots[0] if slots else None,
            "last_send": slots[-1] if slots else None,
        }

    async def cancel(self, project_id: UUID, member: str, person_id: UUID) -> dict[str, Any]:
        """Take the member's request to this person out of the queue before it goes."""

        await self._project(project_id, member)
        try:
            async with self.db.pool.acquire() as conn:
                cancelled = await requests.cancel(conn, project_id, member, person_id)
        except requests.RequestError as error:
            raise _refused(error) from None
        return {"cancelled": cancelled}

    async def _unpick(self, project_id: UUID, batch_id: UUID) -> None:
        if await self.db.get_run_by_start_key(
            project_id=project_id, start_idempotency_key=f"linkedin-requests:{batch_id}"
        ):
            return
        async with self.db.pool.acquire() as conn, conn.transaction():
            await requests.unpick(conn, batch_id)

    async def _reopen(self, project_id: UUID, batch_id: UUID) -> None:
        """The sending run never started: the batch goes back to Decisions as it was, so the
        member sees why and approves again. A run that did start keeps the approval."""

        if await self.db.get_run_by_start_key(
            project_id=project_id, start_idempotency_key=f"linkedin-requests:{batch_id}"
        ):
            return
        async with self.db.pool.acquire() as conn, conn.transaction():
            await requests.reopen(conn, batch_id)

    async def _sending(self, project_id, batch_id, member, trigger_source, oauth_client_id):
        """The run that sends the batch: the member's run on the project that still has
        requests waiting, or a new one, recorded on the batch before the member's line is
        unlocked."""

        async with self.db.pool.acquire() as conn, conn.transaction():
            run_id = await requests.sending_run(conn, batch_id, datetime.now(UTC))
            if run_id is None:
                run_id = await self._start(
                    project_id, batch_id, member, trigger_source, oauth_client_id
                )
                await conn.execute(
                    "UPDATE linkedin_request_batches SET send_run_id = $2 WHERE id = $1",
                    batch_id,
                    run_id,
                )
        return run_id

    async def _start(self, project_id, batch_id, member, trigger_source, oauth_client_id):
        from tin_lite.run_service import start_workflow_run

        workflow = await self.db.get_workflow(requests.WORKFLOW_ID)
        if workflow is None:
            raise RequestsRefused(503, "Sending isn't available right now. Try again later.")
        from tin_lite.billing_contracts import BillingError
        from tin_lite.failures import public_error_text
        from tin_lite.integrations import IntegrationError
        from tin_lite.run_service import WorkflowExecutorUnavailableError
        from tin_lite.workflow_inputs import WorkflowInputError

        try:
            run = await start_workflow_run(
                runtime=self.runtime,
                settings=self.settings,
                workflow=workflow,
                project_id=project_id,
                started_by_clerk_user_id=member,
                start_idempotency_key=f"linkedin-requests:{batch_id}",
                input_payload={"batch_id": str(batch_id)},
                trigger_source=trigger_source,
                started_by_oauth_client_id=oauth_client_id,
                _linkedin_requests=True,
            )
        except (
            BillingError,
            IntegrationError,
            WorkflowExecutorUnavailableError,
            WorkflowInputError,
        ) as exc:
            raise RequestsRefused(
                409, public_error_text(exc, "Tin couldn't start sending. Approve again shortly.")
            ) from None
        return run.id
