"""Pinned procedures, real disposable Postgres/MCP, and synthetic provider HTTP only."""

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from temporalio.testing import ActivityEnvironment
from test_private_workflows import ACTOR, PATH, activate, authoring_guide, fixture
from test_procedure_publication import publication_db as publication_db
from test_project_connections import CONFIG, SECRET, integrations, payload, public_dns

from tin_lite.activities import TinActivities
from tin_lite.code_services import CodeServiceError, CodeServices
from tin_lite.community import REPOSITORY_ROOT, CheckoutStorage
from tin_lite.e2b_runtime import SandboxProcedureResult
from tin_lite.private_workflows import PrivateWorkflowError, validate_private_definition
from tin_lite.procedure_services import SERVICE_CAPABILITY, SERVICE_PROVIDER, ProcedureServices
from tin_lite.procedures import load_pinned_codex_procedure, validate_procedure_artifact
from tin_lite.publication import read_run_output
from tin_lite.run_service import start_workflow_run
from tin_lite.run_tools import create_run_tools_app
from tin_lite.run_usage import read_run_usage
from tin_lite.workflow_packages import decode_workflow_source

TOKEN = "synthetic-procedure-service-grant"  # noqa: S105
SECOND_SECRET = "synthetic-second-api-key"  # noqa: S105
SECOND_PROVIDER = "custom.api.analytics"


def manifest():
    guide = authoring_guide(settings=SimpleNamespace(), project_id=UUID(int=1))
    value = json.loads(guide["example_files"][PATH])
    definition = value["definition"]
    definition["integration_requirements"] = [
        {"provider_key": "custom.api.crm", "capabilities": ["http.read"], "required": True},
        {"provider_key": SECOND_PROVIDER, "capabilities": ["http.write"], "required": True},
    ]
    definition["procedure"]["services"] = {
        "crm": {"provider_key": "custom.api.crm", "max_calls": 2, "max_response_bytes": 8000},
        "analytics": {"provider_key": SECOND_PROVIDER, "max_calls": 4, "max_response_bytes": 8000},
    }
    return value


def definition(value=None):
    return decode_workflow_source(
        json.dumps(value or manifest()).encode(), definition_path=PATH
    ).definition


def test_procedure_service_contract_uses_the_shared_binding_shape():
    spec = validate_private_definition(definition())
    assert [s.name for s in spec.services] == ["crm", "analytics"]
    assert spec.services[1].capabilities == ("http.write",)


async def test_posthog_example_rejects_an_unbounded_model_report():
    """Offline output-contract fixture; this does not grade analytics correctness."""
    pinned = await load_pinned_codex_procedure(
        storage=CheckoutStorage(REPOSITORY_ROOT),
        repo_id="fixture",
        commit_sha="fixture",
        definition_path="workflow_packages/example.posthog_funnel/workflow.json",
    )
    report = b"# Funnel\nStatus: incomplete. Event definitions did not identify activation.\n"
    validate_procedure_artifact(report, spec=pinned)
    # A readable model answer can still be unusable when it dumps excessive evidence.
    with pytest.raises(ValueError, match="1-32000 bytes"):
        validate_procedure_artifact(report + b"| stage | 4 | 2 | 1 |\n" * 2000, spec=pinned)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["procedure"]["services"]["crm"].update(max_calls=5),
        lambda d: d["procedure"]["services"]["crm"].update(max_response_bytes=64001),
        lambda d: d["procedure"]["services"]["crm"].update(origin="https://other.example"),
        lambda d: d["procedure"]["services"]["crm"].update(provider_key="custom.api.other"),
        lambda d: d["procedure"]["services"].pop("analytics"),
        lambda d: d["integration_requirements"][0].update(required=False),
        lambda d: d["procedure"]["sandbox"].update(profile="browser", egress="open"),
        lambda d: d["procedure"].update(identity={"create": True}),
    ],
)
def test_procedure_services_reject_extra_or_unbounded_authority(change):
    value = manifest()
    change(value["definition"])
    with pytest.raises(ValueError):
        validate_private_definition(definition(value))


