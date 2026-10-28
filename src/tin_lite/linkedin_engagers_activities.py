"""Activities for `social.linkedin_engagers` (#435).

Reads run in the LinkedIn sandbox with the founder's saved session, each under an effect
receipt: the receipt holds a read's answer only until its people are written, then keeps
counts. An interrupted read is never silently bought again. Temporal sees post URNs, page
offsets and counts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import linkedin_engagers as engagers
from tin_lite import linkedin_requests as requests
from tin_lite import people_records as records
from tin_lite.connection_collection import (
    PROVIDER,
    CollectionError,
    cloud_ready,
    enabled,
)
from tin_lite.connection_collection_connection import permission, usable
from tin_lite.connection_collection_store import document
from tin_lite.failures import failure_projection, split_failure_input
from tin_lite.integrations import (
    IntegrationAuthorizationError,
    IntegrationError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
)
from tin_lite.linkedin_cloud import ENGAGEMENT_OPERATION
from tin_lite.linkedin_engagement import PostsResult, ReactionsResult
from tin_lite.linkedin_invitations import RelationshipsResult
from tin_lite.model_providers import (
    MessageRole,
    ModelMessage,
    ModelOutputTruncated,
    ModelProviderError,
    ModelRequest,
)
from tin_lite.model_usage import model_usage_scope
from tin_lite.organic_audit_publication import publish_artifacts
from tin_lite.people_records import Value
from tin_lite.workflow_inputs import WorkflowInputError

logger = logging.getLogger(__name__)

KEY = engagers.KEY
MODEL_SECONDS = 120
UNSCORED = "Tin couldn't score this person this time; the next run tries again."
FALLBACK = "Tin couldn't finish reading who engaged with your LinkedIn posts. Try again later."
# Reasons that end every read in the run: the account needs its owner, or LinkedIn objected.
STOPS = frozenset(
    {"session_expired", "account_changed", "challenge", "rate_limited", "access_denied"}
)
SAYS = {
    "session_expired": (
        "Your saved LinkedIn session has expired. Open Chrome, where Tin for Chrome is signed "
        "in to LinkedIn, and Tin refreshes it; then run this again."
    ),
    "account_changed": (
        "The LinkedIn account signed in to Chrome isn't the one connected to this project. "
        "Sign in to LinkedIn as yourself in Chrome, or reconnect LinkedIn under Integrations, "
        "then run this again."
    ),
    "challenge": (
        "LinkedIn asked to verify your account, so Tin stopped reading. Open LinkedIn in "
        "Chrome and finish its check, then run this again."
    ),
    "rate_limited": "LinkedIn asked Tin to slow down, so Tin stopped reading for now.",
    "access_denied": "LinkedIn refused to show this, so Tin stopped reading for now.",
    "read_interrupted": "An earlier read was interrupted, so Tin skipped it rather than repeat it.",
}


def _heartbeat(detail) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


def refusal(code: str) -> Exception:
    """The founder-facing failure for a read that stopped the whole run."""

    text = SAYS.get(code)
    if code in {"session_expired", "account_changed", "challenge"}:
        return IntegrationAuthorizationError(text)
    if code == "rate_limited":
        return IntegrationRateLimitedError(text)
    if code in {"access_denied", "read_interrupted"}:
        return IntegrationError(text)
    if code in {"unsupported_contract", "unsupported_identity", "unsupported_search_contract"}:
        return IntegrationError("LinkedIn answered in a form Tin doesn't read yet.")
    return IntegrationUpstreamError("Tin's LinkedIn reader didn't finish. Try again later.")


async def saved_session(db, settings, cipher, project_id, member) -> tuple[dict, dict]:
    """A member's saved session and LinkedIn account, under the collection's own checks: cloud
    reading allowed, a live session from a paired browser, and the member's own connection."""

    if not enabled(settings, project_id):
        raise WorkflowInputError("Reading LinkedIn isn't turned on for this project yet.")
    if not cloud_ready(settings) or cipher is None:
        raise IntegrationUpstreamError("Tin's LinkedIn reader isn't available right now.")
    # Each member has their own LinkedIn connection; a run reads its starter's posts.
    connection = await db.pool.fetchrow(
        """SELECT * FROM integration_connections
           WHERE project_id = $1 AND provider_key = $2 AND status = 'connected'
             AND connected_by_clerk_user_id = $3""",
        project_id,
        PROVIDER,
        member,
    )
    if connection is None:
        raise IntegrationAuthorizationError(
            "Connect your own LinkedIn under Integrations, with Tin for Chrome, first."
        )
    config = document(connection["configuration"])
    if not permission(config) or not usable(config) or not connection["credential_ciphertext"]:
        raise IntegrationAuthorizationError(
            "Turn on cloud reading on the LinkedIn card, then open Chrome so Tin for "
            "Chrome can save your session."
        )
    if not await db.pool.fetchval(
        """SELECT EXISTS (SELECT 1 FROM connection_extension_devices
           WHERE connection_id = $1 AND revoked_at IS NULL AND expires_at > now())""",
        connection["id"],
    ):
        raise IntegrationAuthorizationError(SAYS["session_expired"])
    sealed = json.loads(
        cipher.decrypt(
            connection["credential_ciphertext"],
            context=f"credential:{project_id}:{PROVIDER}",
        )
    )
    if sealed.get("version") != 2 or not isinstance(sealed.get("actor"), dict):
        raise IntegrationAuthorizationError(SAYS["session_expired"])
    if datetime.fromisoformat(sealed["expires_at"]) <= datetime.now(UTC):
        raise IntegrationAuthorizationError(SAYS["session_expired"])
    return sealed["session"], sealed["actor"]


