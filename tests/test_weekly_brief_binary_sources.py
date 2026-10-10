"""Weekly briefs keep media receipts without treating media bytes as prose."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tin_lite.activities import TinActivities
from tin_lite.domain import RunStatus


@pytest.mark.parametrize(
    "media",
    [b"\x00\x00\x00\x20ftypisom" + b"\x00" * 23 + byte for byte in (b"\x85", b"\xbd")]
    + [b"\x00ftypisom"],
    ids=["locin-invalid-byte-35", "readntype-invalid-byte-35", "utf8-with-nul"],
)
async def test_weekly_brief_keeps_binary_receipt_and_reads_later_text(media):
    runs = [
        SimpleNamespace(
            id=uuid4(),
            artifact_ref=f"fixture://{path}",
            artifact_path=path,
            canonical_commit_sha="a" * 40,
            executor="codex.procedure",
            status=RunStatus.SUCCEEDED,
            task_summary="Demo saved; quality has not been reviewed.",
            task_result=None,
            error_message=None,
        )
        for path in ("demos/demo.mp4", "reports/review.md")
    ]
    report = "# Review\nÇeviri testi geçti; video kalitesi incelenmedi."
    files = {runs[0].artifact_path: media, runs[1].artifact_path: report.encode()}
    fixture = SimpleNamespace(
        _integration_evidence=AsyncMock(return_value="No connection needed."),
        _storage=SimpleNamespace(
            read_canonical_artifact=AsyncMock(side_effect=lambda **kw: files[kw["path"]])
        ),
        _db=SimpleNamespace(
            list_runs_for_period=AsyncMock(return_value=runs),
            list_product_activity_for_period=AsyncMock(return_value=[]),
        ),
    )
    project = SimpleNamespace(
        id=uuid4(), state_repo_id="fixture", memory_commit_sha=None, memory_index_path=None
    )
    end = datetime(2026, 10, 10, tzinfo=UTC)
    sources = await TinActivities._weekly_brief_sources(
        fixture,
        run_id=uuid4(),
        project=project,
        period_start=end - timedelta(days=7),
        period_end=end,
    )
    media_source, text_source = sources[1:]
    assert media_source.artifact_ref == runs[0].artifact_ref
    assert json.loads(media_source.content) == {
        "workflow": "codex.procedure",
        "status": "succeeded",
        "summary": runs[0].task_summary,
        "result": None,
        "error": None,
    }
    assert text_source.artifact_ref == runs[1].artifact_ref
    assert text_source.content == report


async def test_weekly_brief_does_not_hide_storage_failure():
    fixture = SimpleNamespace(
        _integration_evidence=AsyncMock(return_value="No connection needed."),
        _storage=SimpleNamespace(
            read_canonical_artifact=AsyncMock(side_effect=OSError("unavailable"))
        ),
        _db=SimpleNamespace(
            list_runs_for_period=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        id=uuid4(),
                        artifact_ref="fixture://demo.mp4",
                        artifact_path="demos/demo.mp4",
                        canonical_commit_sha="a" * 40,
                    )
                ]
            )
        ),
    )
    project = SimpleNamespace(
        id=uuid4(), state_repo_id="fixture", memory_commit_sha=None, memory_index_path=None
    )
    end = datetime(2026, 10, 10, tzinfo=UTC)
    with pytest.raises(OSError, match="unavailable"):
        await TinActivities._weekly_brief_sources(
            fixture,
            run_id=uuid4(),
            project=project,
            period_start=end - timedelta(days=7),
            period_end=end,
        )
