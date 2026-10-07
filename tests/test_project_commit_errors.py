"""code.storage ref-update rejections reach members and agents as invalid or conflict."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pierre_storage.errors import RefUpdateError

from tin_lite.code_storage import CodeStorage
from tin_lite.mcp_errors import tool_error
from tin_lite.project_files import ProjectFileService, StaleProjectRevisionError

HEAD = "a" * 40
CHANGES = [{"operation": "upsert", "path": "notes.md", "content": "same as before"}]


class RejectingRepo:
    def __init__(self, error: RefUpdateError) -> None:
        self.error = error

    async def list_files(self, **values) -> dict:
        return {"paths": ["notes.md"]}

    async def list_commits(self, **values) -> dict:
        return {"commits": [{"sha": HEAD, "message": "an earlier commit"}]}

    def create_commit(self, **values):
        error = self.error

        class Builder:
            def add_file_from_string(self, path: str, content: str):
                return self

            async def send(self) -> dict:
                raise error

        return Builder()


def storage_rejecting(monkeypatch: pytest.MonkeyPatch, error: RefUpdateError) -> CodeStorage:
    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    storage = CodeStorage(organization="tin", private_key=pem)
    repo = RejectingRepo(error)

    async def get_repo(repo_id: str):
        return repo

    async def head_sha(repo, branch: str):
        return HEAD

    monkeypatch.setattr(storage, "get_repo", get_repo)
    monkeypatch.setattr(storage, "head_sha", head_sha)
    return storage


def file_service(storage: CodeStorage):
    @asynccontextmanager
    async def lock(**values):
        yield

    database = SimpleNamespace(
        project_file_change_lock=lock,
        get_project_file_change=AsyncMock(return_value=None),
        start_project_file_change=AsyncMock(),
        fail_project_file_change=AsyncMock(),
        complete_project_file_change=AsyncMock(),
    )
    return ProjectFileService(database=database, storage=storage), database  # type: ignore[arg-type]


async def commit(service):
    project = SimpleNamespace(id=uuid4(), state_repo_id="projects/p", canonical_branch="main")
    return await service.commit(
        project=project,
        actor_clerk_user_id="user_member",
        client_id=None,
        request_id=uuid4(),
        expected_revision=HEAD,
        message="tick the plan",
        changes=CHANGES,
    )


@pytest.mark.asyncio
async def test_no_changes_is_invalid_and_the_request_is_not_left_started(monkeypatch):
    storage = storage_rejecting(monkeypatch, RefUpdateError("no changes to commit"))
    service, database = file_service(storage)
    with pytest.raises(ValueError, match="no changes") as caught:
        await commit(service)
    assert str(tool_error(caught.value)) == (
        "invalid: no changes: files already match expected_revision"
    )
    assert database.fail_project_file_change.await_args.kwargs["error_code"] == "invalid"
    database.complete_project_file_change.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RefUpdateError("expected head moved", status="409", reason="conflict"),
        RefUpdateError("precondition failed", status="precondition_failed"),
    ],
)
async def test_ref_conflict_is_a_stale_revision(monkeypatch, error):
    service, database = file_service(storage_rejecting(monkeypatch, error))
    with pytest.raises(StaleProjectRevisionError) as caught:
        await commit(service)
    assert str(tool_error(caught.value)).startswith("conflict: canonical project state changed")
    assert database.fail_project_file_change.await_args.kwargs["error_code"] == "stale_revision"


@pytest.mark.asyncio
async def test_other_ref_rejection_is_recorded_as_a_storage_failure(monkeypatch):
    error = RefUpdateError("upstream unavailable", status="503", reason="unavailable")
    service, database = file_service(storage_rejecting(monkeypatch, error))
    with pytest.raises(RuntimeError, match=r"rejected the project file commit \(unavailable\)"):
        await commit(service)
    assert database.fail_project_file_change.await_args.kwargs["error_code"] == "storage_failed"


UNDO = "b" * 40


class UndoneRepo:
    """The undo commit for HEAD is already the branch head."""

    def __init__(self, request_id: str) -> None:
        self.undo_message = f"Undo {HEAD[:8]} [project-file:{request_id}]"
        self.restore_commit = AsyncMock()

    async def list_commits(self, **values) -> dict:
        commits = [
            {"sha": UNDO, "message": self.undo_message},
            {"sha": HEAD, "message": "tick the plan"},
        ]
        return {"commits": commits[: values["limit"]]}

    async def get_commit_diff(self, **values) -> dict:
        assert values["sha"] == HEAD
        return {"files": [{"path": "notes.md", "state": "modified"}]}


def storage_after_undo(monkeypatch: pytest.MonkeyPatch, request_id: str):
    storage = storage_rejecting(monkeypatch, RefUpdateError("unused"))
    repo = UndoneRepo(request_id)

    async def get_repo(repo_id: str):
        return repo

    async def head_sha(repo, branch: str):
        return UNDO

    monkeypatch.setattr(storage, "get_repo", get_repo)
    monkeypatch.setattr(storage, "head_sha", head_sha)
    return storage, repo


async def revert(service, request_id):
    project = SimpleNamespace(id=uuid4(), state_repo_id="projects/p", canonical_branch="main")
    return await service.revert_latest(
        project=project,
        actor_clerk_user_id="user_member",
        client_id=None,
        request_id=request_id,
        commit_sha=HEAD,
        expected_revision=HEAD,
    )


@pytest.mark.asyncio
async def test_revert_retry_after_the_undo_landed_returns_that_undo(monkeypatch):
    request_id = uuid4()
    storage, repo = storage_after_undo(monkeypatch, str(request_id))
    service, database = file_service(storage)
    result = await revert(service, request_id)
    assert result.revision == UNDO
    assert result.changed_paths == ("notes.md",)
    repo.restore_commit.assert_not_awaited()
    database.fail_project_file_change.assert_not_awaited()
    assert database.complete_project_file_change.await_args.kwargs["commit_sha"] == UNDO


@pytest.mark.asyncio
async def test_revert_after_another_request_undid_the_head_is_stale(monkeypatch):
    storage, _ = storage_after_undo(monkeypatch, str(uuid4()))
    service, database = file_service(storage)
    with pytest.raises(StaleProjectRevisionError):
        await revert(service, uuid4())
    assert database.fail_project_file_change.await_args.kwargs["error_code"] == "stale_revision"


@pytest.mark.asyncio
async def test_an_invalid_ref_rejection_is_invalid_not_a_conflict(monkeypatch):
    error = RefUpdateError("bad path", status="422", reason="invalid")
    service, database = file_service(storage_rejecting(monkeypatch, error))
    with pytest.raises(ValueError) as caught:
        await commit(service)
    assert str(tool_error(caught.value)).startswith("invalid: code.storage rejected")
    assert database.fail_project_file_change.await_args.kwargs["error_code"] == "invalid"


@pytest.mark.asyncio
async def test_a_storage_failure_reaches_agents_as_a_storage_failure(monkeypatch):
    error = RefUpdateError("upstream unavailable", status="503", reason="unavailable")
    service, _ = file_service(storage_rejecting(monkeypatch, error))
    with pytest.raises(RuntimeError) as caught:
        await commit(service)
    assert str(tool_error(caught.value)).startswith("storage_failed:")


@pytest.mark.asyncio
async def test_a_transport_failure_during_commit_is_recorded(monkeypatch):
    storage = storage_rejecting(monkeypatch, RefUpdateError("unused"))
    repo = await storage.get_repo("projects/p")

    async def list_files(**values):
        raise httpx.ConnectError("storage unreachable")

    monkeypatch.setattr(repo, "list_files", list_files)
    service, database = file_service(storage)
    with pytest.raises(httpx.ConnectError):
        await commit(service)
    assert database.fail_project_file_change.await_args.kwargs["error_code"] == "storage_failed"


class SingleCommitRepo:
    """HEAD is the repository's first commit, so there is nothing to restore."""

    async def list_commits(self, **values) -> dict:
        return {"commits": [{"sha": HEAD, "message": "initial"}]}