async def read_accepted(db, settings, cipher, cloud, run, *, key: str) -> dict:
    """Read who accepted the connection requests of the run's starter on its project that
    still wait for an answer: one receipted read of up to 20, oldest first. A read that can't
    run counts as done for today; LinkedIn objecting pauses their sending, as a send would."""

    member = run.started_by_clerk_user_id
    now = datetime.now(UTC)
    async with db.pool.acquire() as conn:
        waiting = await requests.checking(conn, run.project_id, member, now)
        if not waiting:
            await requests.read_at(conn, run.project_id, member, now)
            return {"outcome": "nothing"}
    try:
        session, actor = await saved_session(db, settings, cipher, run.project_id, member)
    except (IntegrationAuthorizationError, WorkflowInputError, IntegrationUpstreamError):
        async with db.pool.acquire() as conn:
            await requests.read_at(conn, run.project_id, member, now)
        return {"outcome": "unread"}
    async with db.effect_lock(key, ENGAGEMENT_OPERATION) as (conn, receipt):
        states = None
        if receipt is not None and receipt.status == "completed":
            states = (receipt.result or {}).get("states")
        elif receipt is None:
            await db.start_effect(conn, execution_key=key, operation=ENGAGEMENT_OPERATION)
            try:
                raw = await cloud.engagement(
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
                await db.complete_effect(conn, execution_key=key, result={"states": states})
            except (CollectionError, ValueError) as error:
                code = getattr(error, "code", "unsupported_contract")
                await db.complete_effect(
                    conn, execution_key=key, result={"outcome": "error", "reason": code}
                )
                if code in STOPS:
                    async with db.pool.acquire() as other:
                        await requests.pause(other, member, code)
        elif (receipt.result or {}).get("sandbox_id"):
            await cloud.cleanup(receipt.result["sandbox_id"])
            await db.complete_effect(
                conn, execution_key=key, result={"outcome": "error", "reason": "interrupted"}
            )
    async with db.pool.acquire() as conn, conn.transaction():
        if states is None:
            await requests.read_at(conn, run.project_id, member, now)
            return {"outcome": "unread"}
        counts = await requests.checked(conn, run.project_id, member, states, now, run_id=run.id)
    return {"outcome": "read", **counts}


class LinkedInEngagersActivities:
    # The workflow these activities serve, the prefix of their receipts, the scoring step's
    # place in the run's progress and the founder's line when a run fails; the profile viewers
    # workflow reuses the ICP read and the scoring with its own.
    key = KEY
    prefix = "engagers"
    score_step = (2, 4)
    fallback = FALLBACK

    def __init__(
        self,
        *,
        database,
        storage,
        settings,
        cipher,
        cloud,
        router,
        integrations=None,
        temporal=None,
    ):
        self.db, self.storage, self.settings = database, storage, settings
        self.cipher, self.cloud, self.router = cipher, cloud, router
        # Connection requests to the people a read finds start the member's sending run.
        self.integrations, self.temporal = integrations, temporal

    async def _active(self, run_id, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or run.executor != self.key or run.status.value not in {"pending", "running"}:
            raise ApplicationError("This LinkedIn read is no longer active.", non_retryable=True)
        return run

    async def _progress(self, run_id, step: str, current: int, total: int, summary: str):
        await self.db.project_run_progress(
            run_id=run_id, mode="steps", step=step, current=current, total=total, summary=summary
        )
        _heartbeat({"step": step})

    async def _queue(self, run, notes: dict) -> None:
        """Queue a request from the starter to everyone who fits and isn't connected, and say
        how many in the run's line."""

        from tin_lite.linkedin_requests_service import ConnectionRequests

        runtime = SimpleNamespace(
            database=self.db,
            storage=self.storage,
            integrations=self.integrations,
            temporal=self.temporal,
        )
        queued = await ConnectionRequests(runtime, self.settings).queue(
            run.project_id, run.started_by_clerk_user_id, run_id=run.id
        )
        notes["queued"] = queued["queued"]
        if queued.get("refused"):
            notes["queue_refused"] = queued["refused"]

    async def _session(self, run) -> tuple[dict, dict]:
        return await saved_session(
            self.db, self.settings, self.cipher, run.project_id, run.started_by_clerk_user_id
        )

    async def _prepare_icp(self, run) -> None:
        """The ICP file at the project's head, kept in a receipt so every step scores against
        the same words."""

        project = await self.db.get_project(run.project_id)
        key = f"{run.id}:{self.prefix}:icp"
        async with self.db.effect_lock(key, self.key) as (conn, receipt):
            if not (receipt and receipt.status == "completed"):
                await self.db.start_effect(conn, execution_key=key, operation=self.key)
                await self.db.complete_effect(
                    conn, execution_key=key, result=await engagers.read_icp(self.storage, project)
                )
            await records.ensure_fields(conn, run.project_id)

    @activity.defn(name="linkedin_engagers_prepare")
    async def prepare(self, run_id: str) -> None:
        run = await self._active(run_id)
        await self._session(run)
        await self._prepare_icp(run)
        await self.db.mark_run_running(run.id)
        await self._progress(run.id, "posts", 0, 4, "Reading your LinkedIn posts")

    async def _read(self, run, key: str, packet: dict, model):
        """One receipted sandbox read: (answer, None), or (None, reason) when it can't run."""

        session, actor = await self._session(run)
        async with self.db.effect_lock(key, ENGAGEMENT_OPERATION) as (conn, receipt):
            if receipt is not None:
                saved = receipt.result or {}
                if receipt.status == "completed":
                    if saved.get("outcome") == "error":
                        return None, saved.get("reason") or "cloud_failed"
                    return saved, "done"
                if "response" not in saved:
                    # Started, never answered: clean up, and don't buy the same read again.
                    if saved.get("sandbox_id"):
                        await self.cloud.cleanup(saved["sandbox_id"])
                    return None, "read_interrupted"
                return model.model_validate(saved["response"]), None
            await self.db.start_effect(conn, execution_key=key, operation=ENGAGEMENT_OPERATION)
            try:
                answer = await self.cloud.engagement(
                    conn,
                    execution_key=key,
                    run_id=run.id,
                    session=session,
                    packet={**packet, "actor": actor},
                )
                parsed = model.model_validate(answer)
            except CollectionError as error:
                await self.db.complete_effect(
                    conn, execution_key=key, result={"outcome": "error", "reason": error.code}
                )
                return None, error.code
            except ValueError:
                await self.db.complete_effect(
                    conn,
                    execution_key=key,
                    result={"outcome": "error", "reason": "unsupported_contract"},
                )
                return None, "unsupported_contract"
            # Kept until the people are written, so a retry never reads LinkedIn twice.
            current = (await self.db.get_effect(key, conn=conn)).result or {}
            await self.db.save_effect_progress(
                conn, execution_key=key, result={**current, "response": parsed.model_dump()}
            )
            return parsed, None

    @activity.defn(name="linkedin_engagers_posts")
    async def posts(self, run_id: str) -> list[dict]:
        run = await self._active(run_id)
        key = f"{run.id}:engagers:posts"
        parsed, reason = await self._read(
            run,
            key,
            {
                "kind": "linkedin_posts",
                "window_days": engagers.WINDOW_DAYS,
                "limit": engagers.MAX_POSTS,
            },
            PostsResult,
        )
        if reason == "done":
            return parsed["posts"]
        if reason:
            raise refusal(reason)
        posts = [
            {
                "activity_urn": post.activity_urn,
                "thread_urn": post.thread_urn,
                "published_at": post.published_at,
            }
            for post in parsed.posts
        ]
        async with self.db.effect_lock(key, ENGAGEMENT_OPERATION) as (conn, _receipt):
            await self.db.complete_effect(
                conn, execution_key=key, result={"posts": posts, "complete": parsed.complete}
            )
        count = len(posts)
        await self._progress(
            run.id,
            "reactions",
            1,
            4,
            f"Reading who reacted to {count} post{'s' if count != 1 else ''}"
            if count
            else "No posts from the last 90 days",
        )
        return posts

    @activity.defn(name="linkedin_engagers_reactions")
    async def reactions(self, control: dict) -> dict:
        run = await self._active(control["run_id"])
        post = {name: control[name] for name in ("activity_urn", "thread_urn", "published_at")}
        start = int(control["start"])
        key = f"{run.id}:engagers:reactions:{post['thread_urn']}:{start}"
        parsed, reason = await self._read(
            run,
            key,
            {
                "kind": "linkedin_reactions",
                "activity_urn": post["activity_urn"],
                "thread_urn": post["thread_urn"],
                "start": start,
            },
            ReactionsResult,
        )
        if reason == "done":
            return parsed
        if reason:
            outcome = "stopped" if reason in STOPS else "unread"
            return {
                "outcome": outcome,
                "reason": reason,
                "people": 0,
                "next_start": None,
                "capped": False,
            }
        async with self.db.effect_lock(key, ENGAGEMENT_OPERATION) as (conn, _receipt):
            async with conn.transaction():
                counts = await engagers.record_reactions(
                    conn,
                    project_id=run.project_id,
                    run_id=run.id,
                    member=run.started_by_clerk_user_id,
                    post=post,
                    people=[person.model_dump() for person in parsed.people],
                )
                result = {
                    "outcome": "read",
                    "reason": None,
                    "people": counts["people"],
                    "next_start": parsed.next_start,
                    "capped": parsed.capped,
                }
                # The answer leaves the receipt with this; only counts stay.
                await self.db.complete_effect(conn, execution_key=key, result=result)
        _heartbeat({"thread": post["thread_urn"], "start": start})
        return result

    async def _route_pinned(self, run) -> None:
        definition = json.loads(
            await self.storage.read_canonical_artifact(
                repo_id="registry/workflows",
                commit_sha=run.definition_commit_sha,
                path=f"workflows/{self.key}.json",
            )
        )
        route = engagers.ROUTE
        if definition.get("model_route") != {
            "key": route.key,
            "provider": route.provider.value,
            "model": route.model,
            "capabilities": sorted(c.value for c in route.capabilities),
        }:
            raise ApplicationError("Worker does not serve this scoring model.", non_retryable=True)

    async def _score_batch(self, run, batch, *, text: str, profiles: list[str], definition: str):
        """The model's answer for one batch, kept in a receipt before any value is written.

        A batch whose earlier call can't be confirmed isn't bought again; it returns None and
        those people are left for a later run.
        """

        digest = hashlib.sha256(
            (definition + ":" + ",".join(sorted(str(p["id"]) for p in batch))).encode()
        ).hexdigest()[:24]
        key = f"{run.id}:{self.prefix}:score:{digest}"
        async with self.db.effect_lock(key, self.key) as (conn, saved):
            if saved and saved.status == "completed":
                return (saved.result or {}).get("output")
            if saved:
                return None
            await self.db.start_effect(conn, execution_key=key, operation=self.key)
            output = None
            try:
                with model_usage_scope(
                    run_id=run.id, step=f"linkedin_{self.prefix}:icp:{digest}", conn=conn
                ):
                    async with asyncio.timeout(MODEL_SECONDS + 15):
                        result = await self.router.generate(
                            engagers.ROUTE.key,
                            ModelRequest(
                                system=engagers.score_instructions(profiles),
                                messages=(
                                    ModelMessage(
                                        role=MessageRole.USER,
                                        content=engagers.score_input(text, batch),
                                    ),
                                ),
                                output_schema=engagers.score_schema(profiles),
                                output_schema_name="icp_scores",
                                max_output_tokens=engagers.MAX_OUTPUT_TOKENS,
                            ),
                            timeout_seconds=MODEL_SECONDS,
                        )
                output = result.parsed
            except (ModelProviderError, ModelOutputTruncated, TimeoutError) as error:
                # A check on model output never fails the run that paid for it.
                logger.warning("ICP scoring batch failed for run %s: %s", run.id, type(error))
            await self.db.complete_effect(conn, execution_key=key, result={"output": output})
            return output

    @activity.defn(name="linkedin_engagers_score")
    async def score(self, run_id: str) -> None:
        await self._score(run_id)

    async def _score(self, run_id: str) -> None:
        run = await self._active(run_id)
        icp = (await self.db.get_effect(f"{run.id}:{self.prefix}:icp")).result or {}
        if not icp.get("profiles"):
            return
        if self.router is None:
            raise IntegrationUpstreamError("Tin's scoring model isn't available right now.")
        await self._route_pinned(run)
        project = await self.db.get_project(run.project_id)
        raw = await self.storage.read_canonical_artifact_if_exists(
            repo_id=project.state_repo_id, commit_sha=icp["revision"], path=icp["path"]
        )
        text = (raw or b"")[: engagers.ICP_BYTES].decode("utf-8", "replace")
        await self._progress(run.id, "score", *self.score_step, "Scoring people against your ICP")
        tried: list[UUID] = []
        while len(tried) < engagers.MAX_SCORED:
            async with self.db.pool.acquire() as conn:
                batch = await engagers.unscored(
                    conn,
                    run.project_id,
                    icp["definition"],
                    limit=engagers.SCORE_BATCH,
                    skip=tried,
                )
            if not batch:
                break
            tried += [person["id"] for person in batch]
            output = await self._score_batch(
                run, batch, text=text, profiles=icp["profiles"], definition=icp["definition"]
            )
            if output is None:
                # Left without this definition, so a later run scores them.
                unscored = [Value("icp", None, UNSCORED), Value("icp_fit", None, UNSCORED)]
                written = [(person, unscored, "") for person in batch]
            else:
                written = [
                    (person, values, icp["definition"])
                    for person, values in zip(
                        batch, engagers.scores(output, batch, icp["profiles"]), strict=True
                    )
                ]
            async with self.db.pool.acquire() as conn:
                for person, values, definition in written:
                    try:
                        await records.set_values(
                            conn,
                            run.project_id,
                            person["id"],
                            values,
                            set_by_kind="run",
                            set_by=str(run.id),
                            definition=definition,
                            refresh=bool(definition),
                        )
                    except records.RecordError as error:
                        # The founder forgot this person while the model scored them.
                        if error.code != "record_gone":
                            raise
            _heartbeat({"scored": len(tried)})

    @activity.defn(name="linkedin_engagers_publish")
    async def publish(self, control: dict) -> None:
        run = await self._active(control["run_id"])
        notes = dict(control["notes"])
        icp = (await self.db.get_effect(f"{run.id}:engagers:icp")).result or {}
        notes["icp"] = icp.get("path")
        if notes.get("stopped"):
            notes["notice"] = SAYS.get(notes["stopped"], SAYS["access_denied"])
        notes["new_people"] = await self.db.pool.fetchval(
            "SELECT count(*) FROM records WHERE project_id = $1 AND source_run_id = $2",
            run.project_id,
            run.id,
        )
        await self._queue(run, notes)
        await self._progress(run.id, "publish", 3, 4, "Writing the list")
        project = await self.db.get_project(run.project_id)
        key = f"{run.id}:engagers:files"
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                publication = receipt.result
            else:
                async with self.db.project_state_lock(conn, project.id):
                    saved = (receipt.result or {}) if receipt else {}
                    documents = saved.get("documents")
                    intent = saved.get("publication")
                    if not isinstance(documents, dict):
                        if receipt is None:
                            await self.db.start_effect(conn, execution_key=key, operation=KEY)
                        documents = engagers.render(
                            await engagers.engagers(
                                conn, run.project_id, run.started_by_clerk_user_id
                            ),
                            notes=notes,
                            now=datetime.now(UTC),
                        )
                        await self.db.save_effect_progress(
                            conn, execution_key=key, result={"documents": documents}
                        )
                        intent = None

                    async def save_intent(value):
                        await self.db.save_publication_intent(conn, execution_key=key, intent=value)

                    async def validate_active():
                        await self._active(run.id, conn=conn)

                    revision = await publish_artifacts(
                        storage=self.storage,
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        documents={path: text.encode() for path, text in documents.items()},
                        paths=engagers.PATHS,
                        limits=engagers.LIMITS,
                        message=f"{KEY} {run.id} [{key}]",
                        intent=intent,
                        save_intent=save_intent,
                        validate_active=validate_active,
                        # Tin's own list: each run replaces the last one.
                        replaceable=frozenset(engagers.PATHS.values()),
                    )
                publication = {
                    "canonical_commit_sha": revision,
                    "artifact_path": engagers.SUMMARY_PATH,
                }
                await self.db.complete_effect(conn, execution_key=key, result=publication)
        projection = f"{run.id}:engagers:projection"
        async with self.db.effect_lock(projection, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=projection, operation=KEY)
            sha, path = publication["canonical_commit_sha"], publication["artifact_path"]
            # What the founder can do comes first: the run's summary line is short.
            summary = " ".join(filter(None, (notes.get("notice"), engagers.sentence(notes))))
            await self.db._complete_readonly_report_projection(
                conn,
                execution_key=projection,
                run_id=run.id,
                canonical_commit_sha=sha,
                artifact_path=path,
                artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{path}",
                summary=summary.strip(),
                workflow_key=KEY,
            )

    async def _accepted(self, run_id: str) -> dict:
        """Who accepted the starter's connection requests on the project, read about once a day
        by whichever of their LinkedIn reads comes first. History keeps only the count."""

        run = await self._active(run_id)
        async with self.db.pool.acquire() as conn:
            due = await requests.reading_due(
                conn, run.project_id, run.started_by_clerk_user_id, datetime.now(UTC)
            )
        if not due:
            return {"outcome": "nothing", "accepted": 0}
        read = await read_accepted(
            self.db,
            self.settings,
            self.cipher,
            self.cloud,
            run,
            key=f"{run.id}:{self.prefix}:accepted",
        )
        return {"outcome": read["outcome"], "accepted": read.get("accepted", 0)}

    @activity.defn(name="linkedin_engagers_accepted")
    async def accepted(self, run_id: str) -> dict:
        return await self._accepted(run_id)

    @activity.defn(name="linkedin_engagers_failure")
    async def failure(self, argument: str | dict) -> None:
        await self._failure(argument)

    async def _failure(self, argument: str | dict) -> None:
        run_id, passed = split_failure_input(argument)
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != self.key:
            return
        await self.db.project_failure(run_id=run.id, **failure_projection(passed, self.fallback))
