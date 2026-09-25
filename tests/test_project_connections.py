"""Real disposable Postgres/MCP and mocked provider/compute; live proof is opt-in."""

import asyncio
import base64
import json
from contextlib import AsyncExitStack
from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from e2b import CommandExitException
from fastapi import FastAPI
from pydantic import SecretStr
from temporalio.testing import ActivityEnvironment
from test_billing import billed as billed
from test_private_workflows import ACTOR, structured
from test_procedure_publication import publication_db as publication_db
from test_workflow_code import setup, start

from tin_lite.code_services import CodeServiceError, CodeServices
from tin_lite.e2b_runtime import SERVICE_ERROR_EXIT, E2BRuntime
from tin_lite.integrations import (
    CredentialCipher,
    IntegrationAuthorizationError,
    IntegrationError,
    IntegrationService,
    ServiceCallRefused,
)
from tin_lite.project_connections import configuration, request_api, request_contract
from tin_lite.project_connections_api import router, setup_user
from tin_lite.run_usage import read_run_usage
from tin_lite.workflow_code import example_files, validate_code_definition

KEY = "custom.connected_report"
PROVIDER = "custom.api.crm"
PATH = f"workflow_packages/{KEY}/workflow.json"
SECRET = "fixture-customer-credential-only-in-trusted-transport"  # noqa: S105
CONFIG = {
    "origin": "https://api.crm.example",
    "auth": "bearer",
    "header": "Authorization",
    "secret_name": "CRM_KEY",
    "methods": ["GET"],
    "idempotency_header": None,
}


def definition():
    body = json.loads(example_files(KEY)[PATH])["definition"]
    body["integration_requirements"] = [
        {"provider_key": PROVIDER, "capabilities": ["http.read"], "required": True}
    ]
    body["code"]["services"] = {
        "crm": {"provider_key": PROVIDER, "max_calls": 2, "max_response_bytes": 8000}
    }
    return body


def payload(**changes):
    return {
        "service": "crm",
        "step": "fetch_accounts",
        "operation": "http.request",
        "arguments": {"method": "GET", "path": "/accounts", "params": {"limit": 3}, "body": None},
        **changes,
    }


async def public_dns(*_args, **_kwargs):
    return [(None, None, None, None, ("93.184.215.14", 443))]


async def integrations(f):
    settings = SimpleNamespace(
        **{
            **vars(f.settings),
            "integration_credential_key": SecretStr(base64.urlsafe_b64encode(b"c" * 32).decode()),
        }
    )
    service = IntegrationService(database=f.db, settings=settings)
    f.runtime.integrations = service
    await service.custom.save_secrets(
        f.project.id, ACTOR, [{"name": "CRM_KEY", "value": SECRET, "expected_revision": None}]
    )
    await service.custom.save(f.project.id, ACTOR, PROVIDER, CONFIG, None)
    return service


async def test_secret_metadata_cas_project_binding_rotation_and_revocation(billed):
    f = billed
    service = await integrations(f)
    try:
        metadata = await service.custom.secrets(f.project.id, ACTOR)
        assert SECRET not in json.dumps(metadata)
        old = metadata[0]
        assert old["revision"] != old["encryption_key_id"]
        with pytest.raises(IntegrationError, match="changed"):
            await service.custom.save_secrets(
                f.project.id,
                ACTOR,
                [
                    {"name": "SECOND", "value": "another-value", "expected_revision": None},
                    {"name": "CRM_KEY", "value": "replacement", "expected_revision": None},
                ],
            )
        assert len(await service.custom.secrets(f.project.id, ACTOR)) == 1  # atomic rollback
        with pytest.raises(LookupError):
            await service.custom.secrets(f.project.id, "user_Nonmember")
        with pytest.raises(IntegrationAuthorizationError):
            await service.custom.open_secret(uuid4(), "CRM_KEY")
        row = await f.db.pool.fetchrow(
            "SELECT * FROM project_secrets WHERE project_id=$1", f.project.id
        )
        assert SECRET.encode() not in row["ciphertext"]
        original = service._cipher
        service._cipher = CredentialCipher(base64.urlsafe_b64encode(b"d" * 32).decode())
        with pytest.raises(IntegrationAuthorizationError, match="key is unavailable"):
            await service.custom.open_secret(f.project.id, "CRM_KEY")
        service._cipher = original
        assert await service.custom.open_secret(f.project.id, "CRM_KEY") == SECRET
        updated = await service.custom.save_secrets(
            f.project.id,
            ACTOR,
            [
                {
                    "name": "CRM_KEY",
                    "value": "rotated-customer-value",
                    "expected_revision": old["revision"],
                },
            ],
        )
        assert updated[0]["revision"] != old["revision"]
        assert updated[0]["encryption_key_id"] == old["encryption_key_id"]
        await service.custom.delete_secret(f.project.id, ACTOR, "CRM_KEY", updated[0]["revision"])
        with pytest.raises(IntegrationAuthorizationError):
            await service.custom.open_secret(f.project.id, "CRM_KEY")
    finally:
        await service.close()


