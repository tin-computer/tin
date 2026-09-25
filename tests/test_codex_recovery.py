"""Interrupted paid procedures recover files, never buy another attempt."""

import asyncio
import base64
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from temporalio.exceptions import ApplicationError
from test_billing import billed as billed
from test_codex_api import GRANT
from test_codex_api_billing import paid_relay
from test_procedure_publication import CONTENT, PATH, HistoryRepo, HistoryStorage, activity_fixture
from test_procedure_publication import publication_db as publication_db
from test_product_deep_dive import BASE_INDEX, CODE_MAP, CODE_SECTION, _index, _spec
from test_rollouts import base_values

from tin_lite.codex_api import (
    ATTEMPT,
    PROCEDURE_CONTRACT,
    SESSION_CONTRACT,
    CodexAttemptStopped,
    attempt_failure,
    attempt_key,
    run_api_attempt,
)
from tin_lite.domain import SideEffectConflictError, StaleGenerationError
from tin_lite.e2b_runtime import (
    _INTERRUPTED_OUTPUT_READER,
    E2BRuntime,
    SandboxProcedureInput,
    SandboxProcedureResult,
)
from tin_lite.interrupted_procedure import retain
from tin_lite.publication import PublicationPendingError, read_run_output


@pytest.mark.parametrize("contract", [PROCEDURE_CONTRACT, SESSION_CONTRACT])
async def test_budget_stop_is_durable_without_a_second_paid_request(billed, contract):
    run, relay, client, sent = await paid_relay(billed, contract=contract)
    db = billed.db
    try:
        if contract == PROCEDURE_CONTRACT:
            await db.pool.execute(
                "UPDATE billing_run_budgets "
                "SET terms=jsonb_set(terms,'{codex_contract}',$2::jsonb) "
                "WHERE run_id=$1",
                run.id,
                json.dumps(contract),
            )
        # Simulate verified work consuming the original quote, for both pinned
        # per-operation and session funding. Admission must stop before dispatch.
        await db.pool.execute(
            "UPDATE billing_run_budgets SET committed_nanos=maximum_nanos WHERE run_id=$1",
            run.id,
        )
        with pytest.raises(HTTPException) as failure:
            await relay.admit(run.id, GRANT, "blocked", "responses")
        assert failure.value.status_code == 402
        receipt = await db.get_effect(attempt_key(run.id))
        assert receipt.result["stop_reason"] == "run_limit"
        assert sent == []
        assert GRANT not in json.dumps(receipt.result)
        # Even a later funding change cannot restart this already stopped controller.
        await db.pool.execute(
            "UPDATE billing_run_budgets SET committed_nanos=0 WHERE run_id=$1", run.id
        )
        with pytest.raises(HTTPException):
            await relay.admit(run.id, GRANT, "another", "responses")
        assert sent == []
    finally:
        await client.aclose()
        await relay.close()


async def pin_api(db, run, contract=SESSION_CONTRACT):
    key = f"{run.id}:procedure_sandbox_create"
    async with db.pool.acquire() as conn:
        await db.start_effect(conn, execution_key=key, operation="procedure_sandbox_create")
        await db.complete_effect(conn, execution_key=key, result={"codex_auth": contract})


def procedure_input(**kwargs):
    return SandboxProcedureInput(
        **base_values(),
        context={},
        output_path=PATH,
        output_max_bytes=1000,
        isolated=True,
        timeout_seconds=900,
        **kwargs,
    )


