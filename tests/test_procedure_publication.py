from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from temporalio.exceptions import ApplicationError

from tin_lite.activities import TinActivities
from tin_lite.api import RunView, router
from tin_lite.auth import AuthContext, require_user
from tin_lite.code_storage import CodeStorage
from tin_lite.db import Database, apply_migrations
from tin_lite.domain import RunStatus, SideEffectConflictError, StaleGenerationError, WorkflowRun
from tin_lite.mcp_server import create_mcp_app
from tin_lite.procedures import (
    PinnedCodexProcedure,
    SandboxProfile,
    validate_codex_procedure_definition,
)
from tin_lite.publication import (
    OutputCheckpoint,
    OutputConflictError,
    PublicationPendingError,
    read_run_output,
)
from tin_lite.workflow_creator import creator_files
from tin_lite.workflow_packages import decode_workflow_source

PATH = "reports/RESEARCH.md"
CONTENT = b"# Finished research\n\nThe validated result.\n"


def run_fixture(base: str = "a" * 40) -> WorkflowRun:
    run_id = uuid4()
    return WorkflowRun(
        id=run_id,
        project_id=uuid4(),
        workflow_id=uuid4(),
        executor="codex.procedure",
        definition_commit_sha="d" * 40,
        temporal_workflow_id=f"procedure:{run_id}",
        thread_id=str(run_id),
        generation=1,
        fencing_token=1,
        status=RunStatus.RUNNING,
        lease_active=True,
        lease_owner="owner",
        sandbox_id="sandbox",
        expected_head_sha=base,
        ephemeral_branch=f"procedures/{run_id}/1",
    )


class HistoryRepo:
    """A changing remote, including successful writes whose response is lost."""

    id = "projects/publication-test"

    def __init__(self):
        self.head = "a" * 40
        self.commits = {self.head: {"sha": self.head, "parent_shas": [], "message": "base"}}
        self.trees = {self.head: {"README.md": ("100644", b"Project\n")}}
        self.writes = 0
        self.lose_response = False
        self.before_send = None

    def edit(self, changes: dict, message="Other edit", parent=None):
        parent = parent or self.head
        sha = hashlib.sha1(
            f"commit-{len(self.commits)}".encode(), usedforsecurity=False
        ).hexdigest()
        tree = dict(self.trees[parent])
        for path, value in changes.items():
            if value is None:
                tree.pop(path, None)
            else:
                tree[path] = value if isinstance(value, tuple) else ("100644", value)
        self.commits[sha] = {"sha": sha, "parent_shas": [parent], "message": message}
        self.trees[sha] = tree
        self.head = sha
        return sha

    def create_commit(self, **options):
        repo = self
        files = {}

        class Builder:
            def add_file(self, path, content, *, mode=None):
                files[path] = (mode.value, content) if mode is not None else content
                return self

            async def send(self):
                if repo.before_send:
                    callback, repo.before_send = repo.before_send, None
                    callback()
                if repo.head != options["expected_head_sha"]:
                    raise RuntimeError("CAS rejected")
                sha = repo.edit(files, options["commit_message"])
                repo.writes += 1
                if repo.lose_response:
                    repo.lose_response = False
                    raise httpx.ReadError("lost successful response")
                return {"commit_sha": sha}

        return Builder()

    async def list_files(self, *, ref, ttl=None):
        return {"paths": sorted(self.trees[ref])}

    async def get_commit_diff(self, *, sha, **kwargs):
        parent = self.commits[sha]["parent_shas"][0]
        old, new = self.trees[parent], self.trees[sha]
        return {
            "files": [
                {"path": path, "old_path": None}
                for path in old.keys() | new.keys()
                if old.get(path) != new.get(path)
            ],
            "filtered_files": [],
        }

    async def get_file_stream(self, *, ref, path, **kwargs):
        content = self.trees[ref][path][1]

        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def aread(self):
                return content

        return Response()


