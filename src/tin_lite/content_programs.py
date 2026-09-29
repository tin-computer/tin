"""Shared HTTP/MCP content-program operations; editable content stays in Files."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, uuid5

from tin_lite.content_plan import KEY, parse_plan, paths, plan_path, validate_change
from tin_lite.content_plan_sources import context_files
from tin_lite.organic_audit import canonical_json, digest
from tin_lite.project_files import ProjectFileService, StaleProjectRevisionError

# Delivery settings also hang off the standing drafting roles onboarding schedules.
PROGRAM_KEYS = frozenset({KEY, "content.answer_page", "content.public_article"})


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


class ContentPrograms:
    def __init__(self, *, database, storage):
        self.db, self.storage = database, storage
        self.files = ProjectFileService(database=database, storage=storage)

    @asynccontextmanager
    async def lock(self, program_id):
        async with self.db.effect_lock(f"content-program:{program_id}", KEY) as (conn, _):
            yield conn

    async def configured(self, project_id, program_id):
        configured = await self.db.get_project_workflow(program_id)
        if (
            configured is None
            or configured.project_id != project_id
            or configured.workflow_key not in PROGRAM_KEYS
        ):
            raise LookupError(
                "Content program not found in this project. Call list_project_workflows "
                "to find the id of a saved content.plan program."
            )
        return configured

    async def facts(self, program_id, *, conn=None):
        from tin_lite.content_draft_progress import history

        executor = conn or self.db.pool
        program = await executor.fetchrow(
            "SELECT * FROM content_programs WHERE project_workflow_id = $1", program_id
        )
        batches = await executor.fetch(
            "SELECT * FROM content_plan_batches WHERE project_workflow_id = $1 ORDER BY batch_id",
            program_id,
        )
        revision = await executor.fetchrow(
            "SELECT * FROM content_plan_revisions WHERE project_workflow_id = $1 AND status "
            "IN ('pending', 'applying')",
            program_id,
        )
        return {
            "drafts": (
                await history(executor, project_id=program["project_id"], program_id=program_id)
            )
            if program
            else {},
            "initialized": bool(program and program["plan_revision"]),
            "plan_path": plan_path(program_id),
            "batches": [{**dict(row), "item_ids": decoded(row["item_ids"])} for row in batches],
            "pending_revision": (
                {
                    key: revision[key]
                    for key in ("id", "run_id", "base_revision", "preview_revision", "status")
                }
                | {"batch_ids": decoded(revision["batch_ids"])}
            )
            if revision
            else None,
        }

    async def read(self, *, project_id, program_id, revision=None):
        await self.configured(project_id, program_id)
        project = await self.db.get_project(project_id)
        if revision is None:
            repo = await self.storage.get_repo(project.state_repo_id)
            revision = await self.storage.head_sha(repo, project.canonical_branch)
        content = await self.storage.read_canonical_artifact_if_exists(
            repo_id=project.state_repo_id, commit_sha=revision, path=plan_path(program_id)
        )
        if content is None:
            raise ValueError("The working plan is missing. Restore it in Files before continuing.")
        plan = parse_plan(content)
        if plan["program_id"] != str(program_id):
            raise ValueError("The file belongs to a different content program.")
        return {"revision": revision, "plan": plan}

    async def save(
        self,
        *,
        project_id,
        program_id,
        request_id,
        expected_revision,
        proposed,
        actor,
        client_id=None,
    ):
        async with self.lock(program_id):
            await self.configured(project_id, program_id)
            existing = await self.db.get_project_file_change(
                project_id=project_id, request_id=request_id
            )
            if existing and existing["status"] == "completed":
                project = await self.db.get_project(project_id)
                return await self.files.commit(
                    project=project,
                    actor_clerk_user_id=actor,
                    client_id=client_id,
                    request_id=request_id,
                    expected_revision=expected_revision,
                    message="Update upcoming content batches",
                    changes=[
                        {
                            "operation": "upsert",
                            "path": plan_path(program_id),
                            "content": canonical_json(proposed).decode(),
                        }
                    ],
                )
            current = await self.read(
                project_id=project_id, program_id=program_id, revision=expected_revision
            )
            facts = await self.facts(program_id)
            reserved = {row["batch_id"] for row in facts["batches"]}
            held = set((facts["pending_revision"] or {}).get("batch_ids", []))
            editable = {row["id"] for row in current["plan"]["batches"]} - reserved - held
            validated = validate_change(
                current["plan"],
                proposed,
                editable=editable,
                reserved_items={item for row in facts["batches"] for item in row["item_ids"]},
            )
            project = await self.db.get_project(project_id)
            return await self.files.commit(
                project=project,
                actor_clerk_user_id=actor,
                client_id=client_id,
                request_id=request_id,
                expected_revision=expected_revision,
                message="Update upcoming content batches",
                changes=[
                    {
                        "operation": "upsert",
                        "path": plan_path(program_id),
                        "content": canonical_json(validated).decode(),
                    }
                ],
            )

    async def begin_revision(
        self,
        *,
        project_id,
        program_id,
        request_id,
        expected_revision,
        batch_ids,
        instruction,
        context_paths,
        actor,
    ):
        if (
            not 1 <= len(instruction.strip()) <= 4000
            or not batch_ids
            or len(set(batch_ids)) != len(batch_ids)
        ):
            raise ValueError("Give revision instructions and distinct future batch IDs.")
        fingerprint = digest(
            [
                str(project_id),
                str(program_id),
                expected_revision,
                batch_ids,
                instruction,
                context_paths,
            ]
        )
        async with self.lock(program_id) as conn:
            await self.configured(project_id, program_id)
            existing = await conn.fetchrow(
                "SELECT * FROM content_plan_revisions WHERE id = $1", request_id
            )
            if existing:
                if existing["request_fingerprint"] != fingerprint:
                    raise ValueError("Revision request ID belongs to different content.")
                return dict(existing)
            current = await self.read(project_id=project_id, program_id=program_id)
            if current["revision"] != expected_revision:
                raise ValueError("The project changed. Refresh before requesting a revision.")
            facts = await self.facts(program_id, conn=conn)
            if not facts["initialized"] or facts["pending_revision"]:
                raise ValueError(
                    "Finish initial planning or apply/discard the pending revision first."
                )
            editable = {batch["id"] for batch in current["plan"]["batches"]} - {
                row["batch_id"] for row in facts["batches"]
            }
            if not set(batch_ids) <= editable:
                raise ValueError("Choose only batches that have not started.")
            project = await self.db.get_project(project_id)
            await context_files(
                storage=self.storage,
                project=project,
                revision=expected_revision,
                paths=context_paths,
            )
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO content_plan_revisions (id, project_workflow_id, project_id,
                        request_fingerprint, base_revision, batch_ids, instruction,
                        context_paths, actor_clerk_user_id)
                    VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8::jsonb,$9) RETURNING *
                    """,
                    request_id,
                    program_id,
                    project_id,
                    fingerprint,
                    expected_revision,
                    json.dumps(batch_ids),
                    instruction,
                    json.dumps(context_paths),
                    actor,
                )
                await self.event(
                    conn,
                    project_id,
                    program_id,
                    request_id,
                    "requested",
                    "Content revision requested; selected upcoming batches are held.",
                    actor,
                )
            return dict(row)

    async def resolve(
        self, *, project_id, program_id, revision_id, action, actor, expected_revision=None
    ):
        if action not in {"apply", "discard"}:
            raise ValueError("Choose apply or discard.")
        async with self.lock(program_id) as conn:
            await self.configured(project_id, program_id)
            row = await conn.fetchrow(
                "SELECT * FROM content_plan_revisions WHERE id = $1 AND project_id = $2 AND "
                "project_workflow_id = $3",
                revision_id,
                project_id,
                program_id,
            )
            if row is None:
                raise LookupError("Revision not found.")
            if row["status"] in {"applied", "discarded"}:
                if row["status"] != {"apply": "applied", "discard": "discarded"}[action]:
                    raise ValueError("This revision has already been resolved differently.")
                return {"status": row["status"], "revision": row["applied_revision"]}
            revision = None
            if action == "apply":
                if not row["preview_revision"] or not row["run_id"]:
                    raise ValueError("The preview is not ready. You can discard this request.")
                project = await self.db.get_project(project_id)
                proposal = parse_plan(
                    await self.storage.read_canonical_artifact(
                        repo_id=project.state_repo_id,
                        commit_sha=row["preview_revision"],
                        path=paths(str(row["run_id"]))["plan.json"],
                    )
                )
                base = await self.read(
                    project_id=project_id, program_id=program_id, revision=row["base_revision"]
                )
                apply_base = row["apply_base_revision"] or expected_revision
                if not apply_base:
                    raise ValueError("Apply requires the project revision shown in the editor.")
                if row["status"] == "pending":
                    current = await self.read(
                        project_id=project_id, program_id=program_id, revision=apply_base
                    )
                    if current["plan"] != base["plan"]:
                        raise ValueError(
                            "The working plan changed. Discard this preview and request a "
                            "fresh revision."
                        )
                facts = await self.facts(program_id, conn=conn)
                editable = set(decoded(row["batch_ids"])) - {
                    b["batch_id"] for b in facts["batches"]
                }
                proposal = validate_change(
                    base["plan"],
                    proposal,
                    editable=editable,
                    reserved_items={item for b in facts["batches"] for item in b["item_ids"]},
                )
                # Persist first; a lost response reconciles through the same file request.
                await conn.execute(
                    """UPDATE content_plan_revisions
                    SET status = 'applying', apply_base_revision = $2, updated_at = now()
                    WHERE id = $1 AND status IN ('pending','applying')""",
                    revision_id,
                    apply_base,
                )
                try:
                    result = await self.files.commit(
                        project=project,
                        actor_clerk_user_id=actor,
                        client_id=None,
                        request_id=uuid5(NAMESPACE_URL, f"content-revision:{revision_id}:apply"),
                        expected_revision=apply_base,
                        message="Apply reviewed content revision",
                        changes=[
                            {
                                "operation": "upsert",
                                "path": plan_path(program_id),
                                "content": canonical_json(proposal).decode(),
                            }
                        ],
                    )
                except StaleProjectRevisionError:
                    if row["status"] == "applying":
                        # A lost reply followed by another commit is not proof of rejection.
                        # Keep the hold until the original file request is reconciled.
                        raise RuntimeError(
                            "The earlier Apply remains unconfirmed. Its hold is retained; "
                            "reconcile the original file request before discarding."
                        ) from None
                    # Known rejection, not ambiguous acceptance: allow discard, never rebase.
                    await conn.execute(
                        "UPDATE content_plan_revisions SET status = 'pending' WHERE id = $1",
                        revision_id,
                    )
                    raise
                revision = result.revision
            elif row["status"] == "applying":
                raise ValueError("An apply is being reconciled. Retry Apply before discarding.")
            resolved = "applied" if action == "apply" else "discarded"
            async with conn.transaction():
                await conn.execute(
                    "UPDATE content_plan_revisions SET status = $2, applied_revision = $3, "
                    "updated_at = now() WHERE id = $1",
                    revision_id,
                    resolved,
                    revision,
                )
                await self.event(
                    conn,
                    project_id,
                    program_id,
                    revision_id,
                    resolved,
                    f"Content revision {resolved}; upcoming batches released.",
                    actor,
                )
            return {"status": resolved, "revision": revision}

    @staticmethod
    async def event(conn, project_id, program_id, request_id, action, summary, actor):
        await conn.execute(
            """INSERT INTO activity_events (
                project_id, event_type, details, summary, audience, dedupe_key)
            VALUES ($1, 'content_plan_revision', $2::jsonb, $3, 'product', $4)
            ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING""",
            project_id,
            json.dumps(
                {
                    "kind": "your_edits",
                    "workflow_key": KEY,
                    "project_workflow_id": str(program_id),
                    "actor_clerk_user_id": actor,
                }
            ),
            summary,
            f"content-revision:{request_id}:{action}",
        )
