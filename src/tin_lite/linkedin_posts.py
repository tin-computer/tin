"""Exact LinkedIn drafts and the shared browser/MCP preview-and-confirm service.

This mirrors X's (x_posts.py). Drafts are ordinary project files. A preview freezes the exact
text, images and the connected account Tin posts as; confirming it records the approval and
starts social.linkedin_publish. A member posts as a connected page or as their own profile,
never as another member's. See docs/linkedin-posting.md.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field

from tin_lite.integrations import IntegrationError
from tin_lite.linkedin_posting import may_post_as
from tin_lite.project_files import safe_project_file_path
from tin_lite.project_media import validate_media
from tin_lite.x_posts import ClosedModel, Support, digest
from tin_lite.x_posts import save_effect as _save_effect

KEY = "social.linkedin_publish"
WORKFLOW_ID = UUID("a68b1bda-56bf-407e-8368-53ab1bdcb199")
SCHEMA = "tin.social.linkedin_draft.v1"
# A drafting run keeps its source material with its posts, so drafts are larger than X's.
MAX_DRAFT_BYTES = 256_000
# LinkedIn's limits; the text is counted in UTF-16 units, as LinkedIn and browsers count it.
MAX_TEXT = 3000
MAX_IMAGES = 20
# A failed attempt that sent nothing may be confirmed again; this bounds those attempts.
MAX_ATTEMPTS = 10
TARGET = re.compile(r"^urn:li:(person|organization):[A-Za-z0-9_-]{1,64}$")
INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "approval_id": {"type": "string", "format": "uuid"},
    },
    "required": ["project_id", "approval_id"],
}


def text_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def post_key(approval_id: str) -> str:
    return f"linkedin:post:{approval_id}"


class Attachment(ClosedModel):
    type: Literal["image"]
    path: str = Field(min_length=1, max_length=512)
    alt_text: str = Field(default="", max_length=1000)


class Post(ClosedModel):
    id: str = Field(pattern=r"^p[1-6]$")
    text: str = Field(min_length=1, max_length=12000)
    # The LinkedIn URN of a connected account; empty until the founder picks one.
    target: str = Field(default="", max_length=100)
    readiness: Literal["ready", "needs_evidence", "needs_asset"]
    support: list[Support] = Field(max_length=20)
    editor_notes: str = Field(max_length=4000)
    attachments: list[Attachment] = Field(max_length=MAX_IMAGES)
    missing_assets: list[str] = Field(max_length=10)


class Gap(ClosedModel):
    angle: str = Field(max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)


class Source(ClosedModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,60}$")
    label: str = Field(min_length=1, max_length=300)
    author: str = Field(max_length=200)
    use: Literal["publishable", "background", "reference", "trend"]
    excerpt: str = Field(min_length=1, max_length=16000)


class Parent(ClosedModel):
    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    post_id: str = Field(pattern=r"^p[1-6]$")


class Draft(ClosedModel):
    schema_version: Literal["tin.social.linkedin_draft.v1"]
    posts: list[Post] = Field(max_length=6)
    # What a drafting run keeps with its posts. Tin shows the gaps to the founder; the author,
    # parent and sources let a later revision check against this batch's own material.
    author_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    gaps: list[Gap] = Field(default_factory=list, max_length=12)
    parent: Parent | None = None
    sources: list[Source] = Field(default_factory=list, max_length=24)


def validate_draft(value) -> dict:
    if isinstance(value, bytes):
        if len(value) > MAX_DRAFT_BYTES:
            raise ValueError("The LinkedIn draft is too large.")
        value = json.loads(value.decode("utf-8"))
    parsed = Draft.model_validate(value)
    if not parsed.posts and not parsed.gaps:
        raise ValueError("A LinkedIn draft needs a post, or a note on what's missing.")
    if len({source.id for source in parsed.sources}) != len(parsed.sources):
        raise ValueError("Each source in the draft needs its own ID.")
    ids = set()
    for post in parsed.posts:
        if post.id in ids or not post.text.strip() or text_length(post.text) > MAX_TEXT:
            raise ValueError("Each post needs a unique ID and text within 3,000 characters.")
        ids.add(post.id)
        if any(ord(c) < 32 and c not in "\n\t" for c in post.text):
            raise ValueError("Remove control characters from the post.")
        if post.target and not TARGET.fullmatch(post.target):
            raise ValueError("Choose a connected LinkedIn account to post as.")
        paths = set()
        for item in post.attachments:
            suffix = PurePosixPath(item.path).suffix.lower()
            if (
                not safe_project_file_path(item.path)
                or suffix not in {".png", ".jpg", ".jpeg"}
                or item.path in paths
            ):
                raise ValueError("Choose distinct PNG or JPEG images in this project.")
            paths.add(item.path)
    result = parsed.model_dump()
    if len(json.dumps(result).encode()) > MAX_DRAFT_BYTES:
        raise ValueError("The LinkedIn draft is too large.")
    return result


async def save_effect(db, key, value):
    return await _save_effect(db, key, value, operation=KEY)


async def approved_payload(db, approval_id, project_id, actor=None):
    receipt = await db.get_effect(f"linkedin:approved:{UUID(str(approval_id))}")
    value = receipt.result if receipt and receipt.status == "completed" else None
    if not value or value.get("project_id") != str(project_id):
        raise ValueError("Preview the LinkedIn post and explicitly confirm publication first.")
    if actor is not None and value.get("actor") != actor:
        raise ValueError("This publication belongs to a different approval request.")
    return value


def _account(account: dict) -> dict:
    return {key: account[key] for key in ("id", "urn", "kind", "name")}


def _active_accounts(connection, actor) -> list[dict]:
    """The connected pages and the actor's own profile; never another member's profile."""

    return [
        _account(item)
        for item in connection.configuration.get("accounts") or []
        if item.get("active") and may_post_as(connection, item, actor)
    ]


def _unavailable_accounts(connection, actor) -> list[dict]:
    """The connection's other accounts, named so a draft aimed at one can say why it can't
    post: another member's own profile, or an account LinkedIn no longer connects."""

    return [
        {
            "urn": item["urn"],
            "kind": item["kind"],
            "name": item["name"],
            "reason": "another_member" if item.get("active") else "disconnected",
        }
        for item in connection.configuration.get("accounts") or []
        if not (item.get("active") and may_post_as(connection, item, actor))
    ]


