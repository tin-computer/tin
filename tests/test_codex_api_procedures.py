"""The ordinary-procedure extension must not upgrade historical pilot contracts."""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from test_billing import ACTOR, finish
from test_billing import billed as billed
from test_codex_api import BODY, post, result_event, setup_relay
from test_codex_api_billing import facts, paid_relay
from test_codex_isolation import load_sandbox_module
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.codex_api import (
    CONTRACT,
    DIAGRAM_CONTRACT,
    PROCEDURE_CONTRACT,
    PROCEDURE_CONTRACT_V2,
    USAGE,
    attempt_key,
    execution_profile,
    select_contract,
)
from tin_lite.codex_api_pricing import RATE_CARD, api_terms, price_response
from tin_lite.codex_api_relay import request_body, web_usage
from tin_lite.procedures import SandboxProfile
from tin_lite.run_usage import read_run_usage


@pytest.mark.parametrize("profile", ["default", "isolated", "browser", "studio"])
async def test_profile_selection_is_scoped_and_old_oauth_stays_oauth(publication_db, profile):
    _, _, run, _ = await activity_fixture(publication_db)
    sandbox = SandboxProfile(
        profile=profile,
        timeout_seconds=1200,
        egress="open" if profile in {"browser", "studio"} else "fenced",
    )
    settings = SimpleNamespace(codex_api_projects={run.project_id}, luna_api_key="synthetic")
    async with publication_db.pool.acquire() as conn:
        selected = await select_contract(
            db=publication_db,
            conn=conn,
            run=run,
            procedure=SimpleNamespace(sandbox=sandbox),
            settings=settings,
        )
    # Unpinned API runs of every supported profile, isolated included, use v3.
    assert selected == PROCEDURE_CONTRACT
    effective = execution_profile(sandbox, selected)
    assert effective.timeout_seconds == 1200
    assert effective.profile == (
        "isolated"
        if profile in {"default", "isolated"}
        else "browser_api"
        if profile == "browser"
        else "studio_api"
    )
    with pytest.raises(ValueError, match="retired"):
        execution_profile(sandbox, {"mode": "chatgpt_oauth"})
    assert sandbox.profile == profile  # Never mutate the workflow definition.


@pytest.mark.parametrize("contract", [PROCEDURE_CONTRACT_V2, PROCEDURE_CONTRACT, DIAGRAM_CONTRACT])
def test_procedure_context_config_does_not_change_v1(tmp_path, contract):
    import tomllib

    module = load_sandbox_module("codex_api_config")
    path = tmp_path / "config.toml"
    path.write_text('model="gpt-6-sol"\n[mcp_servers.test]\ncommand="true"\n')
    module.configure(
        path,
        {
            "TIN_PROCEDURE_ISOLATED": "1",
            "TIN_CODEX_API_URL": "https://tin.test/internal/codex-api/run/v1",
            "TIN_CODEX_API_GRANT": "synthetic",
            "TIN_CODEX_API_CONTRACT": json.dumps(contract),
        },
    )
    config = tomllib.loads(path.read_text())
    assert config["model_context_window"] == 128000
    assert config["model_auto_compact_token_limit"] == 96000
    assert "model_context_window" not in config["mcp_servers"]["test"]
    assert config["model_providers"]["tin_api"]["request_max_retries"] == 0


async def test_default_activity_pins_effective_image_without_changing_definition(publication_db):
    activities, _, run, _ = await activity_fixture(publication_db)
    activities._settings = SimpleNamespace(codex_api_projects={run.project_id}, luna_api_key="test")
    create = AsyncMock(return_value="new-api-sandbox")
    activities._sandboxes.create = create
    await publication_db.pool.execute(
        "UPDATE workflow_runs SET lease_owner=NULL, sandbox_id=NULL, lease_active=false "
        "WHERE id=$1",
        run.id,
    )
    await activities.create_codex_procedure_sandbox(str(run.id))
    assert create.call_args.kwargs["profile"].isolated
    receipt = await publication_db.get_effect(f"{run.id}:procedure_sandbox_create")
    assert receipt.result["codex_auth"] == PROCEDURE_CONTRACT
    assert receipt.result["sandbox_profile"] == "isolated"
    activities._settings.codex_api_projects = set()
    await activities.create_codex_procedure_sandbox(str(run.id))
    assert create.await_count == 1
    _, spec = await activities._pinned_codex_procedure(run.id)
    assert spec.sandbox.profile == "default"


