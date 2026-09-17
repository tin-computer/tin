from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from tin_lite import analytics
from tin_lite.api import router
from tin_lite.auth import AuthContext, ClerkAuth, _display_name, _primary_email, require_user


def build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_test",
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
    )
    return app


async def post(app: FastAPI, body: dict | None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/events/install-prompt-copied", json=body)


@pytest.mark.asyncio
async def test_copying_the_install_command_records_who_and_where(monkeypatch) -> None:
    client = analytics.configure("phc_test", "https://ph.example", source="test")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(client, "capture", lambda event, **kw: calls.append((event, kw)))

    response = await post(build_app(), {"agent": "codex"})

    assert response.status_code == 204
    event, kw = calls[-1]
    assert event == "install_prompt_copied" and kw["distinct_id"] == "user_test"
    assert kw["properties"] == {
        "clerk_user_id": "user_test",
        "surface": "app",
        "agent": "codex",
    }
    analytics.configure(None, "")


@pytest.mark.asyncio
async def test_an_unknown_agent_is_dropped_rather_than_recorded(monkeypatch) -> None:
    client = analytics.configure("phc_test", "https://ph.example", source="test")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(client, "capture", lambda event, **kw: calls.append((event, kw)))

    assert (await post(build_app(), {"agent": "<script>"})).status_code == 204
    assert calls[-1][1]["properties"]["agent"] is None

    assert (await post(build_app(), None)).status_code == 204
    assert calls[-1][1]["properties"]["agent"] is None
    analytics.configure(None, "")


def test_display_name_prefers_a_real_name_then_the_username() -> None:
    assert _display_name({"first_name": "Alfie", "last_name": "Marsh"}) == "Alfie Marsh"
    assert _display_name({"first_name": "Alfie", "last_name": None}) == "Alfie"
    assert _display_name({"first_name": None, "username": "alfie"}) == "alfie"
    assert _display_name({}) is None


def test_primary_email_prefers_the_address_clerk_marks_primary() -> None:
    payload = {
        "primary_email_address_id": "idb",
        "email_addresses": [
            {"id": "ida", "email_address": "old@b.co"},
            {"id": "idb", "email_address": "alfie@rocketgtm.co"},
        ],
    }
    assert _primary_email(payload) == "alfie@rocketgtm.co"
    # No primary marked: fall back to the first address on the account.
    assert _primary_email({"email_addresses": [{"id": "x", "email_address": "a@b.co"}]}) == "a@b.co"
    assert _primary_email({"email_addresses": []}) is None


@pytest.mark.asyncio
async def test_identity_returns_both_fields_in_one_clerk_call() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "first_name": "Alfie",
                "last_name": "Marsh",
                "primary_email_address_id": "id1",
                "email_addresses": [{"id": "id1", "email_address": "alfie@rocketgtm.co"}],
            },
        )

    auth = ClerkAuth.__new__(ClerkAuth)
    auth._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.clerk.com/v1"
    )
    assert await auth.identity("user_1") == {
        "email": "alfie@rocketgtm.co",
        "name": "Alfie Marsh",
    }
    assert seen == ["/v1/users/user_1"]
    await auth._client.aclose()


@pytest.mark.asyncio
async def test_identity_is_anonymous_when_clerk_fails() -> None:
    auth = ClerkAuth.__new__(ClerkAuth)
    auth._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500)),
        base_url="https://api.clerk.com/v1",
    )
    assert await auth.identity("user_1") == {"email": None, "name": None}
    assert await auth.primary_email("user_1") is None
    await auth._client.aclose()
