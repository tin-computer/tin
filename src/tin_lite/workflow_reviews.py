"""One exact-version review boundary for HTTP, MCP and durable dispatch."""

import hashlib
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from tin_lite import content_draft, content_editorial_judgment
from tin_lite.domain import RunStatus
from tin_lite.project_files import safe_project_file_path
from tin_lite.workflow_review_store import ReviewConflict, accept_approval, digest, unpack

SUPPORTED = frozenset({"content.generate", "content.public_article"})
SUPPORTED_IDS = frozenset(
    {
        UUID("00000000-0000-4000-8000-000000000031"),
        UUID("00000000-0000-4000-8000-000000000009"),
    }
)
CAPABILITY = "content-revision.v1"
MAX_CONTEXT_BYTES = 64_000


def review_token(run, artifact):
    return digest({"run": str(run.id), "version": run.review_version, "artifact": artifact})


class WorkflowReviews:
    def __init__(self, *, runtime, settings):
        self.runtime, self.settings = runtime, settings
        self.db, self.storage = runtime.database, runtime.storage

    async def source(self, run_id, actor):
        run = await self.db.get_run(run_id)
        if not run or not await self.db.has_project_access(
            project_id=run.project_id, clerk_user_id=actor
        ):
            raise LookupError("Review not found.")
        definition = await self.db.get_workflow(run.workflow_id)
        if not definition or definition.project_id is not None or definition.key not in SUPPORTED:
            raise ValueError("This workflow does not support article revisions yet.")
        return run, definition

    async def artifact(self, run):
        artifact_run = run
        if not run.artifact_path and run.review_source_run_id:
            previous = await self.db.pool.fetchrow(
                "SELECT artifact_run_id FROM workflow_review_commands WHERE successor_run_id=$1",
                run.id,
            )
            if previous:
                artifact_run = await self.db.get_run(previous["artifact_run_id"])
        if (
            not artifact_run
            or not artifact_run.artifact_path
            or not artifact_run.canonical_commit_sha
        ):
            raise ReviewConflict("There is no saved copy to review yet.")
        receipt = await self.db.get_effect(f"{artifact_run.id}:procedure_canonical_commit")
        if not receipt or receipt.status != "completed" or not receipt.result:
            raise ReviewConflict("The saved copy's publication proof is unavailable.")
        if (
            receipt.result.get("canonical_commit_sha") != artifact_run.canonical_commit_sha
            or receipt.result.get("artifact_path") != artifact_run.artifact_path
        ):
            raise ReviewConflict("The saved copy does not match its publication proof.")
        return artifact_run, {
            "run_id": str(artifact_run.id),
            "path": artifact_run.artifact_path,
            "revision": artifact_run.canonical_commit_sha,
            "sha256": (receipt.result.get("checkpoint") or {}).get("sha256"),
            "assessment": content_editorial_judgment.no_draft(receipt.result),
        }

    async def view(self, run_id, actor):
        from tin_lite.reviewed_documents import ReviewedDocuments, document_spec

        run = await self.db.get_run(run_id)
        if run and await self.db.has_project_access(project_id=run.project_id, clerk_user_id=actor):
            if await document_spec(self.db, self.storage, run):
                return await ReviewedDocuments(database=self.db, storage=self.storage).view(
                    run_id, actor
                )
        run, definition = await self.source(run_id, actor)
        root = run.review_root_run_id or run.id
        versions = await self.db.pool.fetch(
            "SELECT id, status, review_version, artifact_path, canonical_commit_sha, created_at "
            "FROM workflow_runs WHERE project_id=$1 AND (id=$2 OR review_root_run_id=$2) "
            "ORDER BY review_version DESC LIMIT 100",
            run.project_id,
            root,
        )
        current = versions[0]
        try:
            _, artifact = await self.artifact(run)
        except ReviewConflict:
            artifact = None
        latest = current["id"] == run.id
        ready = latest and artifact is not None and run.review_decision is None
        can_revise = ready and (
            run.status == RunStatus.NEEDS_INPUT
            or (run.status == RunStatus.SUCCEEDED and artifact["assessment"])
            or (run.status == RunStatus.FAILED and run.review_source_run_id is not None)
        )
        if definition.key == content_draft.KEY and can_revise:
            prepared = await self.db.get_effect(content_draft.receipt_key(UUID(artifact["run_id"])))
            can_revise = bool(
                prepared
                and prepared.status == "completed"
                and prepared.result
                and prepared.result.get("output_validator") in content_draft.CLEAN_VALIDATORS
            )
        incoming = await self.db.pool.fetchrow(
            "SELECT feedback, reference_files FROM workflow_review_commands "
            "WHERE successor_run_id=$1",
            run.id,
        )
        publication = await self.db.get_effect(f"{run.id}:procedure_canonical_commit")
        summary = (publication.result or {}).get("review_change_summary") if publication else None
        return {
            "run_id": str(run.id),
            "project_id": str(run.project_id),
            "root_run_id": str(root),
            "current_run_id": str(current["id"]),
            "version": run.review_version,
            "status": run.status.value,
            "artifact": artifact,
            "review_token": review_token(run, artifact) if artifact else None,
            "can_request_changes": can_revise,
            "can_approve": ready
            and run.status == RunStatus.NEEDS_INPUT
            and not artifact["assessment"],
            "is_current": latest,
            "versions": [dict(v) for v in versions],
            "feedback": incoming["feedback"] if incoming else None,
            "change_summary": summary,
            "review_url": f"/document/{run.id}?project={run.project_id}&return=decisions",
        }

    async def request_changes(
        self,
        *,
        run_id,
        actor,
        feedback,
        request_id,
        token,
        reference_files=(),
        billing_quote_id=None,
        trigger_client=None,
        trigger_source="manual",
        oauth_client_id=None,
    ):
        run, definition = await self.source(run_id, actor)
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 8000:
            raise ValueError("Describe the changes in 1–8,000 characters.")
        paths = list(reference_files)
        if (
            len(paths) > 8
            or len(set(paths)) != len(paths)
            or any(not isinstance(p, str) or not safe_project_file_path(p) for p in paths)
        ):
            raise ValueError("Choose up to eight different project files.")
        fingerprint = digest(
            {"run_id": str(run_id), "feedback": feedback, "files": paths, "token": token}
        )
        existing = await self.db.pool.fetchrow(
            "SELECT * FROM workflow_review_commands WHERE project_id=$1 AND request_id=$2",
            run.project_id,
            request_id,
        )
        if existing:
            if (
                existing["request_digest"] != fingerprint
                or existing["actor_clerk_user_id"] != actor
            ):
                raise ReviewConflict("This request ID already belongs to different feedback.")
            return await self.db.get_run(existing["successor_run_id"])
        view = await self.view(run_id, actor)
        if token != view["review_token"] or not view["can_request_changes"]:
            raise ReviewConflict("This version cannot be revised. Open the current review first.")
        if (definition.definition.get("human_review") or {}).get("revision_adapter") != CAPABILITY:
            raise ReviewConflict("The revision-capable workflow definition is not installed yet.")
        project = await self.db.get_project(run.project_id)
        refs = []
        if paths:
            repo = await self.storage.get_repo(project.state_repo_id)
            revision = await self.storage.head_sha(repo, project.canonical_branch)
            for path in paths:
                raw = await self.storage.read_canonical_artifact(
                    repo_id=project.state_repo_id, commit_sha=revision, path=path
                )
                if len(raw) > 20_000 or sum(f["bytes"] for f in refs) + len(raw) > 60_000:
                    raise ValueError("Reference files must fit 20 KB each and 60 KB together.")
                raw.decode("utf-8")
                refs.append(
                    {
                        "path": path,
                        "revision": revision,
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
        artifact_run, artifact = await self.artifact(run)
        command = {
            "id": uuid4(),
            "project_id": run.project_id,
            "request_id": request_id,
            "actor_clerk_user_id": actor,
            "source_run_id": run.id,
            "root_run_id": run.review_root_run_id or run.id,
            "artifact_run_id": artifact_run.id,
            "coordinator_run_id": run.id if run.status == RunStatus.NEEDS_INPUT else None,
            "action": "revise",
            "request_digest": fingerprint,
            "review_token": token,
            "artifact": artifact,
            "feedback": feedback,
            "reference_files": refs,
        }
        if run.status == RunStatus.FAILED:
            prior = await self.db.pool.fetchrow(
                "SELECT coordinator_run_id FROM workflow_review_commands WHERE successor_run_id=$1",
                run.id,
            )
            command["coordinator_run_id"] = prior["coordinator_run_id"] if prior else None
        if definition.key == content_draft.KEY:
            from tin_lite.content_draft_sources import ContentDraftSources

            sources = ContentDraftSources(database=self.db, storage=self.storage)
            command["program_revision"] = await self.db.pool.fetchval(
                "SELECT plan_revision FROM content_programs WHERE project_workflow_id=$1 "
                "AND project_id=$2",
                UUID(run.input["program_id"]),
                run.project_id,
            )
            selection = await sources.selection(artifact_run.id)
            prepared = await sources.saved(artifact_run.id)
            # select checks the exact selected item against today's editable plan.
            checked = await sources.select(
                project_id=run.project_id,
                inputs={
                    **run.input,
                    "item_id": prepared["item"]["id"],
                    "plan_revision": prepared["plan_revision"],
                },
            )
            if checked["item"] != prepared["item"]:
                raise ReviewConflict(
                    "The brief changed. Resolve the plan amendment before revising."
                )
            if not selection:
                raise ReviewConflict("The original article selection is unavailable.")
            command["draft_selection"] = selection
        # Validate every pinned source and the combined packet before purchasing compute.
        await revision_context(self.db, self.storage, command, artifact_run)
        from tin_lite.run_service import start_workflow_run

        successor = await start_workflow_run(
            runtime=self.runtime,
            settings=self.settings,
            workflow=definition,
            project_id=run.project_id,
            started_by_clerk_user_id=actor,
            input_payload=run.input,
            project_workflow_id=run.project_workflow_id,
            start_idempotency_key=f"review:{request_id}",
            _prepare_only=True,
            billing_quote_id=billing_quote_id,
            trigger_client=trigger_client,
            trigger_source=trigger_source,
            started_by_oauth_client_id=oauth_client_id,
            _review_transition=command,
        )
        # Acceptance is durable even if the next signal acknowledgement is lost.
        return successor

    async def compare(self, run_id, actor):
        run, _ = await self.source(run_id, actor)
        if not run.review_source_run_id or not run.artifact_path:
            raise ReviewConflict("There is no revised copy to compare yet.")
        command = await self.db.pool.fetchrow(
            "SELECT artifact_run_id FROM workflow_review_commands WHERE successor_run_id=$1",
            run.id,
        )
        previous = await self.db.get_run(command["artifact_run_id"])
        project = await self.db.get_project(run.project_id)
        result = {}
        total = 0
        for name, version in (("previous", previous), ("revised", run)):
            raw = await self.storage.read_canonical_artifact(
                repo_id=project.state_repo_id,
                commit_sha=version.canonical_commit_sha,
                path=version.artifact_path,
            )
            total += len(raw)
            if total > 300_000:
                raise ValueError(
                    "These versions are too large for comparison. Read each full copy."
                )
            result[name] = {
                "run_id": str(version.id),
                "version": version.review_version,
                "path": version.artifact_path,
                "content": raw.decode("utf-8"),
            }
        return result

    async def cancel_failed_revision(self, *, run_id, actor, token):
        from tin_lite.workflow_review_store import accept_cancellation

        run, _ = await self.source(run_id, actor)
        _, artifact = await self.artifact(run)
        if not run.review_source_run_id or token != review_token(run, artifact):
            raise ReviewConflict("Read the failed revision before stopping its review chain.")
        incoming = await self.db.pool.fetchrow(
            "SELECT coordinator_run_id FROM workflow_review_commands WHERE successor_run_id=$1",
            run.id,
        )
        command = {
            "id": uuid4(),
            "project_id": run.project_id,
            "request_id": uuid5(NAMESPACE_URL, f"tin:cancel-review:{run.id}:{token}"),
            "actor_clerk_user_id": actor,
            "source_run_id": run.id,
            "root_run_id": run.review_root_run_id,
            "artifact_run_id": UUID(artifact["run_id"]),
            "coordinator_run_id": incoming["coordinator_run_id"],
            "action": "cancel",
            "request_digest": digest({"cancel": str(run.id), "token": token}),
            "review_token": token,
            "artifact": artifact,
        }
        await accept_cancellation(self.db, command)
        return await self.db.get_run(run.id)

    async def approve(self, *, run_id, actor, token=None):
        from tin_lite.reviewed_documents import ReviewedDocuments, document_spec

        run = await self.db.get_run(run_id)
        if run and await self.db.has_project_access(project_id=run.project_id, clerk_user_id=actor):
            if await document_spec(self.db, self.storage, run):
                return await ReviewedDocuments(database=self.db, storage=self.storage).approve(
                    run_id=run_id,
                    actor=actor,
                    token=token,
                )
        run, _ = await self.source(run_id, actor)
        _, artifact = await self.artifact(run)
        expected = review_token(run, artifact)
        if token is not None and token != expected:
            raise ReviewConflict("This approval targets another version. Read the current draft.")
        existing = await self.db.pool.fetchrow(
            "SELECT action FROM workflow_review_commands WHERE source_run_id=$1",
            run.id,
        )
        if existing and existing["action"] == "approve":
            return run
        if existing or run.status == RunStatus.SUPERSEDED:
            raise ReviewConflict(
                "This draft has been replaced. Read the current version before approving."
            )
        if run.review_decision == "approved":
            return run
        if artifact["assessment"]:
            raise ReviewConflict("This is an editorial assessment, not an article to approve.")
        command = {
            "id": uuid4(),
            "project_id": run.project_id,
            "request_id": uuid5(NAMESPACE_URL, f"tin:approve:{run.id}:{expected}"),
            "actor_clerk_user_id": actor,
            "source_run_id": run.id,
            "root_run_id": run.review_root_run_id or run.id,
            "artifact_run_id": run.id,
            "coordinator_run_id": run.id,
            "action": "approve",
            "request_digest": digest({"approve": str(run.id), "token": expected}),
            "review_token": expected,
            "artifact": artifact,
        }
        await accept_approval(self.db, command)
        return await self.db.get_run(run.id)


async def revision_context(database, storage, command, source_run):
    """Keep original sources pinned; snapshot the checkout for additional references."""
    project = await database.get_project(source_run.project_id)
    artifact = command["artifact"]
    raw = await storage.read_canonical_artifact(
        repo_id=project.state_repo_id, commit_sha=artifact["revision"], path=artifact["path"]
    )
    if artifact.get("sha256") and hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
        raise ReviewConflict("The saved copy does not match its publication digest.")
    notes = await storage.read_canonical_artifact_if_exists(
        repo_id=project.state_repo_id,
        commit_sha=artifact["revision"],
        path=content_draft.notes_path(artifact["path"]),
    )
    references = []
    for ref in command["reference_files"]:
        content = await storage.read_canonical_artifact(
            repo_id=project.state_repo_id, commit_sha=ref["revision"], path=ref["path"]
        )
        if len(content) != ref["bytes"] or hashlib.sha256(content).hexdigest() != ref["sha256"]:
            raise ReviewConflict("A reference does not match the file you submitted.")
        references.append({**ref, "content": content.decode("utf-8")})
    earlier = await database.pool.fetch(
        "SELECT feedback FROM workflow_review_commands WHERE root_run_id=$1 AND action='revise' "
        "AND source_run_id<>$2 ORDER BY created_at DESC LIMIT 3",
        command["root_run_id"],
        command["source_run_id"],
    )
    from tin_lite.writing_style import STYLE_PATH

    prepared = await database.get_effect(content_draft.receipt_key(source_run.id))
    original_context = prepared.result if prepared and prepared.status == "completed" else {}
    root = (
        await database.get_run(source_run.review_root_run_id)
        if source_run.review_root_run_id
        else source_run
    )
    style_revision = (
        (original_context.get("style") or {}).get("revision")
        or root.expected_head_sha
        or artifact["revision"]
    )
    style = await storage.read_canonical_artifact_if_exists(
        repo_id=project.state_repo_id,
        commit_sha=style_revision,
        path=STYLE_PATH,
    )
    repo = await storage.get_repo(project.state_repo_id)
    project_revision = await storage.head_sha(repo, project.canonical_branch)
    if not project_revision:
        raise ValueError("The revision's project files are unavailable.")
    context = {
        "schema": CAPABILITY,
        "project_revision": project_revision,
        "source": artifact,
        "previous_copy": raw.decode("utf-8"),
        "generation_notes": notes.decode("utf-8") if notes else None,
        "feedback": command["feedback"],
        "earlier_feedback": [r["feedback"] for r in reversed(earlier)],
        "reference_files": references,
        "writing_guide": style.decode("utf-8") if style else None,
    }
    import json

    if len(json.dumps([context, original_context]).encode()) > MAX_CONTEXT_BYTES:
        raise ValueError(
            "This revision exceeds the review context limit. Use fewer or shorter references."
        )
    return context


async def saved_revision_context(database, storage, run):
    key = f"review:{run.id}:context"
    async with database.effect_lock(key, CAPABILITY) as (conn, existing):
        if existing and existing.status == "completed":
            return existing.result
        row = await conn.fetchrow(
            "SELECT * FROM workflow_review_commands WHERE successor_run_id=$1", run.id
        )
        if row is None:
            return None
        command = unpack(row)
        source = await database.get_run(command["artifact_run_id"])
        context = await revision_context(database, storage, command, source)
        await database.start_effect(conn, execution_key=key, operation=CAPABILITY)
        await database.complete_effect(conn, execution_key=key, result=context)
        return context