async def test_procedure_preflight_failure_does_not_fall_back_to_oauth(publication_db):
    activities, _, run, _ = await activity_fixture(publication_db)
    activities._settings = SimpleNamespace(codex_api_projects={run.project_id}, luna_api_key="test")
    create = AsyncMock(return_value="new-api-sandbox")
    activities._sandboxes.create = create
    await publication_db.pool.execute(
        "UPDATE workflow_runs SET lease_owner=NULL, sandbox_id=NULL, lease_active=false "
        "WHERE id=$1",
        run.id,
    )
    failures = [ConnectionError("code.storage read failed")] * 2
    check = activities._check_private_attempt

    async def flaky_check(*args):
        if failures:
            raise failures.pop()
        return await check(*args)

    activities._check_private_attempt = flaky_check
    # Transient failures before the auth pin is saved are not historical OAuth receipts.
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await activities.create_codex_procedure_sandbox(str(run.id))
    create.assert_not_awaited()
    await activities.create_codex_procedure_sandbox(str(run.id))
    assert create.call_args.kwargs["profile"].isolated
    receipt = await publication_db.get_effect(f"{run.id}:procedure_sandbox_create")
    assert receipt.status == "completed"
    assert receipt.result["codex_auth"] == PROCEDURE_CONTRACT


async def test_unmarked_procedure_create_receipt_keeps_oauth(publication_db):
    activities, _, run, _ = await activity_fixture(publication_db)
    activities._settings = SimpleNamespace(codex_api_projects={run.project_id}, luna_api_key="test")
    create = AsyncMock(return_value="new-api-sandbox")
    activities._sandboxes.create = create
    key = f"{run.id}:procedure_sandbox_create"
    async with publication_db.pool.acquire() as conn:
        await publication_db.start_effect(
            conn, execution_key=key, operation="procedure_sandbox_create"
        )
    # Historical OAuth pins are never upgraded or allowed to create an unsafe runner.
    for _ in range(2):
        with pytest.raises(ValueError, match="retired"):
            await activities.create_codex_procedure_sandbox(str(run.id))
    assert (await publication_db.get_effect(key)).result["codex_auth"] == {"mode": "chatgpt_oauth"}
    create.assert_not_awaited()


