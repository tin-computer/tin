from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tin_lite.code_storage import CodeStorage


async def test_bulk_dates_are_pinned_paginated_and_cached_without_reading_files(monkeypatch):
    storage = CodeStorage(organization="tin", private_key="unused")
    repo = SimpleNamespace(id="projects/test", api_base_url="https://storage.test")
    repo.generate_jwt = lambda *args: "fixture"
    monkeypatch.setattr(storage, "get_repo", AsyncMock(return_value=repo))
    seen = []

    def response(request):
        seen.append(dict(request.url.params))
        assert request.url.path.endswith("/files/metadata")
        first = not request.url.params.get("cursor")
        return httpx.Response(
            200,
            json={
                "files": [
                    {"path": "a.md" if first else "b.md", "last_commit_sha": "b" * 40},
                    {"path": "no-date.md", "last_commit_sha": "c" * 40},
                ],
                "commits": {"b" * 40: {"date": "2026-09-20T12:00:00-07:00"}},
                "has_more": first,
                "next_cursor": "page-two" if first else None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        monkeypatch.setattr(storage, "_http_client", lambda: client)
        expected = {name: datetime(2026, 9, 20, 19, tzinfo=UTC) for name in ("a.md", "b.md")}
        for _ in range(2):
            assert (
                await storage.canonical_file_modified_dates(repo_id=repo.id, revision="a" * 40)
                == expected
            )
        assert len(seen) == 2
        assert {item["ref"] for item in seen} == {"a" * 40}
        await storage.canonical_file_modified_dates(repo_id=repo.id, revision="d" * 40)
        assert len(seen) == 4  # A new snapshot cannot reuse old metadata.


@pytest.mark.parametrize("date", [None, "invalid", "2026-09-20T12:00:00"])
async def test_missing_or_ambiguous_dates_are_not_replaced_with_head_time(monkeypatch, date):
    storage = CodeStorage(organization="tin", private_key="unused")
    monkeypatch.setattr(storage, "get_repo", AsyncMock())
    monkeypatch.setattr(
        storage,
        "_publication_json",
        AsyncMock(
            return_value={
                "files": [{"path": "a.md", "last_commit_sha": "b" * 40}],
                "commits": {"b" * 40: {"date": date}},
            }
        ),
    )
    assert await storage.canonical_file_modified_dates(repo_id="test", revision="a" * 40) == {}


async def test_repeated_metadata_cursor_stops_instead_of_looping(monkeypatch):
    storage = CodeStorage(organization="tin", private_key="unused")
    monkeypatch.setattr(storage, "get_repo", AsyncMock())
    read = AsyncMock(return_value={"files": [], "has_more": True, "next_cursor": "same"})
    monkeypatch.setattr(storage, "_publication_json", read)
    with pytest.raises(RuntimeError, match="pagination"):
        await storage.canonical_file_modified_dates(repo_id="test", revision="a" * 40)
    assert read.await_count == 2