@pytest.fixture
async def connected(publication_db, monkeypatch):
    f = await fixture(publication_db)
    service = await integrations(f)
    f.runtime.integrations = service
    await service.custom.save_secrets(
        f.project.id,
        ACTOR,
        [{"name": "ANALYTICS_KEY", "value": SECOND_SECRET, "expected_revision": None}],
    )
    await service.custom.save(
        f.project.id,
        ACTOR,
        SECOND_PROVIDER,
        {
            **CONFIG,
            "origin": "https://analytics.example",
            "auth": "header",
            "header": "X-Api-Key",
            "secret_name": "ANALYTICS_KEY",
            "methods": ["POST"],
        },
        None,
    )
    f.files[PATH] = json.dumps(manifest()).encode()
    f.revision = f.storage.repo.edit(f.files)
    active = await activate(f)
    f.workflow = await f.db.get_workflow(UUID(active["workflow_id"]))
    f.run = await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={"brief": "Inspect an activation funnel"},
    )
    f.run = await f.db.attach_sandbox(
        run_id=f.run.id,
        sandbox_id="fixture-procedure",
        lease_owner="fixture-owner",
        expected_head_sha=f.revision,
        ephemeral_branch=f"procedures/{f.run.id}/1",
        require_active=True,
    )
    await f.db.create_run_tool_grant(
        run_id=f.run.id,
        sandbox_id=f.run.sandbox_id,
        token=TOKEN,
        connection_id=None,
        external_account_id="",
        provider_key=SERVICE_PROVIDER,
        capabilities=(SERVICE_CAPABILITY,),
        ttl_seconds=900,
    )
    f.requests = []
    f.fail_response = False

    def wire(request):
        f.requests.append(request)
        if f.fail_response:
            raise httpx.ReadTimeout("fixture lost response")
        if request.headers["host"] == "api.crm.example":
            assert request.headers["Authorization"] == f"Bearer {SECRET}"
            return httpx.Response(200, stream=httpx.ByteStream(b'{"accounts":[{"id":"one"}]}'))
        assert request.headers["host"] == "analytics.example"
        assert request.headers["X-Api-Key"] == SECOND_SECRET
        assert "Authorization" not in request.headers
        return httpx.Response(200, stream=httpx.ByteStream(b'{"results":[[4,2,1]]}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as provider:
        monkeypatch.setattr(
            "tin_lite.procedure_services.CodeServices",
            partial(CodeServices, client=provider, resolver=public_dns),
        )
        f.gateway = ProcedureServices(
            database=f.db, storage=f.storage, integrations=service, settings=f.settings
        )
        f.server, f.app = create_run_tools_app(settings=f.settings, runtime=lambda: f.runtime)
        try:
            yield f
        finally:
            await service.close()


async def call(f, **changes):
    return await f.gateway.call(token=TOKEN, payload=payload(**changes))


async def test_mcp_uses_two_custom_apis_and_records_replayable_external_usage(connected):
    f = connected
    pinned = await load_pinned_codex_procedure(
        storage=f.storage,
        repo_id=f.project.state_repo_id,
        commit_sha=f.revision,
        definition_path=PATH,
    )
    context = pinned.sandbox_context(inputs={"project_id": str(f.project.id)})
    assert len(context["services"]) == 2
    assert "request_service" in context["prompt"]
    assert SECRET not in json.dumps(context) and SECOND_SECRET not in json.dumps(context)
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
        for name, arguments in [
            (
                "request_service",
                {
                    "service": "crm",
                    "step": "fetch_accounts",
                    "path": "/accounts",
                    "params": {"limit": 3},
                },
            ),
            (
                "request_service",
                {
                    "service": "analytics",
                    "step": "funnel",
                    "method": "POST",
                    "path": "/api/projects/1/query/",
                    "body": {"query": {"kind": "HogQLQuery", "query": "SELECT 4, 2, 1"}},
                },
            ),
            # Both MCP entry points share the same receipt and cannot repeat an effect.
            ("call_service", payload()),
        ]:
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
            result = response.json()["result"]
            assert not result.get("isError"), result
            assert SECRET not in response.text and SECOND_SECRET not in response.text
        denied = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search_gmail", "arguments": {"query": "anything"}},
            },
        )
        assert denied.json()["result"]["isError"]  # No legacy-tool bypass of binding limits.
    assert len(f.requests) == 2
    assert await call(f) == {"status": 200, "data": {"accounts": [{"id": "one"}]}}
    assert len(f.requests) == 2  # Same logical call, through a new gateway instance.
    usage = await read_run_usage(database=f.db, run=f.run)
    assert "connected_provider" in json.dumps(usage)
    rows = await f.db.pool.fetch(
        "SELECT result FROM effect_receipts WHERE operation='external_usage_v1'"
    )
    assert len(rows) == 2
    for row in rows:
        record = json.loads(row["result"])
        assert record["reported_cost_usd"] is None and record["usage"] == {"requests": 1}