@pytest.mark.parametrize(
    "change",
    [
        {"origin": "http://api.example.com"},
        {"origin": "https://u:p@api.example.com"},
        {"origin": "https://127.0.0.1"},
        {"origin": "https://api.example.com/path"},
        {"origin": "https://api.example.com:8443"},
        {"origin": "https://api.example.com#fragment"},
        {"auth": "header", "header": "Host"},
        {"auth": "header", "header": "Cookie"},
        {"auth": "header", "header": "X-HTTP-Method-Override"},
        {"methods": ["CONNECT"]},
        {"secret_name": "../.env"},
        {"idempotency_header": "Authorization"},
    ],
)
def test_connection_contract_rejects_destination_and_header_authority(change):
    with pytest.raises(ValueError):
        configuration({**CONFIG, **change})


@pytest.mark.parametrize(
    "path",
    [
        "https://other.example/a",
        "//other.example/a",
        "/%2fother",
        "/../a",
        "/%252e%252e/a",
        "/x?auth=a",
        "/x#fragment",
        "/a\\b",
        "/a\nb",
    ],
)
def test_paths_cannot_change_destination(path):
    with pytest.raises(ValueError):
        request_contract({**payload()["arguments"], "path": path})


async def test_http_pins_dns_bounds_response_refuses_redirects_and_credential_echoes():
    connection = SimpleNamespace(configuration=CONFIG)
    seen = []
    mode = "ok"

    def wire(request):
        seen.append(request)
        assert request.url.host == "93.184.215.14"
        assert request.headers["host"] == "api.crm.example"
        assert request.extensions["sni_hostname"] == "api.crm.example"
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.example"})
        data = {"accounts": [{"id": "company-1", "employees": 30}]}
        if mode == "echo":
            data = {"key": SECRET}
        if mode == "large":
            data = {"data": "x" * 9000}
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(data).encode()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        args = dict(maximum=8000, operation_id="stable-op", client=client, resolver=public_dns)
        response = await request_api(connection, SECRET, payload()["arguments"], **args)
        assert response["data"]["accounts"][0]["employees"] == 30
        for mode in ("redirect", "echo", "large"):  # noqa: B007 — captured by wire
            with pytest.raises(IntegrationError):
                await request_api(connection, SECRET, payload()["arguments"], **args)
        for ip in (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "::1",
            "::ffff:127.0.0.1",
            "224.0.0.1",
            "ff02::1",
            "240.0.0.1",
        ):

            async def bad_dns(*a, ip=ip, **k):
                return [(None, None, None, None, (ip, 443))]

            with pytest.raises(IntegrationAuthorizationError):
                await request_api(
                    connection, SECRET, payload()["arguments"], **{**args, "resolver": bad_dns}
                )
        assert len(seen) == 4


@pytest.mark.parametrize(
    "addresses",
    [
        ["93.184.215.14", "2606:4700:4700::1111"],
        ["2606:4700:4700::1111", "93.184.215.14"],
    ],
)
async def test_http_preserves_resolver_preference_and_checks_every_address(addresses):
    seen = []

    async def resolver(*_args, **_kwargs):
        return [(None, None, None, None, (ip, 443)) for ip in addresses]

    def wire(request):
        seen.append(request)
        assert request.url.host == addresses[0]
        assert request.headers["host"] == "api.crm.example"
        assert request.extensions["sni_hostname"] == "api.crm.example"
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        args = dict(maximum=8000, operation_id="stable-op", client=client, resolver=resolver)
        connection = SimpleNamespace(configuration=CONFIG)
        result = await request_api(connection, SECRET, payload()["arguments"], **args)
        assert result == {"status": 200, "data": {"ok": True}}
        assert len(seen) == 1

        addresses.append("127.0.0.1")
        with pytest.raises(IntegrationAuthorizationError, match="not a public address"):
            await request_api(connection, SECRET, payload()["arguments"], **args)
        assert len(seen) == 1


async def test_http_answers_without_json_are_known_outcomes_with_their_status():
    connection = SimpleNamespace(configuration={**CONFIG, "methods": ["GET", "DELETE"]})
    replies = iter(
        [
            httpx.Response(204, stream=httpx.ByteStream(b"")),
            httpx.Response(401, stream=httpx.ByteStream(b"<html>Sign in</html>")),
        ]
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(replies))) as client:
        args = dict(maximum=8000, operation_id="stable-op", client=client, resolver=public_dns)
        delete = {"method": "DELETE", "path": "/accounts/1", "params": {}, "body": None}
        assert await request_api(connection, SECRET, delete, **args) == {
            "status": 204,
            "data": None,
        }
        # The body is withheld, but the provider answered: never an uncertain attempt.
        with pytest.raises(ServiceCallRefused, match="HTTP 401") as refused:
            await request_api(connection, SECRET, payload()["arguments"], **args)
        assert refused.value.code == "invalid_response" and refused.value.status == 401
        assert "Sign in" not in str(refused.value)


def google_spec(provider, capability):
    body = definition()
    body["integration_requirements"] = [
        {"provider_key": provider, "capabilities": [capability], "required": True}
    ]
    body["code"]["services"] = {
        "google": {"provider_key": provider, "max_calls": 2, "max_response_bytes": 16000}
    }
    return validate_code_definition(body)