# Publications of one draft file, newest first; the reader shows each post's latest.
PUBLICATIONS = """
SELECT r.id, r.status, r.error_message, r.created_at, r.finished_at,
       a.result AS approval, p.status AS sent, m.result AS answer
FROM workflow_runs r
JOIN effect_receipts a
  ON a.execution_key = 'linkedin:approved:' || (r.input->>'approval_id')
 AND a.status = 'completed'
LEFT JOIN effect_receipts p
  ON p.execution_key = 'linkedin:post:' || (r.input->>'approval_id')
LEFT JOIN effect_receipts m
  ON m.execution_key = r.id::text || ':linkedin_post' AND m.status = 'completed'
WHERE r.project_id = $1 AND r.executor = $2 AND a.result->>'project_id' = $1::text
  AND a.result->>'path' = $3
ORDER BY r.created_at DESC, r.id DESC
LIMIT 60
"""
STILL_SENDING = {"pending", "running", "paused"}
# A drafting workflow that can redraft one post of a saved batch from a note declares these
# inputs next to an `action` that offers "revise". The reader's Request changes uses them.
REVISION_INPUTS = frozenset({"draft_path", "draft_sha256", "post_id", "feedback"})
WRITER = """
SELECT id FROM workflow_runs
WHERE project_id = $1 AND artifact_path = $2 AND status = 'succeeded'
ORDER BY created_at DESC, id DESC
LIMIT 1
"""
REVISING = """
SELECT id, input->>'post_id' AS post_id FROM workflow_runs
WHERE project_id = $1 AND input->>'action' = 'revise' AND input->>'draft_path' = $2
  AND status IN ('pending', 'running', 'paused')
ORDER BY created_at DESC, id DESC
LIMIT 1
"""


