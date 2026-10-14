"""One approved LinkedIn post through Zernio, with approval-keyed receipts around its writes.

Only the run ID enters Temporal history. The approved copy and image bytes stay in
Postgres/code.storage and are never returned from these activities.

Unlike X, Zernio takes an `Idempotency-Key` and returns the original post for a repeated key
within 24 hours. A post whose answer was lost is therefore sent again with the same key while
that window is open; after it, the run stops and asks the founder to check the page.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite.failures import failure_projection, payload_failure, split_failure_input
from tin_lite.integrations import IntegrationAuthorizationError, IntegrationDeliveryRefusedError
from tin_lite.linkedin_posting import may_post_as
from tin_lite.linkedin_posts import KEY, approved_payload, post_key
from tin_lite.organic_audit_publication import publish_artifacts

# Zernio keeps an Idempotency-Key for 24 hours; stop resending well inside that.
REPLAY_WINDOW = timedelta(hours=23)
SETTLE_POLLS = 20
SETTLE_DELAY_SECONDS = 6
FAILED = {
    "auth_expired": "LinkedIn signed Tin out of this account. Reconnect LinkedIn posting "
    "under Integrations, then preview the post again.",
    "account_issue": "LinkedIn refused the account this post was sent as. Reconnect LinkedIn "
    "posting under Integrations, then preview the post again.",
    "user_content": "LinkedIn refused this post's content, for example as a duplicate of an "
    "earlier post. Change it, then preview it again.",
    "platform_rate_limit": "LinkedIn is limiting how often this account posts. Try again later.",
}
NOT_PUBLISHED = "LinkedIn did not publish the post. Check the page, then try again."
STILL_PUBLISHING = (
    "LinkedIn accepted the post and may still be publishing it. "
    "Check the page before posting again."
)


def _media_key(approval_id: str, index: int) -> str:
    return f"linkedin:media:{approval_id}:{index}"


def _uncertain() -> ApplicationError:
    return ApplicationError(
        "LinkedIn may already have published this post. Check the page before trying again.",
        non_retryable=True,
    )


class LinkedInPublishActivities:
    def __init__(self, *, database, storage, integrations):
        self.db, self.storage, self.integrations = database, storage, integrations

    @property
    def posting(self):
        return self.integrations.linkedin_posting

    async def _active(self, run_id: str, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or run.executor != KEY or run.status.value not in {"pending", "running"}:
            raise ApplicationError("LinkedIn publication is no longer active.", non_retryable=True)
        return run

    async def _allowed(self, run, actor: str, *, conn=None) -> None:
        if not actor or not await self.db.has_project_access(
            project_id=run.project_id, clerk_user_id=actor, conn=conn
        ):
            raise ApplicationError("LinkedIn publishing access was removed.", non_retryable=True)

    async def _progress(self, run_id: UUID, stage: str, number: int, summary: str):
        await self.db.project_run_progress(
            run_id=run_id, mode="steps", step=stage, current=number, total=4, summary=summary
        )
        if activity.in_activity():
            activity.heartbeat({"step": stage})

    async def _image(self, project, payload: dict, index: int, item: dict) -> str:
        """Upload one approved image, once; an upload publishes nothing on its own."""

        key = _media_key(payload["approval_id"], index)
        intent = {"sha256": item["sha256"]}
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                if (receipt.result or {}).get("intent") != intent:
                    raise ApplicationError("LinkedIn image receipt changed.", non_retryable=True)
                return receipt.result["value"]["url"]
            raw = await self.storage.read_project_media(
                repo_id=project.state_repo_id, revision=payload["revision"], path=item["path"]
            )
            if hashlib.sha256(raw).hexdigest() != item["sha256"] or len(raw) != item["bytes"]:
                raise ApplicationError(
                    "An approved image changed or is unavailable. Nothing was posted.",
                    non_retryable=True,
                )
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            url = await self.posting.zernio.upload_media(
                filename=item["path"].rsplit("/", 1)[-1][:100],
                media_type=item["media_type"],
                content=raw,
            )
            await self.db.complete_effect(
                conn, execution_key=key, result={"intent": intent, "value": {"url": url}}
            )
            return url

    async def _send(self, run, actor: str, payload: dict, media: list[dict]) -> dict:
        key = post_key(payload["approval_id"])
        intent = {"account_id": payload["account_id"], "fingerprint": payload["fingerprint"]}
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                if (receipt.result or {}).get("intent") != intent:
                    raise ApplicationError("LinkedIn delivery receipt changed.", non_retryable=True)
                return receipt.result["value"]
            await self._active(str(run.id), conn=conn)
            await self._allowed(run, actor, conn=conn)
            if receipt is not None:
                saved = receipt.result or {}
                attempted = saved.get("attempted_at")
                if saved.get("intent") != intent or not isinstance(attempted, str):
                    raise _uncertain()
                if datetime.now(UTC) - datetime.fromisoformat(attempted) > REPLAY_WINDOW:
                    raise _uncertain()
                # Inside Zernio's window the same key returns the post if the first attempt
                # made one, and makes it if not.
            else:
                await self.db.start_effect(conn, execution_key=key, operation=KEY)
                await self.db.save_effect_progress(
                    conn,
                    execution_key=key,
                    result={"intent": intent, "attempted_at": datetime.now(UTC).isoformat()},
                )
            try:
                value = await self.posting.zernio.create_post(
                    account_id=payload["account_id"],
                    text=payload["text"],
                    media=media,
                    idempotency_key=f"tin-linkedin-{payload['approval_id']}",
                )
            except (IntegrationDeliveryRefusedError, IntegrationAuthorizationError):
                # Zernio answered and made nothing, so a later attempt may send it.
                await self.db.discard_started_effect(conn, execution_key=key)
                raise
            await self.db.complete_effect(
                conn, execution_key=key, result={"intent": intent, "value": value}
            )
            return value

    async def _settle(self, post: dict) -> dict:
        """Wait for LinkedIn's answer when Zernio accepted the post but hasn't published it."""

        for _ in range(SETTLE_POLLS):
            if post["status"] in {"published", "failed", "cancelled"} and (
                post["status"] != "published" or post["url"]
            ):
                return post
            if activity.in_activity():
                activity.heartbeat({"post": "publishing"})
            await asyncio.sleep(SETTLE_DELAY_SECONDS)
            post = await self.posting.zernio.get_post(post["post_id"])
        # Retrying the activity reads the same post again; nothing is sent twice.
        raise ApplicationError(STILL_PUBLISHING)

    async def _publish_report(self, run, project, post: dict, payload: dict) -> dict:
        key = f"{run.id}:linkedin_publish_report"
        path = f"reports/linkedin/{run.id}.md"
        account = payload["account"]
        document = (
            "# LinkedIn post published\n\n"
            f"Posted as: {account['name']} ({account['urn']})\n\n"
            f"URL: {post['url']}\n\n"
            "## Published text\n\n"
            f"{payload['text']}\n"
        ).encode()
        async with self.db.effect_lock(key, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return saved.result
            await self._active(str(run.id), conn=conn)
            await self.db.start_effect(conn, execution_key=key, operation=KEY)

            async def save_intent(intent):
                await self.db.save_publication_intent(conn, execution_key=key, intent=intent)

            async def validate_active():
                await self._active(str(run.id), conn=conn)

            async with self.db.project_state_lock(conn, project.id):
                revision = await publish_artifacts(
                    storage=self.storage,
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    documents={path: document},
                    paths={"LINKEDIN_PUBLISH.md": path},
                    limits={"LINKEDIN_PUBLISH.md": 16_000},
                    message=f"{KEY} {run.id} [{key}]",
                    intent=(saved.result or {}).get("publication") if saved else None,
                    save_intent=save_intent,
                    validate_active=validate_active,
                )
            value = {"canonical_commit_sha": revision, "artifact_path": path}
            await self.db.complete_effect(conn, execution_key=key, result=value)
            return value

    @activity.defn(name="linkedin_publish_execute")
    async def execute(self, run_id: str) -> None:
        prior = await self.db.get_run(UUID(str(run_id)))
        if prior and prior.executor == KEY and prior.status.value == "succeeded":
            return
        run = await self._active(run_id)
        approval_id = str(UUID(str((run.input or {})["approval_id"])))
        actor = run.started_by_clerk_user_id
        payload = await approved_payload(self.db, approval_id, run.project_id, actor=actor)
        payload = {**payload, "approval_id": approval_id}
        project = await self.db.get_project(run.project_id)
        if project is None:
            raise ApplicationError("LinkedIn project is unavailable.", non_retryable=True)
        await self.db.mark_run_running(run.id)
        receipt = await self.db.get_effect(post_key(approval_id))
        if receipt and receipt.status == "completed":
            post = await self._send(run, actor, payload, [])
        else:
            await self._allowed(run, actor)
            await self._progress(run.id, "upload", 0, "Preparing the approved LinkedIn images")
            connection = await self.posting.connection(run.project_id)
            if (
                str(connection.id) != payload["connection_id"]
                or connection.external_account_id != payload["profile_id"]
            ):
                raise ApplicationError(
                    "LinkedIn posting was reconnected. Nothing was posted; preview it again.",
                    non_retryable=True,
                )
            if not may_post_as(connection, payload["account"], actor):
                raise ApplicationError(
                    "Only the member who connected this LinkedIn profile can post as it. "
                    "Nothing was posted.",
                    non_retryable=True,
                )
            media = []
            for index, item in enumerate(payload["attachments"]):
                media.append({**item, "url": await self._image(project, payload, index, item)})
                if activity.in_activity():
                    activity.heartbeat({"image": index})
            await self._progress(run.id, "send", 2, "Publishing the approved LinkedIn post")
            # Read the profile's accounts again right before sending: the account must still
            # be connected to this project, as the same LinkedIn member or page.
            current = await self.posting.zernio.accounts(payload["profile_id"])
            if not any(
                item["id"] == payload["account_id"]
                and item["urn"] == payload["target"]
                and item["active"]
                for item in current
            ):
                raise IntegrationAuthorizationError(
                    "The LinkedIn account this post was approved for is no longer connected. "
                    "Reconnect LinkedIn posting under Integrations, then preview it again."
                )
            post = await self._send(run, actor, payload, media)
        post = await self._settle(post)
        # The approval-keyed receipt is authoritative. This run-keyed mirror keeps LinkedIn's
        # settled answer, published or refused, for the browser/MCP status projection without
        # allowing a second provider call.
        mirror_key = f"{run.id}:linkedin_post"
        async with self.db.effect_lock(mirror_key, KEY) as (conn, saved):
            if not saved:
                await self.db.start_effect(conn, execution_key=mirror_key, operation=KEY)
            if not saved or saved.status != "completed":
                await self.db.complete_effect(conn, execution_key=mirror_key, result=post)
        if post["status"] != "published":
            raise ApplicationError(
                FAILED.get(post.get("error_category") or "", NOT_PUBLISHED), non_retryable=True
            )
        await self._progress(run.id, "save", 3, "Saving the LinkedIn publication result")
        report = await self._publish_report(run, project, post, payload)
        key = f"{run.id}:linkedin_publish_projection"
        async with self.db.effect_lock(key, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            path = report["artifact_path"]
            sha = report["canonical_commit_sha"]
            await self.db.complete_linkedin_publish_projection(
                conn,
                execution_key=key,
                run_id=run.id,
                canonical_commit_sha=sha,
                artifact_path=path,
                artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{path}",
                summary=f"LinkedIn post published: {post['url']}",
            )

    @activity.defn(name="linkedin_publish_failure")
    async def failure(self, argument: str | dict) -> None:
        run_id, passed = split_failure_input(argument)
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != KEY:
            return
        approval_id = str((run.input or {}).get("approval_id", ""))
        receipt = await self.db.get_effect(post_key(approval_id))
        mirror = await self.db.get_effect(f"{run.id}:linkedin_post")
        saved = None
        value = (receipt.result or {}).get("value") or {} if receipt else {}
        published = (mirror.result or {}) if mirror and mirror.status == "completed" else value
        if published.get("status") == "published" and published.get("url"):
            saved = (
                f"LinkedIn published the post at {published['url']}, "
                "but Tin could not save the final report. Do not post again."
            )
        elif receipt and receipt.status == "started":
            saved = "LinkedIn may have published this post. Check the page before trying again."
        elif receipt and receipt.status == "completed":
            # LinkedIn's own answer (a refusal in Tin's words) stands; any other cause after
            # Zernio accepted the post leaves its outcome to check.
            cause = payload_failure(passed)
            if cause is None or cause.kind not in {"explained", "needs_you"}:
                saved = STILL_PUBLISHING
        # Without a post receipt the post was never sent: the cause (a reconnect, a changed
        # account or image, a refusal) says what stopped it, and this line covers the rest.
        await self.db.project_failure(
            run_id=run.id,
            **failure_projection(
                passed,
                "LinkedIn publication stopped before Tin sent the post, so nothing was posted. "
                "Try again.",
                saved=saved,
            ),
        )
