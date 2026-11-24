"""Procedures another agent harness runs (Antigravity on Gemini), beside Codex."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from test_codex_isolation import load_sandbox_module
from test_codex_procedure import _procedure_source
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import agent_harness
from tin_lite.agent_api_relay import (
    GeminiAgentRelay,
    gemini_body,
    router,
    search_queries,
    split_target,
    usage_facts,
)
from tin_lite.agent_harness import (
    ANTIGRAVITY,
    CONTRACTS,
    GEMINI_AGENT_CARD,
    AgentSelection,
    contract,
    is_agent_contract,
    parse_agent,
    price_response,
    request_maximum,
)
from tin_lite.codex_api import (
    ATTEMPT,
    PROCEDURE_CONTRACT_V5,
    SESSION_CONTRACT,
    USAGE,
    attempt_failure,
    attempt_key,
    execution_profile,
    is_api_contract,
    is_procedure_contract,
    is_session_contract,
    select_contract,
    token_hash,
)
from tin_lite.codex_api_pricing import RATE_CARD, api_terms
from tin_lite.codex_api_pricing import price_response as price_any
from tin_lite.codex_api_relay import CODEX_ROUTE
from tin_lite.domain import CODEX_PROCEDURE_EXECUTOR
from tin_lite.procedures import (
    PinnedCodexProcedure,
    SandboxProfile,
    validate_codex_procedure_definition,
)

ROOT = Path(__file__).parents[1]
NEON = AgentSelection(ANTIGRAVITY, "gemini-4-neon", "high")
GRANT = "synthetic-run-bound-agent-grant-for-tests"


def definition(tmp_path, agent=None, **procedure_updates):
    source = _procedure_source(tmp_path / uuid4().hex)
    if agent is not None:
        source = replace(source, agent=agent)
    procedure, _files = source.materialize("research.deep_dive")
    procedure.update(procedure_updates)
    return {
        "key": "research.deep_dive",
        "executor": CODEX_PROCEDURE_EXECUTOR,
        "procedure": procedure,
    }


# Selection and contracts


@pytest.mark.parametrize(
    "value",
    [
        "antigravity",
        {"harness": "codex"},
        {"harness": "antigravity", "model": "gpt-6.1-sol"},
        {"harness": "antigravity", "model": "gemini-4-neon", "effort": "max"},
        {"harness": "antigravity", "model": "gemini-4-neon", "temperature": 1},
    ],
)
def test_only_known_harness_models_and_efforts_are_accepted(value):
    with pytest.raises(ValueError):
        parse_agent(value)


def test_absent_agent_is_codex_and_effort_defaults_to_high():
    assert parse_agent(None) is None
    assert parse_agent({"harness": "antigravity", "model": "gemini-4-neon"}) == NEON


def test_agent_contracts_are_api_contracts_and_never_codex_ones():
    session, operation = contract(NEON, session=True), contract(NEON, session=False)
    assert all(is_agent_contract(value) for value in CONTRACTS)
    assert is_api_contract(session) and is_session_contract(session)
    assert is_api_contract(operation) and not is_session_contract(operation)
    assert operation["max_requests"] > 1000 and "max_requests" not in session
    assert not is_procedure_contract(session) and not is_procedure_contract(operation)
    # Codex contracts keep their exact meaning.
    for codex in (SESSION_CONTRACT, PROCEDURE_CONTRACT_V5):
        assert not is_agent_contract(codex)
        assert CODEX_ROUTE.accepts(codex)
    assert not CODEX_ROUTE.accepts(session)
    # A tampered contract is no contract.
    assert not is_agent_contract({**session, "max_output_tokens": 10_000_000})
    assert not is_api_contract({**session, "model": "gemini-4-carbon"})


def test_agent_runs_are_isolated_and_fenced():
    value = contract(NEON, session=True)
    assert execution_profile(SandboxProfile(), value).profile == "isolated"
    with pytest.raises(ValueError):
        execution_profile(SandboxProfile(profile="browser", egress="open"), value)


def test_agent_failures_name_the_agent_not_codex():
    error = attempt_failure(
        {
            "contract": contract(NEON, session=True),
            "stop_reason": "run_limit",
            "relay_rejection": {"reason": "rate_limited", "status": 429},
        }
    )
    assert "Codex" not in str(error) and "OpenAI" not in str(error)
    assert "The agent stopped" in str(error) and "Google rate-limited" in str(error)
    assert "Codex" in str(attempt_failure({"stop_reason": "run_limit"}))


# Pricing


def gemini_record(**updates):
    record = {
        "pricing": GEMINI_AGENT_CARD,
        "provider": "gemini",
        "model": "gemini-4-neon",
        "service_tier": "standard",
        "outcome": "response_received",
        "endpoint": "streamGenerateContent",
        "usage": {
            "input_tokens": 10_000,
            "cached_input_tokens": 8_000,
            "output_tokens": 600,
            "total_tokens": 10_600,
            "search_queries": 0,
        },
    }
    record.update(updates)
    return record


def test_gemini_usage_is_priced_on_the_pinned_card():
    nanos, observation = price_response(GEMINI_AGENT_CARD, gemini_record())
    # 2,000 uncached at $3, 8,000 cached at $0.15, 600 output at $15 per million.
    assert nanos == 2_000 * 3_000 + 8_000 * 150 + 600 * 15_000
    assert observation["rate_card"] == GEMINI_AGENT_CARD["id"]
    search = gemini_record(
        model="gemini-3.1-flash-lite-preview",
        endpoint="generateContent",
        usage={
            "input_tokens": 300,
            "cached_input_tokens": 0,
            "output_tokens": 40,
            "total_tokens": 340,
            "search_queries": 2,
        },
    )
    assert price_any(GEMINI_AGENT_CARD, search)[0] == 300 * 250 + 40 * 1_500 + 2 * 14_000_000


@pytest.mark.parametrize(
    "updates",
    [
        {"model": "gemini-4-carbon"},
        {"provider": "openai"},
        {"outcome": "unconfirmed"},
        {"pricing": RATE_CARD},
        {"usage": {"input_tokens": 1, "cached_input_tokens": 2, "output_tokens": 0}},
        {
            "usage": {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "total_tokens": 5,
                "search_queries": 0,
            }
        },
    ],
)
def test_incomplete_or_foreign_facts_are_never_priced(updates):
    assert price_response(GEMINI_AGENT_CARD, gemini_record(**updates)) is None


def test_refused_requests_cost_nothing_and_codex_cards_keep_their_pricer():
    refused = gemini_record(outcome="upstream_rejected", usage=None, upstream_status=503)
    assert price_any(GEMINI_AGENT_CARD, refused)[0] == 0
    assert price_any(GEMINI_AGENT_CARD, {**refused, "upstream_status": 200}) is None
    codex = {
        "pricing": RATE_CARD,
        "provider": "openai",
        "model": RATE_CARD["model"],
        "service_tier": "default",
        "outcome": "response_received",
        "endpoint": "responses",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "web_search_calls": 0,
        },
    }
    assert price_any(RATE_CARD, codex)[0] == 100 * 2_000 + 10 * 10_000


def test_agent_terms_pin_the_gemini_card_and_contract(tmp_path):
    codex = api_terms(definition(tmp_path), session_budget=True)
    neon = api_terms(definition(tmp_path, NEON), session_budget=True)
    assert codex["pricing"] == RATE_CARD and not is_agent_contract(codex["codex_contract"])
    assert neon["pricing"] == GEMINI_AGENT_CARD
    assert neon["codex_contract"] == contract(NEON, session=True)
    assert neon["maximum_nanos"] == codex["maximum_nanos"]
    assert neon["funding"] == codex["funding"] and "request_maximum_nanos" not in neon
    child = api_terms(definition(tmp_path, NEON), child=True)
    assert child["codex_contract"] == contract(NEON, session=False)
    assert child["request_maximum_nanos"] == request_maximum(child["codex_contract"])
    assert child["request_maximum_nanos"] < child["maximum_nanos"]


# Procedure definitions


def test_agent_is_pinned_in_the_definition_with_an_isolated_hour(tmp_path):
    codex = validate_codex_procedure_definition(definition(tmp_path))
    assert codex.agent is None and codex.sandbox == SandboxProfile()
    value = definition(tmp_path, NEON)
    assert value["procedure"]["agent"] == NEON.definition()
    spec = validate_codex_procedure_definition(value)
    assert spec.agent == NEON
    assert spec.sandbox.isolated and spec.sandbox.timeout_seconds == 3600
    longer = definition(
        tmp_path / "long",
        NEON,
        sandbox={"profile": "isolated", "egress": "fenced", "timeout_seconds": 7200},
    )
    assert validate_codex_procedure_definition(longer).sandbox.timeout_seconds == 7200


@pytest.mark.parametrize(
    "updates",
    [
        {"sandbox": {"profile": "browser", "egress": "open", "timeout_seconds": 900}},
        {"identity": {"reuse": "none"}},
    ],
)
def test_codex_only_capabilities_refuse_another_harness(tmp_path, updates):
    with pytest.raises(ValueError):
        validate_codex_procedure_definition(definition(tmp_path, NEON, **updates))


def test_agent_reaches_the_sandbox_context():
    pinned = PinnedCodexProcedure(
        workflow_key="research.deep_dive",
        prompt="Research.",
        entry_skill="research-deep-dive",
        skill_files={"research-deep-dive/SKILL.md": b"x"},
        output_path="reports/R.md",
        agent=NEON,
    )
    assert pinned.sandbox_context(inputs={})["agent"] == NEON.definition()
    assert "agent" not in replace(pinned, agent=None).sandbox_context(inputs={})


async def test_unbudgeted_admission_selects_the_agent_contract_and_its_key():
    procedure = SimpleNamespace(sandbox=SandboxProfile(), agent=NEON, output_validator=None)
    conn = SimpleNamespace(fetchval=_fetchval)
    run = SimpleNamespace(id=uuid4(), project_id=uuid4())
    settings = SimpleNamespace(
        codex_api_projects=(run.project_id,), luna_api_key=None, gemini_api_key="key"
    )
    db = SimpleNamespace(get_effect=_no_effect)
    value = await select_contract(db=db, conn=conn, run=run, procedure=procedure, settings=settings)
    assert value == contract(NEON, session=False)
    with pytest.raises(ValueError, match="Gemini"):
        await select_contract(
            db=db,
            conn=conn,
            run=run,
            procedure=procedure,
            settings=replace_settings(settings, gemini_api_key=None),
        )


async def _no_effect(*args, **kwargs):
    return None


async def _fetchval(query, *args):
    return None if "billing_run_budgets" in query else False


def replace_settings(settings, **updates):
    return SimpleNamespace(**{**vars(settings), **updates})


# Relay


def test_gemini_requests_are_checked_and_bounded():
    value = contract(NEON, session=True)
    body = gemini_body(
        json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "tools": [{"functionDeclarations": []}, {"googleSearch": {}}],
                "generationConfig": {"maxOutputTokens": 10_000_000},
                "sessionId": "s",
            }
        ).encode(),
        value,
    )
    assert body["generationConfig"]["maxOutputTokens"] == value["max_output_tokens"]
    thinking = gemini_body(
        json.dumps(
            {"contents": [], "generationConfig": {"thinkingConfig": {"thinkingBudget": 10**9}}}
        ).encode(),
        value,
    )
    assert (
        thinking["generationConfig"]["thinkingConfig"]["thinkingBudget"]
        == (value["max_output_tokens"])
    )
    for bad in (
        {"contents": [], "cachedContent": "cachedContents/other"},
        {"contents": [{"parts": [{"fileData": {"fileUri": "files/other"}}]}]},
        {"contents": [], "tools": [{"codeExecution": {}}]},
        {"contents": "text"},
        {"contents": [], "cached_content": "cachedContents/other"},
        {"contents": [{"parts": [{"file_data": {"file_uri": "files/other"}}]}]},
        {"contents": [], "generation_config": {"max_output_tokens": 10_000_000}},
        {"contents": [], "generationConfig": {"max_output_tokens": 10_000_000}},
        {"contents": [], "generationConfig": {"candidateCount": 8}},
        {"contents": [], "generationConfig": {"thinkingConfig": {"thinking_budget": 10**9}}},
    ):
        with pytest.raises(Exception):  # noqa: B017, PT011 — HTTPException
            gemini_body(json.dumps(bad).encode(), value)
    assert split_target("gemini-4-neon:streamGenerateContent") == (
        "gemini-4-neon",
        "streamGenerateContent",
    )
    for target in ("gemini-4-carbon:generateContent", "gemini-4-neon:countTokens", "../x:y"):
        with pytest.raises(Exception):  # noqa: B017, PT011
            split_target(target)


def test_usage_and_search_facts():
    facts = usage_facts(
        {
            "promptTokenCount": 1000,
            "toolUsePromptTokenCount": 50,
            "cachedContentTokenCount": 400,
            "candidatesTokenCount": 30,
            "thoughtsTokenCount": 70,
            "totalTokenCount": 1150,
        },
        2,
    )
    assert facts["input_tokens"] == 1050 and facts["output_tokens"] == 100
    assert facts["total_tokens"] == 1150 and facts["search_queries"] == 2
    message = {
        "candidates": [{"groundingMetadata": {"webSearchQueries": ["a", "b"]}}, {"content": {}}]
    }
    assert search_queries(message) == 2


def sse(*messages):
    return b"".join(b"data: " + json.dumps(m).encode() + b"\r\n\r\n" for m in messages)


USAGE_EVENT = {
    "candidates": [{"content": {"parts": [{"text": ""}]}, "finishReason": "STOP"}],
    "usageMetadata": {
        "promptTokenCount": 1000,
        "cachedContentTokenCount": 800,
        "candidatesTokenCount": 20,
        "thoughtsTokenCount": 30,
        "totalTokenCount": 1050,
        "serviceTier": "standard",
    },
    "modelVersion": "gemini-4-neon",
}
REQUEST = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}


async def setup_relay(db, handler=None, value=None):
    _, _, run, _ = await activity_fixture(db)
    record = {
        "run_id": str(run.id),
        "outcome": "running",
        "contract": value or contract(NEON, session=False),
        "pricing": GEMINI_AGENT_CARD,
        "grant_sha256": token_hash(GRANT),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "project_id": str(run.project_id),
        "sandbox_id": run.sandbox_id,
        "generation": run.generation,
        "fencing_token": run.fencing_token,
        "thread_id": str(run.thread_id),
        "lease_owner": run.lease_owner,
    }
    async with db.pool.acquire() as conn:
        await db.start_effect(conn, execution_key=attempt_key(run.id), operation=ATTEMPT)
        await db.save_effect_progress(conn, execution_key=attempt_key(run.id), result=record)
    sent = []

    async def upstream(request):
        sent.append(request)
        assert request.headers["x-goog-api-key"] == "synthetic-gemini-key"
        assert request.url.host == "generativelanguage.googleapis.com"
        if handler:
            return await handler(request)
        return httpx.Response(200, content=sse({"candidates": []}, USAGE_EVENT))

    relay = GeminiAgentRelay(
        database=db,
        api_key="synthetic-gemini-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    app = FastAPI()
    app.state.runtime = SimpleNamespace(agent_api=relay)
    app.include_router(router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://tin.test")
    return run, relay, client, sent


async def post(client, run, body=None, grant=GRANT, target="gemini-4-neon:streamGenerateContent"):
    return await client.post(
        f"/internal/agent-api/{run.id}/gemini/v1beta/models/{target}",
        content=json.dumps(body or REQUEST).encode(),
        headers={"x-goog-api-key": grant, "content-type": "application/json"},
    )


async def test_streams_settle_usage_and_identical_bytes_are_bought_once(publication_db):
    run, relay, client, sent = await setup_relay(publication_db)
    try:
        response = await post(client, run)
        assert response.status_code == 200 and b"usageMetadata" in response.content
        assert sent[0].url.params["alt"] == "sse"
        assert (await post(client, run)).status_code == 409
        assert len(sent) == 1
        receipt = json.loads(
            await publication_db.pool.fetchval(
                "SELECT result FROM effect_receipts WHERE operation=$1 AND status='completed'",
                USAGE,
            )
        )
        assert receipt["provider"] == "gemini" and receipt["model"] == "gemini-4-neon"
        assert receipt["execution_kind"] == "agent_gemini_api"
        assert receipt["usage"]["input_tokens"] == 1000
        assert receipt["usage"]["output_tokens"] == 50
        assert price_any(GEMINI_AGENT_CARD, receipt)[0] > 0
        assert "synthetic-gemini-key" not in json.dumps(receipt) and '"hi"' not in json.dumps(
            receipt
        )
    finally:
        await client.aclose()
        await relay.close()


async def test_helper_models_are_admitted_and_others_refused(publication_db):
    async def handler(request):
        return httpx.Response(
            200,
            json={
                **USAGE_EVENT,
                "candidates": [{"groundingMetadata": {"webSearchQueries": ["q"]}}],
            },
        )

    run, relay, client, sent = await setup_relay(publication_db, handler)
    try:
        search = await post(client, run, target="gemini-3.1-flash-lite-preview:generateContent")
        assert search.status_code == 200 and "alt" not in sent[0].url.params
        receipt = json.loads(
            await publication_db.pool.fetchval(
                "SELECT result FROM effect_receipts WHERE operation=$1", USAGE
            )
        )
        assert receipt["usage"]["search_queries"] == 1
        refused = await post(client, run, target="gemini-4-carbon:generateContent")
        assert refused.status_code == 404
        assert len(sent) == 1
    finally:
        await client.aclose()
        await relay.close()


async def test_refused_requests_may_be_retried(publication_db):
    answers = iter([httpx.Response(503), httpx.Response(200, content=sse(USAGE_EVENT))])

    async def handler(request):
        return next(answers)

    run, relay, client, sent = await setup_relay(publication_db, handler)
    try:
        assert (await post(client, run)).status_code == 502
        assert (await post(client, run)).status_code == 200
        assert len(sent) == 2
        outcomes = sorted(
            json.loads(row["result"])["outcome"]
            for row in await publication_db.pool.fetch(
                "SELECT result FROM effect_receipts WHERE operation=$1", USAGE
            )
        )
        assert outcomes == ["response_received", "upstream_rejected"]
    finally:
        await client.aclose()
        await relay.close()


async def test_an_unconfirmed_stream_is_never_bought_again(publication_db):
    async def broken(request):
        return httpx.Response(200, content=b"data: {not json}\n\n")

    run, relay, client, sent = await setup_relay(publication_db, broken)
    try:
        await post(client, run)
        assert (await post(client, run)).status_code == 409 and len(sent) == 1
    finally:
        await client.aclose()
        await relay.close()


@pytest.mark.parametrize("grant", ["wrong-grant-of-sufficient-length-000000", "short"])
async def test_grants_fail_closed(publication_db, grant):
    run, relay, client, sent = await setup_relay(publication_db)
    try:
        assert (await post(client, run, grant=grant)).status_code == 403 and not sent
    finally:
        await client.aclose()
        await relay.close()


async def test_a_codex_grant_cannot_buy_gemini(publication_db):
    run, relay, client, sent = await setup_relay(publication_db, value=SESSION_CONTRACT)
    try:
        assert (await post(client, run)).status_code == 403 and not sent
    finally:
        await client.aclose()
        await relay.close()


# Sandbox


def test_image_pins_the_same_harness_the_contract_names():
    template = load_sandbox_module("template")
    helper = load_sandbox_module("isolated_procedure")
    assert template.ANTIGRAVITY_VERSION == agent_harness.ANTIGRAVITY_VERSION
    assert template.ANTIGRAVITY_SHA512 == agent_harness.ANTIGRAVITY_SHA512
    assert template.ANTIGRAVITY_URL == agent_harness.ANTIGRAVITY_URL
    assert helper.AGENT_VERSION == agent_harness.ANTIGRAVITY_VERSION
    value = contract(NEON, session=True)
    assert agent_harness.agent_ready_line(value) == (
        f"TIN_AGENT_READY_V1 antigravity {helper.AGENT_VERSION}"
    )
    assert "agent_procedure.py" in (ROOT / "sandbox" / "template.py").read_text()


def test_helper_starts_the_harness_as_the_worker_with_fenced_google_hosts(monkeypatch):
    helper = load_sandbox_module("isolated_procedure")
    monkeypatch.setattr(
        helper,
        "proxy_environment",
        lambda: ["HTTPS_PROXY=https://tin-run:p@proxy.test", "NO_PROXY=tin.test,127.0.0.1"],
    )
    monkeypatch.setattr(helper, "studio_environment", list)
    command = helper.agent_command(
        {
            "GEMINI_API_KEY": GRANT,
            "GOOGLE_GEMINI_BASE_URL": "https://tin.test/internal/agent-api/r/gemini",
            "TIN_AGENT_MODEL": "gemini-4-neon-high",
            "TIN_AGENT_SCHEMA": "/home/user/.agents/agent-result-schema.json",
            "TIN_RUN_ID": "00000000-0000-0000-0000-000000000001",
        }
    )
    assert command[:3] == ["/usr/bin/setpriv", "--reuid=tin-work", "--regid=tin-work"]
    assert "--no-new-privs" in command
    no_proxy = next(item for item in command if item.startswith("NO_PROXY="))
    assert "play.googleapis.com" in no_proxy and "generativelanguage.googleapis.com" in no_proxy
    assert f"GEMINI_API_KEY={GRANT}" in command and "AGY_CLI_DISABLE_AUTO_UPDATE=true" in command
    agy = command.index("/usr/local/bin/agy")
    assert command[agy + 1 :][:4] == [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    ]
    assert "gemini-4-neon-high" in command and "--dangerously-skip-permissions" in command


def test_helper_refuses_untrusted_agent_settings(monkeypatch, tmp_path):
    helper = load_sandbox_module("isolated_procedure")
    good = {
        "GEMINI_API_KEY": GRANT,
        "GOOGLE_GEMINI_BASE_URL": "https://tin.test/internal/agent-api/r/gemini",
        "TIN_AGENT_MODEL": "gemini-4-neon-high",
        "TIN_AGENT_SCHEMA": "/home/user/.agents/agent-result-schema.json",
    }
    monkeypatch.setattr(helper.Path, "is_file", lambda self: True)
    for bad in (
        {**good, "GOOGLE_GEMINI_BASE_URL": "https://attacker.test/gemini"},
        {**good, "TIN_AGENT_MODEL": "gemini; rm -rf /"},
        {**good, "TIN_AGENT_SCHEMA": "/etc/passwd"},
        {**good, "OPENAI_API_KEY": "x"},
        {**good, "TIN_RUN_TOOLS_URL": "https://tools.test"},
    ):
        monkeypatch.setattr(helper, "trusted_config", lambda path, label, bad=bad: bad)
        with pytest.raises(RuntimeError):
            helper.agent_values()
    monkeypatch.setattr(helper, "trusted_config", lambda path, label: good)
    assert helper.agent_values() == good


def controller():
    return load_sandbox_module("agent_procedure")


def context(**updates):
    value = {
        "workflow_key": "research.deep_dive",
        "prompt": "Research the topic.\n\nRUN CONTEXT: run 1",
        "entry_skill": "research-deep-dive",
        "skill_files": {"research-deep-dive/SKILL.md": "---\nname: x\n---\n"},
        "workspace": {"kind": "project.state"},
        "output": {"kind": "project.artifact", "path": "reports/R.md", "max_bytes": 100},
        "verification": {"commands": []},
        "project_skills": [],
        "inputs": {"topic": "agents"},
    }
    value.update(updates)
    return value


def test_brief_gives_a_goal_and_tools_not_a_method():
    module = controller()
    text = module.brief(context(), "project.artifact", "reports/R.md")
    assert "Research the topic." in text and '"topic": "agents"' in text
    assert "research-deep-dive" in text and "subagents" in text
    assert "/home/tin-work/scratch" in text and "reports/R.md" in text
    assert "don't publish, post, send, merge, push or contact anyone" in text
    assert "$research-deep-dive" not in text and "exec_command" not in text


def test_missing_or_oversized_deliverables_send_the_agent_back(monkeypatch, tmp_path):
    module = controller()
    monkeypatch.setattr(module, "STATE_DIR", tmp_path)
    value = context()
    problems = module.deliverable_problems(value, "project.artifact", {"summary": "s"})
    assert problems and "does not exist" in problems[0]
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "R.md").write_text("x" * 200)
    assert (
        "at most 100 bytes"
        in module.deliverable_problems(value, "project.artifact", {"summary": "s"})[0]
    )
    (tmp_path / "reports" / "R.md").write_text("done")
    assert module.deliverable_problems(value, "project.artifact", {"summary": "s"}) == []
    assert module.deliverable_problems(value, "project.artifact", None)
    pull = context(output={"kind": "github.pull_request", "max_files": 3, "max_bytes": 100})
    assert module.deliverable_problems(pull, "github.pull_request", {"summary": ""})
    # A pull request's title and body fall back to the summary and message, as for Codex.
    assert not module.deliverable_problems(pull, "github.pull_request", {"summary": "s"})


def test_progress_follows_the_agents_own_plan():
    module = controller()
    info = {
        "name": "update_steps",
        "parameters": {
            "steps": [
                {"status": "completed", "title": "Read the project"},
                {"status": "in_progress", "title": "Read competitors' pricing pages"},
                {"status": "pending", "title": "Write the report"},
            ]
        },
    }
    step = {"step_type": "tool", "tool_name": "update_steps", "state": "ACTIVE"}
    # agy reports tool_info as a Python repr; JSON is read as well.
    assert module.plan_progress({**step, "tool_info": repr(info)}) == (
        "Read competitors' pricing pages"
    )
    assert module.plan_progress({**step, "tool_info": json.dumps(info)}) == (
        "Read competitors' pricing pages"
    )
    for bad in (
        {**step, "tool_info": "__import__('os').system('x')"},
        {**step, "tool_name": "run_command", "tool_info": repr(info)},
        {**step, "tool_info": repr({"parameters": {"steps": [{"status": "pending"}]}})},
    ):
        assert module.plan_progress(bad) is None