class FailingRestoreRepo:
    async def list_commits(self, **values) -> dict:
        return {"commits": [{"sha": HEAD, "message": "tick"}, {"sha": UNDO, "message": "x"}]}

    async def get_commit_diff(self, **values) -> dict:
        return {"files": [{"path": "notes.md", "state": "modified"}]}

    async def restore_commit(self, **values) -> dict:
        raise httpx.ReadTimeout("storage timed out")


def storage_at_head(monkeypatch: pytest.MonkeyPatch, repo) -> CodeStorage:
    storage = storage_rejecting(monkeypatch, RefUpdateError("unused"))

    async def get_repo(repo_id: str):
        return repo

    monkeypatch.setattr(storage, "get_repo", get_repo)
    return storage


@pytest.mark.asyncio
async def test_undo_without_a_parent_is_invalid_not_stale(monkeypatch):
    service, database = file_service(storage_at_head(monkeypatch, SingleCommitRepo()))
    with pytest.raises(ValueError, match="no restorable parent"):
        await revert(service, uuid4())
    assert database.fail_project_file_change.await_args.kwargs["error_code"] == "invalid"


@pytest.mark.asyncio
async def test_undo_of_an_older_commit_is_invalid_not_stale(monkeypatch):
    service, database = file_service(storage_at_head(monkeypatch, SingleCommitRepo()))
    project = SimpleNamespace(id=uuid4(), state_repo_id="projects/p", canonical_branch="main")
    with pytest.raises(ValueError, match="only the current project revision"):
        await service.revert_latest(
            project=project,
            actor_clerk_user_id="user_member",
            client_id=None,
            request_id=uuid4(),
            commit_sha=UNDO,
            expected_revision=HEAD,
        )
    assert database.fail_project_file_change.await_args.kwargs["error_code"] == "invalid"


@pytest.mark.asyncio
async def test_undo_that_fails_in_transport_is_not_left_started(monkeypatch):
    service, database = file_service(storage_at_head(monkeypatch, FailingRestoreRepo()))
    with pytest.raises(httpx.ReadTimeout):
        await revert(service, uuid4())
    assert database.fail_project_file_change.await_args.kwargs["error_code"] == "storage_failed"