def test_only_fixed_tin_verifiers_run_after_freeze_without_grants(monkeypatch):
    bridge = load_sandbox_module("procedure_app_server")
    command = "/opt/tin-lite/metadata-venv/bin/python -I /opt/tin-lite/verify-technical-metadata.py"
    context = {"workflow_key": "organic.technical_fix", "verification": {"commands": [command]}}
    assert bridge.trusted_verifier({"workflow_key": "custom.fake"}, command) is None
    assert bridge.trusted_verifier(context, command + " && echo bad") is None
    calls = []
    monkeypatch.setattr(bridge, "ISOLATED", True)
    monkeypatch.setattr(bridge, "execute", lambda: 0)
    monkeypatch.setattr(bridge, "_decode_context", lambda: context)
    monkeypatch.setattr(bridge.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        bridge.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(wait=lambda **kw: None)
    )
    monkeypatch.setattr(
        bridge.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setenv("TIN_CODEX_API_GRANT", "private-grant")
    monkeypatch.setenv("TIN_PROCEDURE_CONTEXT_PATH", "/home/user/.tin-lite/context.json")
    assert bridge.main() == 0
    assert calls[-2][0][-1] == "freeze"
    assert calls[-1][0] == bridge.trusted_verifier(context, command)
    assert "TIN_CODEX_API_GRANT" not in calls[-1][1]["env"]
    assert calls[-1][1]["env"]["TIN_PROCEDURE_CONTEXT_PATH"].endswith("context.json")


@pytest.mark.parametrize("contract", [CONTRACT, PROCEDURE_CONTRACT_V2, PROCEDURE_CONTRACT])
@pytest.mark.parametrize("additional", [False, True])
def test_namespaced_local_tools_preserve_pinned_limits(contract, additional):
    body = {
        **BODY,
        "tools": [
            {
                "type": "namespace",
                "name": "functions",
                "tools": [
                    {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
                ],
            },
            {"type": "web_search"},
        ],
    }
    tools = body.pop("tools")
    if additional:
        body["input"] = [{"type": "additional_tools", "tools": tools}]
    else:
        body["tools"] = tools
    accepted = request_body(json.dumps(body).encode(), "responses", contract)
    assert accepted["max_output_tokens"] == contract["max_output_tokens"]
    if contract == PROCEDURE_CONTRACT:
        assert "max_tool_calls" not in accepted
    else:
        assert accepted["max_tool_calls"] == 1
    assert (accepted["input"][0]["tools"] if additional else accepted["tools"]) == tools


@pytest.mark.parametrize("contract", [CONTRACT, PROCEDURE_CONTRACT_V2, PROCEDURE_CONTRACT])
@pytest.mark.parametrize("additional", [False, True])
@pytest.mark.parametrize("kind", ["mcp", "file_search", "web_search", "namespace"])
def test_namespaces_cannot_hide_hosted_or_nested_tools(contract, additional, kind):
    tools = [{"type": "namespace", "name": "tin-run", "tools": [{"type": kind}]}]
    body = {**BODY}
    if additional:
        body["input"] = [{"type": "additional_tools", "tools": tools}]
    else:
        body["tools"] = tools
    with pytest.raises(HTTPException):
        request_body(json.dumps(body).encode(), "responses", contract)


def test_v2_supplies_hosted_search_without_changing_v1_or_multiplying_tools():
    raw = json.dumps(BODY).encode()
    assert request_body(raw, "responses", PROCEDURE_CONTRACT)["tools"] == [{"type": "web_search"}]
    assert "tools" not in request_body(raw, "responses", CONTRACT)
    supplied = {**BODY, "tools": [{"type": "web_search"}]}
    assert (
        request_body(json.dumps(supplied).encode(), "responses", PROCEDURE_CONTRACT)["tools"]
        == supplied["tools"]
    )
    assert request_body(raw, "responses/compact", PROCEDURE_CONTRACT).get("tools") is None


@pytest.mark.parametrize("requested", [None, 1, 20])
def test_new_procedures_do_not_limit_search_followups(requested):
    raw = json.dumps({**BODY, "max_tool_calls": requested}).encode()
    assert "max_tool_calls" not in request_body(raw, "responses", PROCEDURE_CONTRACT)
    for old in (CONTRACT, PROCEDURE_CONTRACT_V2):
        assert request_body(raw, "responses", old)["max_tool_calls"] == 1


def test_search_followups_are_observed_without_charging_extra_searches():
    output = [
        {"type": "web_search_call", "status": "completed", "action": {"type": kind}}
        for kind in ("search", "open_page", "find_in_page", "search")
    ]
    assert web_usage(output, PROCEDURE_CONTRACT["protocol"]) == {
        "web_search_calls": 2,
        "web_open_pages": 1,
        "web_find_calls": 1,
    }
    assert web_usage(output, PROCEDURE_CONTRACT_V2["protocol"]) == {"web_search_calls": 4}
    output[0]["status"] = "searching"
    assert web_usage(output, PROCEDURE_CONTRACT["protocol"])["web_search_calls"] is None
    assert web_usage(None, PROCEDURE_CONTRACT["protocol"])["web_search_calls"] is None


def test_draft_sources_reach_model_without_exposing_controller_context():
    bridge = load_sandbox_module("procedure_app_server")
    draft = {
        "frontmatter": {"program_id": "program", "brief_sha256": "a" * 64},
        "verification": ["Verify the documented channels"],
        "item": {"id": "item-3"},
    }
    message = bridge._content_draft_instruction(
        {
            "content_draft": draft,
            "identity": {"password": "private-secret"},
            "grant": "private-grant",
        }
    )
    assert "item-3" in message and "brief_sha256" in message and "documented channels" in message
    assert "private" not in message
    assert bridge._content_draft_instruction({"identity": "private"}) == ""


async def upgrade_attempt(db, run, contract=PROCEDURE_CONTRACT):
    await db.pool.execute(
        "UPDATE effect_receipts SET result=result || $2::jsonb WHERE execution_key=$1",
        attempt_key(run.id),
        json.dumps({"contract": contract, "pricing": RATE_CARD}),
    )


@pytest.mark.parametrize("historical", [False, True])
async def test_stream_hands_off_sources_once_without_polluting_accounting(
    publication_db, historical
):
    from test_codex_web_evidence import source

    item = source()
    output_done = {"type": "response.output_item.done", "output_index": 0, "item": item}
    wire = b"data: " + json.dumps(output_done).encode() + b"\n\n" + result_event(output=[item])

    async def upstream(request):
        return httpx.Response(200, content=wire)

    run, relay, client, sent = await setup_relay(publication_db, upstream)
    try:
        contract = PROCEDURE_CONTRACT_V2 if historical else PROCEDURE_CONTRACT
        await upgrade_attempt(publication_db, run, contract)
        response = await post(client, run)
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        observations = [
            e["item"]
            for e in events
            if e.get("type") == "response.output_item.done"
            and e["item"]["type"] == "function_call_output"
        ]
        assert len(observations) == (0 if historical else 1)
        if not historical:
            assert "phone_number and text" in observations[0]["output"]
            assert events[-1]["response"]["output"][1] == observations[0]
            # What the controller sends after a local step is ordinary explicit
            # tool history; no shared provider response lookup or new purchase.
            assert (
                await post(
                    client,
                    run,
                    {
                        **BODY,
                        "input": [
                            observations[0],
                            {"role": "user", "content": "Continue after the local tool."},
                        ],
                    },
                )
            ).status_code == 200
            assert json.loads(sent[-1].content)["input"][0] == observations[0]
        receipt = await publication_db.pool.fetchval(
            "SELECT result FROM effect_receipts WHERE operation=$1 ORDER BY created_at LIMIT 1",
            USAGE,
        )
        text = json.dumps(receipt)
        assert "phone_number" not in text and "snippet" not in text and "results" not in text
        assert "response_received" in text
    finally:
        await client.aclose()
        await relay.close()


@pytest.mark.parametrize("kind", ["mcp", "image_generation", "computer"])
def test_additional_tool_declarations_cannot_bypass_priced_tool_allowlist(kind):
    raw = json.dumps(
        {**BODY, "input": [{"type": "additional_tools", "tools": [{"type": kind}]}]}
    ).encode()
    with pytest.raises(HTTPException):
        request_body(raw, "responses", PROCEDURE_CONTRACT)


@pytest.mark.parametrize("contract", [CONTRACT, PROCEDURE_CONTRACT_V2, PROCEDURE_CONTRACT])
async def test_procedures_use_standard_responses_for_hosted_search(publication_db, contract):
    run, relay, client, sent = await setup_relay(publication_db)
    try:
        extended = contract != CONTRACT
        if extended:
            await upgrade_attempt(publication_db, run, contract)
        inputs = [
            {
                "type": "additional_tools",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "tin-run",
                        "tools": [{"type": "function", "name": "call_service"}],
                    }
                ],
            }
        ]
        response = await post(
            client,
            run,
            {**BODY, "input": inputs},
            headers={
                "x-openai-internal-codex-responses-lite": "true",
                "x-codex-beta-features": "remote_compaction_v2",
            },
        )
        assert response.status_code == 200
        assert ("x-openai-internal-codex-responses-lite" in sent[0].headers) is not extended
        assert sent[0].headers["x-codex-beta-features"] == "remote_compaction_v2"
        body = json.loads(sent[0].content)
        assert body["input"] == inputs
        assert body.get("tools", []) == ([{"type": "web_search"}] if extended else [])
        if contract == PROCEDURE_CONTRACT:
            assert "max_tool_calls" not in body
        else:
            assert body["max_tool_calls"] == 1
    finally:
        await client.aclose()
        await relay.close()


