"""One approved X post, with approval-keyed receipts around every provider write.

Only the run ID enters Temporal history. The approved copy and media bytes remain in
Postgres/code.storage and are never returned from these activities.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite.organic_audit_publication import publish_artifacts
from tin_lite.x_posts import KEY, approved_payload

VIDEO_CHUNK = 5_000_000
MAX_VIDEO_POLLS = 30
MAX_VIDEO_WAIT = 480


def _media_key(approval_id: str, index: int, stage: str, attempt: int = 1) -> str:
    return f"x:media:{approval_id}:{index}:{attempt}:{stage}"


def _post_key(approval_id: str) -> str:
    return f"x:post:{approval_id}"


class XPublishActivities:
    def __init__(self, *, database, storage, integrations):
        self.db, self.storage, self.x = database, storage, integrations.x

    async def _active(self, run_id: str, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if not run or run.executor != KEY or run.status.value not in {"pending", "running"}:
            raise ApplicationError("X publication is no longer active.", non_retryable=True)
        return run

    async def _progress(self, run_id: UUID, stage: str, number: int, summary: str):
        await self.db.project_run_progress(
            run_id=run_id,
            mode="steps",
            step=stage,
            current=number,
            total=4,
            summary=summary,
        )
        if activity.in_activity():
            activity.heartbeat({"step": stage})

    async def _effect(self, key: str, intent: dict, operation, *, run, actor: str):
        """Do not replay a write after dispatch when X supplies no idempotency key."""
        async with self.db.effect_lock(key, KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                if (receipt.result or {}).get("intent") != intent:
                    raise ApplicationError("X delivery receipt changed.", non_retryable=True)
                return receipt.result["value"]
            if receipt is not None:
                raise ApplicationError(
                    "An X delivery attempt has an uncertain outcome. "
                    "Check the X account before retrying.",
                    non_retryable=True,
                )
            await self._active(str(run.id), conn=conn)
            if not await self.db.has_project_access(
                project_id=run.project_id, clerk_user_id=actor, conn=conn
            ):
                raise ApplicationError("X publishing access was removed.", non_retryable=True)
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.save_effect_progress(
                conn,
                execution_key=key,
                result={"intent": intent, "attempted_at": datetime.now(UTC).isoformat()},
            )
            value = await operation()
            await self.db.complete_effect(
                conn,
                execution_key=key,
                result={"intent": intent, "value": value},
            )
            return value

    async def _media_bytes(self, project, payload: dict, item: dict) -> bytes:
        raw = await self.storage.read_project_media(
            repo_id=project.state_repo_id, revision=payload["revision"], path=item["path"]
        )
        if hashlib.sha256(raw).hexdigest() != item["sha256"] or len(raw) != item["bytes"]:
            raise ApplicationError(
                "Approved media changed or is unavailable. Nothing was posted.", non_retryable=True
            )
        return raw

    @staticmethod
    def _expiry(value: dict) -> datetime:
        seconds = value.get("expires_after_secs")
        if type(seconds) is not int or not 60 <= seconds <= 7 * 86400:
            raise ApplicationError("X did not confirm media availability.", non_retryable=True)
        return datetime.now(UTC) + timedelta(seconds=seconds)

    async def _upload_image(self, connection, raw: bytes, media_type: str) -> dict:
        result = await self.x.upload_image(connection, content=raw, media_type=media_type)
        return {**result, "expires_at": self._expiry(result).isoformat()}

    async def _initialize_video(self, connection, size: int) -> dict:
        result = await self.x.initialize_video(connection, total_bytes=size)
        return {**result, "expires_at": self._expiry(result).isoformat()}

    async def _image(
        self, project, payload: dict, connection, index: int, item: dict, *, run, actor
    ) -> str:
        raw = await self._media_bytes(project, payload, item)
        approval_id = payload["approval_id"]
        intent = {"account_id": payload["account_id"], "sha256": item["sha256"]}
        for attempt in (1, 2):
            uploaded = await self._effect(
                _media_key(approval_id, index, "upload", attempt),
                intent,
                lambda: self._upload_image(connection, raw, item["media_type"]),
                run=run,
                actor=actor,
            )
            expiry = datetime.fromisoformat(uploaded["expires_at"])
            if expiry <= datetime.now(UTC) + timedelta(minutes=2):
                continue
            media_id = uploaded["media_id"]
            if item.get("alt_text"):
                await self._effect(
                    _media_key(approval_id, index, "alt", attempt),
                    {**intent, "media_id": media_id, "text": item["alt_text"]},
                    lambda media_id=media_id: self.x.set_alt_text(
                        connection, media_id=media_id, text=item["alt_text"]
                    ),
                    run=run,
                    actor=actor,
                )
            return media_id
        raise ApplicationError("X media expired before posting.", non_retryable=True)

    async def _video(
        self, project, payload: dict, connection, index: int, item: dict, *, run, actor
    ) -> str:
        raw = await self._media_bytes(project, payload, item)
        approval_id = payload["approval_id"]
        intent = {"account_id": payload["account_id"], "sha256": item["sha256"]}
        for attempt in (1, 2):
            initialized = await self._effect(
                _media_key(approval_id, index, "initialize", attempt),
                intent,
                lambda: self._initialize_video(connection, len(raw)),
                run=run,
                actor=actor,
            )
            expiry = datetime.fromisoformat(initialized["expires_at"])
            if expiry <= datetime.now(UTC) + timedelta(minutes=10):
                continue
            media_id = initialized["media_id"]
            for offset in range(0, len(raw), VIDEO_CHUNK):
                segment = offset // VIDEO_CHUNK
                await self._effect(
                    _media_key(approval_id, index, f"append:{segment}", attempt),
                    {**intent, "media_id": media_id, "segment": segment},
                    lambda offset=offset, segment=segment, media_id=media_id: self.x.append_video(
                        connection,
                        media_id=media_id,
                        segment_index=segment,
                        content=raw[offset : offset + VIDEO_CHUNK],
                    ),
                    run=run,
                    actor=actor,
                )
                if activity.in_activity():
                    activity.heartbeat({"segment": segment})
            await self._effect(
                _media_key(approval_id, index, "finalize", attempt),
                {**intent, "media_id": media_id},
                lambda media_id=media_id: self.x.finalize_video(connection, media_id=media_id),
                run=run,
                actor=actor,
            )
            await self._wait_video(connection, media_id)
            if expiry > datetime.now(UTC) + timedelta(minutes=2):
                return media_id
        raise ApplicationError("X video expired before posting.", non_retryable=True)

    async def _wait_video(self, connection, media_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + MAX_VIDEO_WAIT
        for _ in range(MAX_VIDEO_POLLS):
            status = await self.x.media_status(connection, media_id=media_id)
            data = status.get("data")
            info = data.get("processing_info", {}) if isinstance(data, dict) else None
            state = info.get("state") if isinstance(info, dict) else None
            if state == "succeeded" or (isinstance(data, dict) and info == {}):
                return
            if state == "failed":
                raise ApplicationError(
                    "X could not process the approved video.", non_retryable=True
                )
            if state not in {"pending", "in_progress"}:
                raise ApplicationError(
                    "X video processing status is unavailable.", non_retryable=True
                )
            delay = info.get("check_after_secs", 5)
            if type(delay) is not int or not 1 <= delay <= 30:
                delay = 5
            if asyncio.get_running_loop().time() + delay > deadline:
                break
            if activity.in_activity():
                activity.heartbeat({"video": "processing"})
            await asyncio.sleep(delay)
        raise ApplicationError(
            "X video processing timed out; no post was sent.", non_retryable=True
        )

    async def _publish_report(self, run, project, post: dict, payload: dict) -> dict:
        key = f"{run.id}:x_publish_report"
        path = f"reports/x/{run.id}.md"
        document = (
            "# X post published\n\n"
            f"Account: @{payload['account_id']}\n\n"
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
                    paths={"X_PUBLISH.md": path},
                    limits={"X_PUBLISH.md": 16_000},
                    message=f"{KEY} {run.id} [{key}]",
                    intent=(saved.result or {}).get("publication") if saved else None,
                    save_intent=save_intent,
                    validate_active=validate_active,
                )
            value = {"canonical_commit_sha": revision, "artifact_path": path}
            await self.db.complete_effect(conn, execution_key=key, result=value)
            return value

    @activity.defn(name="x_publish_execute")
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
            raise ApplicationError("X project is unavailable.", non_retryable=True)
        await self.db.mark_run_running(run.id)
        post_intent = {"account_id": payload["account_id"], "fingerprint": payload["fingerprint"]}
        post_receipt = await self.db.get_effect(_post_key(approval_id))
        if post_receipt and post_receipt.status == "completed":
            if (post_receipt.result or {}).get("intent") != post_intent:
                raise ApplicationError("X delivery receipt changed.", non_retryable=True)
            post = post_receipt.result["value"]
        elif post_receipt is not None:
            raise ApplicationError(
                "X may already have published this post. Check the account before trying again.",
                non_retryable=True,
            )
        else:
            if not actor or not await self.db.has_project_access(
                project_id=run.project_id, clerk_user_id=actor
            ):
                raise ApplicationError("X publishing access was removed.", non_retryable=True)
            await self._progress(run.id, "upload", 0, "Preparing approved X attachments")
            connection = await self.x.connection(run.project_id, "x.posts.publish")
            if (
                str(connection.id) != payload["connection_id"]
                or connection.external_account_id != payload["account_id"]
            ):
                raise ApplicationError("X account changed. Nothing was posted.", non_retryable=True)
            attachments = payload["attachments"]
            media_ids = []
            if attachments:
                await self.x.connection(run.project_id, "x.media.upload")
            for index, item in enumerate(attachments):
                if item["type"] == "image":
                    media_ids.append(
                        await self._image(
                            project, payload, connection, index, item, run=run, actor=actor
                        )
                    )
                elif item["type"] == "video":
                    media_ids.append(
                        await self._video(
                            project, payload, connection, index, item, run=run, actor=actor
                        )
                    )
                else:
                    raise ApplicationError("Approved X attachment is invalid.", non_retryable=True)
            await self._progress(run.id, "send", 2, "Publishing the approved X post")
            # Recheck permission and account immediately before the final dispatch.
            if not await self.db.has_project_access(project_id=run.project_id, clerk_user_id=actor):
                raise ApplicationError("X publishing access was removed.", non_retryable=True)
            current = await self.x.connection(run.project_id, "x.posts.publish")
            if current.id != connection.id or current.external_account_id != payload["account_id"]:
                raise ApplicationError("X account changed. Nothing was posted.", non_retryable=True)
            post = await self._effect(
                _post_key(approval_id),
                post_intent,
                lambda: self.x.create_post(
                    current, text=payload["text"], media_ids=media_ids or None
                ),
                run=run,
                actor=actor,
            )
        # The approval-keyed receipt is authoritative. This run-keyed mirror feeds the
        # existing browser/MCP status projection without allowing a second provider call.
        mirror_key = f"{run.id}:x_post"
        async with self.db.effect_lock(mirror_key, KEY) as (conn, saved):
            if not saved:
                await self.db.start_effect(conn, execution_key=mirror_key, operation=KEY)
            if not saved or saved.status != "completed":
                await self.db.complete_effect(conn, execution_key=mirror_key, result=post)
        await self._progress(run.id, "save", 3, "Saving the X publication result")
        report = await self._publish_report(run, project, post, payload)
        key = f"{run.id}:x_publish_projection"
        async with self.db.effect_lock(key, KEY) as (conn, saved):
            if saved and saved.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            path = report["artifact_path"]
            sha = report["canonical_commit_sha"]
            await self.db.complete_x_publish_projection(
                conn,
                execution_key=key,
                run_id=run.id,
                canonical_commit_sha=sha,
                artifact_path=path,
                artifact_ref=f"code.storage://{project.state_repo_id}@{sha}/{path}",
                summary=f"X post published: {post['url']}",
            )

    @activity.defn(name="x_publish_failure")
    async def failure(self, run_id: str) -> None:
        run = await self.db.get_run(UUID(str(run_id)))
        if not run or run.executor != KEY:
            return
        approval_id = str((run.input or {}).get("approval_id", ""))
        receipt = await self.db.get_effect(_post_key(approval_id))
        if receipt and receipt.status == "completed":
            value = (receipt.result or {}).get("value") or {}
            detail = (
                f"X accepted the post at {value.get('url', 'its account')}, "
                "but Tin could not save the final report. Do not post again."
            )
        elif receipt and receipt.status == "started":
            detail = "X may have published this post. Check the account before trying again."
        else:
            detail = (
                "X publication stopped before Tin confirmed a post. "
                "Check the run details before trying again."
            )
        await self.db.project_failure(run_id=run.id, error_message=detail)
