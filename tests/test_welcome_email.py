from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from tin_lite.auth import ClerkAuth
from tin_lite.welcome_email import send_welcome_email


class EmailAuth:
    def __init__(self, email: str | None) -> None:
        self.email = email

    async def primary_email(self, clerk_user_id: str) -> str | None:
        assert clerk_user_id == "user_New1"
        return self.email


@pytest.mark.asyncio
@pytest.mark.parametrize("app_url", [None, "https://app.tin.test"])
async def test_welcome_email_goes_through_resend_with_the_prompt_and_both_pages(app_url) -> None:
    sent: list[httpx.Request] = []

    def resend(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"id": "email_1"})

    settings = SimpleNamespace(
        resend_api_key=SecretStr("re_test"),
        welcome_email_from="Tin <hello@tin.test>",
        welcome_email_reply_to="Emre <emre@tin.test>",
        switchboard_public_url="https://tin.test",
        app_url=app_url,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(resend)) as client:
        ok = await send_welcome_email(
            settings=settings,
            auth=EmailAuth("founder@example.com"),
            clerk_user_id="user_New1",
            client=client,
        )

    assert ok is True
    [request] = sent
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["authorization"] == "Bearer re_test"
    body = json.loads(request.content)
    assert body["from"] == "Tin <hello@tin.test>"
    assert body["to"] == ["founder@example.com"]
    assert body["subject"] == "Your agent is getting to work on your project"
    assert body["reply_to"] == ["Emre <emre@tin.test>"]
    assert "I'm Emre, one of Tin's co-founders" in body["html"]
    assert "hit reply and tell me. It comes straight to me." in body["text"]
    assert f"{app_url or 'https://tin.test'}/system" in body["html"]
    assert f"{app_url or 'https://tin.test'}/decisions" in body["text"]


@pytest.mark.asyncio
async def test_welcome_email_is_off_without_a_key_and_skips_unknown_addresses() -> None:
    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(never)) as client:
        off = await send_welcome_email(
            settings=SimpleNamespace(resend_api_key=None),
            auth=EmailAuth("founder@example.com"),
            clerk_user_id="user_New1",
            client=client,
        )
        no_address = await send_welcome_email(
            settings=SimpleNamespace(
                resend_api_key=SecretStr("re_test"),
                welcome_email_from="Tin <hello@tin.test>",
                switchboard_public_url="https://tin.test",
            ),
            auth=EmailAuth(None),
            clerk_user_id="user_New1",
            client=client,
        )

    assert off is False
    assert no_address is False


@pytest.mark.asyncio
async def test_primary_email_comes_from_clerks_user_record() -> None:
    def clerk(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.clerk.com/v1/users/user_New1"
        assert request.headers["authorization"] == "Bearer test-secret"
        return httpx.Response(
            200,
            json={
                "first_name": "Alex",
                "last_name": "River",
                "primary_email_address_id": "idn_2",
                "email_addresses": [
                    {"id": "idn_1", "email_address": "old@example.com"},
                    {"id": "idn_2", "email_address": "founder@example.com"},
                ],
            },
        )

    auth = ClerkAuth(
        SimpleNamespace(
            clerk_secret_key=SecretStr("test-secret"),
            clerk_jwt_key=None,
            clerk_authorized_parties=("https://tin.test",),
            mcp_oauth_client_ids=frozenset(),
            switchboard_public_url="https://tin.test",
            clerk_frontend_api_url="https://clerk.tin.test",
        )
    )
    auth._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.clerk.com/v1",
        transport=httpx.MockTransport(clerk),
        headers={"Authorization": "Bearer test-secret"},
    )
    try:
        assert await auth.primary_email("user_New1") == "founder@example.com"
        assert await auth.member_profile("user_New1") == {
            "display_name": "Alex River",
            "email": "founder@example.com",
        }
    finally:
        await auth.close()