async def test_context_and_request_limits_are_pinned_before_paid_intent(publication_db):
    run, relay, client, sent = await setup_relay(publication_db)
    try:
        large = {**BODY, "input": "x" * 300000}
        assert (await post(client, run, large)).status_code == 413
        assert not sent
        assert not await publication_db.pool.fetchval(
            "SELECT count(*) FROM effect_receipts WHERE operation=$1", USAGE
        )
        await upgrade_attempt(publication_db, run)
        for i in range(10):
            assert (await post(client, run, {**large, "instructions": str(i)})).status_code == 200
        assert len(sent) == 10  # More than the unchanged v1 eight-request ceiling.
        assert json.loads(sent[0].content)["max_output_tokens"] == 8192
    finally:
        await client.aclose()
        await relay.close()


async def test_relay_rejections_are_logged_without_request_content(publication_db, caplog):
    run, relay, client, sent = await setup_relay(publication_db)
    try:
        with caplog.at_level("WARNING", logger="tin_lite.codex_api_relay"):
            response = await post(client, run, {**BODY, "input": "private-prompt" * 30000})
        assert response.status_code == 413
        assert not sent
        [record] = [r for r in caplog.records if r.name == "tin_lite.codex_api_relay"]
        message = record.getMessage()
        assert f"run={run.id}" in message and "status=413" in message
        assert "private-prompt" not in message
    finally:
        await client.aclose()
        await relay.close()