@pytest.mark.parametrize(
    ("provider", "capability", "operation", "arguments", "reason"),
    [
        (
            "analytics.gsc",
            "search_analytics.read",
            "search_analytics.read",
            {"start_date": "2026-09-10", "end_date": "2026-09-01"},
            "date range",
        ),
        (
            "analytics.gsc",
            "search_analytics.read",
            "search_analytics.read",
            {"start_date": 20260801, "end_date": "2026-08-31"},
            "YYYY-MM-DD",
        ),
        (
            "analytics.gsc",
            "search_analytics.read",
            "search_analytics.read",
            {"start_date": "2026-08-01", "end_date": "2026-08-31", "row_limit": 50000},
            "row limit",
        ),
        (
            "analytics.gsc",
            "search_analytics.read",
            "search_analytics.read",
            {"start_date": "2026-08-01", "end_date": "2026-08-31", "dimensions": [["query"]]},
            "dimensions",
        ),
        (
            "analytics.gsc",
            "search_analytics.read",
            "search_analytics.read",
            {"start_date": "2026-08-01"},
            "",
        ),
        (
            "workspace.google",
            "gmail.messages.read",
            "gmail.messages.search",
            {"query": 7, "max_results": 10},
            "search query",
        ),
        (
            "workspace.google",
            "gmail.messages.read",
            "gmail.messages.search",
            {"query": "from:founder", "max_results": "10"},
            "result limit",
        ),
        (
            "workspace.google",
            "gmail.messages.read",
            "gmail.thread.read",
            {"thread_id": 42},
            "thread ID",
        ),
        (
            "workspace.google",
            "calendar.events.read",
            "calendar.events.list",
            {
                "time_min": "2026-09-02T00:00:00Z",
                "time_max": "2026-09-01T00:00:00Z",
                "query": "",
                "max_results": 10,
            },
            "Calendar range",
        ),
        (
            "workspace.google",
            "calendar.events.read",
            "calendar.events.list",
            {"time_min": 1, "time_max": "2026-09-01T00:00:00Z", "query": "", "max_results": 10},
            "ISO 8601",
        ),
    ],
)
async def test_google_service_values_are_contract_errors_before_any_receipt(
    provider, capability, operation, arguments, reason
):
    # No database, run or connection: a malformed value must be refused before any of them.
    services = CodeServices(database=None, integrations=None, authorize=None)
    with pytest.raises(CodeServiceError, match="declared contract") as refused:
        await services.call(
            conn=None,
            run=None,
            workflow=None,
            spec=google_spec(provider, capability),
            payload={
                "service": "google",
                "step": "read",
                "operation": operation,
                "arguments": arguments,
            },
        )
    assert reason in str(refused.value)


def code_bridge(monkeypatch):
    """A fake sandbox whose package makes `requests` bridge calls, then exits with `exit`."""
    replies, outcomes = [], []
    package = SimpleNamespace(requests=2, reraises=False, exit=None, result=b'{"ok": true}')

    async def command(cmd, **kwargs):
        if kwargs.get("on_stdout") is None:
            return SimpleNamespace(stdout="")
        for _ in range(package.requests):
            await kwargs["on_stdout"]("TIN_MODEL_REQUEST\n")
            if package.reraises and "error" in replies[-1]:
                # The runner reports only that the service error escaped, by its exit status.
                package.exit = SERVICE_ERROR_EXIT
                break
        if package.exit is not None:
            raise CommandExitException(stderr="", stdout="", exit_code=package.exit, error=None)
        return SimpleNamespace(stdout="")

    async def read(path, **kwargs):
        if path.endswith("request.json"):
            return b'{"kind": "service"}'
        if isinstance(package.result, Exception):
            raise package.result
        return package.result

    async def write(path, data, **kwargs):
        if path.endswith("response.tmp"):
            replies.append(json.loads(data))

    async def service(_request):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sandbox = SimpleNamespace(
        sandbox_id="fixture-code-service",
        commands=SimpleNamespace(run=command),
        files=SimpleNamespace(read=read, write=write, rename=AsyncMock()),
        kill=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "tin_lite.e2b_runtime.AsyncSandbox.connect", AsyncMock(return_value=sandbox)
    )
    runtime = E2BRuntime(
        api_key="synthetic", template="default", timeout_seconds=60, egress_allow_hosts=()
    )
    run = dict(sandbox_id="fixture-code-service", packet={"timeout_seconds": 5}, model_call=service)
    return runtime, run, replies, outcomes, package


async def test_code_bridge_hands_service_errors_to_authored_code(monkeypatch):
    runtime, run, replies, outcomes, package = code_bridge(monkeypatch)
    # A settled refusal reaches the package, which asks again under a new step.
    outcomes[:] = [CodeServiceError("Stripe rate-limited this read (fixture)."), {"status": 200}]
    assert await runtime.run_code_and_kill(**run) == b'{"ok": true}'
    assert replies == [
        {"error": "Stripe rate-limited this read (fixture)."},
        {"result": {"status": 200}},
    ]
    # A package that lets the error escape still fails with Tin's own reason, not a generic one.
    replies.clear()
    package.requests, package.reraises = 1, True
    outcomes[:] = [CodeServiceError("Service response unavailable or invalid; fixture.")]
    with pytest.raises(CodeServiceError, match="Service response unavailable"):
        await runtime.run_code_and_kill(**run)
    assert replies == [{"error": "Service response unavailable or invalid; fixture."}]
    # A run that lost its authority stops without handing anything back to authored code.
    replies.clear()
    package.exit = None
    outcomes[:] = [CodeServiceError("The run no longer has permission.", fatal=True)]
    with pytest.raises(CodeServiceError, match="no longer has permission"):
        await runtime.run_code_and_kill(**run)
    assert replies == []