@pytest.mark.parametrize(
    "failure", [RuntimeError("provider secret"), TimeoutError(), asyncio.CancelledError()]
)
async def test_failed_attempt_preserves_relay_reason_revokes_and_never_rebuys(
    publication_db, failure
):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    await pin_api(db, run)

    async def call(input):
        await db.pool.execute(
            'UPDATE effect_receipts SET result=result || \'{"stop_reason":"run_limit"}\'::jsonb '
            "WHERE execution_key=$1",
            attempt_key(run.id),
        )
        await input.failure_sink(failure)
        receipt = await db.get_effect(attempt_key(run.id))
        assert receipt.result["outcome"] != "running"
        raise failure

    paid = AsyncMock(side_effect=call)
    async with db.pool.acquire() as conn:
        with pytest.raises(type(failure)):
            await run_api_attempt(
                db=db,
                conn=conn,
                run=run,
                sandbox_id=run.sandbox_id,
                run_input=procedure_input(),
                call=paid,
            )
        with pytest.raises(CodexAttemptStopped, match="quoted spending maximum"):
            await run_api_attempt(
                db=db,
                conn=conn,
                run=run,
                sandbox_id=run.sandbox_id,
                run_input=procedure_input(),
                call=paid,
            )
    assert paid.await_count == 1
    record = (await db.get_effect(attempt_key(run.id))).result
    assert "provider secret" not in json.dumps(record)
    assert record["failure_type"] == type(failure).__name__
    if isinstance(failure, TimeoutError):
        assert record["outcome"] == "failed"
        assert "timed out" in str(attempt_failure({**record, "stop_reason": None}))


@pytest.mark.parametrize("relay_reason", [None, "request_limit"])
async def test_controller_token_stop_is_named_unless_the_relay_stopped_first(
    publication_db, relay_reason
):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    await pin_api(db, run, PROCEDURE_CONTRACT)
    failure = RuntimeError("Codex procedure reached its observed token limit")

    async def call(input):
        if relay_reason:
            await db.pool.execute(
                "UPDATE effect_receipts SET result=result || jsonb_build_object("
                "'stop_reason', $2::text) WHERE execution_key=$1",
                attempt_key(run.id),
                relay_reason,
            )
        await input.usage_sink({"source": "isolated_codex_controller", "limit_reached": False})
        await input.usage_sink({"source": "isolated_codex_controller", "limit_reached": True})
        await input.failure_sink(failure)
        raise failure

    async with db.pool.acquire() as conn:
        with pytest.raises(RuntimeError):
            await run_api_attempt(
                db=db,
                conn=conn,
                run=run,
                sandbox_id=run.sandbox_id,
                run_input=procedure_input(),
                call=AsyncMock(side_effect=call),
            )
    record = (await db.get_effect(attempt_key(run.id))).result
    assert record["outcome"] == "failed"
    assert record["stop_reason"] == (relay_reason or "token_limit")
    expected = "request limit" if relay_reason else "pinned token limit"
    assert expected in str(attempt_failure(record))


async def test_retry_recovers_completed_revision_when_discovery_branch_is_gone(publication_db):
    db = publication_db
    activities, storage, run, checkpoint = await activity_fixture(db)
    await pin_api(db, run)
    async with db.pool.acquire() as conn:
        await run_api_attempt(
            db=db,
            conn=conn,
            run=run,
            sandbox_id=run.sandbox_id,
            run_input=procedure_input(),
            call=AsyncMock(
                return_value=SandboxProcedureResult(
                    ephemeral_commit_sha=checkpoint.ephemeral_commit_sha,
                    summary="Ready",
                    message="Ready",
                )
            ),
        )
    await db.pool.execute(
        "DELETE FROM effect_receipts WHERE execution_key=$1", f"{run.id}:procedure_artifact_persist"
    )
    storage.branch_revision = None
    await activities.persist_codex_procedure_artifact(str(run.id))
    receipt = await db.get_effect(f"{run.id}:procedure_artifact_persist")
    assert receipt.status == "completed"
    assert receipt.result["checkpoint"]["ephemeral_commit_sha"] == checkpoint.ephemeral_commit_sha
    assert storage.repo.writes == 0


