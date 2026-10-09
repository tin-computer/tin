"""Bounded LinkedIn draft files; editing never grants publication authority."""

import hashlib
import json
from dataclasses import replace
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tin_lite.project_files import StaleProjectRevisionError, safe_project_file_path
from tin_lite.workflow_packages import load_workflow_source

MAX_BYTES = 128_000
SCHEMA = "tin.social.linkedin_draft.v1"


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Source(Closed):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,60}$")
    label: str = Field(min_length=1, max_length=300)
    author: str = Field(max_length=200)
    use: Literal["publishable", "background", "reference"]
    excerpt: str = Field(min_length=1, max_length=16000)


class Support(Closed):
    source_id: str = Field(max_length=60)
    excerpt: str = Field(min_length=1, max_length=4000)


class Post(Closed):
    id: str = Field(pattern=r"^p[1-6]$")
    text: str = Field(min_length=1, max_length=2500)
    angle: str = Field(min_length=1, max_length=1000)
    template_id: str = Field(min_length=1, max_length=100)
    readiness: Literal["ready", "needs_evidence", "edited"]
    support: list[Support] = Field(max_length=20)
    editor_notes: str = Field(max_length=4000)


class Gap(Closed):
    angle: str = Field(max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)


class Parent(Closed):
    path: str = Field(max_length=512)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    post_id: str = Field(pattern=r"^p[1-6]$")


class Draft(Closed):
    schema_version: Literal["tin.social.linkedin_draft.v1"]
    batch_id: str = Field(min_length=1, max_length=100)
    author: str = Field(min_length=1, max_length=200)
    audience: str = Field(min_length=1, max_length=8000)
    style_path: str = Field(max_length=512)
    brand_path: str = Field(max_length=512)
    template_path: str = Field(max_length=512)
    sources: list[Source] = Field(max_length=24)
    posts: list[Post] = Field(max_length=6)
    gaps: list[Gap] = Field(max_length=12)
    parent: Parent | None


def validate_draft(value):
    if isinstance(value, bytes):
        if len(value) > MAX_BYTES:
            raise ValueError("The LinkedIn draft is too large.")
        value = json.loads(value)
    draft = Draft.model_validate(value)
    sources = {source.id: source for source in draft.sources}
    if len(sources) != len(draft.sources) or len({p.id for p in draft.posts}) != len(draft.posts):
        raise ValueError("Draft and source IDs must be unique.")
    if not draft.posts and not draft.gaps:
        raise ValueError("A draft needs posts or a specific explanation of missing evidence.")
    for path in [draft.style_path, draft.brand_path, draft.template_path] + (
        [draft.parent.path] if draft.parent else []
    ):
        if path and not safe_project_file_path(path):
            raise ValueError("Choose a file within this project.")
    for post in draft.posts:
        if not post.text.strip() or any(ord(c) < 32 and c not in "\n\t" for c in post.text):
            raise ValueError("Post text is empty or contains control characters.")
        for support in post.support:
            source = sources.get(support.source_id)
            if not source or source.use != "publishable" or support.excerpt not in source.excerpt:
                raise ValueError("Draft evidence must quote a publishable source exactly.")
    result = draft.model_dump()
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ValueError("The LinkedIn draft is too large.")
    return result


def draft_path(path):
    return (
        safe_project_file_path(path)
        and path.startswith("social/linkedin/drafts/")
        and path.endswith(".json")
        and "/" not in path.removeprefix("social/linkedin/drafts/")
    )


