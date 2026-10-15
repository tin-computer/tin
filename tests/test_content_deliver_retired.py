"""Approvals start website.change; content.deliver left the codebase.

test_retired_workflows covers the deleted workflows leaving the catalog."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from tin_lite import content_repository_delivery as delivery
from tin_lite.content_delivery import adaptation_start_key


def _page(**extra):
    return SimpleNamespace(id=uuid4(), project_id=uuid4(), started_by_clerk_user_id="u", **extra)


INTENT = {"settings": {"repository": "acme/site", "mode": "github_commit"}, "route": "/a/{slug}"}


async def test_an_approved_page_starts_website_change(monkeypatch):
    page = _page()
    website = SimpleNamespace(id=delivery.WEBSITE_CHANGE_ID)
    database = SimpleNamespace(
        get_run_by_start_key=AsyncMock(return_value=None),
        get_workflow=AsyncMock(return_value=website),
    )
    start = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr("tin_lite.run_service.start_workflow_run", start)
    await delivery.start_approved_adaptation(
        runtime=SimpleNamespace(database=database), settings=None, run=page, intent=INTENT
    )
    database.get_workflow.assert_awaited_once_with(delivery.WEBSITE_CHANGE_ID)
    call = start.await_args.kwargs
    assert call["workflow"] is website
    assert call["start_idempotency_key"] == adaptation_start_key(page.id)
    assert call["input_payload"] == {
        "source": "content_draft",
        "source_run_id": str(page.id),
        "expected_repository": "acme/site",
    }