class HistoryStorage(CodeStorage):
    def __init__(self):
        self.repo = HistoryRepo()
        self.page_size = 2
        self.incomplete_history = False
        self.bad_metadata = False
        self.branch_revision = None
        self.reads = []

    async def get_repo(self, repo_id):
        return self.repo

    async def head_sha(self, repo, branch):
        return repo.head

    async def _file_equals(self, repo, *, ref, path, expected):
        # code.storage answers 404 for a path the revision lacks; the real method maps it to False.
        return path in repo.trees[ref] and repo.trees[ref][path][1] == expected

    async def _publication_json(self, repo, endpoint, **params):
        ref = params["ref"]
        if endpoint == "commits":
            if params.get("ephemeral"):
                return {
                    "commits": [repo.commits[self.branch_revision]] if self.branch_revision else []
                }
            entries = []
            while ref:
                commit = repo.commits[ref]
                entries.append(commit)
                ref = (commit["parent_shas"] or [None])[0]
            start = int(params.get("cursor", "0"))
            end = start + self.page_size
            return {
                "commits": entries[start:end],
                "next_cursor": str(end),
                "has_more": end < len(entries) and not self.incomplete_history,
            }
        assert endpoint == "files/metadata"
        self.reads.append((ref, params["path"]))
        files = [
            {
                "path": path,
                "mode": mode,
                "type": "blob" if mode in {"100644", "100755"} else "symlink",
                "size": len(content),
            }
            for path, (mode, content) in sorted(repo.trees[ref].items())
            if path == params["path"] or path.startswith(params["path"] + "/")
        ]
        return {
            "ref": "bad" if self.bad_metadata else ref,
            "files": files[:2],
            "has_more": len(files) > 2,
        }


def saved_checkpoint(storage, run, *, path=PATH, content=CONTENT, media_type="text/markdown"):
    head = storage.repo.head
    revision = storage.repo.edit({path: content}, parent=run.expected_head_sha)
    storage.branch_revision = revision
    storage.repo.head = head
    return OutputCheckpoint.create(
        run=run, revision=revision, path=path, media_type=media_type, content=content
    )


async def publish(storage, checkpoint, state, validate=None, *, content=CONTENT):
    async def save_intent(value):
        state["intent"] = value

    async def valid():
        state["validations"] = state.get("validations", 0) + 1

    return await storage.publish_procedure_output(
        repo_id=storage.repo.id,
        branch="main",
        checkpoint=checkpoint,
        content=content,
        execution_key=f"{checkpoint.run_id}:procedure_canonical_commit",
        workflow_key="research.deep_dive",
        intent=state.get("intent"),
        legacy_attempt=False,
        save_intent=save_intent,
        validate_lease=validate or valid,
    )


@pytest.mark.asyncio
async def test_creator_json_checkpoint_publishes_once_and_remains_readable():
    path = "workflow_packages/custom.workflow_create/workflow.json"
    definition = decode_workflow_source(
        creator_files()[path].encode(), definition_path=path
    ).definition
    spec = validate_codex_procedure_definition(definition)
    content = b'{"format":"tin-workflow-candidate-v1","limitations":[]}\n'
    storage = HistoryStorage()
    run = run_fixture()
    checkpoint = saved_checkpoint(
        storage, run, path=spec.output_path, content=content, media_type=spec.output_media_type
    )
    restored = OutputCheckpoint.load(checkpoint.to_dict(), run=run)
    restored.validate_content(content)
    with pytest.raises(ValueError, match="no longer matches"):
        restored.validate_content(content + b" ")
    with pytest.raises(ValueError, match="validated checkpoint"):
        OutputCheckpoint.load(checkpoint.to_dict(), run=replace(run, generation=2))
    storage.repo.edit({"README.md": b"Later project edit\n"})
    state = {}
    revision, changed = await publish(storage, restored, state, content=content)
    assert changed and storage.repo.writes == 1
    assert await publish(storage, restored, state, content=content) == (revision, True)
    assert storage.repo.writes == 1
    output = await read_run_output(
        storage=storage,
        run=replace(run, artifact_path=spec.output_path, canonical_commit_sha=revision),
        repo_id=storage.repo.id,
    )
    assert output.path == "reports/WORKFLOW_CANDIDATE.json" and output.content == content
    assert storage.repo.trees[revision]["README.md"][1] == b"Later project edit\n"


