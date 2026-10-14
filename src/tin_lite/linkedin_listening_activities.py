"""LinkedIn listening's activities: one post's timed readings, and the daily results run.

Only run IDs and small control facts (a checkpoint name, the post's publish time, an outcome)
enter Temporal history. Readings go to Postgres, and the results files are rewritten from it.
Every read names accounts from the project's own LinkedIn connection, never from a request.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite.domain import LINKEDIN_LISTEN_READ_ATTEMPTS
from tin_lite.failures import failure_projection, split_failure_input
from tin_lite.integrations import (
    IntegrationAuthorizationError,
    IntegrationError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
)
from tin_lite.linkedin_listening import (
    CHECKPOINTS,
    LABELS,
    LIMITS,
    LISTEN_KEY,
    LISTEN_WORKFLOW_ID,
    PATHS,
    RESULTS_KEY,
    RESULTS_MD,
    WINDOW_DAYS,
    connected,
    counts,
    daily_label,
    due,
    excerpt,
    listen_start_key,
    mark_removed,
    published_post,
    record_followers,
    record_reading,
    render,
    snapshot,
    track,
    tracked,
    untracked_publications,
)
from tin_lite.organic_audit_publication import publish_artifacts
from tin_lite.zernio import ACCOUNT_GONE, ANALYTICS_REFUSED, RESULTS_UNAVAILABLE

logger = logging.getLogger(__name__)

PUBLISH_KEY = "social.linkedin_publish"
UNANSWERED = "LinkedIn didn't answer"
LISTEN_FALLBACK = (
    f"Tin stopped reading this LinkedIn post's results. The readings it took are in {RESULTS_MD}."
)
RESULTS_FALLBACK = "Tin couldn't read today's LinkedIn results. It reads them again next time."
# Zernio not answering, or limiting Tin: a reading retries, then is skipped.
UNAVAILABLE = (IntegrationUpstreamError, IntegrationRateLimitedError)


def _attempt() -> int:
    # Outside Temporal (tests, a direct call) there is no later attempt.
    return activity.info().attempt if activity.in_activity() else LINKEDIN_LISTEN_READ_ATTEMPTS


def _heartbeat(detail) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


def _active_accounts(connection) -> list[dict]:
    return [item for item in connection.configuration.get("accounts") or [] if item.get("active")]


class LinkedInListeningActivities:
    def __init__(self, *, database, storage, integrations, settings):
        self.db, self.storage = database, storage
        self.integrations, self.settings = integrations, settings

    @property
    def posting(self):
        return self.integrations.linkedin_posting

    async def _active(self, run_id, executor: str, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or run.executor != executor or run.status.value not in {"pending", "running"}:
            raise ApplicationError("LinkedIn listening is no longer active.", non_retryable=True)
        return run

    async def _progress(self, run_id, step: str, current: int, total: int, summary: str):
        await self.db.project_run_progress(
            run_id=run_id, mode="steps", step=step, current=current, total=total, summary=summary
        )
        _heartbeat({"step": step})

    async def _accounts(self, project_id) -> list[dict]:
        """The project's connected accounts; none once LinkedIn posting is disconnected."""

        try:
            return _active_accounts(await self.posting.connection(project_id))
        except IntegrationError:
            return []

    async def _publish(self, run, *, key: str, operation: str, accounts: list[dict]) -> dict:
        """Rewrite the results files from Postgres, once per key.

        Writes are serialized per project and each renders the record inside that lock. The
        rendered bytes wait in the receipt until the commit lands, so a retry after a lost
        answer reconciles the same bytes; the receipt then keeps only the commit.
        """

        project = await self.db.get_project(run.project_id)
        if project is None:
            raise ApplicationError("The project is unavailable.", non_retryable=True)
        async with self.db.effect_lock(key, operation) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result
            await self._active(run.id, operation, conn=conn)
            async with self.db.project_state_lock(conn, project.id):
                saved = (receipt.result or {}) if receipt else {}
                documents = saved.get("documents")
                intent = saved.get("publication")
                if not isinstance(documents, dict):
                    await self.db.start_effect(conn, execution_key=key, operation=operation)
                    now = datetime.now(UTC)
                    record = await snapshot(self.db, project.id, now=now, conn=conn)
                    documents = render(record, accounts, now=now)
                    await self.db.save_effect_progress(
                        conn, execution_key=key, result={"documents": documents}
                    )
                    intent = None

                async def save_intent(value):
                    await self.db.save_publication_intent(conn, execution_key=key, intent=value)

                async def validate_active():
                    await self._active(run.id, operation, conn=conn)

                revision = await publish_artifacts(
                    storage=self.storage,
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    documents={path: text.encode() for path, text in documents.items()},
                    paths=PATHS,
                    limits=LIMITS,
                    message=f"{operation} {run.id} [{key}]",
                    intent=intent,
                    save_intent=save_intent,
                    validate_active=validate_active,
                    # Tin's own files: each write replaces the last one.
                    replaceable=frozenset(PATHS.values()),
                )
            value = {"canonical_commit_sha": revision, "artifact_path": RESULTS_MD}
            await self.db.complete_effect(conn, execution_key=key, result=value)
            return value

    async def _project(self, run, key: str, publication: dict, summary: str, workflow_key: str):
        project = await self.db.get_project(run.project_id)
        async with self.db.effect_lock(key, workflow_key) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=workflow_key)
            sha, path = publication["canonical_commit_sha"], publication["artifact_path"]
            await self.db._complete_readonly_report_projection(
                conn,
                execution_key=key,
                run_id=run.id,
                canonical_commit_sha=sha,
                artifact_path=path,
                artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{path}",
                summary=summary,
                workflow_key=workflow_key,
            )

    # One post Tin published, read at fixed times after posting.

    @activity.defn(name="linkedin_listen_start")
    async def start(self, publish_run_id: str) -> dict | None:
        """Prepare listening for a post Tin just published; None when there's nothing to read.

        Listening never changes how publishing ended: a refusal here is logged, and the daily
        run picks the post up instead.
        """

        from tin_lite.billing_contracts import BillingError
        from tin_lite.run_service import WorkflowExecutorUnavailableError, start_workflow_run
        from tin_lite.workflow_inputs import WorkflowInputError
        from tin_lite.workflow_prerequisites import PrerequisiteError

        publish = await self.db.get_run(UUID(str(publish_run_id)))
        if (
            publish is None
            or publish.executor != PUBLISH_KEY
            or publish.status.value != "succeeded"
            or await published_post(self.db, publish) is None
        ):
            return None
        workflow = await self.db.get_workflow(LISTEN_WORKFLOW_ID)
        if workflow is None:
            logger.warning("LinkedIn listening is not in the catalog; sync the workflow catalog")
            return None
        try:
            run = await start_workflow_run(
                runtime=SimpleNamespace(
                    database=self.db, storage=self.storage, integrations=self.integrations
                ),
                settings=self.settings,
                workflow=workflow,
                project_id=publish.project_id,
                started_by_clerk_user_id=publish.started_by_clerk_user_id,
                start_idempotency_key=listen_start_key(publish.id),
                input_payload={"publish_run_id": str(publish.id)},
                trigger_source=publish.trigger_source,
                trigger_client=publish.trigger_client,
                started_by_oauth_client_id=publish.started_by_oauth_client_id,
                _prepare_only=True,
                _linkedin_listen=True,
            )
        except (
            BillingError,
            IntegrationError,
            LookupError,
            PrerequisiteError,
            WorkflowExecutorUnavailableError,
            WorkflowInputError,
        ) as exc:
            logger.warning(
                "LinkedIn listening did not start for %s: %s", publish.id, type(exc).__name__
            )
            return None
        if run.status.value not in {"pending", "running"}:
            return None
        return {
            "run_id": str(run.id),
            "executor": run.executor,
            "temporal_workflow_id": run.temporal_workflow_id,
        }

    @activity.defn(name="linkedin_listen_prepare")
    async def prepare(self, run_id: str) -> dict:
        key = f"{run_id}:linkedin_listen:prepare"
        async with self.db.effect_lock(key, LISTEN_KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return {"published_at": receipt.result["published_at"]}
            run = await self._active(run_id, LISTEN_KEY, conn=conn)
            publish = await self.db.get_run(UUID(str(run.input["publish_run_id"])), conn=conn)
            if (
                publish is None
                or publish.project_id != run.project_id
                or publish.executor != PUBLISH_KEY
                or publish.status.value != "succeeded"
            ):
                raise ApplicationError(
                    "Tin reads only posts it published in this project.", non_retryable=True
                )
            post = await published_post(self.db, publish, conn=conn)
            if post is None:
                raise ApplicationError(
                    "Tin couldn't find the post it published, so there is nothing to read.",
                    non_retryable=True,
                )
            await track(self.db, project_id=run.project_id, post=post, conn=conn)
            published = post["published_at"].isoformat()
            await self.db.start_effect(conn, execution_key=key, operation=LISTEN_KEY)
            await self.db.complete_effect(
                conn, execution_key=key, result={"urn": post["urn"], "published_at": published}
            )
        await self.db.mark_run_running(run.id)
        await self._progress(
            run.id, "listen", 0, len(CHECKPOINTS), "The first reading is 1 hour after posting."
        )
        return {"published_at": published}

    async def _read_post(self, run, post: dict, checkpoint: str) -> dict:
        connection = await self.posting.connection(run.project_id)
        if not connected(_active_accounts(connection), post):
            raise IntegrationAuthorizationError(ACCOUNT_GONE)
        try:
            values = await self.posting.zernio.post_analytics(post["account_id"], post["urn"])
        except UNAVAILABLE:
            if _attempt() < LINKEDIN_LISTEN_READ_ATTEMPTS:
                raise
            return {"outcome": "skipped", "reason": UNANSWERED}
        if values is None:
            await mark_removed(self.db, project_id=run.project_id, urn=post["urn"])
            return {"outcome": "removed"}
        await record_reading(
            self.db,
            project_id=run.project_id,
            urn=post["urn"],
            checkpoint=checkpoint,
            values=values,
        )
        return {"outcome": "read", "values": values}

    @activity.defn(name="linkedin_listen_read")
    async def read(self, control: dict) -> str:
        run_id, checkpoint = str(control["run_id"]), control["checkpoint"]
        if checkpoint not in CHECKPOINTS:
            raise ApplicationError("Unknown LinkedIn reading.", non_retryable=True)
        key = f"{run_id}:linkedin_listen:{checkpoint}"
        run = await self._active(run_id, LISTEN_KEY)
        reading = await self.db.get_effect(key)
        if reading is None or reading.status != "completed":
            prepared = await self.db.get_effect(f"{run_id}:linkedin_listen:prepare")
            post = await tracked(self.db, run.project_id, prepared.result["urn"])
            value = await self._read_post(run, post, checkpoint)
            async with self.db.effect_lock(key, LISTEN_KEY) as (conn, saved):
                if saved is None or saved.status != "completed":
                    await self.db.start_effect(conn, execution_key=key, operation=LISTEN_KEY)
                    await self.db.complete_effect(conn, execution_key=key, result=value)
            reading = await self.db.get_effect(key)
        value = reading.result
        if value["outcome"] != "skipped":
            try:
                await self._publish(
                    run,
                    key=f"{run_id}:linkedin_results:{checkpoint}",
                    operation=LISTEN_KEY,
                    accounts=await self._accounts(run.project_id),
                )
            except ApplicationError:
                raise
            except Exception:
                if _attempt() < LINKEDIN_LISTEN_READ_ATTEMPTS:
                    raise
                # The reading is kept, and the next write of the files includes it.
                logger.exception("LinkedIn results files were not written for %s", run_id)
        order = list(CHECKPOINTS)
        position = order.index(checkpoint) + 1
        later = (
            f" The next reading is {LABELS[order[position]]} after posting."
            if (position < len(order) and value["outcome"] != "removed")
            else ""
        )
        summary = {
            "read": lambda: f"After {LABELS[checkpoint]}: {counts(value.get('values') or {})}.",
            "removed": lambda: "The post was removed from LinkedIn, so Tin stopped reading it.",
            "skipped": lambda: f"Skipped the reading after {LABELS[checkpoint]}: {UNANSWERED}.",
        }[value["outcome"]]()
        await self._progress(run.id, checkpoint, position, len(order), (summary + later)[:240])
        return value["outcome"]

    @activity.defn(name="linkedin_listen_finish")
    async def finish(self, run_id: str) -> None:
        key = f"{run_id}:linkedin_listen:projection"
        done = await self.db.get_effect(key)
        if done and done.status == "completed":
            return
        run = await self._active(run_id, LISTEN_KEY)
        outcomes = {}
        publication = None
        for checkpoint in CHECKPOINTS:
            reading = await self.db.get_effect(f"{run_id}:linkedin_listen:{checkpoint}")
            if not reading or reading.status != "completed":
                continue
            outcomes[checkpoint] = reading.result
            if reading.result["outcome"] != "skipped":
                written = await self.db.get_effect(f"{run_id}:linkedin_results:{checkpoint}")
                done = written is not None and written.status == "completed"
                publication = written.result if done else None
        if publication is None:
            # The last reading's files weren't written, or every reading was skipped.
            publication = await self._publish(
                run,
                key=f"{run_id}:linkedin_results:final",
                operation=LISTEN_KEY,
                accounts=await self._accounts(run.project_id),
            )
        await self._project(run, key, publication, listen_summary(outcomes), LISTEN_KEY)

    @activity.defn(name="linkedin_listen_failure")
    async def listen_failure(self, argument: str | dict) -> None:
        run_id, passed = split_failure_input(argument)
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != LISTEN_KEY:
            return
        await self.db.project_failure(run_id=run.id, **failure_projection(passed, LISTEN_FALLBACK))

    # The daily run: every tracked post from the window, new page posts, and followers.

    @activity.defn(name="linkedin_results_prepare")
    async def results_prepare(self, run_id: str) -> None:
        run = await self._active(run_id, RESULTS_KEY)
        # Without a connected account there is nothing to read; the founder can reconnect.
        if not _active_accounts(await self.posting.connection(run.project_id)):
            raise IntegrationAuthorizationError("Reconnect LinkedIn posting under Integrations.")
        await self.db.mark_run_running(run.id)
        await self._progress(run.id, "prepare", 0, 2, "Reading LinkedIn results")

    @activity.defn(name="linkedin_results_read")
    async def results_read(self, run_id: str) -> None:
        key = f"{run_id}:linkedin_results:read"
        saved = await self.db.get_effect(key)
        if saved and saved.status == "completed":
            return
        run = await self._active(run_id, RESULTS_KEY)
        now = datetime.now(UTC)
        connection = await self.posting.connection(run.project_id)
        accounts = _active_accounts(connection)
        zernio = self.posting.zernio
        notes = {"read": 0, "new": 0, "removed": 0, "skipped": 0, "unanswered": 0}
        refused: set[str] = set()

        # Posts Tin published whose listening run never started or never tracked them.
        for publish_id in await untracked_publications(self.db, run.project_id, now=now):
            publish = await self.db.get_run(publish_id)
            post = await published_post(self.db, publish) if publish else None
            if post is not None:
                await track(self.db, project_id=run.project_id, post=post)
        # Page posts made directly on LinkedIn; a profile's can't be listed.
        for account in accounts:
            if account["kind"] != "organization":
                continue
            try:
                posts = await zernio.page_posts(account["id"])
            except IntegrationAuthorizationError:
                refused.add(account["urn"])
                continue
            except UNAVAILABLE:
                notes["unanswered"] += 1
                continue
            for item in posts:
                published = datetime.fromisoformat(item["published_at"])
                if published <= now - timedelta(days=WINDOW_DAYS + 1):
                    continue
                if await tracked(self.db, run.project_id, item["urn"]) is None:
                    notes["new"] += 1
                await track(
                    self.db,
                    project_id=run.project_id,
                    post={
                        "urn": item["urn"],
                        "account_id": account["id"],
                        "account_urn": account["urn"],
                        "account_kind": account["kind"],
                        "account_name": account["name"],
                        "url": item["url"],
                        "published_at": published,
                        "excerpt": excerpt(item["text"]),
                    },
                )
        # One reading of each post from the window.
        for post in await due(self.db, run.project_id, now=now):
            if post["account_urn"] in refused or not connected(accounts, post):
                notes["skipped"] += 1
                continue
            try:
                values = await zernio.post_analytics(post["account_id"], post["urn"])
            except IntegrationAuthorizationError:
                refused.add(post["account_urn"])
                notes["skipped"] += 1
                continue
            except UNAVAILABLE:
                notes["unanswered"] += 1
                continue
            if values is None:
                await mark_removed(self.db, project_id=run.project_id, urn=post["urn"])
                notes["removed"] += 1
                continue
            await record_reading(
                self.db,
                project_id=run.project_id,
                urn=post["urn"],
                checkpoint=daily_label(post["published_at"], now),
                values=values,
                replace=True,
            )
            notes["read"] += 1
            _heartbeat({"read": notes["read"]})
        # Each account's follower count today.
        followers = {}
        try:
            followers = await zernio.follower_counts([item["id"] for item in accounts])
        except UNAVAILABLE:
            notes["unanswered"] += 1
        for account in accounts:
            if account["id"] in followers:
                await record_followers(
                    self.db,
                    project_id=run.project_id,
                    account_urn=account["urn"],
                    day=now.date(),
                    followers=followers[account["id"]],
                )
        if notes["unanswered"] and not notes["read"] and not followers:
            # Nothing came back at all: try again, then fail as temporary.
            raise IntegrationUpstreamError(RESULTS_UNAVAILABLE)
        if accounts and refused >= {item["urn"] for item in accounts}:
            raise IntegrationAuthorizationError(ANALYTICS_REFUSED)
        notes["refused"] = sorted(item["name"] for item in accounts if item["urn"] in refused)
        notes["followers"] = [
            {"name": item["name"], "followers": followers[item["id"]]}
            for item in accounts
            if item["id"] in followers
        ]
        async with self.db.effect_lock(key, RESULTS_KEY) as (conn, receipt):
            if receipt is None or receipt.status != "completed":
                await self.db.start_effect(conn, execution_key=key, operation=RESULTS_KEY)
                await self.db.complete_effect(conn, execution_key=key, result=notes)
        await self._progress(run.id, "read", 1, 2, "Writing the LinkedIn results")

    @activity.defn(name="linkedin_results_publish")
    async def results_publish(self, run_id: str) -> None:
        key = f"{run_id}:linkedin_results:projection"
        done = await self.db.get_effect(key)
        if done and done.status == "completed":
            return
        run = await self._active(run_id, RESULTS_KEY)
        notes = (await self.db.get_effect(f"{run_id}:linkedin_results:read")).result
        publication = await self._publish(
            run,
            key=f"{run_id}:linkedin_results:files",
            operation=RESULTS_KEY,
            accounts=await self._accounts(run.project_id),
        )
        await self._project(run, key, publication, results_summary(notes), RESULTS_KEY)

    @activity.defn(name="linkedin_results_failure")
    async def results_failure(self, argument: str | dict) -> None:
        run_id, passed = split_failure_input(argument)
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != RESULTS_KEY:
            return
        await self.db.project_failure(run_id=run.id, **failure_projection(passed, RESULTS_FALLBACK))


def listen_summary(outcomes: dict[str, dict]) -> str:
    """The listening run's last line: what it read, and what it couldn't."""

    read = [name for name, value in outcomes.items() if value["outcome"] == "read"]
    skipped = [name for name, value in outcomes.items() if value["outcome"] == "skipped"]
    removed = any(value["outcome"] == "removed" for value in outcomes.values())
    parts = []
    if read:
        last = outcomes[read[-1]]["values"]
        parts.append(f"After {LABELS[read[-1]]}: {counts(last)}.")
    if removed:
        parts.append("The post was removed from LinkedIn, so Tin stopped reading it.")
    if skipped:
        names = ", ".join(LABELS[name] for name in skipped)
        parts.append(f"Skipped after {names}: {UNANSWERED}.")
    if not parts:
        parts.append("No reading was taken.")
    return " ".join(parts)


def results_summary(notes: dict) -> str:
    read, removed = notes["read"], notes["removed"]
    parts = []
    if read:
        parts.append(f"Read {read} LinkedIn post{'s' if read != 1 else ''} from the last 30 days.")
    elif not (removed or notes["skipped"] or notes["unanswered"]):
        parts.append("No LinkedIn posts from the last 30 days to read.")
    if notes["new"]:
        parts.append(f"Found {notes['new']} new page post{'s' if notes['new'] != 1 else ''}.")
    if removed:
        parts.append(
            f"{removed} {'was' if removed == 1 else 'were'} removed from LinkedIn, "
            "so Tin stopped reading "
            f"{'it' if removed == 1 else 'them'}."
        )
    if notes.get("followers"):
        parts.append(
            "Followers: "
            + ", ".join(f"{item['name']} {item['followers']:,}" for item in notes["followers"])
            + "."
        )
    if notes.get("refused"):
        parts.append(f"Reconnect LinkedIn posting to read {' and '.join(notes['refused'])}.")
    if notes["unanswered"]:
        count = notes["unanswered"]
        parts.append(
            f"{UNANSWERED} {count} time{'s' if count != 1 else ''}; the next run reads again."
        )
    return " ".join(parts)