async def test_pinned_contract_does_not_follow_latest_workflow_definition(connected):
    f = connected
    changed = manifest()
    changed["definition"]["procedure"]["services"]["crm"]["max_calls"] = 1
    f.storage.repo.edit({PATH: json.dumps(changed).encode()})
    await f.db.pool.execute(
        "UPDATE workflows SET definition=$2::jsonb, current_commit_sha=$3 WHERE id=$1",
        f.workflow.id,
        json.dumps(definition(changed)),
        f.storage.repo.head,
    )
    await call(f)
    await call(f, step="second")
    with pytest.raises(CodeServiceError, match="limit"):
        await call(f, step="third")
    assert len(f.requests) == 2


async def test_uncertain_response_cannot_be_repurchased_by_another_step(connected):
    f = connected
    f.fail_response = True
    with pytest.raises(CodeServiceError, match="not be repeated"):
        await call(f)
    f.fail_response = False
    for step in ["fetch_accounts", "changed_step"]:
        with pytest.raises(CodeServiceError, match="unconfirmed|unresolved"):
            await call(f, step=step)
    assert len(f.requests) == 1


async def test_concurrent_calls_obey_one_shared_limit_and_replay(connected):
    f = connected
    await asyncio.gather(call(f), call(f))
    assert len(f.requests) == 1
    results = await asyncio.gather(
        call(f, step="two"), call(f, step="three"), return_exceptions=True
    )
    assert sum(isinstance(r, CodeServiceError) for r in results) == 1
    assert len(f.requests) == 2


@pytest.mark.parametrize(
    "change",
    ["membership", "stopped", "fence", "sandbox", "expiry", "private_gate", "secret", "connection"],
)
async def test_access_is_rechecked_before_cached_results(connected, change):
    f = connected
    await call(f)
    if change == "membership":
        await f.db.pool.execute("DELETE FROM project_memberships WHERE project_id=$1", f.project.id)
    elif change == "private_gate":
        f.settings.private_workflow_projects = set()
    elif change == "secret":
        await f.db.pool.execute("DELETE FROM project_secrets WHERE project_id=$1", f.project.id)
    elif change == "connection":
        await f.db.pool.execute(
            "UPDATE integration_connections SET status='needs_attention' WHERE project_id=$1",
            f.project.id,
        )
    elif change == "expiry":
        await f.db.pool.execute(
            "UPDATE run_tool_grants SET created_at=now()-interval '2 seconds', "
            "expires_at=now()-interval '1 second'"
        )
    else:
        assignments = {
            "stopped": "status='stopped'",
            "fence": "fencing_token=fencing_token+1",
            "sandbox": "sandbox_id='replacement'",
        }
        await f.db.pool.execute(
            f"UPDATE workflow_runs SET {assignments[change]} WHERE id=$1",  # noqa: S608 — fixed test cases
            f.run.id,
        )
    with pytest.raises((PermissionError, CodeServiceError, PrivateWorkflowError)):
        await call(f)
    assert len(f.requests) == 1


async def test_undeclared_services_changed_steps_and_origin_overrides_fail(connected):
    f = connected
    for request in [
        payload(service="other"),
        payload(arguments={**payload()["arguments"], "path": "https://other.example"}),
        payload(operation="arbitrary.operation"),
    ]:
        with pytest.raises(CodeServiceError):
            await f.gateway.call(token=TOKEN, payload=request)
    assert not f.requests
    await call(f)
    with pytest.raises(CodeServiceError, match="different request"):
        await call(f, arguments={**payload()["arguments"], "path": "/other"})
    with pytest.raises(PermissionError):
        await f.gateway.call(token="wrong-grant", payload=payload())  # noqa: S106


async def test_gateway_completes_with_only_one_pool_slot_available(connected):
    f = connected
    async with AsyncExitStack() as held:
        for _ in range(f.db.pool.get_max_size() - 1):
            await held.enter_async_context(f.db.pool.acquire())
        await asyncio.wait_for(call(f), timeout=5)
        await asyncio.wait_for(call(f), timeout=5)
    assert len(f.requests) == 1


async def test_expiry_while_waiting_for_gateway_lock_denies_dispatch(connected, monkeypatch):
    f = connected
    original = f.db.effect_lock
    waiting = asyncio.Event()
    gate = f"{f.run.id}:code-service-gate"

    @asynccontextmanager
    async def observed_lock(key, operation, **kwargs):
        if key == gate:
            waiting.set()
        async with original(key, operation, **kwargs) as result:
            yield result

    async with original(gate, "code_service_call_v1"):
        monkeypatch.setattr(f.db, "effect_lock", observed_lock)
        pending = asyncio.create_task(call(f))
        await asyncio.wait_for(waiting.wait(), timeout=5)
        await f.db.pool.execute(
            "UPDATE run_tool_grants SET created_at=now()-interval '2 seconds', "
            "expires_at=now()-interval '1 second'"
        )
    with pytest.raises(CodeServiceError, match="permission"):
        await asyncio.wait_for(pending, timeout=5)
    assert not f.requests