@pytest.mark.asyncio
async def test_unrelated_edit_survives_publication_and_duplicate():
    storage = HistoryStorage()
    checkpoint = saved_checkpoint(storage, run_fixture())
    storage.repo.edit({"README.md": b"Founder edit\n"})
    state = {}
    sha, changed = await publish(storage, checkpoint, state)
    assert changed and storage.repo.trees[sha]["README.md"][1] == b"Founder edit\n"
    assert storage.repo.trees[sha][PATH][1] == CONTENT
    storage.repo.edit({"later.md": b"Another change\n"})
    assert await publish(storage, checkpoint, state) == (sha, True)
    assert storage.repo.writes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [b"Founder report\n", None, ("120000", b"elsewhere"), ("100755", b"Original\n")]
)
async def test_output_changes_fail_closed(change):
    storage = HistoryStorage()
    base = storage.repo.edit({PATH: b"Original\n"})
    checkpoint = saved_checkpoint(storage, run_fixture(base))
    edited = storage.repo.edit({PATH: change})
    with pytest.raises(OutputConflictError):
        await publish(storage, checkpoint, {})
    assert storage.repo.head == edited and storage.repo.writes == 0
    assert (
        await storage.read_procedure_checkpoint(
            repo_id=storage.repo.id, revision=checkpoint.ephemeral_commit_sha, path=PATH
        )
    ) == CONTENT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes", [{"reports": ("120000", b"elsewhere")}, {PATH + "/nested.md": b"Nested"}]
)
async def test_parent_symlink_and_destination_directory_are_not_missing_files(changes):
    storage = HistoryStorage()
    checkpoint = saved_checkpoint(storage, run_fixture())
    storage.repo.edit(changes)
    with pytest.raises(OutputConflictError):
        await publish(storage, checkpoint, {})
    assert storage.repo.writes == 0


@pytest.mark.asyncio
async def test_lost_response_and_later_output_edit_recover_original_across_pages():
    storage = HistoryStorage()
    checkpoint = saved_checkpoint(storage, run_fixture())
    storage.repo.edit({"other.md": b"First edit"})
    storage.repo.lose_response = True
    state = {}
    with pytest.raises(PublicationPendingError):
        await publish(storage, checkpoint, state)
    original = storage.repo.head
    for index in range(6):
        storage.repo.edit({PATH: f"Newer report {index}".encode()})
    latest = storage.repo.head
    assert await publish(storage, checkpoint, state) == (original, True)
    assert storage.repo.writes == 1 and storage.repo.head == latest
    assert storage.repo.trees[latest][PATH][1] == b"Newer report 5"


@pytest.mark.asyncio
async def test_incomplete_history_never_authorizes_a_second_write():
    storage = HistoryStorage()
    checkpoint = saved_checkpoint(storage, run_fixture())
    storage.repo.lose_response = True
    state = {}
    with pytest.raises(PublicationPendingError):
        await publish(storage, checkpoint, state)
    for index in range(5):
        storage.repo.edit({f"other-{index}.md": b"Later"})
    storage.incomplete_history = True
    with pytest.raises(PublicationPendingError, match="incomplete"):
        await publish(storage, checkpoint, state)
    assert storage.repo.writes == 1


@pytest.mark.asyncio
async def test_cas_race_rechecks_path_instead_of_overwriting():
    storage = HistoryStorage()
    checkpoint = saved_checkpoint(storage, run_fixture())
    storage.repo.before_send = lambda: storage.repo.edit({PATH: b"Racing edit"})
    state = {}
    with pytest.raises(PublicationPendingError):
        await publish(storage, checkpoint, state)
    with pytest.raises(OutputConflictError):
        await publish(storage, checkpoint, state)
    assert storage.repo.writes == 0


@pytest.mark.asyncio
async def test_stale_generation_cannot_write_but_can_reconcile_an_earlier_effect():
    storage = HistoryStorage()
    checkpoint = saved_checkpoint(storage, run_fixture())

    async def stale():
        raise StaleGenerationError("revoked")

    with pytest.raises(StaleGenerationError):
        await publish(storage, checkpoint, {}, stale)
    assert storage.repo.writes == 0
    state = {}
    sha, _ = await publish(storage, checkpoint, state)
    assert await publish(storage, checkpoint, state, stale) == (sha, True)


