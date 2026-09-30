"""Project file paths, search and history at the code.storage boundary."""

from __future__ import annotations

import enum
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pierre_storage.errors import ApiError
from pierre_storage.types import DiffFileState

from tin_lite.code_storage import CodeStorage
from tin_lite.mcp_errors import tool_error
from tin_lite.project_files import (
    ProjectFileService,
    normalize_project_file_mutations,
    safe_project_file_path,
)

HEAD = "a" * 40
NON_CANONICAL = ["reports//plan.md", "./plan.md", "reports/./plan.md", "reports/", "plan.md/"]


@pytest.mark.parametrize("path", NON_CANONICAL)
def test_a_path_code_storage_would_rewrite_is_refused(path):
    """code.storage keeps the raw string; "a//b" was reported written and never stored."""
    assert not safe_project_file_path(path)
    with pytest.raises(ValueError, match="unsafe or protected project path"):
        normalize_project_file_mutations([{"operation": "upsert", "path": path, "content": "x"}])
    with pytest.raises(ValueError, match="rename destination is unsafe"):
        normalize_project_file_mutations(
            [{"operation": "rename", "path": "notes/a.md", "new_path": path}]
        )


def test_the_same_file_under_two_spellings_is_not_two_changes():
    with pytest.raises(ValueError):
        normalize_project_file_mutations(
            [
                {"operation": "upsert", "path": "notes/a.md", "content": "one"},
                {"operation": "delete", "path": "notes//a.md"},
            ]
        )


def test_paths_tin_writes_itself_stay_valid():
    from tin_lite import domain, keyword_plan
    from tin_lite.brand_contract import BRAND_PATH, DESIGN_PATH
    from tin_lite.content_delivery import settings_path
    from tin_lite.system_wiki import PROJECT_SCANNING_PATH
    from tin_lite.writing_style import STYLE_PATH

    paths = [
        settings_path(uuid4()),
        domain.answer_page_path("How do I pick a CRM?", "2026-09-30", "abcd1234"),
        domain.SCAN_REPORT_PATH,
        domain.MEMORY_INDEX_PATH,
        domain.GROWTH_ONBOARDING_PLAN_PATH,
        domain.EMAIL_SHORTLIST_PATH,
        BRAND_PATH,
        DESIGN_PATH,
        PROJECT_SCANNING_PATH,
        STYLE_PATH,
        *keyword_plan.paths(str(uuid4())).values(),
    ]
    assert [path for path in paths if not safe_project_file_path(path)] == []


@pytest.mark.asyncio
async def test_a_non_canonical_commit_never_reaches_storage():
    storage = SimpleNamespace(commit_project_changes=AsyncMock())

    @asynccontextmanager
    async def lock(**values):
        yield

    database = SimpleNamespace(
        project_file_change_lock=lock,
        get_project_file_change=AsyncMock(return_value=None),
        start_project_file_change=AsyncMock(),
    )
    service = ProjectFileService(database=database, storage=storage)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await service.commit(
            project=SimpleNamespace(id=uuid4(), state_repo_id="p", canonical_branch="main"),
            actor_clerk_user_id="user_member",
            client_id=None,
            request_id=uuid4(),
            expected_revision=HEAD,
            message="rename",
            changes=[{"operation": "rename", "path": "notes/a.md", "new_path": "notes//b.md"}],
        )
    storage.commit_project_changes.assert_not_awaited()
    database.start_project_file_change.assert_not_awaited()


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("notes/a.md", True),
        ("notes", True),
        ("notes/", True),
        (":(icase)NOTES", False),
        (":(exclude)notes", False),
        (":!notes", False),
        ("../notes", False),
        (".env", False),
        ("keys/deploy.pem", False),
    ],
)
def test_search_scopes_are_paths_not_pathspec_magic(path, allowed):
    from tin_lite.project_files import safe_project_search_path

    assert safe_project_search_path(path) is allowed


def storage_with(monkeypatch: pytest.MonkeyPatch, repo) -> CodeStorage:
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

    async def get_repo(repo_id: str):
        return repo

    monkeypatch.setattr(storage, "get_repo", get_repo)
    return storage


def status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://tin.code.storage/api/v1/repos/grep")
    return httpx.HTTPStatusError(
        "storage", request=request, response=httpx.Response(code, request=request)
    )


class GrepRepo:
    def __init__(self, matches=None, error: Exception | None = None) -> None:
        self.matches = matches or []
        self.error = error
        self.patterns: list[str] = []

    async def grep(self, **values) -> dict:
        self.patterns.append(values["pattern"])
        if self.error is not None:
            raise self.error
        return {"matches": self.matches, "has_more": False}


async def search(storage: CodeStorage, query: str = "plan"):
    return await storage.search_canonical_files(repo_id="p", revision=HEAD, query=query)


