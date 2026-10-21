"""Activities for `social.linkedin_requests` (#450): send a member's approved requests, one at a
time. The run sends every batch it holds: the one that started it and those approved while it
still had requests waiting.

The workflow asks what to do next and follows: send, wait, read who accepted, or finish. Every
decision about time (the window, the gap, the caps, a pause) is made here from Postgres, so the
workflow's history holds only request IDs, waits and outcomes. Each send is receipted before
the session reaches LinkedIn, and a send that may have gone out is never repeated.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import linkedin_requests as requests
from tin_lite.connection_collection import CollectionError
from tin_lite.failures import failure_projection, split_failure_input
from tin_lite.integrations import IntegrationAuthorizationError, IntegrationUpstreamError
from tin_lite.linkedin_cloud import ENGAGEMENT_OPERATION
from tin_lite.linkedin_engagers_activities import saved_session
from tin_lite.linkedin_invitations import InviteResult, RelationshipsResult
from tin_lite.organic_audit_publication import publish_artifacts
from tin_lite.workflow_inputs import WorkflowInputError

logger = logging.getLogger(__name__)

KEY = requests.KEY
REQUEST_OPERATION = "linkedin_request_v1"
REPORT_PATH = "people/connection-requests.md"
PATHS = {"connection-requests.md": REPORT_PATH}
LIMITS = {"connection-requests.md": 64_000}
FALLBACK = "Tin couldn't finish sending your connection requests. The ones not sent wait."
# What stops all sending until the member resumes it.
STOPPING = frozenset(
    {"challenge", "session_expired", "account_changed", "access_denied", "rate_limited"}
)


def _heartbeat(detail) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


def next_week(now: datetime, settings: dict) -> datetime:
    """Monday's window start in the project's time zone: when LinkedIn's weekly limit ends."""

    zone = ZoneInfo(settings["timezone"])
    local = now.astimezone(zone)
    monday = local.date() + timedelta(days=7 - local.weekday())
    return requests._at(monday, settings["start_minute"], zone)