@pytest.mark.parametrize(
    "failure",
    [
        # The package caught the refusal, then failed or ran out of time on its own.
        dict(exit=1),
        # E2B could not return the result after the package had handled the refusal.
        dict(result=RuntimeError("fixture sandbox read failed")),
    ],
)
async def test_code_bridge_keeps_a_handled_service_error_out_of_later_failures(
    monkeypatch, failure
):
    runtime, run, replies, outcomes, package = code_bridge(monkeypatch)
    package.requests = 1
    for name, value in failure.items():
        setattr(package, name, value)
    outcomes[:] = [CodeServiceError("Stripe rate-limited this read (fixture).")]
    with pytest.raises(RuntimeError, match="Code workflow failed") as failed:
        await runtime.run_code_and_kill(**run)
    assert not isinstance(failed.value, CodeServiceError)
    assert replies == [{"error": "Stripe rate-limited this read (fixture)."}]


def test_code_runner_and_bridge_agree_on_the_escaped_service_error_status():
    # code_runner imports Unix-only modules, so read its constant without importing it.
    source = Path(find_spec("tin_lite.code_runner").origin).read_text(encoding="utf-8")
    assert f"\nSERVICE_ERROR_EXIT = {SERVICE_ERROR_EXIT}\n" in source


class ServiceCompute:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        return "fixture-code-service"

    async def kill(self, sandbox_id):
        pass

    async def run_code_and_kill(self, *, packet, model_call, **kwargs):
        assert SECRET not in json.dumps(packet)
        result = await model_call({"kind": "service", "payload": payload()})
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("fixture worker loss after durable response")
        return json.dumps(
            {
                "path": "reports/custom/ORDER_REPORT.md",
                "content": "# Connected report\nAccounts: " + str(len(result["data"]["accounts"])),
            }
        ).encode()


async def prepared(f, monkeypatch, compute=None):
    service = await integrations(f)
    server, common, code = await setup(f, monkeypatch, compute=compute or ServiceCompute())
    common._integrations = code.services.integrations = service
    files = example_files(KEY)
    manifest = json.loads(files[PATH])
    manifest["definition"] = definition()
    files[PATH] = json.dumps(manifest)
    revision = f.storage.repo.edit({p: raw.encode() for p, raw in files.items()})
    selection = {"project_id": str(f.project.id), "path": PATH, "revision": revision}
    validated = structured(await server.call_tool("validate_workflow_package", selection))
    assert validated["valid"], validated
    active = structured(
        await server.call_tool(
            "activate_workflow_package",
            {**selection, "expected_revision": None, "request_id": str(uuid4())},
        )
    )
    response = await start(f, server, active)
    return service, server, code, response["id"]


async def test_auth_failure_marks_current_connection_and_rotation_restores_readiness(
    billed, monkeypatch
):
    f = billed
    service, _, code, run_id = await prepared(f, monkeypatch)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(401, stream=httpx.ByteStream(b'{"error":"expired"}'))
            )
        ) as client:
            code.services.client, code.services.resolver = client, public_dns
            with pytest.raises(RuntimeError, match="worker loss"):
                await ActivityEnvironment().run(code.execute, run_id)
        connection = await f.db.get_integration_connection(
            project_id=f.project.id, provider_key=PROVIDER
        )
        assert connection.status == "needs_attention"
        assert connection.last_error_code == "authentication_failed"
        prior = (await service.custom.secrets(f.project.id, ACTOR))[0]
        await service.custom.save_secrets(
            f.project.id,
            ACTOR,
            [
                {
                    "name": "CRM_KEY",
                    "value": "rotated-fixture-only",
                    "expected_revision": prior["revision"],
                }
            ],
        )
        connection = await f.db.get_integration_connection(
            project_id=f.project.id, provider_key=PROVIDER
        )
        assert connection.status == "connected" and not connection.last_error_code
        assert connection.configuration["access_verified"] is False
    finally:
        await service.close()