@pytest.mark.asyncio
async def test_search_finds_the_query_as_literal_text(monkeypatch):
    repo = GrepRepo()
    await search(storage_with(monkeypatch, repo), "price (USD) $5.00 [beta] a|b ^c* {1}")
    assert repo.patterns == [r"price \(USD\) \$5\.00 \[beta\] a\|b \^c\* \{1\}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [400, 408, 422])
async def test_a_refused_search_is_invalid_not_a_crash(monkeypatch, code):
    storage = storage_with(monkeypatch, GrepRepo(error=status_error(code)))
    with pytest.raises(ValueError) as caught:
        await search(storage)
    assert str(tool_error(caught.value)).startswith("invalid:")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [status_error(404), ApiError("missing", status_code=404)])
async def test_an_unknown_revision_is_not_found(monkeypatch, error):
    class MissingRepo(GrepRepo):
        async def list_files(self, **values) -> dict:
            raise error

    storage = storage_with(monkeypatch, MissingRepo(error=error))
    with pytest.raises(LookupError) as searched:
        await search(storage)
    with pytest.raises(LookupError) as listed:
        await storage.list_canonical_files_at(repo_id="p", revision="f" * 40)
    assert str(tool_error(searched.value)).startswith("not_found:")
    assert str(tool_error(listed.value)).startswith("not_found:")


@pytest.mark.asyncio
async def test_a_non_ascii_path_is_decoded_and_one_odd_path_does_not_fail_the_search(monkeypatch):
    line = [{"line_number": 1, "text": "plan", "type": "match"}]
    repo = GrepRepo(
        matches=[
            {"path": '"caf\\303\\251.md"', "lines": line},
            {"path": '"bad\\q.md"', "lines": line},
            {"path": "notes/plan.md", "lines": line},
        ]
    )
    matches, _ = await search(storage_with(monkeypatch, repo))
    assert [item["path"] for item in matches] == ["café.md", "notes/plan.md"]


@pytest.mark.asyncio
async def test_search_never_shows_files_read_refuses(monkeypatch):
    line = [{"line_number": 1, "text": "TOKEN=plan", "type": "match"}]
    repo = GrepRepo(
        matches=[
            {"path": ".env", "lines": line},
            {"path": "config/.env.production", "lines": line},
            {"path": "keys/deploy.pem", "lines": line},
            {"path": "credentials.json", "lines": line},
            {"path": "notes/plan.md", "lines": line},
        ]
    )
    matches, _ = await search(storage_with(monkeypatch, repo))
    assert [item["path"] for item in matches] == ["notes/plan.md"]


@pytest.mark.asyncio
async def test_history_reports_the_plain_diff_state(monkeypatch):
    class HistoryRepo:
        async def list_commits(self, **values) -> dict:
            return {
                "commits": [
                    {"sha": HEAD, "message": "add", "author_name": "Tin", "date": None},
                ]
            }

        async def get_commit_diff(self, **values) -> dict:
            return {"files": [{"path": "notes/a.md", "state": DiffFileState.ADDED}]}

    assert isinstance(DiffFileState.ADDED, enum.Enum)
    storage = storage_with(monkeypatch, HistoryRepo())
    history = await storage.canonical_file_history(repo_id="p", branch="main", path="notes/a.md")
    assert [item["state"] for item in history] == ["added"]


def http_client(storage) -> tuple[httpx.AsyncClient, str]:
    from fastapi import FastAPI

    from tin_lite.api import router
    from tin_lite.auth import AuthContext, require_user
    from tin_lite.domain import Project

    project = Project(uuid4(), "Tin POC", "projects/tin-poc", "main")

    class Database:
        async def has_project_access(self, *, project_id, clerk_user_id):
            return True

        async def get_project(self, project_id):
            return project

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_test",
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
    )
    app.state.runtime = SimpleNamespace(database=Database(), storage=storage)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, f"/api/projects/{project.id}/files"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LookupError("project revision not found"), 404),
        (ValueError("code.storage could not run this project search"), 422),
    ],
)
async def test_http_search_maps_storage_refusals(error, expected):
    class Storage:
        async def search_canonical_files(self, **values):
            raise error

    client, files = http_client(Storage())
    async with client:
        response = await client.get(f"{files}/search", params={"query": "(", "revision": "f" * 40})
        magic = await client.get(
            f"{files}/search",
            params={"query": "plan", "revision": "f" * 40, "path": ":(icase)NOTES"},
        )
    assert response.status_code == expected
    assert magic.status_code == 422


@pytest.mark.asyncio
async def test_http_read_and_list_at_an_unknown_revision_are_404():
    class Storage:
        async def list_canonical_files_at(self, **values):
            raise LookupError("project revision not found")

    client, files = http_client(Storage())
    async with client:
        listed = await client.get(files, params={"revision": "f" * 40})
        read = await client.get(f"{files}/raw", params={"path": "notes/a.md", "revision": "f" * 40})
    assert listed.status_code == 404
    assert read.status_code == 404
