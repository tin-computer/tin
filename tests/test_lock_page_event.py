"""The locked dashboard reports two moments: it was seen, and the install line was copied."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from tin_lite import analytics
from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user


def lock_page_app(monkeypatch) -> tuple[FastAPI, list[dict]]:
    captured: list[dict] = []

    def capture(event, *, distinct_id, properties=None, project_id=None):
        captured.append(
            {
                "event": event,
                "distinct_id": distinct_id,
                "properties": properties,
                "project_id": project_id,
            }
        )

    monkeypatch.setattr(analytics, "capture", capture)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_browser",
        token_type="session_token",  # noqa: S106
    )
    return app, captured


@pytest.mark.asyncio
async def test_lock_page_events_carry_the_project_and_agent(monkeypatch) -> None:
    app, captured = lock_page_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        viewed = await client.post(
            "/api/events/lock-page", json={"action": "viewed", "project_id": "project-1"}
        )
        copied = await client.post(
            "/api/events/lock-page",
            json={"action": "install_copied", "agent": "codex", "project_id": "project-1"},
        )
    assert viewed.status_code == 204 and copied.status_code == 204
    assert [item["event"] for item in captured] == ["lock_page_viewed", "lock_page_install_copied"]
    assert captured[0]["project_id"] == "project-1"
    assert captured[0]["properties"] == {"clerk_user_id": "user_browser", "agent": None}
    assert captured[1]["properties"]["agent"] == "codex"


@pytest.mark.asyncio
async def test_lock_page_steps_it_watched_are_events(monkeypatch) -> None:
    app, captured = lock_page_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for action in ("agent_connected", "setup_started", "plan_ready"):
            response = await client.post(
                "/api/events/lock-page",
                json={"action": action, "agent": "claude", "project_id": "project-1"},
            )
            assert response.status_code == 204
    assert [item["event"] for item in captured] == [
        "lock_page_agent_connected",
        "lock_page_setup_started",
        "lock_page_plan_ready",
    ]
    assert all(item["properties"]["agent"] == "claude" for item in captured)


@pytest.mark.asyncio
async def test_lock_page_event_rejects_free_text(monkeypatch) -> None:
    app, captured = lock_page_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unknown = await client.post("/api/events/lock-page", json={"action": "rated it 5 stars"})
        odd_agent = await client.post(
            "/api/events/lock-page", json={"action": "install_copied", "agent": "<script>"}
        )
    assert unknown.status_code == 400
    assert odd_agent.status_code == 204
    assert captured == [
        {
            "event": "lock_page_install_copied",
            "distinct_id": "user_browser",
            "properties": {"clerk_user_id": "user_browser", "agent": None},
            "project_id": None,
        }
    ]