async def test_retry_without_output_stops_before_new_sandbox(publication_db):
    db = publication_db
    activities, storage, run, _ = await activity_fixture(db)
    await db.pool.execute(
        "DELETE FROM effect_receipts WHERE execution_key=$1", f"{run.id}:procedure_artifact_persist"
    )
    storage.branch_revision = None
    async with db.pool.acquire() as conn:
        await db.start_effect(conn, execution_key=attempt_key(run.id), operation=ATTEMPT)
        await db.save_effect_progress(
            conn,
            execution_key=attempt_key(run.id),
            result={"outcome": "failed", "stop_reason": "run_limit"},
        )
    with pytest.raises(ApplicationError, match="quoted spending maximum") as failure:
        await activities.persist_codex_procedure_artifact(str(run.id))
    assert failure.value.non_retryable
    assert activities._sandboxes.calls == 1
    receipt = await db.get_effect(f"{run.id}:procedure_artifact_persist")
    assert "quoted spending maximum" in receipt.error_message
    assert "already attempted" not in receipt.error_message


class PartialRepo(HistoryRepo):
    def __init__(self):
        super().__init__()
        self.branches = {}

    def create_commit(self, **options):
        assert options["ephemeral"]
        assert options["target_branch"].startswith("interrupted-procedures/")
        repo, files = self, {}

        class Builder:
            def add_file(self, path, content):
                files[path] = content
                return self

            async def send(self):
                head = repo.head
                revision = repo.edit(files)
                repo.head = head
                repo.branches[options["target_branch"]] = revision
                repo.writes += 1
                if repo.lose_response:
                    repo.lose_response = False
                    raise TimeoutError("lost acknowledgement")
                return {"commit_sha": revision}

        return Builder()


class PartialStorage(HistoryStorage):
    def __init__(self):
        super().__init__()
        self.repo = PartialRepo()

    async def procedure_checkpoint_revision(self, *, repo_id, branch):
        return self.repo.branches.get(branch)


@pytest.mark.parametrize("lose_ack", [False, True])
async def test_partial_survives_storage_ack_loss_without_publication(publication_db, lose_ack):
    db = publication_db
    activities, _, run, _ = await activity_fixture(db)
    storage = PartialStorage()
    project = await db.get_project(run.project_id)
    _, spec = await activities._pinned_codex_procedure(run.id)
    storage.repo.lose_response = lose_ack
    await db.pool.execute(
        "DELETE FROM effect_receipts WHERE execution_key=$1", f"{run.id}:procedure_artifact_persist"
    )
    await db.pool.execute("UPDATE workflow_runs SET retained_output=NULL WHERE id=$1", run.id)
    async with db.pool.acquire() as conn:
        kwargs = dict(
            db=db, conn=conn, storage=storage, run=run, project=project, spec=spec, base=None
        )
        if lose_ack:
            with pytest.raises(PublicationPendingError):
                await retain(**kwargs, content=CONTENT)
            assert (await db.get_run(run.id)).retained_output is None
            await retain(**kwargs)
        else:
            await retain(**kwargs, content=CONTENT)
        # Remove the branch as well: completed retention reads its immutable identity.
        storage.repo.branches.clear()
        await retain(**kwargs)
    updated = await db.get_run(run.id)
    assert updated.retained_output["reason"] == "execution_interrupted"
    assert updated.canonical_commit_sha is None
    assert storage.repo.head == run.expected_head_sha and storage.repo.writes == 1
    assert await db.get_effect(f"{run.id}:procedure_artifact_persist") is None
    assert (
        await read_run_output(
            storage=storage, run=updated, repo_id=project.state_repo_id, source="retained"
        )
    ).content == CONTENT
    await db.pool.execute(
        "UPDATE workflow_runs SET status='failed', lease_active=false WHERE id=$1", run.id
    )
    async with db.pool.acquire() as conn:
        with pytest.raises(SideEffectConflictError, match="not eligible for resolution"):
            await db.set_output_resolution(
                conn,
                run_id=run.id,
                checkpoint=updated.retained_output,
                resolution={"state": "applying"},
            )


@pytest.mark.parametrize(
    "bad", [b"", b"\xff", b"x" * 250001], ids=["empty", "invalid_utf8", "oversized"]
)
async def test_invalid_partial_never_written(publication_db, bad):
    activities, _, run, _ = await activity_fixture(publication_db)
    _, spec = await activities._pinned_codex_procedure(run.id)
    storage = PartialStorage()
    async with publication_db.pool.acquire() as conn:
        with pytest.raises(ValueError):
            await retain(
                db=publication_db,
                conn=conn,
                storage=storage,
                run=run,
                project=await publication_db.get_project(run.project_id),
                spec=spec,
                base=None,
                content=bad,
            )
    assert storage.repo.writes == 0