class LinkedInDrafts:
    def __init__(self, runtime):
        self.runtime = runtime
        self.db, self.storage = runtime.database, runtime.storage

    async def project(self, project_id, actor):
        if not await self.db.has_project_access(project_id=project_id, clerk_user_id=actor):
            raise LookupError("Project not found.")
        project = await self.db.get_project(project_id)
        if project is None:
            raise LookupError("Project not found.")
        return project

    async def read(self, project_id, actor, path):
        project = await self.project(project_id, actor)
        if not draft_path(path):
            raise ValueError("Choose a LinkedIn draft from this project's Files.")
        paths, revision = await self.storage.list_canonical_files(
            repo_id=project.state_repo_id, branch=project.canonical_branch
        )
        if path not in paths:
            raise LookupError("LinkedIn draft not found.")
        raw = await self.storage.read_bounded_project_file(
            repo_id=project.state_repo_id, commit_sha=revision, path=path, max_bytes=MAX_BYTES
        )
        if raw is None:
            raise LookupError("LinkedIn draft not found.")
        source = await self.revision_source(project_id, path)
        return dict(
            revision_source_run_id=str(source[0].id) if source else None,
            path=path,
            revision=revision,
            draft=validate_draft(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    async def save(
        self, project_id, actor, *, path, expected_revision, request_id, draft, client_id=None
    ):
        project = await self.project(project_id, actor)
        if not draft_path(path):
            raise ValueError("Choose a LinkedIn draft from this project's Files.")
        value = validate_draft(draft)
        # The same revision guard used by all project files preserves concurrent edits.
        content = json.dumps(value, ensure_ascii=False, indent=2)
        if len(content.encode()) > MAX_BYTES:
            raise ValueError("The LinkedIn draft is too large.")
        result = await self.runtime.project_files.commit(
            project=project,
            actor_clerk_user_id=actor,
            client_id=client_id,
            request_id=UUID(str(request_id)),
            expected_revision=expected_revision,
            message="Edit LinkedIn draft",
            changes=[{"operation": "upsert", "path": path, "content": content}],
        )
        return dict(
            path=path,
            revision=result.revision,
            draft=value,
            sha256=hashlib.sha256(content.encode()).hexdigest(),
        )

    async def revision_source(self, project_id, path):
        source_id = await self.db.pool.fetchval(
            "SELECT r.id FROM workflow_runs r JOIN workflows w ON w.id=r.workflow_id "
            "WHERE r.project_id=$1 AND r.artifact_path=$2 AND r.status='succeeded' "
            "AND w.project_id=$1 AND w.executor='workflow.code' AND w.status='active' "
            "ORDER BY r.created_at DESC LIMIT 1",
            project_id,
            path,
        )
        if not source_id:
            return None
        run = await self.db.get_run(source_id)
        workflow = await self.db.get_workflow(run.workflow_id)
        project = await self.db.get_project(project_id)
        if not run.definition_commit_sha or workflow.definition_repo_id != project.state_repo_id:
            return None
        source = await load_workflow_source(
            storage=self.storage,
            repo_id=workflow.definition_repo_id,
            commit_sha=run.definition_commit_sha,
            definition_path=workflow.definition_path,
        )
        definition = source.definition
        fields = definition.get("input_schema", {}).get("properties", {})
        code = definition.get("code", {})
        if (
            definition.get("system") != "linkedin"
            or definition.get("executor") != "workflow.code"
            or definition.get("integration_requirements")
            or code.get("services")
            or code.get("output", {}).get("path") != "social/linkedin/drafts/{date}-{slug}.json"
            or "revise" not in fields.get("action", {}).get("enum", [])
            or not {"draft_path", "draft_sha256", "post_id", "feedback"} <= fields.keys()
        ):
            return None
        return run, replace(
            workflow, definition=definition, current_commit_sha=run.definition_commit_sha
        )

    async def prepare_revision(
        self, project_id, actor, *, path, expected_sha256, post_id, feedback, request_id
    ):
        await self.project(project_id, actor)
        if not feedback.strip() or len(feedback) > 4000:
            raise ValueError("Describe the requested change in at most 4,000 characters.")
        key = f"linkedin-revision:{actor}:{UUID(str(request_id))}"
        requested = dict(
            action="revise",
            draft_path=path,
            draft_sha256=expected_sha256,
            post_id=post_id,
            feedback=feedback,
        )
        existing = await self.db.get_run_by_start_key(
            project_id=project_id, start_idempotency_key=key
        )
        if existing:
            if existing.started_by_clerk_user_id != actor or any(
                (existing.input or {}).get(k) != v for k, v in requested.items()
            ):
                raise ValueError("This request ID belongs to different feedback.")
            return {"existing": existing}
        view = await self.read(project_id, actor, path)
        if view["sha256"] != expected_sha256:
            raise StaleProjectRevisionError(
                "The draft changed. Reload it before requesting changes."
            )
        if post_id not in {p["id"] for p in view["draft"]["posts"]}:
            raise ValueError("Choose a post in this batch.")
        source = await self.revision_source(project_id, path)
        if not source:
            raise ValueError("This draft has no available private revision workflow.")
        run, workflow = source
        inputs = {**(run.input or {}), **requested}
        inputs.pop("project_id", None)
        return {"workflow": workflow, "inputs": inputs, "key": key}
