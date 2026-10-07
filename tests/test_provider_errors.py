"""A provider's own error reaches the run that made the call: status, type, code and message.

Debugging a PostHog funnel took four rounds because the HogQL error (windowFunnel wanting
toDateTime) never reached the run: procedures saw "Error executing tool call_service" and
code saw Tin's sentence alone. These tests pin the provider's words, redacted and cut, on
both paths, and pin the generic wrapper for failures that are not the provider's.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_billing import billed as billed
from test_posthog_connection import ACCESS, SIGNUPS, PostHog
from test_private_workflows import ACTOR
from test_procedure_publication import publication_db as publication_db
from test_procedure_services import TOKEN
from test_procedure_services import connected as connected
from test_project_connections import code_bridge, payload

from tin_lite.code_services import CodeServiceError
from tin_lite.integrations import (
    POSTHOG_PROVIDER,
    IntegrationProviderError,
    IntegrationUpstreamError,
    ServiceCallRefused,
    _provider_json,
)
from tin_lite.provider_errors import MESSAGE_LIMIT, ProviderErrorDetail, detail, explain
from tin_lite.redaction import redact_message
from tin_lite.stripe_connection import StripeReader

STRIPE_KEY = "rk_test_" + "k" * 40
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl"
HOGQL = (
    "Illegal type DateTime64(6, 'UTC') of first argument of aggregate function windowFunnel, "
    "must be DateTime; wrap it in toDateTime(timestamp)."
)


# ---------------------------------------------------------------- shapes and redaction


@pytest.mark.parametrize(
    "body,expected",
    [
        (  # PostHog and other DRF APIs
            {"type": "validation_error", "code": "invalid_input", "detail": HOGQL, "attr": None},
            ("validation_error", "invalid_input", HOGQL),
        ),
        (  # Stripe
            {
                "error": {
                    "type": "invalid_request_error",
                    "code": "resource_missing",
                    "message": "No such price: 'price_1'",
                    "param": "price",
                }
            },
            ("invalid_request_error", "resource_missing", "No such price: 'price_1'"),
        ),
        (  # Google
            {
                "error": {
                    "code": 400,
                    "message": "Invalid value at 'start_date'",
                    "status": "INVALID_ARGUMENT",
                    "errors": [{"reason": "badRequest", "message": "ignored"}],
                }
            },
            ("INVALID_ARGUMENT", "badRequest", "Invalid value at 'start_date'"),
        ),
        (  # OAuth token endpoints
            {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
            (None, "invalid_grant", "Token has been expired or revoked."),
        ),
        (None, (None, None, None)),
        ("<html>bad gateway</html>", (None, None, None)),
    ],
)
def test_detail_reads_the_shapes_providers_send(body, expected):
    said = detail("Provider", 400, body)
    assert (said.type, said.code, said.message) == expected
    assert said.status == 400


def test_messages_are_redacted_flattened_and_cut():
    body = {
        "detail": (
            f"Bad query\nfor ada.lovelace+test@example.com using {ACCESS} and Bearer "
            f"{'b' * 24}, token={'t' * 20}, {JWT}, sk-{'s' * 20}; "
            "LIKE '%@example.com' stays." + " x" * 1000
        )
    }
    said = detail("PostHog", 400, body, secrets=(ACCESS,))
    assert "\n" not in said.message and len(said.message) <= MESSAGE_LIMIT
    assert said.message.endswith("…")
    for leaked in (ACCESS, "ada.lovelace", "b" * 24, "t" * 20, JWT, "s" * 20):
        assert leaked not in said.message
    assert "[redacted]@example.com" in said.message
    assert "LIKE '%@example.com' stays." in said.message
    # Redaction is idempotent, so a receipt read back is unchanged.
    assert ProviderErrorDetail.from_dict(said.to_dict()) == said
    assert redact_message(said.message) == said.message


def test_explain_adds_what_the_provider_said_and_nothing_when_it_said_nothing():
    said = ProviderErrorDetail("PostHog", 400, "validation_error", "invalid_input", HOGQL)
    assert explain("PostHog rejected the query (HTTP 400).", said) == (
        f"PostHog rejected the query (HTTP 400). PostHog said: validation_error/invalid_input: "
        f"{HOGQL}"
    )
    status_only = ProviderErrorDetail("Google", 502)
    assert explain("Google could not complete the request (502)", status_only) == (
        "Google could not complete the request (502)"
    )
    assert explain("Tin's own sentence.", None) == "Tin's own sentence."


async def test_stripe_refusals_keep_stripes_words_without_the_key():
    def stripe(request):
        assert request.headers["authorization"] == f"Bearer {STRIPE_KEY}"
        error = {
            "type": "invalid_request_error",
            "code": "parameter_invalid_integer",
            "message": f"Invalid integer: abc (key {STRIPE_KEY})",
            "param": "limit",
        }
        return httpx.Response(400, stream=httpx.ByteStream(json.dumps({"error": error}).encode()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(stripe)) as client:
        with pytest.raises(ServiceCallRefused) as refused:
            await StripeReader(client, STRIPE_KEY).get("/v1/prices", {"limit": "abc"})
    assert str(refused.value) == "Stripe could not complete the read (HTTP 400)."
    said = refused.value.provider_error
    assert (said.status, said.type, said.code) == (
        400,
        "invalid_request_error",
        "parameter_invalid_integer",
    )
    assert said.message == "Invalid integer: abc (key [redacted])"


def test_google_error_status_keeps_googles_words_and_tins_message():
    response = httpx.Response(
        400,
        json={
            "error": {
                "code": 400,
                "message": "Invalid value at 'start_date' (TYPE_STRING), \"2026-13-01\"",
                "status": "INVALID_ARGUMENT",
            }
        },
    )
    with pytest.raises(IntegrationProviderError) as refused:
        _provider_json(response, provider="Google Search Console")
    # Founder-facing routes keep Tin's message unchanged.
    assert str(refused.value) == "Google Search Console could not complete the request (400)"
    assert refused.value.provider_error.type == "INVALID_ARGUMENT"
    assert "start_date" in refused.value.provider_error.message


# ---------------------------------------------------------------- code workflows


async def connected_posthog(f, service, api):
    service._settings.posthog_oauth_enabled = True
    service._settings.switchboard_public_url = "https://lite.tin.test"
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    started = await service.start_connect(
        project_id=f.project.id, provider_key=POSTHOG_PROVIDER, clerk_user_id=ACTOR
    )
    query = parse_qs(urlsplit(started.authorization_url).query)
    api.challenge = query["code_challenge"][0]
    await service.posthog.complete(state=query["state"][0], code="good-code", clerk_user_id=ACTOR)


class HogQLError(PostHog):
    """PostHog that answers the query endpoint with `error` (a 400 body) while it is set."""

    error: dict | None = None

    def __call__(self, request):
        if self.error is not None and request.url.path.endswith("/query/"):
            self.requests.append(request)
            return self.reply(400, self.error)
        return super().__call__(request)


async def test_code_workflow_sees_the_hogql_error_redacted_and_on_its_receipt(billed, monkeypatch):
    from temporalio.testing import ActivityEnvironment
    from test_project_connections import definition, prepared, public_dns

    from tin_lite.workflow_code import validate_code_definition

    f = billed
    service, _, code, run_id = await prepared(f, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(b'{"accounts":[]}'))
        )
    ) as crm:
        code.services.client, code.services.resolver = crm, public_dns
        with pytest.raises(RuntimeError, match="worker loss"):
            await ActivityEnvironment().run(code.execute, run_id)
    api = HogQLError(region="us")
    original = service._client
    try:
        await connected_posthog(f, service, api)
        run, workflow, _, _, _ = await code.selected(run_id)
        body = definition()
        body["integration_requirements"] = [
            {"provider_key": POSTHOG_PROVIDER, "capabilities": ["query.read"], "required": True}
        ]
        body["code"]["services"] = {
            "ph": {"provider_key": POSTHOG_PROVIDER, "max_calls": 4, "max_response_bytes": 4000}
        }
        changed, spec = replace(workflow, definition=body), validate_code_definition(body)

        def call(conn, step):
            return code.services.call(
                conn=conn,
                run=run,
                workflow=changed,
                spec=spec,
                payload={
                    "service": "ph",
                    "step": step,
                    "operation": "query.hogql",
                    "arguments": {"name": "signups_by_day", "query": SIGNUPS},
                },
            )

        api.error = {
            "type": "validation_error",
            "code": "invalid_input",
            "detail": f"{HOGQL} Filter: person.properties.email = 'ada@example.com'; {ACCESS}",
            "attr": None,
        }
        async with f.db.pool.acquire() as conn:
            api.requests.clear()
            with pytest.raises(CodeServiceError) as refused:
                await call(conn, "funnel")
            message = str(refused.value)
            assert message.startswith(
                "PostHog rejected the query (HTTP 400). PostHog said: "
                "validation_error/invalid_input: Illegal type DateTime64"
            )
            assert "toDateTime(timestamp)" in message
            assert ACCESS not in message and "ada@" not in message
            assert "[redacted]@example.com" in message
            assert refused.value.code == "query_error"
            said = refused.value.provider_error
            assert said["provider"] == "PostHog" and said["status"] == 400
            assert said["type"] == "validation_error" and said["code"] == "invalid_input"
            # The same step replays the settled refusal without asking PostHog again.
            with pytest.raises(CodeServiceError) as replayed:
                await call(conn, "funnel")
            assert str(replayed.value) == message
            assert replayed.value.provider_error == said
            assert len(api.requests) == 1
            # A refusal is a known outcome: the fixed query runs under a new step.
            api.error = None
            fixed = await call(conn, "funnel_fixed")
            assert fixed["rows"]

            # A response Tin cannot read is not the provider's error: it stays generic.
            def garbled(*_args, **_kwargs):
                raise IntegrationUpstreamError("PostHog returned an invalid query response")

            monkeypatch.setattr("tin_lite.posthog_connection.project_query", garbled)
            with pytest.raises(CodeServiceError) as uncertain:
                await call(conn, "garbled")
            assert str(uncertain.value).startswith("Service response unavailable or invalid")
            assert uncertain.value.provider_error is None
            assert "PostHog" not in str(uncertain.value)
        receipt = json.loads(
            await f.db.pool.fetchval(
                """SELECT result FROM effect_receipts
                   WHERE operation='code_service_call_v1' AND result->>'step'='funnel'"""
            )
        )
        assert receipt["error"] == "query_error"
        assert receipt["message"] == message
        assert receipt["provider_error"] == said
        dumped = json.dumps(
            [
                dict(r)
                for r in await f.db.pool.fetch(
                    "SELECT result FROM effect_receipts WHERE operation='code_service_call_v1'"
                )
            ],
            default=str,
        )
        assert ACCESS not in dumped and "ada@" not in dumped
    finally:
        await service._client.aclose()
        service._client = original
        await service.close()


async def test_code_bridge_hands_the_provider_error_to_authored_code(monkeypatch):
    runtime, run, replies, outcomes, package = code_bridge(monkeypatch)
    package.requests = 1
    said = ProviderErrorDetail("PostHog", 400, "validation_error", "invalid_input", HOGQL)
    message = explain("PostHog rejected the query (HTTP 400).", said)
    outcomes[:] = [CodeServiceError(message, code="query_error", provider_error=said.to_dict())]
    assert await runtime.run_code_and_kill(**run) == b'{"ok": true}'
    assert replies == [
        {"error": message, "code": "query_error", "provider_error": said.to_dict()},
    ]


def test_authored_code_reads_the_provider_error_from_the_value_error(monkeypatch):
    from tin_lite import code_runner

    said = {"provider": "PostHog", "status": 400, "code": "invalid_input", "message": HOGQL}
    reply = {"error": f"PostHog rejected the query (HTTP 400). PostHog said: {HOGQL}"}

    class Socket:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def settimeout(self, _seconds):
            pass

        def connect(self, _path):
            pass

        def sendall(self, _raw):
            pass

        def makefile(self, _mode):
            import io

            return io.BytesIO(json.dumps(reply).encode() + b"\n")

    monkeypatch.setattr(code_runner.socket, "socket", Socket)
    services = code_runner.Context.services
    reply.update(code="query_error", provider_error=said)
    with pytest.raises(ValueError) as refused:
        services._call({"kind": "service", "payload": {}})
    assert isinstance(refused.value, code_runner.ServiceError)
    assert str(refused.value) == reply["error"]
    assert refused.value.code == "query_error" and refused.value.provider_error == said
    # A gateway from before this change sends only the message; the attributes stay None.
    del reply["code"], reply["provider_error"]
    with pytest.raises(code_runner.ServiceError) as older:
        services._call({"kind": "service", "payload": {}})
    assert older.value.code is None and older.value.provider_error is None


# ---------------------------------------------------------------- procedures


@asynccontextmanager
async def mcp(f):
    """One lifespan of the run-tools app; yields `call(name, arguments)` for tools/call."""
    async with (
        f.app.router.lifespan_context(f.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=f.app),
            base_url="https://tin.test",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/json, text/event-stream",
            },
        ) as client,
    ):

        async def call(name, arguments):
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            assert response.status_code == 200, response.text
            return response.json()["result"]

        yield call


def tool_error(result):
    assert result["isError"], result
    text = result["content"][0]["text"]
    prefix = "Error executing tool call_service: "
    assert text.startswith(prefix), text
    return json.loads(text.removeprefix(prefix))


async def test_procedure_call_service_returns_the_provider_error(connected, monkeypatch):
    f = connected
    said = detail(
        "CRM",
        422,
        {
            "error": {
                "type": "invalid_request",
                "code": "unknown_field",
                "message": f"Unknown field 'plan' for owner bob@crm.example ({ACCESS})",
            }
        },
        secrets=(ACCESS,),
    )

    async def refuse(*_args, **_kwargs):
        raise ServiceCallRefused(
            "The CRM refused the read (HTTP 422).", code="provider_error", provider_error=said
        )

    monkeypatch.setattr("tin_lite.code_services.request_api", refuse)
    async with mcp(f) as call:
        error = tool_error(await call("call_service", payload()))
        # The same step replays the settled refusal through either tool.
        assert tool_error(await call("call_service", payload())) == error
    assert error["code"] == "provider_error"
    assert error["message"].startswith(
        "The CRM refused the read (HTTP 422). CRM said: invalid_request/unknown_field: "
        "Unknown field 'plan'"
    )
    assert error["provider_error"] == said.to_dict()
    assert "bob@" not in json.dumps(error) and ACCESS not in json.dumps(error)
    receipt = json.loads(
        await f.db.pool.fetchval(
            "SELECT result FROM effect_receipts WHERE operation='code_service_call_v1'"
        )
    )
    assert receipt["provider_error"] == said.to_dict()
    assert receipt["message"] == error["message"]


async def test_procedure_sees_tins_own_refusals_too(connected):
    async with mcp(connected) as call:
        error = tool_error(await call("call_service", payload(service="nope")))
    assert error == {
        "code": "service_error",
        "message": "The service request differs from its declared contract.",
    }


async def test_procedure_internal_failures_stay_generic(connected, monkeypatch):
    async def crash(*_args, **_kwargs):
        raise RuntimeError("asyncpg: password=hunter2 connection reset")

    monkeypatch.setattr("tin_lite.procedure_services.ProcedureServices.call", crash)
    async with mcp(connected) as call:
        result = await call("call_service", payload())
    assert result["isError"]
    assert result["content"][0]["text"] == "Error executing tool call_service"
    assert "hunter2" not in json.dumps(result)