async def test_new_fenced_grant_recovers_completed_step_after_sandbox_loss(connected):
    f = connected
    expected = await call(f)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET generation=generation+1, fencing_token=fencing_token+1, "
        "sandbox_id='recovered-sandbox' WHERE id=$1",
        f.run.id,
    )
    token = TOKEN + "-recovery"
    await f.db.create_run_tool_grant(
        run_id=f.run.id,
        sandbox_id="recovered-sandbox",
        token=token,
        connection_id=None,
        external_account_id="",
        provider_key=SERVICE_PROVIDER,
        capabilities=(SERVICE_CAPABILITY,),
        ttl_seconds=900,
    )
    with pytest.raises(PermissionError):
        await call(f)
    assert await f.gateway.call(token=token, payload=payload()) == expected
    assert len(f.requests) == 1


async def test_activity_issues_service_grant_and_publishes_synthetic_report_once(
    connected, monkeypatch
):
    """Real activity/DB/receipt/publication path; no real model or E2B execution."""
    f = connected
    f.settings.switchboard_public_url = "https://localhost"
    f.settings.forward_proxy_url = None
    sandboxes = SimpleNamespace(
        create=AsyncMock(return_value="fixture-procedure"),
        kill=AsyncMock(),
        is_running=AsyncMock(return_value=False),
    )
    activities = TinActivities(
        database=f.db,
        storage=f.storage,
        sandboxes=sandboxes,
        integrations=f.runtime.integrations,
        settings=f.settings,
    )
    monkeypatch.setattr(
        f.storage,
        "sandbox_remotes",
        lambda **_: SimpleNamespace(
            canonical_url="https://storage.example/canonical",
            canonical_auth_header="fixture",
            ephemeral_url="https://storage.example/ephemeral",
            ephemeral_auth_header="fixture",
        ),
    )
    computes = []

    async def synthetic_compute(*, run, run_input, **_):
        computes.append(run_input)
        assert run_input.isolated
        # The procedure knows its own run, as code workflows do through ctx["run_id"].
        assert run_input.context["run"] == {"id": str(run.id)}
        assert f"Tin run ID is {run.id}." in run_input.context["prompt"]
        assert run_input.run_tools_url == "https://localhost/internal/run-tools/mcp"
        grant = await f.db.authorize_run_tool_grant(token=run_input.run_tools_grant)
        assert grant.provider_key == SERVICE_PROVIDER and grant.connection_id is None
        assert SECRET not in json.dumps(run_input.context)
        query = payload(
            service="analytics",
            step="fixture_funnel",
            arguments={
                "method": "POST",
                "path": "/api/projects/1/query/",
                "params": {},
                "body": {"query": {"kind": "HogQLQuery", "query": "SELECT 4, 2, 1"}},
            },
        )
        response = await f.gateway.call(token=run_input.run_tools_grant, payload=query)
        assert response["data"]["results"] == [[4, 2, 1]]
        head = f.storage.repo.head
        content = "# Synthetic funnel\n4 → 2 → 1\nConversion: 25%\n".encode()
        revision = f.storage.repo.edit(
            {run_input.output_path: content}, parent=run.expected_head_sha
        )
        f.storage.branch_revision = revision
        f.storage.repo.head = head
        return SandboxProcedureResult(revision, "Synthetic report", "Ready")

    monkeypatch.setattr(activities, "_run_accounted_procedure", synthetic_compute)
    env = ActivityEnvironment()
    run_id = str(f.run.id)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET lease_owner=NULL, sandbox_id=NULL, lease_active=false "
        "WHERE id=$1",
        f.run.id,
    )
    await env.run(activities.create_codex_procedure_sandbox, run_id)
    await env.run(activities.persist_codex_procedure_artifact, run_id)
    await env.run(activities.persist_codex_procedure_artifact, run_id)
    await env.run(activities.commit_codex_procedure_artifact, run_id)
    await env.run(activities.project_codex_procedure_result, run_id)
    run = await f.db.get_run(f.run.id)
    assert run.status.value == "succeeded"
    assert len(computes) == 1 and len(f.requests) == 1
    assert f.storage.repo.writes == 1
    output = await read_run_output(storage=f.storage, run=run, repo_id=f.project.state_repo_id)
    assert "Conversion: 25%" in output.content.decode()