def revises_posts(schema) -> bool:
    properties = (schema or {}).get("properties") or {}
    action = properties.get("action") or {}
    return REVISION_INPUTS <= properties.keys() and "revise" in (action.get("enum") or [])


def _json(value) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def _publication(row) -> dict:
    """Where one approved post stands on LinkedIn, from the run and its receipts.

    Without a send receipt nothing reached LinkedIn. LinkedIn's settled answer is recorded
    once it comes; a send with no answer may have posted, so it is never called unpublished.
    """

    approval, answer = _json(row["approval"]), _json(row["answer"])
    status = row["status"]
    published = answer.get("status") == "published" and bool(answer.get("url"))
    if status in STILL_SENDING:
        state = "publishing"
    elif status == "succeeded" or published:
        state = "published"
    elif row["sent"] is None or answer.get("status") in {"failed", "cancelled"}:
        state = "not_published"
    else:
        state = "uncertain"
    account = approval.get("account") or {}
    when = answer.get("published_at") if published else None
    return {
        "post_id": approval.get("post_id"),
        "run_id": str(row["id"]),
        "state": state,
        "url": answer.get("url") if published else None,
        "at": when or (row["finished_at"] or row["created_at"]).isoformat(),
        "account": {key: account.get(key) for key in ("urn", "kind", "name")},
        "text": approval.get("text", ""),
        "message": row["error_message"] if state in {"not_published", "uncertain"} else None,
    }


