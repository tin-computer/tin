"""The founder's coding agent revises a pending brand or style proposal before approval.

Emre, 10/1: Decisions keeps Approve and Discard; instead of a "request changes" button, the
founder's coding agent revises the waiting material. Tin accepts a revision only while the run
still waits in Decisions, only for that run's own proposal files, and only when the files pass
the validators the capture itself used. An accepted revision is one project commit plus a
`capture_proposal_revisions` row naming who sent it; the run's review then points at that
commit, so Decisions and the reader show the revised text. Approval binds the exact version the
founder read: reviewed_documents.py for the brand pair, StyleProposalReview below for the
writing guide. Runs pinned before brand.capture and style.capture 1.2.0 keep their old rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from tin_lite.code_storage import ProjectStateChangedError
from tin_lite.domain import RunStatus
from tin_lite.procedure_documents import AGENT_REVISION, resolve_run_path, validate_document
from tin_lite.project_files import ProjectFileMutation
from tin_lite.workflow_packages import load_workflow_source
from tin_lite.workflow_review_store import ReviewConflict, accept_approval, digest, unpack

STYLE_KEY = "style.capture"
BRAND_VALIDATOR = "brand-design-capture.v1"
TOOL = "revise_capture_proposal"
MAX_NOTE_CHARS = 500
PROPOSAL_PREFIXES = ("brand/proposals/", "style/proposals/")
CLIENT_LABELS = {"claude_code": "Claude Code", "codex": "Codex"}
STALE = "This proposal changed after you opened it. Read it again, then approve."
EDITED = (
    "A proposed file changed outside Tin's revision route, so it cannot be approved as it is. "
    f"Ask your coding agent to revise it with {TOOL}, or discard it."
)


class ProposalFile(BaseModel):
    """One proposal file's complete revised text, as an agent sends it over HTTP or MCP."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=64_000)


class RevisionRefused(ValueError):
    """This run, path or request cannot take a revision; nothing was written."""


class RevisionInvalid(ValueError):
    """The revised text fails the capture's own validators; nothing was written."""


@dataclass(frozen=True)
class ProposalContract:
    """What one waiting run lets an agent revise: its own proposal files, primary first."""

    kind: str  # "brand" or "style"
    label: str
    paths: tuple[str, ...]


async def binds_style_approval(database, storage, run) -> bool:
    """A 1.2.0 writing guide approves through its review token; 1.1.0 signals the run."""
    return run.executor == STYLE_KEY and await contract(database, storage, run) is not None


async def pinned_definition(database, storage, run) -> dict | None:
    workflow = await database.get_workflow(run.workflow_id)
    if workflow is None or workflow.project_id is not None:
        return None
    if workflow.current_commit_sha == run.definition_commit_sha:
        return workflow.definition
    source = await load_workflow_source(
        storage=storage,
        repo_id=workflow.definition_repo_id,
        commit_sha=run.definition_commit_sha,
        definition_path=workflow.definition_path,
    )
    return source.definition


async def contract(database, storage, run) -> ProposalContract | None:
    """The revision contract the run pinned, or None for every other run, including 1.1.0."""
    if run is None or not run.review_required or run.created_at is None:
        return None
    if run.executor == STYLE_KEY:
        from tin_lite.style_capture import proposal_path

        definition = await pinned_definition(database, storage, run)
        if not definition or definition.get("proposal_revision") != AGENT_REVISION:
            return None
        return ProposalContract(
            "style", "writing style guide", (proposal_path(run.id, run.created_at),)
        )
    if run.executor != "codex.procedure":
        return None
    from tin_lite.reviewed_documents import document_spec

    spec = await document_spec(database, storage, run)
    if (
        spec is None
        or spec.documents is None
        or not spec.documents.agent_revision
        or spec.output_validator != BRAND_VALIDATOR
    ):
        return None
    primary = resolve_run_path(spec.output_path_template, run.id, run.created_at)
    companion = spec.documents.resolve(run.id, run.created_at).companion_path
    return ProposalContract("brand", "brand and design guide", (primary, companion))