@pytest.mark.asyncio
async def test_no_change_result_survives_later_edits_without_fictitious_commit():
    storage = HistoryStorage()
    base = storage.repo.edit({PATH: CONTENT})
    checkpoint = saved_checkpoint(storage, run_fixture(base))
    state = {}
    assert await publish(storage, checkpoint, state) == (base, False)
    storage.repo.edit({PATH: b"Later founder edit"})
    assert await publish(storage, checkpoint, state) == (base, False)
    assert storage.repo.writes == 0


@pytest.mark.parametrize("changed_after_checkpoint", [False, True])
async def test_code_checkpoint_reuses_unchanged_file_without_bypassing_publication(
    changed_after_checkpoint,
):
    storage = HistoryStorage()
    base = storage.repo.edit({PATH: CONTENT})
    run = replace(run_fixture(base), executor="workflow.code")
    revision = await storage.stage_native_output(
        repo_id=storage.repo.id,
        branch="main",
        run_id=str(run.id),
        generation=run.generation,
        path=PATH,
        content=CONTENT,
        executor="workflow.code",
    )
    assert revision == base and storage.repo.writes == 0
    checkpoint = OutputCheckpoint.create(
        run=run, revision=revision, path=PATH, media_type="text/markdown", content=CONTENT
    )
    state = {}
    if changed_after_checkpoint:
        storage.repo.edit({PATH: b"Later founder edit"})
        with pytest.raises(OutputConflictError):
            await publish(storage, checkpoint, state)
    else:
        assert await publish(storage, checkpoint, state) == (base, False)
        assert state["validations"] == 1 and state["intent"]["no_change"] is True
    assert storage.repo.writes == 0


@pytest.mark.asyncio
async def test_invalid_metadata_or_checkpoint_never_causes_publication():
    storage = HistoryStorage()
    checkpoint = saved_checkpoint(storage, run_fixture())
    storage.bad_metadata = True
    with pytest.raises(PublicationPendingError):
        await publish(storage, checkpoint, {})
    storage.bad_metadata = False
    with pytest.raises(ValueError, match="validated checkpoint"):
        await publish(storage, replace(checkpoint, sha256="0" * 64), {})
    assert storage.repo.writes == 0