async def test_multiple_searches_compaction_and_retry_settle_once(billed, monkeypatch):
    f = billed
    monkeypatch.setattr(
        f.billing, "terms", lambda definition, project_id, inputs=None: api_terms({"procedure": {}})
    )
    run, relay, client, sent = await paid_relay(f, contract=PROCEDURE_CONTRACT)

    def upstream(request):
        sent.append(request)
        return httpx.Response(
            200,
            content=result_event(
                output=[
                    {
                        "type": "web_search_call",
                        "id": f"ws_{i}",
                        "status": "completed",
                        "action": {"type": "search"},
                    }
                    for i in range(3)
                ]
                if len(sent) > 1
                else [],
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "total_tokens": 1100,
                    "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 0},
                },
            ),
        )

    await relay.client.aclose()
    relay.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    try:
        # The pinned custom-provider CLI compacts with an ordinary Responses call.
        # Remote /compact has no supplier model/tier echo or output ceiling and
        # remains outside billing; do not invent those facts to price it.
        assert (
            await post(
                client,
                run,
                {"model": CONTRACT["model"], "input": "compact"},
                operation="responses/compact",
            )
        ).status_code == 422
        assert not sent
        assert (
            await post(client, run, {**BODY, "input": "Compact this context"})
        ).status_code == 200
        assert (
            await post(client, run, {**BODY, "tools": [{"type": "web_search"}]})
        ).status_code == 200
        assert (
            await post(client, run, {**BODY, "tools": [{"type": "web_search"}]})
        ).status_code == 409
        await finish(f, run)
        await f.billing.settle(run.id)
        await f.billing.settle(run.id)
        charge = await f.billing.run_charge(run.id, ACTOR)
        assert charge["charged_usd"] == "0.04"
        assert len(sent) == 2
        assert all("max_tool_calls" not in json.loads(request.content) for request in sent)
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM billing_ledger WHERE run_id=$1 AND kind='charge'", run.id
            )
            == 1
        )
        usage = await read_run_usage(database=f.db, run=run)
        assert Decimal(usage["own"]["totals"]["known_api_list_price_usd"]) == Decimal("0.03564")
    finally:
        await client.aclose()
        await relay.close()