async def test_stale_lease_cannot_retain_partial(publication_db):
    activities, _, run, _ = await activity_fixture(publication_db)
    _, spec = await activities._pinned_codex_procedure(run.id)
    storage = PartialStorage()
    await publication_db.pool.execute(
        "UPDATE workflow_runs SET fencing_token=fencing_token+1 WHERE id=$1", run.id
    )
    async with publication_db.pool.acquire() as conn:
        with pytest.raises(StaleGenerationError):
            await retain(
                db=publication_db,
                conn=conn,
                storage=storage,
                run=run,
                project=await publication_db.get_project(run.project_id),
                spec=spec,
                base=None,
                content=CONTENT,
            )
    assert storage.repo.writes == 0


async def test_activity_recovers_partial_ack_then_reports_failure(publication_db):
    db = publication_db
    activities, _, run, _ = await activity_fixture(db)
    storage = PartialStorage()
    activities._storage = storage
    project = await db.get_project(run.project_id)
    _, spec = await activities._pinned_codex_procedure(run.id)
    await db.pool.execute(
        "DELETE FROM effect_receipts WHERE execution_key=$1", f"{run.id}:procedure_artifact_persist"
    )
    await db.pool.execute("UPDATE workflow_runs SET retained_output=NULL WHERE id=$1", run.id)
    async with db.pool.acquire() as conn:
        await db.start_effect(conn, execution_key=attempt_key(run.id), operation=ATTEMPT)
        await db.save_effect_progress(
            conn,
            execution_key=attempt_key(run.id),
            result={"outcome": "failed", "stop_reason": "run_limit"},
        )
        storage.repo.lose_response = True
        with pytest.raises(PublicationPendingError):
            await retain(
                db=db,
                conn=conn,
                storage=storage,
                run=run,
                project=project,
                spec=spec,
                base=None,
                content=CONTENT,
            )
    with pytest.raises(ApplicationError, match="quoted spending maximum") as failure:
        await activities.persist_codex_procedure_artifact(str(run.id))
    assert failure.value.non_retryable
    updated = await db.get_run(run.id)
    assert updated.retained_output["reason"] == "execution_interrupted"
    assert updated.canonical_commit_sha is None
    assert storage.repo.writes == 1 and storage.repo.head == run.expected_head_sha
    receipt = await db.get_effect(f"{run.id}:procedure_artifact_persist")
    assert receipt.status == "failed"
    assert "quoted spending maximum" in receipt.error_message


def test_frozen_reader_skips_old_missing_oversize_and_linked_files(tmp_path, make_symlink):
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)  # noqa: S603,S607
    output = root / "report.md"
    output.write_text("old")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)  # noqa: S603,S607
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )  # noqa: S603,S607

    def read():
        return subprocess.run(  # noqa: S603
            [sys.executable, "-c", _INTERRUPTED_OUTPUT_READER, "report.md", "100", str(root)],
            check=True,
            capture_output=True,
        ).stdout.strip()  # noqa: S603

    assert read() == b""
    output.write_text("partial")
    assert base64.b64decode(read()) == b"partial"
    output.write_text("x" * 101)
    assert read() == b""
    output.unlink()
    assert read() == b""
    target = tmp_path / "secret"
    target.write_text("must not read")
    make_symlink(output, target)
    assert read() == b""
    output.unlink()
    output.hardlink_to(target)
    assert read() == b""