def _row(row) -> dict[str, Any]:
    value = dict(row)
    if isinstance(value.get("files"), str):
        value["files"] = json.loads(value["files"])
    return value


async def revisions(database, run_id, *, conn=None) -> list[dict[str, Any]]:
    rows = await (conn or database.pool).fetch(
        "SELECT * FROM capture_proposal_revisions WHERE run_id=$1 ORDER BY number", run_id
    )
    return [_row(row) for row in rows]


async def latest_revision(database, run_id, *, conn=None) -> dict[str, Any] | None:
    row = await (conn or database.pool).fetchrow(
        "SELECT * FROM capture_proposal_revisions WHERE run_id=$1 ORDER BY number DESC LIMIT 1",
        run_id,
    )
    return _row(row) if row else None


def by_label(row) -> str:
    if label := CLIENT_LABELS.get(row.get("client") or ""):
        return label
    return "a coding agent" if row["source"] == "mcp" else "the Tin API"


def by_phrase(row, viewer) -> str:
    """Who revised it, as the viewer reads it: "Claude Code", "another member's Codex"."""
    mine = row["actor_clerk_user_id"] == viewer
    if label := CLIENT_LABELS.get(row.get("client") or ""):
        return label if mine else f"another member's {label}"
    if row["source"] == "mcp":
        return "your coding agent" if mine else "another member's coding agent"
    return "you, through the Tin API" if mine else "another member, through the Tin API"


def summary(rows, viewer) -> dict[str, Any]:
    """Who revised the proposal, how often and when, for Decisions and the agent."""
    if not rows:
        return {"count": 0}
    last = rows[-1]
    return {
        "count": len(rows),
        "latest_at": last["created_at"].isoformat(),
        "latest_by": by_phrase(last, viewer),
        "latest_by_you": last["actor_clerk_user_id"] == viewer,
        "history": [
            {
                "number": row["number"],
                "revision": row["revision"],
                "at": row["created_at"].isoformat(),
                "by": by_label(row),
                "note": row["note"],
                "files": row["files"],
            }
            for row in rows
        ],
    }


def revised_checkpoint(checkpoint, row, run):
    """The run's saved pair, rebound to the revision commit and the revised files' digests."""
    from tin_lite.publication import OutputCheckpoint

    files = {item["path"]: item for item in row["files"]}

    def rebound(item):
        saved = files[item.artifact_path]
        return replace(
            item,
            ephemeral_commit_sha=row["revision"],
            sha256=saved["sha256"],
            byte_count=saved["bytes"],
        )

    result = replace(rebound(checkpoint), companions=tuple(map(rebound, checkpoint.companions)))
    return OutputCheckpoint.load(result.to_dict(), run=run)


async def pending_owner(database, storage, project_id, paths) -> UUID | None:
    """The waiting revisable run whose proposal includes one of these paths, if any."""
    if not any(path.startswith(PROPOSAL_PREFIXES) for path in paths):
        return None
    rows = await database.pool.fetch(
        "SELECT id FROM workflow_runs WHERE project_id=$1 AND status='needs_input' "
        "AND review_required AND review_decision IS NULL "
        "AND executor IN ('codex.procedure', 'style.capture') "
        "AND (artifact_path LIKE 'brand/proposals/%' OR artifact_path LIKE 'style/proposals/%')",
        project_id,
    )
    for row in rows:
        run = await database.get_run(row["id"])
        proposal = await contract(database, storage, run)
        if proposal and set(proposal.paths) & set(paths):
            return run.id
    return None