@pytest.fixture
async def publication_db():
    """Real migrations/SQL in a disposable schema, never the configured product DB."""
    dsn = os.environ.get("TIN_LITE_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("set TIN_LITE_TEST_DATABASE_DSN for isolated Postgres contract tests")
    schema = "publication_test_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    database = Database(dsn)
    try:
        database._pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=10, server_settings={"search_path": schema}
        )
        scoped_dsn = dsn + ("&" if "?" in dsn else "?") + f"search_path={schema}"
        migrations = Path(__file__).parents[1] / "migrations"
        assert "024_retained_procedure_output.sql" in await apply_migrations(scoped_dsn, migrations)
        assert await apply_migrations(scoped_dsn, migrations) == []
        yield database
    finally:
        await database.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def activity_fixture(database, *, review=False, isolated=False, created=False):
    storage = HistoryStorage()
    run = replace(run_fixture(), review_required=review)
    project = await database.create_project(name="Publication proof", state_repo_id=storage.repo.id)
    run = replace(run, project_id=project.id)
    await database.pool.execute(
        """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
                current_commit_sha, version_label, definition)
           VALUES ($1, 'research.deep_dive', 'Research', 'codex.procedure', 'registry/workflows',
                   'research.json', $2, '1', '{}')""",
        run.workflow_id,
        run.definition_commit_sha,
    )
    await database.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
            temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
            lease_owner, sandbox_id, expected_head_sha, ephemeral_branch, review_required)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
        run.id,
        run.project_id,
        run.workflow_id,
        run.executor,
        run.definition_commit_sha,
        run.temporal_workflow_id,
        run.thread_id,
        run.generation,
        run.fencing_token,
        run.status.value,
        run.lease_active,
        run.lease_owner,
        run.sandbox_id,
        run.expected_head_sha,
        run.ephemeral_branch,
        run.review_required,
    )
    checkpoint = saved_checkpoint(storage, run)
    spec = PinnedCodexProcedure(
        workflow_key="research.deep_dive",
        prompt="",
        entry_skill="research",
        skill_files={},
        output_path=PATH,
        output_media_type="text/markdown",
        sandbox=SandboxProfile(profile="isolated" if isolated else "default"),
    )
    if isolated and created:
        creation_key = f"{run.id}:procedure_sandbox_create"
        async with database.effect_lock(creation_key, "procedure_sandbox_create") as (conn, _):
            await database.start_effect(
                conn, execution_key=creation_key, operation="procedure_sandbox_create"
            )
            await database.complete_effect(
                conn,
                execution_key=creation_key,
                result={"sandbox_profile": "isolated", "codex_auth": {"mode": "chatgpt_oauth"}},
            )

    class Activities(TinActivities):
        async def _pinned_codex_procedure(self, requested_id):
            assert requested_id == run.id
            return SimpleNamespace(
                key="research.deep_dive", title="Research", project_id=None
            ), spec

    class Sandboxes:
        calls = 0

        async def kill(self, sandbox_id):
            self.calls += 1

        async def create(self, **kwargs):
            raise AssertionError("publication recovery must not create a sandbox")

    activities = Activities(
        database=database, storage=storage, sandboxes=Sandboxes(), settings=SimpleNamespace()
    )
    key = f"{run.id}:procedure_artifact_persist"
    async with database.effect_lock(key, "procedure_artifact_persist") as (conn, _):
        await database.start_effect(conn, execution_key=key, operation="procedure_artifact_persist")
        await database.complete_procedure_persist(
            conn,
            execution_key=key,
            run_id=run.id,
            result={
                "checkpoint": checkpoint.to_dict(),
                "summary": "Research is ready.",
                "sandbox_killed": True,
            },
            sandbox_killed=True,
        )
    return activities, storage, run, checkpoint


@pytest.mark.asyncio
async def test_postgres_completion_is_monotonic_and_rejects_incompatible_results(publication_db):
    db = publication_db
    key = "monotonic:test"
    async with db.effect_lock(key, "test") as (conn, _):
        await db.start_effect(conn, execution_key=key, operation="test")
        await db.complete_effect(conn, execution_key=key, result={"sha": "original"})
        await db.fail_effect(conn, execution_key=key, error_message="later cleanup failure")
        await db.complete_effect(conn, execution_key=key, result={"sha": "original"})
        with pytest.raises(SideEffectConflictError):
            await db.complete_effect(conn, execution_key=key, result={"sha": "different"})
    result = await db.get_effect(key)
    assert result.status == "completed" and result.result == {"sha": "original"}


