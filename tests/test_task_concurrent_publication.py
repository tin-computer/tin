"""A task proposes one immutable diff while other runs keep saving project files."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_procedure_publication import HistoryStorage
from test_project_task import TASK_RAW_DIFF

from tin_lite.publication import OutputConflictError


async def apply(storage, base, *, files=None):
    return await storage.apply_task_diff(
        repo_id=storage.repo.id,
        branch="main",
        expected_head_sha=storage.repo.head,
        original_base_sha=base,
        raw_diff=TASK_RAW_DIFF,
        expected_diff_sha256=hashlib.sha256(TASK_RAW_DIFF.encode()).hexdigest(),
        reviewed_files=files or [{"path": "report.md", "old_path": None}],
        execution_key="run:task_apply",
        run_id="run",
    )


def ready_storage():
    storage = HistoryStorage()
    storage.repo.create_commit_from_diff = AsyncMock(return_value={"commit_sha": "c" * 40})
    storage.repo.list_commits = AsyncMock(return_value={"commits": []})
    return storage


async def test_unrelated_save_does_not_prevent_exact_task_application():
    storage = ready_storage()
    base = storage.repo.head
    current = storage.repo.edit({"social.md": b"Another run's output\n"})
    assert await apply(storage, base) == "c" * 40
    request = storage.repo.create_commit_from_diff.await_args.kwargs
    assert request["expected_head_sha"] == current
    assert request["diff"] == TASK_RAW_DIFF
    assert storage.repo.trees[current]["social.md"][1] == b"Another run's output\n"


@pytest.mark.parametrize(
    "original,current",
    [
        (None, b"Another run created this file\n"),
        (b"Original\n", b"Concurrent edit outside the task's patch hunk\n"),
        (b"Original\n", None),
        (("100644", b"Original\n"), ("100755", b"Original\n")),
        (b"Original\n", ("120000", b"elsewhere")),
    ],
)
async def test_changed_destination_is_preserved_even_when_a_patch_might_apply(original, current):
    storage = ready_storage()
    base = storage.repo.edit({"report.md": original})
    head = storage.repo.edit({"report.md": current})
    with pytest.raises(OutputConflictError):
        await apply(storage, base)
    assert storage.repo.head == head
    storage.repo.create_commit_from_diff.assert_not_awaited()


async def test_rename_checks_the_original_path_too():
    storage = ready_storage()
    base = storage.repo.edit({"old.md": b"Original\n"})
    storage.repo.edit({"old.md": b"Someone edited the renamed source\n"})
    with pytest.raises(OutputConflictError):
        await apply(storage, base, files=[{"path": "report.md", "old_path": "old.md"}])
    storage.repo.create_commit_from_diff.assert_not_awaited()


async def test_write_race_keeps_expected_head_and_never_forces_a_patch():
    storage = ready_storage()
    base = storage.repo.head
    storage.repo.edit({"social.md": b"Independent output\n"})

    async def raced(**request):
        storage.repo.edit({"report.md": b"Written after the comparison\n"})
        assert request["expected_head_sha"] != storage.repo.head
        raise RuntimeError("CAS rejected")

    storage.repo.create_commit_from_diff.side_effect = raced
    with pytest.raises(RuntimeError, match="CAS rejected"):
        await apply(storage, base)
    assert storage.repo.create_commit_from_diff.await_count == 1


async def test_lost_success_is_reconciled_before_a_changed_file_causes_a_second_write():
    storage = ready_storage()
    base = storage.repo.head
    committed = storage.repo.edit({"report.md": b"# Report\n"})
    storage.repo.list_commits.return_value = {
        "commits": [{"sha": committed, "message": "project.task run [run:task_apply]"}]
    }
    assert await apply(storage, base) == committed
    storage.repo.create_commit_from_diff.assert_not_awaited()


async def test_review_preview_reads_the_immutable_proposal(monkeypatch):
    from uuid import uuid4

    from tin_lite import api
    from tin_lite.domain import RunStatus

    run = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        status=RunStatus.NEEDS_INPUT,
        ephemeral_branch="tasks/run/1",
        canonical_commit_sha=None,
        task_diff={"source_revision": "c" * 40, "files": [{"path": "report.md"}]},
    )
    storage = SimpleNamespace(read_procedure_checkpoint=AsyncMock(return_value=b"# Proposal"))
    database = SimpleNamespace(
        get_project=AsyncMock(return_value=SimpleNamespace(state_repo_id="test"))
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                runtime=SimpleNamespace(
                    database=database,
                    storage=storage,
                )
            )
        )
    )
    monkeypatch.setattr(api, "_project_task_from_postgres", AsyncMock(return_value=run))
    _, content = await api._read_project_task_review_file(run.id, "report.md", request, object())
    assert content == b"# Proposal"
    storage.read_procedure_checkpoint.assert_awaited_once_with(
        repo_id="test", revision="c" * 40, path="report.md"
    )