async def validate(storage, project, run, proposal, contents) -> None:
    """The validators the capture used: the brand pair's contract, or the guide's shape."""
    try:
        if proposal.kind == "style":
            from tin_lite.style_capture import validate_guide

            validate_guide(contents[proposal.paths[0]])
            return
        from tin_lite import brand_contract
        from tin_lite.brand_capture import validate_pair

        primary, companion = proposal.paths
        validate_document(contents[primary], brand_contract.BRAND_MAX)
        validate_document(contents[companion], brand_contract.DESIGN_MAX)
        await validate_pair(storage, project, run, contents[primary], contents[companion])
    except (ValueError, UnicodeDecodeError) as exc:
        raise RevisionInvalid(f"The revised {proposal.label} is invalid: {exc}") from None


async def review_view(database, storage, run_id, actor) -> dict[str, Any]:
    run = await database.get_run(run_id)
    if run is not None and run.executor == STYLE_KEY:
        return await StyleProposalReview(database=database, storage=storage).view(run_id, actor)
    from tin_lite.reviewed_documents import ReviewedDocuments

    return await ReviewedDocuments(database=database, storage=storage).view(run_id, actor)


class CaptureRevisions:
    def __init__(self, *, database, storage):
        self.db, self.storage = database, storage

    def _result(self, run, row, proposal, *, replayed):
        return {
            "run_id": str(run.id),
            "project_id": str(run.project_id),
            "proposal": proposal.label,
            "revision_number": row["number"],
            "project_revision": row["revision"],
            "files": row["files"],
            "replayed": replayed,
        }

    async def _replay(self, project_id, request_id, request_digest, *, conn=None):
        row = await (conn or self.db.pool).fetchrow(
            "SELECT * FROM capture_proposal_revisions WHERE project_id=$1 AND request_id=$2",
            project_id,
            request_id,
        )
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise RevisionRefused("This request_id was already used for a different revision.")
        return _row(row)

    async def revise(
        self,
        *,
        run_id: UUID,
        actor: str,
        review_token: str,
        request_id: UUID,
        files: list[dict[str, Any]],
        note: str = "",
        source: str,
        client: str | None = None,
        oauth_client_id: str | None = None,
    ) -> dict[str, Any]:
        run = await self.db.get_run(run_id)
        if run is None or not await self.db.has_project_access(
            project_id=run.project_id, clerk_user_id=actor
        ):
            raise LookupError("Run not found.")
        proposal = await contract(self.db, self.storage, run)
        if proposal is None:
            raise RevisionRefused(
                "Only a brand.capture or style.capture proposal from version 1.2.0 on can be "
                "revised here. Start a new capture, or approve or discard this one in Decisions."
            )
        changes = self._changes(files, proposal)
        note = " ".join(str(note or "").split())
        if len(note) > MAX_NOTE_CHARS:
            raise RevisionRefused(f"Keep the note under {MAX_NOTE_CHARS} characters.")
        request_digest = digest(
            {
                "run": str(run.id),
                "token": review_token,
                "files": {path: hashlib.sha256(raw).hexdigest() for path, raw in changes.items()},
                "note": note,
            }
        )
        if saved := await self._replay(run.project_id, request_id, request_digest):
            return self._result(run, saved, proposal, replayed=True)
        project = await self.db.get_project(run.project_id)
        async with (
            self.db.pool.acquire() as conn,
            self.db.project_state_lock(conn, project.id),
            conn.transaction(),
        ):
            # Approval and Discard take these row locks too, so a revision never lands
            # between the founder's decision and the run reading the approved version.
            await conn.execute("SELECT id FROM projects WHERE id=$1 FOR UPDATE", project.id)
            await conn.execute("SELECT id FROM workflow_runs WHERE id=$1 FOR UPDATE", run.id)
            if saved := await self._replay(run.project_id, request_id, request_digest, conn=conn):
                return self._result(run, saved, proposal, replayed=True)
            run = await self.db.get_run(run.id, conn=conn)
            decided = await conn.fetchval(
                "SELECT true FROM workflow_review_commands WHERE source_run_id=$1", run.id
            )
            if (
                run.status != RunStatus.NEEDS_INPUT
                or run.review_decision is not None
                or decided
                or run.artifact_path != proposal.paths[0]
            ):
                raise RevisionRefused(
                    "This proposal is no longer waiting in Decisions, so it cannot be revised. "
                    "Start a new capture to propose another version."
                )
            view = await review_view(self.db, self.storage, run.id, actor)
            if review_token != view["review_token"]:
                raise ReviewConflict(
                    "The proposal changed since you read it. Call get_workflow_review again, "
                    "then revise the current version."
                )
            current = {
                path: await self.storage.read_canonical_artifact(
                    repo_id=project.state_repo_id, commit_sha=run.canonical_commit_sha, path=path
                )
                for path in proposal.paths
            }
            contents = {**current, **changes}
            if contents == current:
                raise RevisionRefused("The revision matches the current proposal; nothing changed.")
            await validate(self.storage, project, run, proposal, contents)
            repo = await self.storage.get_repo(project.state_repo_id)
            head = await self.storage.head_sha(repo, project.canonical_branch)
            at_head = {
                path: await self.storage.read_output_destination(
                    repo_id=project.state_repo_id, revision=head, path=path
                )
                for path in proposal.paths
            }
            stale = [p for p in proposal.paths if (at_head[p] or (None, None))[1] != contents[p]]
            number = len(await revisions(self.db, run.id, conn=conn)) + 1
            sha = head
            if stale:  # Otherwise a lost response already wrote exactly this version.
                try:
                    sha, _ = await self.storage.commit_project_changes(
                        repo_id=project.state_repo_id,
                        branch=project.canonical_branch,
                        expected_head_sha=head,
                        request_id=str(request_id),
                        message=f"Revise the proposed {proposal.label} ({number})",
                        changes=tuple(
                            ProjectFileMutation("upsert", path, contents[path]) for path in stale
                        ),
                    )
                except ProjectStateChangedError:
                    raise ReviewConflict(
                        "Project files changed while saving. Retry with the same request_id."
                    ) from None
            entries = [
                {
                    "path": path,
                    "sha256": hashlib.sha256(contents[path]).hexdigest(),
                    "bytes": len(contents[path]),
                }
                for path in proposal.paths
            ]
            row = await conn.fetchrow(
                """INSERT INTO capture_proposal_revisions
                   (id, project_id, run_id, number, request_id, request_digest,
                    actor_clerk_user_id, oauth_client_id, client, source, base_revision,
                    revision, files, note)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14)
                   RETURNING *""",
                uuid4(),
                project.id,
                run.id,
                number,
                request_id,
                request_digest,
                actor,
                oauth_client_id,
                client,
                source,
                run.canonical_commit_sha,
                sha,
                json.dumps(entries),
                note,
            )
            await conn.execute(
                """UPDATE workflow_runs SET canonical_commit_sha=$2, artifact_ref=$3,
                   progress_summary=$4, progress_updated_at=now() WHERE id=$1""",
                run.id,
                sha,
                f"code.storage://{project.state_repo_id}@{sha}/{run.artifact_path}",
                "Revised by your coding agent. Waiting for your review.",
            )
            await conn.execute(
                """UPDATE run_decisions SET items=(
                       SELECT COALESCE(jsonb_agg(CASE WHEN item->>'file' = ANY($2::text[])
                           THEN jsonb_set(item, '{revision}', to_jsonb($3::text))
                           ELSE item END), '[]'::jsonb)
                       FROM jsonb_array_elements(items) AS item)
                   WHERE run_id=$1 AND status='pending'""",
                run.id,
                list(proposal.paths),
                sha,
            )
            await self.db.add_activity(
                conn=conn,
                run_id=run.id,
                event_type="capture_proposal_revised",
                details={
                    "kind": "your_edits",
                    "number": number,
                    "revision": sha,
                    "by": by_label(row),
                    "actor_clerk_user_id": actor,
                },
                summary=(
                    f"{by_label(row)[:1].upper()}{by_label(row)[1:]} revised the proposed "
                    f"{proposal.label} (revision {number}). It still waits for your decision."
                ),
                audience="product",
                dedupe_key=f"{run.id}:proposal_revision:{number}",
            )
        return self._result(run, _row(row), proposal, replayed=False)

    @staticmethod
    def _changes(files, proposal) -> dict[str, bytes]:
        if not isinstance(files, list) or not 1 <= len(files) <= len(proposal.paths):
            raise RevisionRefused(
                f"Send 1 to {len(proposal.paths)} files: " + ", ".join(proposal.paths) + "."
            )
        changes: dict[str, bytes] = {}
        for item in files:
            path = item.get("path") if isinstance(item, dict) else None
            content = item.get("content") if isinstance(item, dict) else None
            if path not in proposal.paths:
                raise RevisionRefused(
                    f"{path} is not one of this run's proposal files. Revise only "
                    + ", ".join(proposal.paths)
                    + "."
                )
            if path in changes:
                raise RevisionRefused(f"{path} appears twice.")
            if not isinstance(content, str):
                raise RevisionRefused(f"Send the complete revised text of {path}.")
            changes[path] = content.encode("utf-8")
        return changes