@pytest.mark.asyncio
async def test_real_activity_concurrent_duplicates_have_one_effect_and_projection(publication_db):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)
    storage.repo.edit({"README.md": b"Founder edit"})
    await asyncio.gather(
        *(activities.commit_codex_procedure_artifact(str(run.id)) for _ in range(3))
    )
    await asyncio.gather(
        *(activities.project_codex_procedure_result(str(run.id)) for _ in range(3))
    )
    projected = await publication_db.get_run(run.id)
    assert storage.repo.writes == 1 and projected.status == RunStatus.SUCCEEDED
    assert projected.retained_output is None and not projected.lease_active
    assert storage.repo.trees[projected.canonical_commit_sha]["README.md"][1] == b"Founder edit"
    assert (
        await publication_db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='codex_procedure_ready'",
            run.id,
        )
        == 1
    )
    assert (
        await publication_db.pool.fetchval(
            "SELECT count(*) FROM effect_receipts WHERE execution_key=$1 AND status='completed'",
            f"{run.id}:procedure_projection",
        )
        == 1
    )
    # Simulate an older completed receipt whose event was never recorded.
    await publication_db.pool.execute(
        "DELETE FROM activity_events WHERE run_id=$1 AND event_type='codex_procedure_ready'",
        run.id,
    )
    await activities.project_codex_procedure_result(str(run.id))
    assert (
        await publication_db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='codex_procedure_ready'",
            run.id,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_real_commit_then_db_failure_and_later_edit_reuses_saved_work(
    publication_db, monkeypatch
):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)
    original_complete = publication_db.complete_procedure_publication

    async def fail(*args, **kwargs):
        raise ConnectionError("database unavailable after remote save")

    monkeypatch.setattr(publication_db, "complete_procedure_publication", fail)
    with pytest.raises(ConnectionError):
        await activities.commit_codex_procedure_artifact(str(run.id))
    committed = storage.repo.head
    storage.repo.edit({PATH: b"New founder version"})
    monkeypatch.setattr(publication_db, "complete_procedure_publication", original_complete)
    await activities.commit_codex_procedure_artifact(str(run.id))
    projected = await publication_db.get_run(run.id)
    assert projected.canonical_commit_sha == committed and storage.repo.writes == 1
    assert storage.repo.trees[storage.repo.head][PATH][1] == b"New founder version"


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_receipt_and_retries_cleanup(publication_db, monkeypatch):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)
    original_release = publication_db.release_lease

    async def fail(run_id):
        raise ConnectionError("cleanup unavailable")

    monkeypatch.setattr(publication_db, "release_lease", fail)
    with pytest.raises(ConnectionError):
        await activities.commit_codex_procedure_artifact(str(run.id))
    receipt = await publication_db.get_effect(f"{run.id}:procedure_canonical_commit")
    assert receipt.status == "completed"
    assert (await publication_db.get_run(run.id)).canonical_commit_sha is not None
    monkeypatch.setattr(publication_db, "release_lease", original_release)
    await activities.commit_codex_procedure_artifact(str(run.id))
    assert storage.repo.writes == 1 and not (await publication_db.get_run(run.id)).lease_active


@pytest.mark.asyncio
async def test_projection_is_atomic_and_still_requires_review(publication_db, monkeypatch):
    activities, storage, run, checkpoint = await activity_fixture(publication_db, review=True)
    saved = await publication_db.get_run(run.id)
    assert saved.progress_step == "save" and saved.progress_current == 2
    await activities.commit_codex_procedure_artifact(str(run.id))
    assert (await publication_db.get_run(run.id)).progress_step == "finalize"
    assert await activities.request_codex_procedure_review(str(run.id))
    waiting = await publication_db.get_run(run.id)
    assert waiting.progress_step == "review" and waiting.progress_current == 3
    assert waiting.progress_summary == "Result saved. Waiting for your review."
    # Old activity deliveries must not regress a later review projection.
    await activities.persist_codex_procedure_artifact(str(run.id))
    await activities.commit_codex_procedure_artifact(str(run.id))
    assert (await publication_db.get_run(run.id)).progress_step == "review"
    with pytest.raises(RuntimeError, match="review"):
        await activities.project_codex_procedure_result(str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.NEEDS_INPUT
    await activities.record_codex_procedure_approval(str(run.id))
    await activities.request_codex_procedure_review(str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.RUNNING
    assert (await publication_db.get_run(run.id)).progress_step == "finalize"
    approved_status = (await publication_db.get_run(run.id)).status
    original_add = publication_db.add_activity

    async def fail(**kwargs):
        raise ConnectionError("product event failed")

    monkeypatch.setattr(publication_db, "add_activity", fail)
    with pytest.raises(ConnectionError):
        await activities.project_codex_procedure_result(str(run.id))
    assert (await publication_db.get_run(run.id)).status == approved_status
    receipt = await publication_db.get_effect(f"{run.id}:procedure_projection")
    assert receipt.status != "completed"
    monkeypatch.setattr(publication_db, "add_activity", original_add)
    await activities.project_codex_procedure_result(str(run.id))
    assert (await publication_db.get_run(run.id)).status == RunStatus.SUCCEEDED
    await activities.persist_codex_procedure_artifact(str(run.id))
    await activities.request_codex_procedure_review(str(run.id))
    done = await publication_db.get_run(run.id)
    assert done.progress_step == "complete" and done.progress_current == done.progress_total == 3


@pytest.mark.asyncio
async def test_conflict_retains_readable_output_without_new_review_gate(publication_db):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)
    edited = storage.repo.edit({PATH: b"Founder version"})
    with pytest.raises(ApplicationError) as failure:
        await activities.commit_codex_procedure_artifact(str(run.id))
    assert failure.value.non_retryable and failure.value.type == "OutputConflictError"
    await activities.project_codex_procedure_failure(
        {"run_id": str(run.id), "reason": "workflow failed"}
    )
    projected = await publication_db.get_run(run.id)
    assert projected.status == RunStatus.FAILED and projected.canonical_commit_sha is None
    assert projected.retained_output["reason"] == "output_conflict"
    assert not projected.lease_active and storage.repo.head == edited and storage.repo.writes == 0
    assert (
        await publication_db.pool.fetchval(
            "SELECT count(*) FROM run_decisions WHERE run_id=$1", run.id
        )
        == 0
    )
    output = await read_run_output(
        storage=storage, run=projected, repo_id=storage.repo.id, source="retained"
    )
    assert output.content == CONTENT
    view = RunView.model_validate(projected).model_dump(mode="json")
    assert view["retained_output"]["revision"] == checkpoint.ephemeral_commit_sha
    assert "ephemeral_branch" not in json.dumps(view)
    await activities.persist_codex_procedure_artifact(str(run.id))
    assert (await publication_db.get_run(run.id)).retained_output["reason"] == "output_conflict"


@pytest.mark.asyncio
async def test_publication_event_failure_rolls_back_receipt_and_availability(
    publication_db, monkeypatch
):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)
    original_add = publication_db.add_activity

    async def fail(**kwargs):
        raise ConnectionError("event unavailable")

    monkeypatch.setattr(publication_db, "add_activity", fail)
    with pytest.raises(ConnectionError):
        await activities.commit_codex_procedure_artifact(str(run.id))
    key = f"{run.id}:procedure_canonical_commit"
    assert (await publication_db.get_effect(key)).status == "failed"
    assert (await publication_db.get_run(run.id)).canonical_commit_sha is None
    monkeypatch.setattr(publication_db, "add_activity", original_add)
    await activities.commit_codex_procedure_artifact(str(run.id))
    assert storage.repo.writes == 1