async def test_mcp_fixture_recovers_service_response_at_zero_credits_and_reports_external_usage(
    billed, monkeypatch
):
    f = billed
    service, server, code, run_id = await prepared(f, monkeypatch)
    calls = []

    def wire(request):
        calls.append(request)
        return httpx.Response(
            200, stream=httpx.ByteStream(b'{"accounts":[{"id":"company-1","employees":30}]}')
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        code.services.client, code.services.resolver = client, public_dns
        env = ActivityEnvironment()
        with pytest.raises(RuntimeError, match="worker loss"):
            await env.run(code.execute, run_id)
        progress = await f.db.get_run(UUID(run_id))
        assert progress.progress_summary == "Completed 1 managed step · fetch_accounts"
        assert progress.progress_percent is None
        # Rotation is permitted without repurchasing the completed response.
        prior = (await service.custom.secrets(f.project.id, ACTOR))[0]
        await service.custom.save_secrets(
            f.project.id,
            ACTOR,
            [
                {
                    "name": "CRM_KEY",
                    "value": "replacement-fixture-key",
                    "expected_revision": prior["revision"],
                }
            ],
        )
        await env.run(code.execute, run_id)
        await env.run(code.publish, run_id)
        await code.project(run_id)
        assert len(calls) == 1
        run = await f.db.get_run(UUID(run_id))
        assert run.status.value == "succeeded"
        output = structured(await server.call_tool("read_run_output", {"run_id": run_id}))
        assert "Accounts: 1" in str(output)
        usage = await read_run_usage(database=f.db, run=run)
        external = [o for o in usage["own"]["observations"] if o["kind"] == "connected_api"]
        assert len(external) == 1 and external[0]["usage"]["requests"] == 1
        assert external[0]["provider_reported_cost_usd"] is None
        assert external[0]["tin_credit_deduction"] is False
        assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0
        assert await f.db.pool.fetchval("SELECT balance_nanos FROM billing_accounts") == 0
        assert SECRET not in str(await f.db.pool.fetch("SELECT result FROM effect_receipts"))
    await service.close()


async def test_uncertain_service_blocks_repurchase_even_with_new_step(billed, monkeypatch):
    f = billed
    service, _, code, run_id = await prepared(f, monkeypatch)
    calls = []

    def wire(request):
        calls.append(request)
        raise httpx.ReadTimeout("fixture provider with sensitive diagnostic", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        code.services.client, code.services.resolver = client, public_dns
        env = ActivityEnvironment()
        with pytest.raises(Exception, match="will not be repeated"):
            await env.run(code.execute, run_id)
        run, workflow, _, spec, _ = await code.selected(run_id)
        async with f.db.pool.acquire() as conn:
            for step in ("fetch_accounts", "new_step"):
                with pytest.raises(CodeServiceError, match="unconfirmed|unresolved"):
                    await code.services.call(
                        conn=conn, run=run, workflow=workflow, spec=spec, payload=payload(step=step)
                    )
        assert len(calls) == 1
    await service.close()


async def test_oversized_service_response_is_named_settled_and_does_not_block_later_steps(
    billed, monkeypatch
):
    f = billed
    service, _, code, run_id = await prepared(f, monkeypatch)
    calls = []

    def wire(request):
        calls.append(request)
        if len(calls) == 1:
            body = json.dumps({"accounts": [{"id": f"company-{i}"} for i in range(1000)]})
            return httpx.Response(200, stream=httpx.ByteStream(body.encode()))
        return httpx.Response(200, stream=httpx.ByteStream(b'{"accounts":[]}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        code.services.client, code.services.resolver = client, public_dns
        with pytest.raises(Exception, match=r"max_response_bytes \(8000\)"):
            await ActivityEnvironment().run(code.execute, run_id)
        run, workflow, _, spec, _ = await code.selected(run_id)
        async with f.db.pool.acquire() as conn:
            # The same step replays its settled outcome without another provider call.
            with pytest.raises(CodeServiceError, match=r"max_response_bytes \(8000\)"):
                await code.services.call(
                    conn=conn, run=run, workflow=workflow, spec=spec, payload=payload()
                )
            assert len(calls) == 1
            # A later step is not wedged behind it, and the refused call still counts.
            result = await code.services.call(
                conn=conn, run=run, workflow=workflow, spec=spec, payload=payload(step="smaller")
            )
            assert result == {"status": 200, "data": {"accounts": []}}
            with pytest.raises(CodeServiceError, match="limit"):
                await code.services.call(
                    conn=conn, run=run, workflow=workflow, spec=spec, payload=payload(step="third")
                )
        assert len(calls) == 2
        rows = await f.db.pool.fetch(
            """SELECT operation, status, result FROM effect_receipts
               WHERE operation IN ('code_service_call_v1', 'external_usage_v1')
               ORDER BY created_at"""
        )
        assert all(row["status"] == "completed" for row in rows)
        refused = json.loads(rows[0]["result"])
        assert refused["error"] == "response_too_large" and "response" not in refused
        usage = await read_run_usage(database=f.db, run=await f.db.get_run(UUID(run_id)))
        external = [o for o in usage["own"]["observations"] if o["kind"] == "connected_api"]
        assert [o["outcome"] for o in external] == ["response_received", "response_received"]
    await service.close()


async def test_non_json_service_answer_is_settled_and_marks_a_rejected_credential(
    billed, monkeypatch
):
    f = billed
    service, _, code, run_id = await prepared(f, monkeypatch)
    calls = []

    def wire(request):
        calls.append(request)
        return httpx.Response(401, stream=httpx.ByteStream(b"<html>Sign in again</html>"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        code.services.client, code.services.resolver = client, public_dns
        with pytest.raises(Exception, match="HTTP 401 with a body that is not JSON"):
            await ActivityEnvironment().run(code.execute, run_id)
        connection = await f.db.get_integration_connection(
            project_id=f.project.id, provider_key=PROVIDER
        )
        assert connection.status == "needs_attention"
        assert connection.last_error_code == "authentication_failed"
        assert len(calls) == 1
        # The answered step is settled with Tin's message, never left as an unconfirmed attempt.
        rows = await f.db.pool.fetch(
            """SELECT status, result FROM effect_receipts
               WHERE operation IN ('code_service_call_v1', 'external_usage_v1')"""
        )
        assert len(rows) == 2 and all(row["status"] == "completed" for row in rows)
        assert "invalid_response" in [json.loads(row["result"]).get("error") for row in rows]
        assert "Sign in" not in str([row["result"] for row in rows])
    await service.close()


async def test_search_console_service_forwards_paging_filters_and_bound(billed, monkeypatch):
    from dataclasses import replace

    f = billed
    service, _, code, run_id = await prepared(f, monkeypatch)
    service._settings.google_oauth_client_id = "test-client"
    service._settings.google_oauth_client_secret = SecretStr("fixture-google-client")
    await f.db.upsert_integration_connection(
        project_id=f.project.id,
        provider_key="analytics.gsc",
        external_account_id="fixture-google-user",
        external_account_label="fixture",
        configuration={"selected_site_url": "https://fixture.example"},
        credential_ciphertext=None,
        credential_key_version=None,
        connected_by_clerk_user_id=ACTOR,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(b'{"accounts":[]}'))
        )
    ) as client:
        code.services.client, code.services.resolver = client, public_dns
        with pytest.raises(RuntimeError, match="worker loss"):
            await ActivityEnvironment().run(code.execute, run_id)
    run, workflow, _, _, _ = await code.selected(run_id)
    body = definition()
    body["code"]["services"] = {
        "gsc": {"provider_key": "analytics.gsc", "max_calls": 2, "max_response_bytes": 16000}
    }
    body["integration_requirements"] = [
        {
            "provider_key": "analytics.gsc",
            "capabilities": ["search_analytics.read"],
            "required": True,
        }
    ]
    seen = []

    async def analytics(**kwargs):
        seen.append(kwargs)
        return {"rows": [], "truncated": True, "next_start_row": 200}

    monkeypatch.setattr(service, "search_console_analytics", analytics)
    filters = [{"dimension": "page", "operator": "contains", "expression": "/blog/"}]
    arguments = {
        "start_date": "2026-08-01",
        "end_date": "2026-08-31",
        "dimensions": ["query", "page"],
        "row_limit": 500,
        "start_row": 200,
        "dimension_filters": filters,
    }
    selected = {
        "service": "gsc",
        "step": "queries",
        "operation": "search_analytics.read",
        "arguments": arguments,
    }
    changed = replace(workflow, definition=body)
    spec = validate_code_definition(body)
    async with f.db.pool.acquire() as conn:
        result = await code.services.call(
            conn=conn, run=run, workflow=changed, spec=spec, payload=selected
        )
        assert result["next_start_row"] == 200
        assert seen[0]["max_response_bytes"] == 16000
        assert seen[0]["start_row"] == 200 and seen[0]["dimension_filters"] == filters
        with pytest.raises(CodeServiceError, match="declared contract"):
            await code.services.call(
                conn=conn,
                run=run,
                workflow=changed,
                spec=spec,
                payload={
                    **selected,
                    "step": "extra",
                    "arguments": {**arguments, "aggregation_type": "byPage"},
                },
            )
        # A bad value is the author's contract error, never an unresolved step blocking the run.
        with pytest.raises(CodeServiceError, match="declared contract: Search Console date"):
            await code.services.call(
                conn=conn,
                run=run,
                workflow=changed,
                spec=spec,
                payload={
                    **selected,
                    "step": "reversed",
                    "arguments": {**arguments, "end_date": "2026-07-01"},
                },
            )
        assert len(seen) == 1
        result = await code.services.call(
            conn=conn, run=run, workflow=changed, spec=spec, payload={**selected, "step": "next"}
        )
        assert result["next_start_row"] == 200 and len(seen) == 2
    await service.close()


async def test_write_only_api_never_echoes_values_in_validation_or_metadata(billed):
    f = billed
    service = await integrations(f)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = f.runtime
    app.dependency_overrides[setup_user] = lambda: SimpleNamespace(clerk_user_id=ACTOR)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        url = f"/api/projects/{f.project.id}/connections/secrets"
        response = await client.put(
            url,
            json={
                "entries": [{"name": "BAD", "value": "sensitive\nvalue", "expected_revision": None}]
            },
        )
        assert response.status_code == 422 and "sensitive" not in response.text
        response = await client.get(url)
        assert response.status_code == 200 and SECRET not in response.text
        response = await client.get(f"/api/projects/{uuid4()}/connections/secrets")
        assert response.status_code == 404
    await service.close()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["integration_requirements"][0].update(required=False),
        lambda d: d["integration_requirements"][0].update(capabilities=["gmail.messages.send"]),
        lambda d: d["code"]["services"]["crm"].update(max_calls=9),
        lambda d: d["code"]["services"]["crm"].update(max_response_bytes=64001),
        lambda d: d["code"]["services"]["crm"].update(provider_key="custom.api.other"),
        lambda d: d["code"]["services"]["crm"].update(secret=SECRET),
    ],
)
def test_workflow_bindings_do_not_expand_connection_authority(mutate):
    value = deepcopy(definition())
    mutate(value)
    with pytest.raises(ValueError):
        validate_code_definition(value)


async def test_rewrap_changes_key_identity_without_changing_credential_revision(billed):
    from tin_lite.project_connections import rewrap_project_secrets

    service = await integrations(billed)
    original = service._cipher
    target = CredentialCipher(base64.urlsafe_b64encode(b"e" * 32).decode())
    before = (await service.custom.secrets(billed.project.id, ACTOR))[0]
    await rewrap_project_secrets(billed.db, billed.project.id, source=original, target=target)
    service._cipher = target
    assert await service.custom.open_secret(billed.project.id, "CRM_KEY") == SECRET
    after = (await service.custom.secrets(billed.project.id, ACTOR))[0]
    assert after["revision"] == before["revision"]
    assert after["encryption_key_id"] != before["encryption_key_id"]
    with pytest.raises(IntegrationAuthorizationError):
        await rewrap_project_secrets(billed.db, billed.project.id, source=original, target=target)
    await rewrap_project_secrets(billed.db, billed.project.id, source=target, target=original)
    service._cipher = original
    assert await service.custom.open_secret(billed.project.id, "CRM_KEY") == SECRET
    await service.close()


async def test_existing_adapter_and_custom_capabilities_recheck_before_cached_results(
    billed, monkeypatch
):
    from dataclasses import replace

    from tin_lite.integrations import ProviderOption

    f = billed
    service, _, code, run_id = await prepared(f, monkeypatch)
    service._settings.google_oauth_client_id = "test-client"
    service._settings.google_oauth_client_secret = SecretStr("fixture-google-client")
    await f.db.upsert_integration_connection(
        project_id=f.project.id,
        provider_key="analytics.gsc",
        external_account_id="fixture-google-user",
        external_account_label="fixture",
        configuration={"selected_site_url": "https://fixture.example"},
        credential_ciphertext=None,
        credential_key_version=None,
        connected_by_clerk_user_id=ACTOR,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(b'{"accounts":[]}'))
        )
    ) as client:
        code.services.client, code.services.resolver = client, public_dns
        with pytest.raises(RuntimeError, match="worker loss"):
            await ActivityEnvironment().run(code.execute, run_id)
        run, workflow, _, spec, _ = await code.selected(run_id)
        body = definition()
        body["code"]["services"] = {
            "gsc": {"provider_key": "analytics.gsc", "max_calls": 1, "max_response_bytes": 8000}
        }
        body["integration_requirements"] = [
            {"provider_key": "analytics.gsc", "capabilities": ["sites.list"], "required": True}
        ]
        calls = []

        async def sites(*, project_id):
            assert project_id == run.project_id
            calls.append(project_id)
            return [ProviderOption("https://fixture.example", "Fixture")]

        monkeypatch.setattr(service, "google_sites", sites)
        selected = {"service": "gsc", "step": "sites", "operation": "sites.list", "arguments": {}}
        async with f.db.pool.acquire() as conn:
            for _ in range(2):
                result = await code.services.call(
                    conn=conn,
                    run=run,
                    workflow=replace(workflow, definition=body),
                    spec=validate_code_definition(body),
                    payload=selected,
                )
                assert result["sites"][0]["label"] == "Fixture"
            assert len(calls) == 1
            with pytest.raises(CodeServiceError, match="limit"):
                await code.services.call(
                    conn=conn,
                    run=run,
                    workflow=replace(workflow, definition=body),
                    spec=validate_code_definition(body),
                    payload={**selected, "step": "second"},
                )
            with pytest.raises(CodeServiceError, match="different request"):
                await code.services.call(
                    conn=conn,
                    run=run,
                    workflow=workflow,
                    spec=spec,
                    payload=payload(
                        arguments={"method": "GET", "path": "/other", "params": {}, "body": None}
                    ),
                )
            with pytest.raises(CodeServiceError, match="declared contract"):
                await code.services.call(
                    conn=conn,
                    run=run,
                    workflow=workflow,
                    spec=spec,
                    payload=payload(
                        step="write",
                        arguments={"method": "POST", "path": "/accounts", "params": {}, "body": {}},
                    ),
                )
            await service.disconnect(project_id=f.project.id, provider_key=PROVIDER)
            with pytest.raises(CodeServiceError, match="unavailable"):
                await code.services.call(
                    conn=conn, run=run, workflow=workflow, spec=spec, payload=payload()
                )
    await service.close()


