"""Exact X drafts and shared browser/MCP preview-and-confirm service."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from tin_lite.project_files import safe_project_file_path
from tin_lite.project_media import validate_media
from tin_lite.x_text import weighted_length

KEY = "social.x_publish"
WORKFLOW_ID = UUID("7c8ba495-9ae6-4c84-9815-a947c87423ba")
MAX_DRAFT_BYTES = 64_000
INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "approval_id": {"type": "string", "format": "uuid"},
    },
    "required": ["project_id", "approval_id"],
}


def digest(value) -> str:
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    return hashlib.sha256(raw).hexdigest()


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Attachment(ClosedModel):
    type: Literal["image", "video"]
    path: str = Field(min_length=1, max_length=512)
    alt_text: str = Field(default="", max_length=1000)


class Support(ClosedModel):
    source_path: str = Field(max_length=512)
    excerpt: str = Field(max_length=4000)


class Post(ClosedModel):
    id: str = Field(pattern=r"^p[1-6]$")
    text: str = Field(min_length=1, max_length=4000)
    readiness: Literal["ready", "needs_evidence", "needs_asset"]
    support: list[Support] = Field(max_length=20)
    editor_notes: str = Field(max_length=4000)
    attachments: list[Attachment] = Field(max_length=4)
    missing_assets: list[str] = Field(max_length=10)


class Draft(ClosedModel):
    schema_version: Literal["tin.social.x_draft.v1"]
    account_id: str = Field(max_length=32, pattern=r"^([0-9]{1,19})?$")
    posts: list[Post] = Field(min_length=1, max_length=6)


def validate_draft(value) -> dict:
    if isinstance(value, bytes):
        if len(value) > MAX_DRAFT_BYTES:
            raise ValueError("The X draft is too large.")
        value = json.loads(value.decode("utf-8"))
    parsed = Draft.model_validate(value)
    ids = set()
    for post in parsed.posts:
        if post.id in ids or not post.text.strip() or weighted_length(post.text) > 280:
            raise ValueError("Each post needs a unique ID and text within X's character limit.")
        ids.add(post.id)
        if any(ord(c) < 32 and c not in "\n\t" for c in post.text):
            raise ValueError("Remove control characters from the post.")
        videos = [a for a in post.attachments if a.type == "video"]
        if videos and len(post.attachments) != 1:
            raise ValueError("Attach up to four images or one video.")
        paths = set()
        for item in post.attachments:
            suffix = PurePosixPath(item.path).suffix.lower()
            allowed = {".mp4"} if item.type == "video" else {".png", ".jpg", ".jpeg"}
            if not safe_project_file_path(item.path) or suffix not in allowed or item.path in paths:
                raise ValueError("Choose distinct, supported media files in this project.")
            if item.type == "video" and item.alt_text:
                raise ValueError("Alt text is supported for images only.")
            paths.add(item.path)
    result = parsed.model_dump()
    if len(json.dumps(result).encode()) > MAX_DRAFT_BYTES:
        raise ValueError("The X draft is too large.")
    return result


async def save_effect(db, key, value):
    async with db.effect_lock(key, KEY) as (conn, saved):
        if saved and saved.status == "completed":
            return saved.result
        await db.start_effect(conn, execution_key=key, operation=KEY)
        await db.complete_effect(conn, execution_key=key, result=value)
    return value


async def approved_payload(db, approval_id, project_id, actor=None):
    receipt = await db.get_effect(f"x:approved:{UUID(str(approval_id))}")
    value = receipt.result if receipt and receipt.status == "completed" else None
    if not value or value.get("project_id") != str(project_id):
        raise ValueError("Preview the X post and explicitly confirm publication first.")
    if actor is not None and value.get("actor") != actor:
        raise ValueError("This publication belongs to a different approval request.")
    return value


class XPosts:
    def __init__(self, runtime, settings):
        self.runtime, self.settings = runtime, settings
        self.db, self.storage = runtime.database, runtime.storage

    async def project(self, project_id, actor):
        if not await self.db.has_project_access(project_id=project_id, clerk_user_id=actor):
            raise LookupError("project not found")
        project = await self.db.get_project(project_id)
        if project is None:
            raise LookupError("project not found")
        return project

    async def read(self, project_id, actor, path):
        project = await self.project(project_id, actor)
        if not safe_project_file_path(path) or not path.endswith(".json"):
            raise ValueError("Choose an X draft JSON file in this project.")
        paths, revision = await self.storage.list_canonical_files(
            repo_id=project.state_repo_id, branch=project.canonical_branch
        )
        if path not in paths:
            raise LookupError("X draft not found")
        raw = await self.storage.read_bounded_project_file(
            repo_id=project.state_repo_id, commit_sha=revision, path=path, max_bytes=MAX_DRAFT_BYTES
        )
        if raw is None:
            raise LookupError("X draft not found")
        source_id = await self.db.pool.fetchval(
            "SELECT r.id FROM workflow_runs r JOIN workflows w ON w.id=r.workflow_id "
            "WHERE r.project_id=$1 AND r.artifact_path=$2 AND r.status='succeeded' "
            "AND w.key='social.x_compose' AND w.project_id IS NULL ORDER BY r.created_at "
            "DESC LIMIT 1",
            project_id,
            path,
        )
        return {
            "path": path,
            "revision": revision,
            "draft": validate_draft(raw),
            "feedback_run_id": str(source_id) if source_id else None,
        }

    async def save(
        self, project_id, actor, *, path, expected_revision, request_id, draft, client_id=None
    ):
        project = await self.project(project_id, actor)
        if not path.endswith(".json"):
            raise ValueError("Choose a JSON draft path.")
        value = validate_draft(draft)
        content = json.dumps(value, ensure_ascii=False, indent=2)
        if len(content.encode("utf-8")) > MAX_DRAFT_BYTES:
            raise ValueError("The X draft is too large to save and read back.")
        result = await self.runtime.project_files.commit(
            project=project,
            actor_clerk_user_id=actor,
            client_id=client_id,
            request_id=UUID(str(request_id)),
            expected_revision=expected_revision,
            message="Edit X draft",
            changes=[
                {
                    "operation": "upsert",
                    "path": path,
                    "content": content,
                }
            ],
        )
        return {"path": path, "revision": result.revision, "draft": value}

    async def snapshot(self, project_id, actor, path, post_id):
        view = await self.read(project_id, actor, path)
        post = next((p for p in view["draft"]["posts"] if p["id"] == post_id), None)
        if not post:
            raise LookupError("post not found")
        if post["readiness"] != "ready" or post["missing_assets"]:
            raise ValueError("Resolve the post's missing facts or assets before publishing.")
        x = self.runtime.integrations.x
        connection = await x.connection(project_id, capability="x.posts.publish")
        if view["draft"]["account_id"] not in {"", connection.external_account_id}:
            raise ValueError("The draft's X account differs from the connected account.")
        project = await self.project(project_id, actor)
        attachments = []
        for item in post["attachments"]:
            await x.connection(project_id, capability="x.media.upload")
            raw = await self.storage.read_project_media(
                repo_id=project.state_repo_id, revision=view["revision"], path=item["path"]
            )
            metadata = validate_media(item["path"], raw)
            attachments.append({**item, **metadata, "sha256": digest(raw)})
        payload = {
            "project_id": str(project_id),
            "path": path,
            "post_id": post_id,
            "text": post["text"],
            "attachments": attachments,
            "connection_id": str(connection.id),
            "account_id": connection.external_account_id,
        }
        return {
            **payload,
            "fingerprint": digest(payload),
            "revision": view["revision"],
            "account": {
                "id": connection.external_account_id,
                "username": connection.external_account_label.lstrip("@"),
            },
        }

    async def preview(self, project_id, actor, *, path, post_id):
        value = await self.snapshot(project_id, actor, path, post_id)
        token = str(uuid4())
        expires = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
        await save_effect(self.db, f"x:preview:{token}", {**value, "valid_until": expires})
        return {
            "preview_token": token,
            "text": value["text"],
            "account": value["account"],
            "valid_until": expires,
            "attachments": [
                {
                    **a,
                    "url": f"/api/projects/{project_id}/files/raw?"
                    + urlencode(
                        {
                            "path": a["path"],
                            "revision": value["revision"],
                        }
                    ),
                }
                for a in value["attachments"]
            ],
        }

    async def publish(self, project_id, actor, *, preview_token, request_id, client_id=None):
        from tin_lite.run_service import start_workflow_run

        await self.project(project_id, actor)
        token = str(UUID(str(preview_token)))
        saved = await self.db.get_effect(f"x:preview:{token}")
        value = saved.result if saved and saved.status == "completed" else None
        if not value or value["project_id"] != str(project_id):
            raise ValueError("Preview this project's post before publishing.")
        request_key = f"x:confirm:{project_id}:{UUID(str(request_id))}"
        approval_id = uuid5(NAMESPACE_URL, f"tin-x:{project_id}:{value['fingerprint']}")
        previous = await self.db.get_effect(f"x:approved:{approval_id}")
        if not previous or previous.status != "completed":
            if datetime.fromisoformat(value["valid_until"]) < datetime.now(UTC):
                raise ValueError("The preview expired. Preview the post again.")
            current = await self.snapshot(project_id, actor, value["path"], value["post_id"])
            if current["fingerprint"] != value["fingerprint"]:
                raise ValueError("The post, attachment or account changed. Preview it again.")
        request = await save_effect(self.db, request_key, {"preview_token": token})
        if request["preview_token"] != token:
            raise ValueError("This request ID was used for another publication.")
        approval = await save_effect(
            self.db,
            f"x:approved:{approval_id}",
            {
                **value,
                "actor": actor,
                "approved_at": datetime.now(UTC).isoformat(),
            },
        )
        existing = await self.db.get_run_by_start_key(
            project_id=project_id, start_idempotency_key=f"x-publish:{approval_id}"
        )
        if approval["actor"] != actor and existing is None:
            raise ValueError(
                "Another member approved this post. "
                "They can retry their confirmation to finish starting it."
            )
        workflow = await self.db.get_workflow(WORKFLOW_ID)
        if workflow is None:
            raise ValueError("X publishing is not installed; sync the workflow catalog.")
        if approval["actor"] != actor:
            run = existing
        else:
            # Re-enter normal dispatch recovery when creation committed but Temporal's
            # acknowledgement was lost. Merely returning a pending row would strand it.
            run = await start_workflow_run(
                runtime=self.runtime,
                settings=self.settings,
                workflow=workflow,
                project_id=project_id,
                started_by_clerk_user_id=actor,
                start_idempotency_key=f"x-publish:{approval_id}",
                input_payload={"approval_id": str(approval_id)},
                trigger_source=existing.trigger_source
                if existing
                else ("mcp" if client_id and client_id != "browser" else "manual"),
                trigger_client=existing.trigger_client if existing else None,
                started_by_oauth_client_id=existing.started_by_oauth_client_id
                if existing
                else (client_id if client_id != "browser" else None),
                _x_publication=True,
            )
        receipt = await self.db.get_effect(f"{run.id}:x_post")
        result = receipt.result if receipt and receipt.status == "completed" else {}
        return {
            "id": str(run.id),
            "run_id": str(run.id),
            "status": run.status.value,
            "url": result.get("url"),
        }