@pytest.mark.asyncio
async def test_completed_publication_repairs_availability_without_reviving_failed_run(
    publication_db,
):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)
    await activities.commit_codex_procedure_artifact(str(run.id))
    await publication_db.pool.execute(
        "UPDATE workflow_runs SET status='failed', canonical_commit_sha=NULL, "
        "artifact_path=NULL, artifact_ref=NULL WHERE id=$1",
        run.id,
    )
    await activities.commit_codex_procedure_artifact(str(run.id))
    projected = await publication_db.get_run(run.id)
    assert projected.status == RunStatus.FAILED and projected.canonical_commit_sha is not None
    assert (
        await read_run_output(storage=storage, run=projected, repo_id=storage.repo.id)
    ).content == CONTENT
    with pytest.raises(RuntimeError, match="terminal failure"):
        await activities.project_codex_procedure_result(str(run.id))
    assert storage.repo.writes == 1


@pytest.mark.asyncio
async def test_legacy_persist_is_pinned_without_rewriting_completed_receipt(publication_db):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)
    key = f"{run.id}:procedure_artifact_persist"
    legacy = {"ephemeral_branch": run.ephemeral_branch, "summary": "Legacy saved result"}
    await publication_db.pool.execute(
        "UPDATE effect_receipts SET result=$2::jsonb WHERE execution_key=$1",
        key,
        json.dumps(legacy),
    )
    await activities.commit_codex_procedure_artifact(str(run.id))
    assert (await publication_db.get_effect(key)).result == legacy
    canonical = await publication_db.get_effect(f"{run.id}:procedure_canonical_commit")
    assert canonical.result["checkpoint"] == checkpoint.to_dict()
    assert storage.repo.writes == 1