def test_local_import_is_literal_selected_and_does_not_execute(tmp_path):
    from tin_lite.secret_import import parse_env

    marker = tmp_path / "not-executed"
    source = f"export CRM_KEY='$(touch {marker})' # literal\nUNSELECTED=private\n"
    parsed = parse_env(source)
    assert parsed["CRM_KEY"] == f"$(touch {marker})"
    assert not marker.exists()
    for source in ("KEY=one\nKEY=two", "lower=bad", "KEY='unclosed", "KEY=\x00"):
        with pytest.raises(ValueError):
            parse_env(source)


async def test_write_only_connection_does_not_advertise_read_readiness(billed):
    from tin_lite.integrations import IntegrationRequirement

    service = await integrations(billed)
    existing = await billed.db.get_integration_connection(
        project_id=billed.project.id, provider_key=PROVIDER
    )
    updated = await service.custom.save(
        billed.project.id,
        ACTOR,
        PROVIDER,
        {**CONFIG, "methods": ["POST"]},
        existing.configuration["revision"],
    )
    assert updated.configuration["granted_capabilities"] == ["http.write"]
    with pytest.raises(IntegrationAuthorizationError):
        await service.ensure_requirements(
            project_id=billed.project.id,
            requirements=(IntegrationRequirement(PROVIDER, ("http.read",)),),
        )
    await service.ensure_requirements(
        project_id=billed.project.id,
        requirements=(IntegrationRequirement(PROVIDER, ("http.write",)),),
    )
    await service.close()


