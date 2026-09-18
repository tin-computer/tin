"""Supplier wire -> durable usage -> shared credit ledger, without paid requests."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest
from test_billing import ACTOR, finish, fund, native_run
from test_billing import billed as billed
from test_private_workflows import app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite.billing_contracts import BillingError, receipt_charge
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.luna import OpenAIResponsesClient
from tin_lite.model_providers import (
    MessageRole,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelRoute,
    OpenAIModelProvider,
    ProviderName,
)
from tin_lite.model_usage import ModelUsageRecorder, model_usage_scope
from tin_lite.service_pricing import CARD, NATIVE_EXECUTORS, model_maximum, service_terms
from tin_lite.usage_capture import (
    begin_observation,
    external_usage_scope,
    observe_tool,
    recover_tool_observation,
)
from tin_lite.workflow_inputs import normalize_workflow_inputs

SPECS = {s.key: s for s in BUILTIN_WORKFLOWS}
SITE = {"site_url": "https://example.com/", "market": "US"}
KEYWORDS = {**SITE, "buyer_context": "Scheduling software for independent consultants."}
PARENT = {**KEYWORDS, "start_date": "2026-10-01"}


async def install(f, key):
    spec = SPECS[key]
    return await f.db.upsert_registry_workflow(
        workflow_id=spec.id,
        key=spec.key,
        title=spec.title,
        description=spec.description,
        executor=spec.executor,
        definition_repo_id="registry/workflows",
        definition_path=f"workflows/{key}.json",
        current_commit_sha="c" * 40,
        version_label=spec.version_label,
        definition=spec.definition,
    )


async def admit(f, key, inputs=None, *, parent=None, step=None):
    if key == "organic.traffic_system":
        f.settings.codex_api_projects = {f.project.id}
    workflow = await install(f, key)
    inputs = normalize_workflow_inputs(
        schema=workflow.definition["input_schema"],
        project_id=f.project.id,
        inputs=inputs or {},
    )
    quote = (
        None
        if parent
        else await f.billing.quote(
            runtime=f.runtime,
            project_id=f.project.id,
            actor=ACTOR,
            workflow_id=workflow.id,
            inputs=inputs,
        )
    )
    run, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=workflow.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=inputs,
        pinned_definition=workflow.definition,
        definition_commit_sha=workflow.current_commit_sha,
        start_idempotency_key=step or str(uuid4()),
        billing_quote_id=UUID(quote["id"]) if quote and quote.get("enabled") else None,
        billing_parent_run_id=parent.id if parent else None,
    )
    return run


def response(*, model="gpt-5.6-luna", searches=2):
    return {
        "id": "resp-supplier-fixture",
        "object": "response",
        "created_at": 123,
        "status": "completed",
        "model": model,
        "service_tier": "default",
        "output": [
            *[
                {
                    "type": "web_search_call",
                    "id": f"search{i}",
                    "status": "completed",
                    "action": {"type": "search", "query": "public question"},
                }
                for i in range(searches)
            ],
            {
                "type": "message",
                "id": "msg1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "A public result.", "annotations": []}],
            },
        ],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 300},
            "output_tokens_details": {"reasoning_tokens": 40},
        },
    }


@pytest.mark.parametrize("key", sorted(NATIVE_EXECUTORS))
def test_native_catalog_prices_are_not_illustrative(key):
    terms = service_terms(SPECS[key].definition)
    assert terms["service_pricing"] == CARD and terms["execution_fee_nanos"] == 0


def test_supplier_rates_caching_search_and_unknowns():
    terms = service_terms(SPECS["visibility.audit"].definition)
    record = {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "service_tier": "default",
        "outcome": "response_received",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "cached_input_tokens": 200,
            "cache_write_input_tokens": 300,
            "web_search_calls": 2,
        },
    }
    assert receipt_charge(terms, "native_model", record)[0] == 20_299_000
    for patch in (
        {"model": "unknown"},
        {"provider": "anthropic"},
        {"service_tier": None},
        {"service_tier": "priority"},
        {"usage": {**record["usage"], "total_tokens": 2}},
        {"usage": {**record["usage"], "cache_write_input_tokens": None}},
    ):
        assert receipt_charge(terms, "native_model", {**record, **patch}) is None
    old = deepcopy(terms)
    terms["service_pricing"]["models"].clear()
    assert old["service_pricing"] == CARD  # Quotes do not mutate the shared card.


@pytest.mark.parametrize("lost_write", [False, True])
async def test_search_response_and_dataforseo_settle_once(billed, monkeypatch, lost_write):
    f = billed
    await fund(f)
    run = await admit(f, "organic.audit", SITE)
    sent = []

    def wire(request):
        sent.append(request)
        payload = json.loads(request.content)
        assert payload["service_tier"] == "default" and payload["max_output_tokens"] > 0
        return httpx.Response(200, json=response())

    client = OpenAIResponsesClient(
        api_key="fake",
        model="gpt-5.6-luna",
        base_url="https://model.test/v1",
        timeout_seconds=5,
        transport=httpx.MockTransport(wire),
    )  # noqa: S106
    observe = f.billing.observe_operation

    async def fail(*a, **kw):
        raise RuntimeError("lost projection")

    if lost_write:
        monkeypatch.setattr(f.billing, "observe_operation", fail)
    try:
        async with f.db.pool.acquire() as conn:
            with external_usage_scope(f.db, conn, run.id, "panel"):
                if lost_write:
                    with pytest.raises(RuntimeError, match="lost projection"):
                        await client.create(
                            {"input": "Private input", "tools": [{"type": "web_search"}]}
                        )
                else:
                    await client.create(
                        {"input": "Private input", "tools": [{"type": "web_search"}]}
                    )
                with pytest.raises(RuntimeError, match="already observed"):
                    await client.create({"input": "Private input"})
            monkeypatch.setattr(f.billing, "observe_operation", observe)
            with external_usage_scope(f.db, conn, run.id, "crawl", maximum_usd="0.05"):
                observation = await begin_observation("dataforseo", "tool", "on_page/task_post")
                await observe_tool(observation, {"cost": 0.0125})
        await finish(f, run)
        await f.billing.reconcile()
        assert await f.billing.settle(run.id) == 30_000_000  # Round only the 0.032799 root total.
        assert len(sent) == 1
        assert (
            await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 1
        )
        rows = await f.db.pool.fetch("SELECT * FROM billing_operations")
        assert all(r["status"] == "observed" for r in rows)
        assert "Private input" not in str(rows)
        assert (await f.billing.overview(f.project.id, ACTOR))["reserved_usd"] == "0.00"
    finally:
        await client.close()


async def test_native_adapter_supplier_usage_and_pre_dispatch_unpriced_rejection(billed):
    f = billed
    await fund(f)
    run = await native_run(f)
    sent = []

    def wire(req):
        sent.append(req)
        return httpx.Response(200, json=response(model="gpt-6-astra", searches=0))

    from openai import AsyncOpenAI

    provider = OpenAIModelProvider(
        api_key="unused",
        client=AsyncOpenAI(
            api_key="fake",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
        ),
    )  # noqa: S106
    route = ModelRoute(
        "test", ProviderName.OPENAI, "gpt-6-astra", frozenset({ModelCapability.TEXT})
    )
    request = ModelRequest(
        messages=(ModelMessage(MessageRole.USER, "Generate a character"),), max_output_tokens=100
    )
    recorder = ModelUsageRecorder(f.db)
    try:
        async with f.db.pool.acquire() as conn:
            with model_usage_scope(run_id=run.id, step="real-price", conn=conn):
                await recorder.generate(
                    route, request, lambda: provider.generate(model=route.model, request=request)
                )
            with model_usage_scope(run_id=run.id, step="unknown", conn=conn):
                with pytest.raises(BillingError, match="no pinned"):
                    await recorder.generate(
                        replace(route, model="unknown"),
                        request,
                        lambda: provider.generate(model="unknown", request=request),
                    )
        await finish(f, run)
        assert await f.billing.settle(run.id) == 10_000_000
        assert len(sent) == 1
    finally:
        await provider.close()


@pytest.mark.parametrize("cost", [None, "0.0125"])
async def test_recovered_crawl_metadata_updates_only_its_existing_usage(billed, cost):
    f = billed
    await fund(f)
    run = await admit(f, "organic.audit", SITE)
    async with f.db.pool.acquire() as conn:
        with external_usage_scope(f.db, conn, run.id, "crawl_submit", maximum_usd="0.05"):
            observation = await begin_observation("dataforseo", "tool", "on_page/task_post")
        for _ in range(2):
            await recover_tool_observation(
                f.db,
                conn,
                run_id=run.id,
                step="crawl_submit",
                endpoint="on_page/task_post",
                cost=cost,
            )
        row = await conn.fetchrow("SELECT * FROM billing_operations WHERE id=$1", observation[2])
        assert row["status"] == ("observed" if cost else "pending")
        if cost:
            assert row["observed_nanos"] == 12_500_000
        # Recovery must not create a charge for a historical unobserved request.
        await recover_tool_observation(
            f.db, conn, run_id=run.id, step="old", endpoint="on_page/task_post", cost="1"
        )
        assert await conn.fetchval("SELECT count(*) FROM billing_operations") == 1


async def test_pre_enrollment_parent_does_not_acquire_retroactive_child_charges(billed):
    f = billed
    await f.db.pool.execute("UPDATE billing_accounts SET run_billing_enabled=false")
    workflow = await install(f, "organic.traffic_system")
    parent, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=workflow.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=normalize_workflow_inputs(
            schema=workflow.definition["input_schema"], project_id=f.project.id, inputs=PARENT
        ),
        pinned_definition=workflow.definition,
        definition_commit_sha=workflow.current_commit_sha,
    )
    await f.db.pool.execute("UPDATE billing_accounts SET run_billing_enabled=true")
    await admit(f, "organic.audit", SITE, parent=parent, step=f"system:{parent.id}:audit")
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 0


def test_connected_email_is_included_without_model_or_send_fee(billed):
    terms = billed.billing.terms(SPECS["outreach.email_campaign"].definition, billed.project.id)
    assert terms["kind"] == "included" and terms["maximum_nanos"] == 0


async def test_parallel_children_share_one_reservation_and_settlement(billed):
    f = billed
    await fund(f)
    parent = await admit(f, "organic.traffic_system", PARENT)
    audit, keywords = await asyncio.gather(
        admit(f, "organic.audit", SITE, parent=parent, step=f"system:{parent.id}:audit"),
        admit(
            f, "organic.keyword_plan", KEYWORDS, parent=parent, step=f"system:{parent.id}:keywords"
        ),
    )
    assert (await f.billing.overview(f.project.id, ACTOR))["reserved_usd"] == "0.00"

    async def charge(run):
        async with f.db.pool.acquire() as conn:
            await f.billing.begin_operation(
                conn, run_id=run.id, operation_id=str(run.id), kind="tool", maximum=4_000_000_000
            )
            await f.billing.observe_operation(
                conn, operation_id=str(run.id), nanos=600_000_000, observation={"basis": "fixture"}
            )
        await finish(f, run)

    await asyncio.gather(charge(audit), charge(keywords))
    assert await f.billing.settle(parent.id) is None
    await finish(f, parent)
    assert await f.billing.settle(parent.id) == 1_200_000_000
    assert (await f.billing.run_charge(audit.id, ACTOR))["included_in_parent"]
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 1
    assert (await f.billing.overview(f.project.id, ACTOR))["reserved_usd"] == "0.00"


async def test_child_identity_and_step_ceiling_are_enforced(billed):
    f = billed
    await fund(f)
    parent = await admit(f, "organic.traffic_system", PARENT)
    with pytest.raises(BillingError, match="allocation"):
        await admit(f, "organic.audit", SITE, parent=parent, step=f"system:{parent.id}:keywords")
    audit = await admit(f, "organic.audit", SITE, parent=parent, step=f"system:{parent.id}:audit")
    async with f.db.pool.acquire() as conn:
        with pytest.raises(BillingError, match="step"):
            await f.billing.begin_operation(
                conn, run_id=audit.id, operation_id="too-large", kind="tool", maximum=6_000_000_000
            )
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0


def test_new_organic_parent_requires_priced_execution_before_research(billed):
    with pytest.raises(BillingError, match="API-billed"):
        billed.billing.terms(SPECS["organic.traffic_system"].definition, billed.project.id, PARENT)


async def test_content_children_share_parent_budget_only_for_pinned_recipe(billed):
    from tin_lite.organic_system import POLICY

    f = billed
    await fund(f)
    parent = await admit(f, "organic.traffic_system", PARENT)
    inputs = {"program_id": str(uuid4()), "item_id": "opp001"}
    with pytest.raises(BillingError, match="allocation"):
        await admit(f, "content.generate", inputs, parent=parent, step=f"system:{parent.id}:draft")
    definitions = {
        "draft": SPECS["content.generate"].definition,
        "delivery": SPECS["content.deliver"].definition,
    }
    async with f.db.pool.acquire() as conn:
        key = f"traffic:{parent.id}:prepare"
        await f.db.start_effect(conn, execution_key=key, operation="organic.traffic_system")
        await f.db.complete_effect(
            conn, execution_key=key, result={"policy": POLICY, "definitions": definitions}
        )
    draft = await admit(
        f, "content.generate", inputs, parent=parent, step=f"system:{parent.id}:draft"
    )
    audit = await admit(f, "organic.audit", SITE, parent=parent, step=f"system:{parent.id}:audit")
    async with f.db.pool.acquire() as conn:
        assert await f.billing.valid_child(
            conn,
            {"run_id": parent.id, "executor": "organic.traffic_system"},
            {"start_idempotency_key": f"system:{parent.id}:delivery"},
            SPECS["content.deliver"].definition,
        )
        assert not await f.billing.valid_child(
            conn,
            {"run_id": parent.id, "executor": "organic.traffic_system"},
            {"start_idempotency_key": f"system:{parent.id}:delivery"},
            {**SPECS["content.deliver"].definition, "title": "Different recipe"},
        )
    for child in (draft, audit):
        async with f.db.pool.acquire() as conn:
            await f.billing.begin_operation(
                conn,
                run_id=child.id,
                operation_id=str(child.id),
                kind="codex_api" if child.id == draft.id else "tool",
                maximum=100_000_000,
            )
            await f.billing.observe_operation(
                conn, operation_id=str(child.id), nanos=50_000_000, observation={"basis": "fixture"}
            )
        await finish(f, child)
        assert (await f.billing.run_charge(child.id, ACTOR))["included_in_parent"]
    await finish(f, parent)
    assert await f.billing.settle(parent.id) == 100_000_000
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 1


async def test_free_onboarding_review_holds_no_credit_reservation(billed):
    f = billed
    await fund(f)
    f.settings.codex_api_projects = {f.project.id}
    parent = await admit(f, "growth.onboarding")
    await f.db.pool.execute("UPDATE workflow_runs SET status='needs_input' WHERE id=$1", parent.id)
    assert await f.billing.settle(parent.id) is None
    await f.billing.reconcile()
    assert (await f.billing.overview(f.project.id, ACTOR))["reserved_usd"] == "0.00"
    assert (await f.billing.run_charge(parent.id, ACTOR))["charged_usd"] == "0.00"
    run = await admit(f, "content.answer_page")
    await f.db.pool.execute("UPDATE workflow_runs SET status='needs_input' WHERE id=$1", run.id)
    assert await f.billing.settle(run.id) == 0


async def test_http_and_mcp_quote_same_actual_rates(billed, monkeypatch):
    f = billed
    workflow = await install(f, "visibility.audit")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        result = await client.post(
            f"/api/projects/{f.project.id}/billing/quotes",
            json={"workflow_id": str(workflow.id), "inputs": {"target": "this project"}},
        )
        assert result.status_code == 200
    server = mcp(f, monkeypatch)
    tool = structured(
        await server.call_tool(
            "quote_workflow_run",
            {
                "project_id": str(f.project.id),
                "workflow_id": workflow.key,
                "inputs": {"target": "this project"},
            },
        )
    )
    assert tool["terms"] == result.json()["terms"]
    assert "illustrative" not in tool["notice"]


def test_reservation_uses_cache_write_upper_bound():
    terms = service_terms(SPECS["visibility.audit"].definition)
    assert (
        model_maximum(
            terms,
            provider="openai",
            model="gpt-5.6-luna",
            input_tokens=1000,
            output_tokens=100,
            searches=2,
        )
        == 20_370_000
    )


async def test_parent_pins_child_api_auth_across_flag_changes(billed):
    f = billed
    await fund(f)
    f.settings.codex_api_projects = {f.project.id}
    parent = await admit(f, "growth.onboarding")
    f.settings.codex_api_projects = set()
    child = await admit(
        f, "growth.onboarding_plan", parent=parent, step=f"onboarding:{parent.id}:plan"
    )
    from tin_lite.free_workflows import included_execution

    async with f.db.pool.acquire() as conn:
        included = await included_execution(f.db, child.id, conn=conn)
    # The parent's API choice is pinned for every child, whatever the flag says later. The plan
    # itself is a native LLM flow, so it carries no Codex API terms of its own.
    assert included["codex_api"] is True and included["api_terms"] is None
    assert included["root_run_id"] == str(parent.id)
    assert (await f.billing.run_charge(child.id, ACTOR))["root_run_id"] == str(parent.id)
    assert (await f.billing.overview(f.project.id, ACTOR))["reserved_usd"] == "0.00"


@pytest.mark.parametrize("key", ["growth.onboarding", "growth.onboarding_plan"])
async def test_start_here_needs_neither_funds_nor_quote(billed, monkeypatch, key):
    f = billed
    workflow = await install(f, key)
    server = mcp(f, monkeypatch)
    quote = structured(
        await server.call_tool(
            "quote_workflow_run",
            {
                "project_id": str(f.project.id),
                "workflow_id": key,
                "inputs": {},
            },
        )
    )
    assert quote["enabled"] is False and quote["maximum_usd"] == "0.00"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        http_quote = await client.post(
            f"/api/projects/{f.project.id}/billing/quotes",
            json={
                "workflow_id": str(workflow.id),
                "inputs": {},
            },
        )
        assert http_quote.json() == quote
    run = await admit(f, key)
    assert (await f.billing.run_charge(run.id, ACTOR))["charged_usd"] == "0.00"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 0
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_quotes") == 0
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger") == 0
    with pytest.raises(BillingError, match="Add credits"):
        await admit(f, "organic.audit", SITE)


async def test_free_onboarding_api_records_supplier_usage_without_debiting_credits(
    billed, monkeypatch
):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from fastapi import FastAPI
    from test_codex_api import GRANT, post, result_event

    from tin_lite.codex_api import (
        ATTEMPT,
        PROCEDURE_CONTRACT,
        attempt_key,
        select_contract,
        token_hash,
    )
    from tin_lite.codex_api_pricing import RATE_CARD
    from tin_lite.codex_api_relay import CodexAPIRelay, router
    from tin_lite.free_workflows import ONBOARDING
    from tin_lite.procedures import SandboxProfile

    f = billed
    f.settings.codex_api_projects = {f.project.id}
    # No Start here step is a Codex procedure any more; the included-run relay path still serves
    # Tin-funded Codex work, so exercise it with a synthetic included procedure.
    monkeypatch.setitem(ONBOARDING, "research.deep_dive", "codex.procedure")
    run = await admit(f, "research.deep_dive", {"question": "Which clinics buy form builders?"})
    await f.db.pool.execute(
        """UPDATE workflow_runs SET status='running', lease_active=true,
           sandbox_id='free-test', lease_owner='test' WHERE id=$1""",
        run.id,
    )
    run = await f.db.get_run(run.id)
    async with f.db.pool.acquire() as conn:
        assert (
            await select_contract(
                db=f.db,
                conn=conn,
                run=run,
                procedure=SimpleNamespace(
                    sandbox=SandboxProfile(profile="default", timeout_seconds=1200)
                ),
                settings=SimpleNamespace(codex_api_projects=set(), luna_api_key="synthetic"),
            )
            == PROCEDURE_CONTRACT
        )
        await f.db.start_effect(conn, execution_key=attempt_key(run.id), operation=ATTEMPT)
        await f.db.save_effect_progress(
            conn,
            execution_key=attempt_key(run.id),
            result={
                "outcome": "running",
                "contract": PROCEDURE_CONTRACT,
                "pricing": RATE_CARD,
                "grant_sha256": token_hash(GRANT),
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                **{
                    k: str(getattr(run, k)) if k in {"project_id", "thread_id"} else getattr(run, k)
                    for k in (
                        "project_id",
                        "sandbox_id",
                        "generation",
                        "fencing_token",
                        "thread_id",
                        "lease_owner",
                    )
                },
            },
        )
    sent = []

    def wire(request):
        sent.append(request)
        return httpx.Response(
            200,
            content=result_event(
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "total_tokens": 1100,
                    "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 300},
                }
            ),
        )

    relay = CodexAPIRelay(
        database=f.db,
        api_key="synthetic",
        client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
    )
    api = FastAPI()
    api.state.runtime = SimpleNamespace(codex_api=relay)
    api.include_router(router)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="https://tin.test"
        ) as client:
            assert (await post(client, run)).status_code == 200
            assert (await post(client, run)).status_code == 409
        assert len(sent) == 1
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM effect_receipts WHERE operation='codex_api_usage_v1'"
            )
            == 1
        )
        assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0
        assert await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger") == 0
        assert (await f.billing.overview(f.project.id, ACTOR))["reserved_usd"] == "0.00"
    finally:
        await relay.close()


def test_search_fee_excludes_open_and_find_followups():
    from tin_lite.usage_capture import billable_search_calls

    output = [
        {"type": "web_search_call", "status": "completed", "action": {"type": kind}}
        for kind in ["search", "open_page", "find_in_page", "search"]
    ]
    assert billable_search_calls(output) == 2
    assert billable_search_calls([{"type": "web_search_call"}]) is None
    assert billable_search_calls(output + [{"type": "web_search_call", "status": "searching"}]) == 2


async def test_unfinished_extra_search_never_locks_credits_for_verified_model_usage(billed):
    f = billed
    await fund(f)
    run = await admit(f, "organic.audit", SITE)

    def wire(request):
        result = response()
        result["output"].append({"type": "web_search_call", "id": "ignored", "status": "searching"})
        return httpx.Response(200, json=result)

    client = OpenAIResponsesClient(
        api_key="synthetic",
        model="gpt-5.6-luna",
        base_url="https://model.test/v1",
        timeout_seconds=5,
        transport=httpx.MockTransport(wire),
    )
    try:
        async with f.db.pool.acquire() as conn:
            with external_usage_scope(f.db, conn, run.id, "extra-search"):
                await client.create({"input": "public question", "tools": [{"type": "web_search"}]})
        await finish(f, run)
        assert await f.billing.settle(run.id) == 20_000_000
        assert (await f.billing.overview(f.project.id, ACTOR))["reserved_usd"] == "0.00"
        observation = json.loads(
            await f.db.pool.fetchval(
                "SELECT observation FROM billing_operations WHERE run_id=$1", run.id
            )
        )
        assert observation["unpriced_search_fees_absorbed"] == 1
    finally:
        await client.close()


def test_old_partial_search_receipt_prices_tokens_but_waives_unconfirmed_search_fees():
    record = {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "service_tier": "default",
        "outcome": "response_received",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "cached_input_tokens": 200,
            "cache_write_input_tokens": 300,
            "web_search_calls": 4,
            "billable_web_search_calls": None,
        },
    }
    amount, proof = receipt_charge(
        service_terms(SPECS["organic.audit"].definition), "native_model", record
    )
    assert amount == 299_000
    assert proof["unpriced_search_fees_absorbed"] == 4


async def test_only_approved_initial_setup_inherits_free_onboarding(billed):
    from test_growth_onboarding import PLAN

    f = billed
    parent = await admit(f, "growth.onboarding")
    step = f"onboarding:{parent.id}:setup:0:visibility.audit:first"
    with pytest.raises(BillingError, match="free setup"):
        await admit(f, "visibility.audit", {"target": "this project"}, parent=parent, step=step)
    async with f.db.pool.acquire() as conn:
        key = f"onboarding:{parent.id}:approved_plan"
        await f.db.start_effect(conn, execution_key=key, operation="growth.onboarding")
        await f.db.complete_effect(
            conn, execution_key=key, result={"text": PLAN, "revision": "d" * 40}
        )
    with pytest.raises(BillingError, match="free setup"):
        await admit(
            f, "visibility.audit", {"target": "not the approved target"}, parent=parent, step=step
        )
    child = await admit(f, "visibility.audit", {"target": "this project"}, parent=parent, step=step)
    assert (await f.billing.run_charge(child.id, ACTOR))["root_run_id"] == str(parent.id)
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 0
    workflow = await install(f, "visibility.audit")
    inputs = normalize_workflow_inputs(
        schema=workflow.definition["input_schema"],
        project_id=f.project.id,
        inputs={"target": "this project"},
    )
    with pytest.raises(BillingError, match="standing spending"):
        await f.db.create_run(
            project_id=f.project.id,
            workflow_id=workflow.id,
            started_by_clerk_user_id=ACTOR,
            input_payload=inputs,
            trigger_source="schedule",
            pinned_definition=workflow.definition,
            definition_commit_sha=workflow.current_commit_sha,
        )
    with pytest.raises(BillingError, match="Add credits"):
        await admit(f, "visibility.audit", {"target": "this project"})


async def test_parallel_paid_calls_cannot_overdraw_parent(billed, monkeypatch):
    f = billed
    await fund(f)
    original = f.billing.terms

    def small_parent(definition, project_id, inputs=None):
        terms = original(definition, project_id, inputs)
        return {**terms, "maximum_nanos": 5_000_000_000} if terms["kind"] == "parent" else terms

    monkeypatch.setattr(f.billing, "terms", small_parent)
    parent = await admit(f, "organic.traffic_system", PARENT)
    audit = await admit(f, "organic.audit", SITE, parent=parent, step=f"system:{parent.id}:audit")
    keywords = await admit(
        f, "organic.keyword_plan", KEYWORDS, parent=parent, step=f"system:{parent.id}:keywords"
    )

    async def dispatch(run):
        async with f.db.pool.acquire() as conn:
            return await f.billing.begin_operation(
                conn, run_id=run.id, operation_id=str(run.id), kind="tool", maximum=4_000_000_000
            )

    results = await asyncio.gather(dispatch(audit), dispatch(keywords), return_exceptions=True)
    assert sum(isinstance(r, BillingError) for r in results) == 1
    assert (
        await f.db.pool.fetchval(
            "SELECT committed_nanos FROM billing_run_budgets WHERE run_id=$1", parent.id
        )
        == 4_000_000_000
    )
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 1


async def test_unknown_supplier_cost_stays_pending_until_reconciliation_deadline(billed):
    f = billed
    await fund(f)
    run = await admit(f, "organic.audit", SITE)
    async with f.db.pool.acquire() as conn:
        with external_usage_scope(f.db, conn, run.id, "missing-cost", maximum_usd="0.05"):
            observation = await begin_observation("dataforseo", "tool", "on_page/task_post")
            await observe_tool(observation, {})
    await finish(f, run)
    assert await f.billing.settle(run.id) is None
    assert (await f.billing.run_charge(run.id, ACTOR))["charged_usd"] is None
    await f.db.pool.execute(
        "UPDATE billing_run_budgets SET reconcile_by=now()-interval '1 second' WHERE run_id=$1",
        run.id,
    )
    assert await f.billing.settle(run.id) == 0
    assert (
        await f.db.pool.fetchval("SELECT status FROM billing_operations WHERE run_id=$1", run.id)
        == "absorbed"
    )