async def test_existing_v2_credit_quote_keeps_its_contract(billed, monkeypatch):
    f = billed
    terms = {**api_terms({"procedure": {}}), "codex_contract": PROCEDURE_CONTRACT_V2}
    monkeypatch.setattr(f.billing, "terms", lambda definition, project_id, inputs=None: terms)
    run, relay, client, sent = await paid_relay(f, contract=PROCEDURE_CONTRACT_V2)
    try:
        async with f.db.pool.acquire() as conn:
            contract = await select_contract(
                db=f.db,
                conn=conn,
                run=run,
                procedure=SimpleNamespace(sandbox=SandboxProfile(profile="default")),
                settings=SimpleNamespace(codex_api_projects=set(), luna_api_key="synthetic"),
            )
        assert contract == PROCEDURE_CONTRACT_V2
        assert (await post(client, run)).status_code == 200
        assert json.loads(sent[0].content)["max_tool_calls"] == 1
    finally:
        await client.aclose()
        await relay.close()


def compact_facts():
    return {
        **facts(1000, 100, 100, 0),
        "endpoint": "responses/compact",
        "protocol": PROCEDURE_CONTRACT["protocol"],
        "response_object": "response.compaction",
        "requested_model": CONTRACT["model"],
        "requested_service_tier": "default",
        "model": None,
        "service_tier": None,
    }


def test_remote_compaction_is_not_priced_from_invented_supplier_fields():
    record = compact_facts()
    assert price_response(RATE_CARD, record) is None
    assert price_response(RATE_CARD, {**record, "protocol": CONTRACT["protocol"]}) is None
    assert price_response(RATE_CARD, {**record, "model": "different"}) is None
    assert price_response(RATE_CARD, {**record, "service_tier": "priority"}) is None
    assert price_response(RATE_CARD, {**record, "response_object": None}) is None
    isolated = api_terms({"procedure": {"sandbox": {"profile": "isolated"}}})
    assert isolated["codex_contract"] == PROCEDURE_CONTRACT
    assert isolated["request_maximum_input_bytes"] == PROCEDURE_CONTRACT["max_request_bytes"]
    assert api_terms({"procedure": {}})["codex_contract"] == PROCEDURE_CONTRACT


def test_diagram_images_have_separate_bytes_not_larger_text_or_spending_limits():
    body = {
        **BODY,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Inspect this rendered candidate"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64," + "A" * 1_200_000,
                    },
                ],
            }
        ],
    }
    raw = json.dumps(body).encode()
    assert "max_tool_calls" not in request_body(raw, "responses", DIAGRAM_CONTRACT)
    for historical in (CONTRACT, PROCEDURE_CONTRACT_V2, PROCEDURE_CONTRACT):
        with pytest.raises(HTTPException) as caught:
            request_body(raw, "responses", historical)
        assert caught.value.status_code == 413
    body["input"][0]["content"][0]["text"] = "x" * 1_048_576
    with pytest.raises(HTTPException, match="text exceeds"):
        request_body(json.dumps(body).encode(), "responses", DIAGRAM_CONTRACT)
    with pytest.raises(HTTPException) as caught:
        request_body(
            b" " * (DIAGRAM_CONTRACT["max_request_bytes"] + 1), "responses", DIAGRAM_CONTRACT
        )
    assert caught.value.status_code == 413
    normal = api_terms({"procedure": {}})
    diagram = api_terms({"procedure": {"output": {"validator": "tin-diagram.reviewed.v1"}}})
    assert diagram["codex_contract"] == DIAGRAM_CONTRACT
    assert diagram["maximum_nanos"] == normal["maximum_nanos"]
    assert diagram["request_maximum_nanos"] == normal["request_maximum_nanos"]
    assert DIAGRAM_CONTRACT["max_observed_tokens"] == PROCEDURE_CONTRACT["max_observed_tokens"]