class LinkedInRequestsActivities:
    def __init__(self, *, database, storage, settings, cipher, cloud):
        self.db, self.storage, self.settings = database, storage, settings
        self.cipher, self.cloud = cipher, cloud

    async def _active(self, run_id, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or run.executor != KEY or run.status.value not in {"pending", "running"}:
            raise ApplicationError(
                "These connection requests are no longer active.", non_retryable=True
            )
        return run

    def _batch(self, run) -> UUID:
        return UUID(str(run.input["batch_id"]))

    async def _progress(self, run, summary: str) -> None:
        await self.db.project_run_progress(
            run_id=run.id, mode="steps", step="send", current=1, total=2, summary=summary
        )
        _heartbeat({"summary": summary})

    @activity.defn(name="linkedin_requests_prepare")
    async def prepare(self, run_id: str) -> None:
        run = await self._active(run_id)
        batch = await self.db.pool.fetchrow(
            "SELECT * FROM linkedin_request_batches WHERE id = $1 AND project_id = $2",
            self._batch(run),
            run.project_id,
        )
        if batch is None or batch["status"] != "approved":
            raise WorkflowInputError("These connection requests weren't approved.")
        if batch["member"] != run.started_by_clerk_user_id:
            raise WorkflowInputError("Only the member whose LinkedIn sends them can start them.")
        await self.db.pool.execute(
            "UPDATE linkedin_request_batches SET send_run_id = $2 WHERE id = $1 "
            "AND send_run_id IS NULL",
            batch["id"],
            run.id,
        )
        await self.db.mark_run_running(run.id)
        await self._progress(run, "Sending your approved connection requests, one at a time")

    @activity.defn(name="linkedin_requests_next")
    async def next(self, run_id: str) -> dict:
        run = await self._active(run_id)
        async with self.db.pool.acquire() as conn:
            return await requests.next_step(conn, run.id, datetime.now(UTC))

    async def _pause(self, member: str, code: str, settings: dict | None = None) -> None:
        async with self.db.pool.acquire() as conn:
            if code == "limit":
                await requests.pause(
                    conn, member, "limit", until=next_week(datetime.now(UTC), settings)
                )
            else:
                await requests.pause(conn, member, code)

    @activity.defn(name="linkedin_requests_send")
    async def send(self, control: dict) -> dict:
        run = await self._active(control["run_id"])
        request_id = UUID(control["request_id"])
        key = f"{run.id}:requests:{request_id}"
        receipt = await self.db.get_effect(key)
        if receipt is None:
            async with self.db.pool.acquire() as conn, conn.transaction():
                reserved = await requests.reserve(conn, request_id, datetime.now(UTC))
            if not reserved.get("go"):
                return {"outcome": "skip" if reserved.get("skip") else "wait", **reserved}
        request = await self.db.pool.fetchrow(
            "SELECT * FROM linkedin_requests WHERE id = $1", request_id
        )
        if request is None:
            return {"outcome": "skip", "skip": True}
        settings = await self._settings(request)
        try:
            session, actor = await saved_session(
                self.db, self.settings, self.cipher, run.project_id, request["member"]
            )
        except IntegrationAuthorizationError:
            if receipt is None:
                await self._settle(run, request_id, "session_expired")
                await self._pause(request["member"], "session_expired")
            return {"outcome": "paused"}
        except (WorkflowInputError, IntegrationUpstreamError):
            if receipt is None:
                await self._settle(run, request_id, "rate_limited")
            return {"outcome": "wait", "wait": 6 * 3600}
        answer, code = await self._invite(run, key, request, session, actor)
        if code is not None:
            outcome = code
        else:
            outcome = answer.outcome
        status = await self._settle(
            run, request_id, outcome, invitation=answer.invitation if answer else None
        )
        if outcome == "limit":
            await self._pause(request["member"], "limit", settings)
        elif outcome == "refused" or outcome in STOPPING:
            await self._pause(request["member"], outcome)
        return {"outcome": outcome, "status": status}

    async def _settings(self, request) -> dict:
        async with self.db.pool.acquire() as conn:
            return await requests.sending(conn, request["project_id"], request["member"])

    async def _settle(self, run, request_id: UUID, outcome: str, *, invitation=None) -> str:
        async with self.db.pool.acquire() as conn, conn.transaction():
            return await requests.settle(
                conn, request_id, outcome, run_id=run.id, invitation=invitation
            )

    async def _invite(self, run, key: str, request, session, actor):
        """One receipted send: (answer, None), or (None, reason). An interrupted send whose
        sandbox started is uncertain; one that never reached a sandbox waits its turn again."""

        async with self.db.effect_lock(key, REQUEST_OPERATION) as (conn, receipt):
            if receipt is not None:
                saved = receipt.result or {}
                if receipt.status == "completed":
                    if saved.get("outcome") == "error":
                        return None, saved.get("reason") or "uncertain"
                    return InviteResult.model_validate(saved["answer"]), None
                if saved.get("sandbox_id"):
                    await self.cloud.cleanup(saved["sandbox_id"])
                    await self.db.complete_effect(
                        conn, execution_key=key, result={"outcome": "error", "reason": "uncertain"}
                    )
                    return None, "uncertain"
                await self.db.complete_effect(
                    conn, execution_key=key, result={"outcome": "error", "reason": "not_sent"}
                )
                return None, "rate_limited"
            await self.db.start_effect(conn, execution_key=key, operation=REQUEST_OPERATION)
            try:
                raw = await self.cloud.engagement(
                    conn,
                    execution_key=key,
                    run_id=run.id,
                    session=session,
                    packet={"kind": "linkedin_invite", "member": request["target"], "actor": actor},
                    seconds=110,
                )
                answer = InviteResult.model_validate(raw)
                if answer.member != request["target"]:
                    raise ValueError("member")
            except CollectionError as error:
                progress = (await self.db.get_effect(key, conn=conn)).result or {}
                if error.code in STOPPING:
                    reason = error.code
                elif not progress.get("sandbox_id"):
                    # No sandbox ever started: nothing reached LinkedIn.
                    reason = "rate_limited"
                else:
                    reason = "uncertain"
                await self.db.complete_effect(
                    conn, execution_key=key, result={"outcome": "error", "reason": reason}
                )
                return None, reason
            except ValueError:
                await self.db.complete_effect(
                    conn, execution_key=key, result={"outcome": "error", "reason": "uncertain"}
                )
                return None, "uncertain"
            current = (await self.db.get_effect(key, conn=conn)).result or {}
            await self.db.complete_effect(
                conn, execution_key=key, result={**current, "answer": answer.model_dump()}
            )
            return answer, None

    @activity.defn(name="linkedin_requests_check")
    async def check(self, run_id: str) -> dict:
        """Read who accepted. A read that can't run counts as done for today."""

        run = await self._active(run_id)
        member = run.started_by_clerk_user_id
        now = datetime.now(UTC)
        async with self.db.pool.acquire() as conn:
            waiting = await requests.checking(conn, run.id, now)
            last = await conn.fetchval(
                "SELECT max(checked_at) FROM linkedin_request_batches WHERE send_run_id = $1",
                run.id,
            )
            if not waiting:
                await requests.read_at(conn, run.id, now)
                return {"outcome": "nothing"}
        stamp = last.isoformat() if last else "first"
        key = f"{run.id}:requests:check:{stamp}"
        try:
            session, actor = await saved_session(
                self.db, self.settings, self.cipher, run.project_id, member
            )
        except (IntegrationAuthorizationError, WorkflowInputError, IntegrationUpstreamError):
            async with self.db.pool.acquire() as conn:
                await requests.read_at(conn, run.id, now)
            return {"outcome": "unread"}
        async with self.db.effect_lock(key, ENGAGEMENT_OPERATION) as (conn, receipt):
            states = None
            if receipt is not None and receipt.status == "completed":
                states = (receipt.result or {}).get("states")
            elif receipt is None:
                await self.db.start_effect(conn, execution_key=key, operation=ENGAGEMENT_OPERATION)
                try:
                    raw = await self.cloud.engagement(
                        conn,
                        execution_key=key,
                        run_id=run.id,
                        session=session,
                        packet={
                            "kind": "linkedin_relationships",
                            "members": [row["target"] for row in waiting],
                            "actor": actor,
                        },
                    )
                    result = RelationshipsResult.model_validate(raw)
                    states = {p.member: p.state for p in result.people}
                    await self.db.complete_effect(
                        conn, execution_key=key, result={"states": states}
                    )
                except (CollectionError, ValueError) as error:
                    code = getattr(error, "code", "unsupported_contract")
                    await self.db.complete_effect(
                        conn, execution_key=key, result={"outcome": "error", "reason": code}
                    )
                    if code in STOPPING:
                        await self._pause(member, code)
            elif (receipt.result or {}).get("sandbox_id"):
                await self.cloud.cleanup(receipt.result["sandbox_id"])
                await self.db.complete_effect(
                    conn, execution_key=key, result={"outcome": "error", "reason": "interrupted"}
                )
        async with self.db.pool.acquire() as conn, conn.transaction():
            if states is None:
                await requests.read_at(conn, run.id, now)
                return {"outcome": "unread"}
            counts = await requests.checked(conn, run.id, states, now)
        return {"outcome": "read", **counts}

    @activity.defn(name="linkedin_requests_finish")
    async def finish(self, run_id: str) -> None:
        run = await self._active(run_id)
        rows = await self.db.pool.fetch(
            """SELECT q.status,
                   (SELECT value_text FROM record_values v
                    WHERE v.record_id = q.record_id AND v.field = 'name') AS name
               FROM linkedin_requests q JOIN linkedin_request_batches b ON b.id = q.batch_id
               WHERE b.send_run_id = $1 AND q.status <> 'declined'
               ORDER BY q.queued_at, q.id""",
            run.id,
        )
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        summary = summary_sentence(counts)
        project = await self.db.get_project(run.project_id)
        key = f"{run.id}:requests:report"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                publication = receipt.result
            else:
                if receipt is None:
                    await self.db.start_effect(conn, execution_key=key, operation=KEY)
                async with self.db.project_state_lock(conn, project.id):

                    async def save_intent(value):
                        await self.db.save_publication_intent(conn, execution_key=key, intent=value)

                    async def validate_active():
                        await self._active(run.id, conn=conn)

                    revision = await publish_artifacts(
                        storage=self.storage,
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        documents={REPORT_PATH: report(rows, summary).encode()},
                        paths=PATHS,
                        limits=LIMITS,
                        message=f"{KEY} {run.id} [{key}]",
                        intent=(receipt.result or {}).get("publication") if receipt else None,
                        save_intent=save_intent,
                        validate_active=validate_active,
                        replaceable=frozenset(PATHS.values()),
                    )
                publication = {"canonical_commit_sha": revision, "artifact_path": REPORT_PATH}
                await self.db.complete_effect(conn, execution_key=key, result=publication)
        projection = f"{run.id}:requests:projection"
        async with self.db.effect_lock(projection, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=projection, operation=KEY)
            sha = publication["canonical_commit_sha"]
            await self.db._complete_readonly_report_projection(
                conn,
                execution_key=projection,
                run_id=run.id,
                canonical_commit_sha=sha,
                artifact_path=REPORT_PATH,
                artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{REPORT_PATH}",
                summary=summary,
                workflow_key=KEY,
            )

    @activity.defn(name="linkedin_requests_failure")
    async def failure(self, argument: str | dict) -> None:
        run_id, passed = split_failure_input(argument)
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != KEY:
            return
        # Nothing queued is sent once the run has failed, from any batch it held: those people
        # can be chosen again in People.
        await self.db.pool.execute(
            """UPDATE linkedin_requests q SET status = 'lapsed', settled_at = now()
               FROM linkedin_request_batches b
               WHERE b.id = q.batch_id AND (b.id = $1 OR b.send_run_id = $2)
                 AND q.status = 'queued'""",
            UUID(str(run.input["batch_id"])),
            run.id,
        )
        await self.db.project_failure(run_id=run.id, **failure_projection(passed, FALLBACK))


LABELS = {
    "sent": "pending",
    "accepted": "accepted",
    "not_pending": "no longer pending",
    "connected": "already connected",
    "invited": "already invited",
    "unknown_person": "not shown by LinkedIn",
    "refused": "refused by LinkedIn",
    "uncertain": "may have gone out",
    "not_sent": "not sent",
    "lapsed": "not sent within 14 days",
    "queued": "waiting",
}


def summary_sentence(counts: dict[str, int]) -> str:
    sent = sum(counts.get(k, 0) for k in ("sent", "accepted", "not_pending"))
    parts = [f"{sent} connection request{'s' if sent != 1 else ''} sent"]
    if counts.get("accepted"):
        parts.append(f"{counts['accepted']} accepted")
    skipped = sum(counts.get(k, 0) for k in ("connected", "invited", "unknown_person"))
    if skipped:
        parts.append(f"{skipped} skipped, already connected or invited")
    for key in ("refused", "uncertain", "not_sent", "lapsed"):
        if counts.get(key):
            parts.append(f"{counts[key]} {LABELS[key]}")
    return ", ".join(parts) + "."


def report(rows, summary: str) -> str:
    lines = ["# Connection requests", "", summary, "", "| Person | What happened |", "|---|---|"]
    for row in rows:
        name = " ".join(str(row["name"] or "Someone").split()).replace("|", "/")
        lines.append(f"| {name} | {LABELS.get(row['status'], row['status'])} |")
    return "\n".join(lines) + "\n"