@pytest.mark.parametrize("outside_change", [False, True])
async def test_partial_code_map_keeps_its_section_contract(publication_db, outside_change):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    storage = PartialStorage()
    await db.pool.execute("UPDATE workflow_runs SET retained_output=NULL WHERE id=$1", run.id)
    content = _index(CODE_MAP.replace("Files opened: 40", "Files opened: 41"))
    if outside_change:
        content = content.replace(b"Durable architecture facts.", b"Unrelated memory overwritten.")
    async with db.pool.acquire() as conn:
        kwargs = dict(
            db=db,
            conn=conn,
            storage=storage,
            run=run,
            project=await db.get_project(run.project_id),
            spec=_spec(CODE_SECTION),
            base=BASE_INDEX.encode(),
            content=content,
        )
        if outside_change:
            with pytest.raises(ValueError):
                await retain(**kwargs)
        else:
            await retain(**kwargs)
    assert storage.repo.writes == (0 if outside_change else 1)
    assert storage.repo.head == run.expected_head_sha


@pytest.mark.parametrize("salvage_fails", [False, True])
async def test_runtime_revokes_freezes_captures_then_kills_even_on_capture_error(
    monkeypatch, salvage_fails
):
    order = []

    class Commands:
        async def run(self, command, **kwargs):
            if command.endswith("isolated-procedure check"):
                return SimpleNamespace(stdout="TIN_ISOLATION_READY_V1")
            if command.endswith("codex_api_config.py --check-v4"):
                return SimpleNamespace(stdout="TIN_CODEX_API_READY_V4")
            if command == "/opt/tin-lite/run-procedure":
                return SimpleNamespace(wait=AsyncMock(side_effect=RuntimeError("original failure")))
            if command.endswith("isolated-procedure freeze"):
                order.append("freeze")
                return SimpleNamespace(stdout="")
            assert command.startswith("python3 -c")
            order.append("read")
            return SimpleNamespace(stdout=base64.b64encode(CONTENT).decode())

    async def kill():
        order.append("kill")

    async def revoke(_exc):
        order.append("revoke")

    async def save(content):
        assert content == CONTENT
        order.append("save")
        if salvage_fails:
            raise RuntimeError("storage unavailable")

    sandbox = SimpleNamespace(
        commands=Commands(), files=SimpleNamespace(write=AsyncMock()), kill=kill
    )
    monkeypatch.setattr(
        "tin_lite.e2b_runtime.AsyncSandbox.connect", AsyncMock(return_value=sandbox)
    )
    runtime = E2BRuntime(
        api_key="synthetic", template="test", timeout_seconds=60, egress_allow_hosts=()
    )
    with pytest.raises(RuntimeError, match="original failure"):
        await runtime.run_procedure_and_kill(
            sandbox_id="sandbox",
            run_input=procedure_input(
                api_url="https://tin.test/relay",
                api_contract=SESSION_CONTRACT,
                api_grant=GRANT,
                usage_sink=AsyncMock(),
                failure_sink=revoke,
                interrupted_output_sink=save,
            ),
        )
    assert order == ["revoke", "freeze", "read", "save", "kill"]


async def test_narration_projects_bounded_progress_only_for_unstepped_active_runs(publication_db):
    db = publication_db
    _, _, run, _ = await activity_fixture(db)
    # While Codex works a procedure has no product step; saving the result adds one.
    await db.pool.execute(
        "UPDATE workflow_runs SET status='running', progress_mode='indeterminate', "
        "progress_step=NULL, progress_current=NULL, progress_total=NULL WHERE id=$1",
        run.id,
    )
    assert await db.project_run_narration(run_id=run.id, summary="Signed in.\n Writing  it.")
    assert (await db.get_run(run.id)).progress_summary == "Signed in. Writing it."
    assert await db.project_run_narration(run_id=run.id, summary="y" * 400)
    summary = (await db.get_run(run.id)).progress_summary
    assert len(summary) == 240 and summary.endswith("…")
    await db.project_run_progress(
        run_id=run.id, mode="steps", current=1, total=3, step="draft", summary="Drafting."
    )
    assert not await db.project_run_narration(run_id=run.id, summary="Agent text")
    assert (await db.get_run(run.id)).progress_summary == "Drafting."
    await db.pool.execute(
        "UPDATE workflow_runs SET status='failed', progress_step=NULL WHERE id=$1", run.id
    )
    assert not await db.project_run_narration(run_id=run.id, summary="Too late")