@pytest.mark.asyncio
async def test_recovered_checkpoint_is_available_even_when_sandbox_cleanup_fails(
    publication_db, monkeypatch
):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)
    key = f"{run.id}:procedure_artifact_persist"
    await publication_db.pool.execute("DELETE FROM effect_receipts WHERE execution_key=$1", key)
    await publication_db.pool.execute(
        "UPDATE workflow_runs SET retained_output=NULL WHERE id=$1", run.id
    )
    original_kill = activities._sandboxes.kill

    async def fail(sandbox_id):
        raise ConnectionError("cleanup unavailable")

    monkeypatch.setattr(activities._sandboxes, "kill", fail)
    with pytest.raises(ConnectionError):
        await activities.persist_codex_procedure_artifact(str(run.id))
    assert (await publication_db.get_effect(key)).status == "completed"
    assert (await publication_db.get_run(run.id)).retained_output is not None
    monkeypatch.setattr(activities._sandboxes, "kill", original_kill)
    await activities.persist_codex_procedure_artifact(str(run.id))
    assert activities._sandboxes.calls == 1


def test_saved_output_ui_routes_refresh_download_and_review_boundaries():
    subprocess.run(  # noqa: S603
        ["node", str(Path(__file__).parent / "js" / "procedure-output.cjs")],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_checkpoint_branch_advance_cannot_change_the_published_result(publication_db):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)
    head = storage.repo.head
    storage.branch_revision = storage.repo.edit(
        {PATH: b"Unvalidated branch update"}, parent=checkpoint.ephemeral_commit_sha
    )
    storage.repo.head = head
    await activities.commit_codex_procedure_artifact(str(run.id))
    assert storage.repo.trees[storage.repo.head][PATH][1] == CONTENT


@pytest.mark.asyncio
async def test_retained_http_reads_are_project_bound_and_do_not_query_temporal(publication_db):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)
    await publication_db.pool.execute(
        "INSERT INTO tin_users (clerk_user_id) VALUES ('user_member')"
    )
    await publication_db.pool.execute(
        "INSERT INTO project_memberships (project_id, clerk_user_id) VALUES ($1, 'user_member')",
        run.project_id,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(database=publication_db, storage=storage)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_member",
        token_type="session_token",  # noqa: S106
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = f"/api/workflows/runs/{run.id}/artifact"
        raw = await client.get(url + "?source=retained")
        document = await client.get(url + "/document?source=retained")
        assert raw.status_code == 200 and raw.content == CONTENT
        assert raw.headers["X-Tin-Output-Revision"] == checkpoint.ephemeral_commit_sha
        assert document.json()["source_url"] == url + "?source=retained"
        assert (await client.get(url)).status_code == 409
        storage.reads.clear()
        app.dependency_overrides[require_user] = lambda: AuthContext(
            clerk_user_id="user_outsider",
            token_type="session_token",  # noqa: S106
        )
        assert (await client.get(url + "?source=retained")).status_code == 404
        assert not storage.reads


@pytest.mark.asyncio
async def test_mcp_saved_output_reader_authorizes_before_storage(publication_db, monkeypatch):
    activities, storage, run, checkpoint = await activity_fixture(publication_db)
    token = SimpleNamespace(subject="user_member", scopes=["openid"], client_id="test_client")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    await publication_db.record_tin_user("user_member")
    await publication_db.pool.execute(
        "INSERT INTO project_memberships (project_id, clerk_user_id) VALUES ($1, 'user_member')",
        run.project_id,
    )
    server, _ = create_mcp_app(
        settings=SimpleNamespace(
            switchboard_public_url="https://tin.test",
            clerk_frontend_api_url="https://clerk.tin.test",
        ),
        auth=SimpleNamespace(),
        runtime=lambda: SimpleNamespace(database=publication_db, storage=storage),
    )
    result = await server.call_tool(
        "read_run_output", {"run_id": str(run.id), "source": "retained"}
    )
    assert "Finished research" in str(result)
    storage.reads.clear()
    token.subject = "user_outsider"
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    with pytest.raises(ToolError, match="not_found: run not found") as failure:
        await server.call_tool("read_run_output", {"run_id": str(run.id), "source": "retained"})
    assert not isinstance(failure.value, UnexpectedToolError)
    assert not storage.reads