async def test_competing_connection_updates_complete_with_one_free_pool_slot(billed):
    service = await integrations(billed)
    old = await billed.db.get_integration_connection(
        project_id=billed.project.id, provider_key=PROVIDER
    )
    try:
        async with AsyncExitStack() as stack:
            for _ in range(billed.db.pool.get_max_size() - 1):
                await stack.enter_async_context(billed.db.pool.acquire())
            async with asyncio.timeout(3):
                results = await asyncio.gather(
                    *(
                        service.custom.save(
                            billed.project.id,
                            ACTOR,
                            PROVIDER,
                            CONFIG,
                            old.configuration["revision"],
                        )
                        for _ in range(3)
                    ),
                    return_exceptions=True,
                )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, IntegrationError) for result in results) == 2
    finally:
        await service.close()


@pytest.mark.parametrize("refresh_status", [200, 401])
async def test_code_services_reuse_activity_connection_when_pool_is_full(
    billed, monkeypatch, refresh_status
):
    from dataclasses import replace

    f = billed
    service, _, code, run_id = await prepared(f, monkeypatch)
    requests = []

    def wire(request):
        requests.append(request.url.host)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(refresh_status, json={"access_token": "fixture-access"})
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"siteEntry": [{"siteUrl": "https://fixture.example"}]})
        return httpx.Response(200, stream=httpx.ByteStream(b'{"accounts":[]}'))

    service._settings.google_oauth_client_id = "test-client"
    service._settings.google_oauth_client_secret = SecretStr("fixture-google-client")
    await f.db.upsert_integration_connection(
        project_id=f.project.id,
        provider_key="analytics.gsc",
        external_account_id="fixture-google-user",
        external_account_label="fixture",
        configuration={},
        credential_ciphertext=service._cipher.encrypt(
            "fixture-refresh", context=f"credential:{f.project.id}:analytics.gsc"
        ),
        credential_key_version=service._cipher.version,
        connected_by_clerk_user_id=ACTOR,
    )
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
            code.services.client, code.services.resolver = client, public_dns
            await service._client.aclose()
            service._client = client
            with pytest.raises(RuntimeError, match="worker loss"):
                await ActivityEnvironment().run(code.execute, run_id)
            run, workflow, _, spec, _ = await code.selected(run_id)
            body = definition()
            body["code"]["services"] = {
                "gsc": {"provider_key": "analytics.gsc", "max_calls": 1, "max_response_bytes": 8000}
            }
            body["integration_requirements"] = [
                {"provider_key": "analytics.gsc", "capabilities": ["sites.list"], "required": True}
            ]
            async with AsyncExitStack() as stack:
                for _ in range(f.db.pool.get_max_size() - 1):
                    await stack.enter_async_context(f.db.pool.acquire())
                async with f.db.pool.acquire() as conn, asyncio.timeout(3):
                    # Recheck credentials for a cached result, then make a fresh custom request.
                    for step in ("fetch_accounts", "second"):
                        result = await code.services.call(
                            conn=conn,
                            run=run,
                            workflow=workflow,
                            spec=spec,
                            payload=payload(step=step),
                        )
                        assert result["data"] == {"accounts": []}
                    call = code.services.call(
                        conn=conn,
                        run=run,
                        workflow=replace(workflow, definition=body),
                        spec=validate_code_definition(body),
                        payload={
                            "service": "gsc",
                            "step": "sites",
                            "operation": "sites.list",
                            "arguments": {},
                        },
                    )
                    if refresh_status == 401:
                        with pytest.raises(CodeServiceError, match="not be repeated"):
                            await call
                    else:
                        assert (await call)["sites"][0]["id"] == "https://fixture.example"
            assert requests.count("oauth2.googleapis.com") == 1
            connection = await f.db.get_integration_connection(
                project_id=f.project.id, provider_key="analytics.gsc"
            )
            assert connection.status == (
                "connected" if refresh_status == 200 else "needs_attention"
            )
            if refresh_status == 200:
                assert requests.count("www.googleapis.com") == 1
                assert (
                    await f.db.pool.fetchval("SELECT count(*) FROM integration_call_receipts") == 1
                )
    finally:
        await service.close()
