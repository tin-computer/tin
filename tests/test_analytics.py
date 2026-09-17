from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from tin_lite import analytics
from tin_lite.analytics import PAYLOAD_CAP, Analytics, clip, redact
from tin_lite.mcp_server import analytics_middleware


def test_redact_hides_secret_looking_keys_recursively() -> None:
    value = {"api_key": "x", "nested": [{"Authorization": "y", "name": "ok"}], "token_count": 3}
    assert redact(value) == {
        "api_key": "[redacted]",
        "nested": [{"Authorization": "[redacted]", "name": "ok"}],
        "token_count": "[redacted]",
    }


def test_clip_keeps_small_values_and_cuts_large_ones() -> None:
    small, facts = clip({"a": 1})
    assert small == {"a": 1} and facts == {"bytes": 8, "truncated": False}
    big, facts = clip("x" * (PAYLOAD_CAP + 10))
    assert isinstance(big, str) and len(big) == PAYLOAD_CAP
    assert facts["truncated"] is True and facts["bytes"] == PAYLOAD_CAP + 12


def test_disabled_client_drops_everything() -> None:
    client = Analytics(None)
    client.capture("x", distinct_id="p")
    assert not client.enabled and client.sent == 0


@pytest.mark.asyncio
async def test_capture_batches_to_posthog() -> None:
    posted: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"status": 1})

    client = Analytics("phc_test", "https://ph.example", source="test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.capture(
        "mcp_tool_called",
        distinct_id="11111111-1111-4111-8111-111111111111",
        project_id="11111111-1111-4111-8111-111111111111",
        properties={"tool": "get_run", "arguments": {"token": "secret"}},
    )
    await client.flush()
    assert client.sent == 1 and posted[0]["api_key"] == "phc_test"
    event = posted[0]["batch"][0]
    assert event["event"] == "mcp_tool_called"
    assert event["properties"]["$groups"] == {"project": "11111111-1111-4111-8111-111111111111"}
    assert "arguments" not in event["properties"]
    assert event["properties"]["source"] == "test"
    await client.aclose()


@pytest.mark.asyncio
async def test_middleware_records_tool_calls_and_session_start(monkeypatch) -> None:
    client = analytics.configure("phc_test", "https://ph.example", source="test")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        client,
        "capture",
        lambda event, **kw: calls.append((event, kw)),
    )
    monkeypatch.setattr(
        "tin_lite.mcp_server.get_access_token",
        lambda: SimpleNamespace(subject="user_1", client_id="client_1"),
    )
    session = SimpleNamespace(
        client_params=SimpleNamespace(client_info=SimpleNamespace(name="claude-code", version="2"))
    )
    result = SimpleNamespace(structured_content={"status": "succeeded"}, content=[], is_error=False)

    async def call_next(ctx):
        return result

    ctx = SimpleNamespace(
        method="tools/call",
        params={"name": "get_run", "arguments": {"project_id": "p1", "run_id": "r1"}},
        session=session,
    )
    assert await analytics_middleware(ctx, call_next) is result
    event, kw = calls[-1]
    assert event == "mcp_tool_called" and kw["distinct_id"] == "p1"
    props = kw["properties"]
    assert props["tool"] == "get_run" and "result" not in props and "arguments" not in props
    assert props["client_name"] == "claude-code" and props["clerk_user_id"] == "user_1"
    assert props["is_error"] is False and props["error_type"] is None

    async def failing(ctx):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await analytics_middleware(ctx, failing)
    assert calls[-1][1]["properties"]["error_type"] == "RuntimeError"

    init = SimpleNamespace(
        method="initialize",
        params={"clientInfo": {"name": "codex", "version": "1"}, "protocolVersion": "2025-06-18"},
        session=session,
    )
    await analytics_middleware(init, call_next)
    event, kw = calls[-1]
    assert event == "mcp_session_started" and kw["distinct_id"] == "user_1"
    assert kw["properties"]["client_name"] == "codex"

    other = SimpleNamespace(method="tools/list", params=None, session=session)
    before = len(calls)
    await analytics_middleware(other, call_next)
    assert len(calls) == before
    analytics.configure(None, "")


def test_large_structured_values_are_redacted_before_clipping():
    clipped, size = clip({"password": "SYNTHETIC_SECRET", "notes": "x" * (PAYLOAD_CAP + 10)})
    assert size["truncated"]
    assert "SYNTHETIC_SECRET" not in json.dumps(clipped)