class LinkedInPosts:
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

    async def posting_as(self, project_id, actor) -> dict:
        """The accounts the actor may post as, and the connection's others. Neither is listed
        while LinkedIn posting isn't connected."""

        try:
            connection = await self.runtime.integrations.linkedin_posting.connection(project_id)
        except IntegrationError:
            return {"connected": False, "accounts": [], "unavailable": []}
        return {
            "connected": True,
            "accounts": _active_accounts(connection, actor),
            "unavailable": _unavailable_accounts(connection, actor),
        }

    async def publications(self, project_id, actor, path) -> dict:
        """Each post's latest publication from this draft file, keyed by post ID."""

        await self.project(project_id, actor)
        latest: dict[str, dict] = {}
        for row in await self.db.pool.fetch(PUBLICATIONS, project_id, KEY, path):
            item = _publication(row)
            latest.setdefault(item["post_id"], item)
        return latest

    async def read(self, project_id, actor, path):
        view = await self._read(project_id, actor, path)
        revising = await self.db.pool.fetchrow(REVISING, project_id, path)
        return {
            **view,
            **await self.posting_as(project_id, actor),
            "publications": await self.publications(project_id, actor, path),
            "revisable": await self._writer(project_id, path) is not None,
            "revising": {"run_id": str(revising["id"]), "post_id": revising["post_id"]}
            if revising
            else None,
        }

    async def _writer(self, project_id, path):
        """The drafting run that wrote this batch, when its workflow can revise one post."""

        row = await self.db.pool.fetchrow(WRITER, project_id, path)
        run = await self.db.get_run(row["id"]) if row else None
        workflow = await self.db.get_workflow(run.workflow_id) if run else None
        if workflow is None:
            return None
        configured = (
            await self.db.get_project_workflow(run.project_workflow_id)
            if run.project_workflow_id
            else None
        )
        if configured is not None and configured.project_id != project_id:
            configured = None
        schema = configured.input_schema if configured else workflow.definition.get("input_schema")
        return (run, workflow, configured, schema) if revises_posts(schema) else None

    async def request_changes(
        self, project_id, actor, *, path, post_id, feedback, request_id, client_id=None
    ):
        """Redraft one post from the founder's note, with the workflow that wrote the batch.

        The run reads the batch as saved (its SHA-256 pins it) and writes a new batch beside
        it with only that post changed; the earlier batch stays in Files.
        """

        from tin_lite.run_service import start_workflow_run

        view = await self._read(project_id, actor, path)
        if not any(post["id"] == post_id for post in view["draft"]["posts"]):
            raise LookupError("post not found")
        writer = await self._writer(project_id, path)
        if writer is None:
            raise ValueError(
                "Only a batch a drafting workflow wrote can be revised from a note. "
                "Edit the post instead."
            )
        run, workflow, configured, schema = writer
        started = await start_workflow_run(
            runtime=self.runtime,
            settings=self.settings,
            workflow=workflow,
            project_id=project_id,
            started_by_clerk_user_id=actor,
            start_idempotency_key=f"linkedin-revise:{UUID(str(request_id))}",
            input_payload={
                **(run.input or {}),
                "action": "revise",
                "draft_path": path,
                "draft_sha256": view["sha256"],
                "post_id": post_id,
                "feedback": feedback.strip(),
            },
            project_workflow_id=configured.id if configured else None,
            definition_commit_sha=(
                configured.definition_commit_sha if configured else run.definition_commit_sha
            ),
            input_schema=schema,
            trigger_source="mcp" if client_id and client_id != "browser" else "manual",
        )
        return {"run_id": str(started.id), "status": started.status.value}

    async def _read(self, project_id, actor, path):
        project = await self.project(project_id, actor)
        if not safe_project_file_path(path) or not path.endswith(".json"):
            raise ValueError("Choose a LinkedIn draft JSON file in this project.")
        paths, revision = await self.storage.list_canonical_files(
            repo_id=project.state_repo_id, branch=project.canonical_branch
        )
        if path not in paths:
            raise LookupError("LinkedIn draft not found")
        raw = await self.storage.read_bounded_project_file(
            repo_id=project.state_repo_id, commit_sha=revision, path=path, max_bytes=MAX_DRAFT_BYTES
        )
        if raw is None:
            raise LookupError("LinkedIn draft not found")
        return {
            "path": path,
            "revision": revision,
            "draft": validate_draft(raw),
            "sha256": digest(raw),
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
            raise ValueError("The LinkedIn draft is too large to save and read back.")
        result = await self.runtime.project_files.commit(
            project=project,
            actor_clerk_user_id=actor,
            client_id=client_id,
            request_id=UUID(str(request_id)),
            expected_revision=expected_revision,
            message="Edit LinkedIn draft",
            changes=[{"operation": "upsert", "path": path, "content": content}],
        )
        return {
            "path": path,
            "revision": result.revision,
            "draft": value,
            "sha256": digest(content.encode()),
        }

    async def snapshot(self, project_id, actor, path, post_id):
        view = await self._read(project_id, actor, path)
        post = next((p for p in view["draft"]["posts"] if p["id"] == post_id), None)
        if not post:
            raise LookupError("post not found")
        if post["readiness"] != "ready" or post["missing_assets"]:
            raise ValueError("Resolve the post's missing facts or images before publishing.")
        connection = await self.runtime.integrations.linkedin_posting.connection(project_id)
        accounts = _active_accounts(connection, actor)
        target = post["target"] or (accounts[0]["urn"] if len(accounts) == 1 else "")
        account = next((item for item in accounts if item["urn"] == target), None)
        if account is None:
            if not post["target"]:
                raise ValueError(
                    "Choose who posts: one of the LinkedIn accounts connected to this project."
                )
            connected = connection.configuration.get("accounts") or []
            if any(item["urn"] == target and item.get("active") for item in connected):
                # Connected, so it is a profile that belongs to another member.
                raise ValueError(
                    "Only the member who connected this LinkedIn profile can post as it. "
                    "Choose a page or your own profile."
                )
            raise ValueError(
                "This post's LinkedIn account isn't connected to this project. "
                "Choose a connected account."
            )
        project = await self.project(project_id, actor)
        attachments = []
        for item in post["attachments"]:
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
            "profile_id": connection.external_account_id,
            "account_id": account["id"],
            "target": account["urn"],
        }
        return {
            **payload,
            "fingerprint": digest(payload),
            "revision": view["revision"],
            "account": account,
        }

    async def preview(self, project_id, actor, *, path, post_id):
        value = await self.snapshot(project_id, actor, path, post_id)
        token = str(uuid4())
        expires = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
        await save_effect(self.db, f"linkedin:preview:{token}", {**value, "valid_until": expires})
        return {
            "preview_token": token,
            "text": value["text"],
            "account": value["account"],
            "valid_until": expires,
            "attachments": [
                {
                    **a,
                    "url": f"/api/projects/{project_id}/files/raw?"
                    + urlencode({"path": a["path"], "revision": value["revision"]}),
                }
                for a in value["attachments"]
            ],
        }

    async def _approval_id(self, project_id, fingerprint) -> UUID:
        """The approval for this exact post. An earlier attempt whose run ended without
        sending anything doesn't use the post up, so confirming again tries once more."""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            approval_id = uuid5(NAMESPACE_URL, f"tin-linkedin:{project_id}:{fingerprint}:{attempt}")
            run = await self.db.get_run_by_start_key(
                project_id=project_id, start_idempotency_key=f"linkedin-publish:{approval_id}"
            )
            if (
                run is None
                or run.status.value not in {"failed", "stopped"}
                or await self.db.get_effect(post_key(str(approval_id))) is not None
            ):
                return approval_id
        raise ValueError("This post failed to publish too many times. Change it and try again.")

    async def publish(self, project_id, actor, *, preview_token, request_id, client_id=None):
        from tin_lite.run_service import start_workflow_run

        await self.project(project_id, actor)
        token = str(UUID(str(preview_token)))
        saved = await self.db.get_effect(f"linkedin:preview:{token}")
        value = saved.result if saved and saved.status == "completed" else None
        if not value or value["project_id"] != str(project_id):
            raise ValueError("Preview this project's post before publishing.")
        request_key = f"linkedin:confirm:{project_id}:{UUID(str(request_id))}"
        request = await self.db.get_effect(request_key)
        if request is not None and request.status == "completed":
            if request.result.get("preview_token") != token:
                raise ValueError("This request ID was used for another publication.")
            approval_id = UUID(request.result["approval_id"])
        else:
            approval_id = await self._approval_id(project_id, value["fingerprint"])
        previous = await self.db.get_effect(f"linkedin:approved:{approval_id}")
        if not previous or previous.status != "completed":
            if datetime.fromisoformat(value["valid_until"]) < datetime.now(UTC):
                raise ValueError("The preview expired. Preview the post again.")
            current = await self.snapshot(project_id, actor, value["path"], value["post_id"])
            if current["fingerprint"] != value["fingerprint"]:
                raise ValueError("The post, an image or the account changed. Preview it again.")
        request = await save_effect(
            self.db, request_key, {"preview_token": token, "approval_id": str(approval_id)}
        )
        if request["preview_token"] != token:
            raise ValueError("This request ID was used for another publication.")
        approval_id = UUID(request["approval_id"])
        approval = await save_effect(
            self.db,
            f"linkedin:approved:{approval_id}",
            {**value, "actor": actor, "approved_at": datetime.now(UTC).isoformat()},
        )
        start_key = f"linkedin-publish:{approval_id}"
        existing = await self.db.get_run_by_start_key(
            project_id=project_id, start_idempotency_key=start_key
        )
        if approval["actor"] != actor and existing is None:
            raise ValueError(
                "Another member approved this post. "
                "They can retry their confirmation to finish starting it."
            )
        workflow = await self.db.get_workflow(WORKFLOW_ID)
        if workflow is None:
            raise ValueError("LinkedIn publishing is not installed; sync the workflow catalog.")
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
                start_idempotency_key=start_key,
                input_payload={"approval_id": str(approval_id)},
                trigger_source=existing.trigger_source
                if existing
                else ("mcp" if client_id and client_id != "browser" else "manual"),
                trigger_client=existing.trigger_client if existing else None,
                started_by_oauth_client_id=existing.started_by_oauth_client_id
                if existing
                else (client_id if client_id != "browser" else None),
                _linkedin_publication=True,
            )
        receipt = await self.db.get_effect(f"{run.id}:linkedin_post")
        result = receipt.result if receipt and receipt.status == "completed" else {}
        return {
            "id": str(run.id),
            "run_id": str(run.id),
            "status": run.status.value,
            "url": result.get("url"),
        }