class StyleProposalReview:
    """The writing guide's review: one proposal file, bound to its exact content on approval.

    Runs pinned to 1.1.0 read here too, so Decisions can show them, but their approval still
    signals the run directly and saves the proposal as it stands then.
    """

    def __init__(self, *, database, storage):
        self.db, self.storage = database, storage

    async def source(self, run_id, actor):
        run = await self.db.get_run(run_id)
        if not run or not await self.db.has_project_access(
            project_id=run.project_id, clerk_user_id=actor
        ):
            raise LookupError("Review not found.")
        if run.executor != STYLE_KEY or not run.review_required:
            raise ReviewConflict("This run does not propose a writing guide.")
        return run, await self.db.get_project(run.project_id)

    async def artifact(self, run, project, *, bound):
        proposal = await self.db.get_effect(f"{run.id}:style_proposal")
        if not proposal or proposal.status != "completed" or not proposal.result:
            raise ReviewConflict("The proposed guide is not ready for review.")
        path = proposal.result["artifact_path"]
        if bound:
            last = await latest_revision(self.db, run.id)
            revision = last["revision"] if last else proposal.result["canonical_commit_sha"]
            waiting = run.status == RunStatus.NEEDS_INPUT and run.review_decision is None
            if waiting and (run.artifact_path != path or run.canonical_commit_sha != revision):
                raise ReviewConflict("The proposed guide does not match its record.")
            raw = await self.storage.read_canonical_artifact(
                repo_id=project.state_repo_id, commit_sha=revision, path=path
            )
            if last and hashlib.sha256(raw).hexdigest() != last["files"][0]["sha256"]:
                raise ReviewConflict("The revised guide does not match its record.")
        else:
            # 1.1.0 approval saves the proposal as it stands, edits in Files included.
            repo = await self.storage.get_repo(project.state_repo_id)
            revision = await self.storage.head_sha(repo, project.canonical_branch)
            entry = await self.storage.read_output_destination(
                repo_id=project.state_repo_id, revision=revision, path=path
            )
            raw, last = (entry[1] if entry else b""), None
        artifact = {
            "run_id": str(run.id),
            "path": path,
            "revision": revision,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
        if last:
            artifact["proposal_revision"] = last["number"]
        return artifact, raw

    async def check_current(self, project, artifact):
        repo = await self.storage.get_repo(project.state_repo_id)
        head = await self.storage.head_sha(repo, project.canonical_branch)
        entry = await self.storage.read_output_destination(
            repo_id=project.state_repo_id, revision=head, path=artifact["path"]
        )
        if entry is None or hashlib.sha256(entry[1]).hexdigest() != artifact["sha256"]:
            raise ReviewConflict(EDITED)

    async def view(self, run_id, actor):
        from tin_lite.writing_style import STYLE_PATH

        run, project = await self.source(run_id, actor)
        bound = await contract(self.db, self.storage, run) is not None
        artifact, raw = await self.artifact(run, project, bound=bound)
        waiting = run.status == RunStatus.NEEDS_INPUT and run.review_decision is None
        conflict = None
        if waiting and bound:
            try:
                await self.check_current(project, artifact)
            except ReviewConflict as exc:
                conflict = str(exc)
        elif waiting and not raw:
            conflict = "The proposed guide was removed. Discard it and start capture again."
        repo = await self.storage.get_repo(project.state_repo_id)
        guide = await self.storage.read_output_destination(
            repo_id=project.state_repo_id,
            revision=await self.storage.head_sha(repo, project.canonical_branch),
            path=STYLE_PATH,
        )
        return {
            "run_id": str(run.id),
            "project_id": str(run.project_id),
            "root_run_id": str(run.id),
            "current_run_id": str(run.id),
            "version": run.review_version,
            "status": run.status.value,
            "artifact": artifact,
            "review_token": digest({"run": str(run.id), "artifact": artifact}),
            "can_request_changes": False,
            "can_approve": waiting and not conflict,
            "is_current": True,
            "versions": [],
            "feedback": None,
            "change_summary": None,
            "documents": [
                {
                    "path": artifact["path"],
                    "sha256": artifact["sha256"],
                    "destination": STYLE_PATH,
                    "change": "new"
                    if guide is None
                    else "unchanged"
                    if guide[1] == raw
                    else "updated",
                }
            ],
            "conflict": conflict,
            "review_url": f"/document/{run.id}?project={run.project_id}&return=decisions",
            **(
                {"proposal_revisions": summary(await revisions(self.db, run.id), actor)}
                if bound
                else {}
            ),
        }

    async def approve(self, *, run_id, actor, token):
        run, project = await self.source(run_id, actor)
        if await contract(self.db, self.storage, run) is None:
            raise ReviewConflict("This guide's approval does not use a review token.")
        existing = await self.db.pool.fetchrow(
            "SELECT * FROM workflow_review_commands WHERE source_run_id=$1", run.id
        )
        if existing:
            if existing["action"] == "approve" and token == existing["review_token"]:
                return run
            raise ReviewConflict("This proposal already has a review decision.")
        artifact, _ = await self.artifact(run, project, bound=True)
        expected = digest({"run": str(run.id), "artifact": artifact})
        if token is None:
            raise ReviewConflict("Read the proposed guide before approving it.")
        if token != expected:
            raise ReviewConflict(STALE)
        await self.check_current(project, artifact)
        command = {
            "id": uuid4(),
            "project_id": run.project_id,
            "request_id": uuid5(NAMESPACE_URL, f"tin:style-proposal:{run.id}:{expected}"),
            "actor_clerk_user_id": actor,
            "source_run_id": run.id,
            "root_run_id": run.id,
            "artifact_run_id": run.id,
            "coordinator_run_id": run.id,
            "action": "approve",
            "request_digest": digest({"approve": str(run.id), "token": expected}),
            "review_token": expected,
            "artifact": artifact,
        }
        await accept_approval(self.db, command)
        return await self.db.get_run(run.id)


async def approved_style_artifact(database, run_id) -> dict[str, Any] | None:
    """The exact proposal an accepted approval bound, or None for a 1.1.0-style approval."""
    row = await database.pool.fetchrow(
        "SELECT * FROM workflow_review_commands WHERE source_run_id=$1 AND action='approve'",
        run_id,
    )
    return unpack(row)["artifact"] if row else None