async def test_diagram_image_allowance_is_pinned_before_paid_intent(publication_db):
    run, relay, client, sent = await setup_relay(publication_db)
    try:
        settings = SimpleNamespace(codex_api_projects={run.project_id}, luna_api_key="test")
        async with publication_db.pool.acquire() as conn:
            contract = await select_contract(
                db=publication_db,
                conn=conn,
                run=run,
                procedure=SimpleNamespace(
                    sandbox=SandboxProfile(), output_validator="tin-diagram.reviewed.v1"
                ),
                settings=settings,
            )
        assert contract == DIAGRAM_CONTRACT
        await upgrade_attempt(publication_db, run, contract)
        image_body = {
            **BODY,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64," + "A" * 1_200_000,
                        },
                    ],
                }
            ],
        }
        assert (await post(client, run, image_body)).status_code == 200
        assert len(sent) == 1
        assert (await post(client, run, {**BODY, "input": "x" * 1_200_000})).status_code == 413
        assert len(sent) == 1
    finally:
        await client.aclose()
        await relay.close()


async def test_diagram_images_use_quoted_tokens_and_settle_once(billed, monkeypatch):
    f = billed
    terms = api_terms({"procedure": {"output": {"validator": "tin-diagram.reviewed.v1"}}})
    monkeypatch.setattr(f.billing, "terms", lambda definition, project_id, inputs=None: terms)
    run, relay, client, sent = await paid_relay(f, contract=DIAGRAM_CONTRACT)
    try:
        body = {
            **BODY,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64," + "A" * 1_200_000,
                        },
                    ],
                }
            ],
        }
        assert (await post(client, run, body)).status_code == 200
        assert (await post(client, run, body)).status_code == 409
        await finish(f, run)
        await f.billing.settle(run.id)
        await f.billing.settle(run.id)
        assert len(sent) == 1
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM billing_ledger WHERE run_id=$1 AND kind='charge'", run.id
            )
            == 1
        )
        assert (await f.billing.run_charge(run.id, ACTOR))["charged_usd"] == "0.02"
    finally:
        await client.aclose()
        await relay.close()


async def test_unbilled_remote_compaction_is_unknown_not_free_or_guessed(publication_db):
    async def wire(_request):
        return httpx.Response(
            200,
            json={
                "object": "response.compaction",
                "id": "cmp_test",
                "output": [{"type": "compaction", "encrypted_content": "private-ciphertext"}],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "total_tokens": 1100,
                    "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 0},
                },
            },
        )

    run, relay, client, sent = await setup_relay(publication_db, wire)
    try:
        await upgrade_attempt(publication_db, run)
        response = await post(
            client,
            run,
            {"model": CONTRACT["model"], "input": "private text"},
            operation="responses/compact",
        )
        assert response.status_code == 200
        usage = await read_run_usage(database=publication_db, run=run)
        assert usage["own"]["totals"]["known_api_list_price_usd"] is None
        assert "private" not in json.dumps(usage)
        assert "private" not in await publication_db.pool.fetchval(
            "SELECT result::text FROM effect_receipts WHERE operation=$1", USAGE
        )
        assert (
            await post(
                client,
                run,
                {"model": CONTRACT["model"], "input": "private text"},
                operation="responses/compact",
            )
        ).status_code == 409
        assert len(sent) == 1
    finally:
        await client.aclose()
        await relay.close()