@pytest.mark.asyncio
async def test_analytics_excludes_tool_content_errors_and_activity_prose(monkeypatch):
    client = Analytics("synthetic", "https://unused.invalid")
    monkeypatch.setattr(client, "_ensure_flusher", lambda: None)
    monkeypatch.setattr(analytics, "_current", client)
    monkeypatch.setattr(
        "tin_lite.mcp_server.get_access_token",
        lambda: SimpleNamespace(subject="member", client_id="client"),
    )
    ctx = SimpleNamespace(
        method="tools/call",
        params={
            "name": "read_project_file",
            "arguments": {
                "project_id": "project",
                "content": "PRIVATE_REQUEST",
                "password": "PRIVATE_SECRET",
                "notes": "PRIVATE_NOTE" * 7000,
            },
        },
        session=None,
    )

    async def content(_):
        return SimpleNamespace(structured_content={"content": "PRIVATE_RESPONSE"}, is_error=False)

    async def failure(_):
        raise RuntimeError("PRIVATE_ERROR")

    await analytics_middleware(ctx, content)
    with pytest.raises(RuntimeError):
        await analytics_middleware(ctx, failure)
    client.capture(
        "activity_ready",
        distinct_id="project",
        properties={
            "run_id": "run",
            "summary": "PRIVATE_SUMMARY",
            "details": {"text": "PRIVATE_DETAILS"},
            "view": "PRIVATE_PLAN",
            "connections": {"note": "PRIVATE_REASON"},
        },
    )
    assert "PRIVATE_" not in json.dumps(list(client._queue))
    assert client._queue[0]["properties"]["result_bytes"] > 0
    assert client._queue[-1]["properties"]["run_id"] == "run"


def test_identity_fields_reach_the_event_and_the_person() -> None:
    client = Analytics("phc_test")
    client.capture(
        "tin_user_created",
        distinct_id="user_1",
        properties={"email": "a@b.co", "name": "Alfie Marsh", "via": "mcp", "note": "secret"},
        set_person=True,
    )
    props = client._queue[-1]["properties"]
    assert props["email"] == "a@b.co" and props["name"] == "Alfie Marsh" and props["via"] == "mcp"
    assert props["$set"] == {"email": "a@b.co", "name": "Alfie Marsh"}
    # The allowlist still fails closed on anything it was not told about.
    assert "note" not in props


def test_person_is_not_set_without_asking_or_without_identity() -> None:
    client = Analytics("phc_test")
    client.capture("tin_user_created", distinct_id="u", properties={"email": "a@b.co"})
    assert "$set" not in client._queue[-1]["properties"]
    client.capture("tin_user_created", distinct_id="u", properties={"via": "web"}, set_person=True)
    assert "$set" not in client._queue[-1]["properties"]


@pytest.mark.asyncio
async def test_new_user_event_carries_identity_fetched_in_the_background() -> None:
    client = analytics.configure("phc_test", "https://ph.example", source="test")
    auth = SimpleNamespace(identity=_answer({"email": "alfie@rocketgtm.co", "name": "Alfie Marsh"}))
    analytics.capture_new_user(auth=auth, clerk_user_id="user_1", via="mcp")
    await _settle()
    event = client._queue[-1]
    assert event["event"] == "tin_user_created" and event["distinct_id"] == "user_1"
    props = event["properties"]
    assert props["email"] == "alfie@rocketgtm.co" and props["name"] == "Alfie Marsh"
    assert props["via"] == "mcp" and props["$set"]["email"] == "alfie@rocketgtm.co"
    analytics.configure(None, "")


@pytest.mark.asyncio
async def test_new_user_event_still_fires_when_clerk_is_unreachable() -> None:
    client = analytics.configure("phc_test", "https://ph.example", source="test")

    async def broken(_clerk_user_id):
        raise httpx.ConnectError("no route")

    analytics.capture_new_user(auth=SimpleNamespace(identity=broken), clerk_user_id="u", via="web")
    await _settle()
    props = client._queue[-1]["properties"]
    assert props["via"] == "web" and props["email"] is None and props["name"] is None
    analytics.configure(None, "")


@pytest.mark.asyncio
async def test_handshake_separates_a_first_connection_from_a_reconnect(monkeypatch) -> None:
    client = analytics.configure("phc_test", "https://ph.example", source="test")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(client, "capture", lambda event, **kw: calls.append((event, kw)))
    monkeypatch.setattr(
        "tin_lite.mcp_server.get_access_token",
        lambda: SimpleNamespace(subject="user_1", client_id="client_1"),
    )

    async def call_next(ctx):
        return None

    init = SimpleNamespace(method="initialize", params={"clientInfo": {}}, session=None)

    await analytics_middleware(init, call_next, seen_before=_answer(False))
    assert calls[-1][1]["properties"]["first_session"] is True

    await analytics_middleware(init, call_next, seen_before=_answer(True))
    assert calls[-1][1]["properties"]["first_session"] is False

    # A broken lookup leaves the question unanswered rather than failing the handshake.
    async def broken(_user_id):
        raise RuntimeError("db down")

    await analytics_middleware(init, call_next, seen_before=broken)
    assert calls[-1][1]["properties"]["first_session"] is None

    await analytics_middleware(init, call_next)
    assert calls[-1][1]["properties"]["first_session"] is None
    analytics.configure(None, "")


def _answer(value):
    async def answer(*_args, **_kwargs):
        return value

    return answer


async def _settle() -> None:
    """Let the background identity lookup and its capture finish."""
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)
