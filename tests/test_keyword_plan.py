from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from test_organic_audit import MemoryDB
from test_procedure_publication import HistoryStorage

from tin_lite import keyword_plan as v1
from tin_lite import keyword_plan_v2 as v2
from tin_lite import keyword_plan_v3 as v3
from tin_lite import keyword_plan_v5 as v5
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.dataforseo import DataForSEOError
from tin_lite.domain import EffectReceipt, RunStatus
from tin_lite.integrations import IntegrationAuthorizationError, IntegrationService
from tin_lite.keyword_data import ENDPOINTS, KeywordData, request_for
from tin_lite.keyword_plan import (
    KEY,
    LIMITS,
    POLICY,
    build_documents,
    check_inputs,
    gsc_property_matches,
    gsc_rows,
    keyword_id,
    keyword_rows,
    overlaps,
    paths,
    phrases,
    review_input,
    safe_url,
    select_candidates,
    serp_items,
    validate_review,
)
from tin_lite.keyword_plan_activities import KeywordPlanActivities, model_failure_cause
from tin_lite.model_providers import ModelProviderError, ModelResult, ModelUsage, ProviderName
from tin_lite.organic_audit import canonical_json, digest
from tin_lite.publication import PublicationPendingError
from tin_lite.workflow_inputs import normalize_workflow_inputs
from tin_lite.workflows import KeywordPlanWorkflow, registered_workflow_implementations

SPEC = next(item for item in BUILTIN_WORKFLOWS if item.key == KEY)
INPUTS = {
    "site_url": "https://example.com/",
    "market": "US",
    "buyer_context": "Scheduling software for small consulting teams.",
    "seed_phrases": ["consulting scheduling software"],
    "max_cost_usd": 10,
}


def provider_row(keyword="consulting scheduling software", *, url="https://example.com/schedule"):
    return {
        "keyword_data": {
            "keyword": keyword,
            "keyword_info": {
                "search_volume": 30,
                "cpc": 2.5,
                "competition": 0.8,
                "last_updated_time": "2026-08-20 00:00:00 +00:00",
            },
            "keyword_properties": {"keyword_difficulty": 15},
        },
        "ranked_serp_element": {"serp_item": {"url": url, "rank_group": 8}},
    }


def review_for(candidates):
    ids = [row["id"] for row in candidates]
    return (
        {
            "groups": [
                {
                    "title": "Consulting scheduling",
                    "keyword_ids": ids,
                    "primary_keyword_id": ids[0],
                    "intent": "commercial",
                    "priority": "high",
                    "rationale": "A relevant buyer evaluates scheduling tools.",
                    "page_approach": "Inspect the existing comparison page first.",
                    "evidence_needed": "Gather original product examples and verified limitations.",
                }
            ],
            "excluded": [],
        }
        if ids
        else {"groups": [], "excluded": []}
    )


def providers():
    async def query(kind, **kwargs):
        if kind == "competitors":
            items = [{"domain": "competitor.example"}]
        elif kind == "serp":
            items = [
                {
                    "type": "organic",
                    "rank_group": 1,
                    "url": "https://competitor.example/compare",
                    "title": "Scheduling tools",
                    "description": "A comparison.",
                }
            ]
        else:
            items = [provider_row()]
        return {
            "items": items,
            "items_count": len(items),
            "total_count": len(items),
            "reported_cost_usd": "0.01",
            "provider_task_id": "fixture-only",
        }

    async def generate(route, request, *, timeout_seconds=None):
        data = json.loads(request.messages[0].content)
        if (
            request.output_schema_name == "keyword_seeds"
            and "core" in request.output_schema["properties"]
        ):
            value = {
                "core": ["scheduling software", "booking api", "calendar integration"],
                "specific": ["consulting scheduling software"],
            }
        elif request.output_schema_name == "keyword_triage":
            value = {key: "direct" for key in data["candidates"]}
        elif (
            request.output_schema_name == "keyword_review"
            and "assignments" in request.output_schema["properties"]
        ):
            legacy = review_for(data["candidates"])["groups"][0]
            value = {
                "groups": [
                    {
                        key: val
                        for key, val in legacy.items()
                        if key not in {"keyword_ids", "primary_keyword_id"}
                    }
                    | {"ref": "g1", "primary": data["candidates"][0]["id"]}
                ],
                "assignments": {row["id"]: "g1" for row in data["candidates"]},
            }
        else:
            value = (
                review_for(data["candidates"])
                if "candidates" in data
                else {"seeds": ["consulting scheduling software"]}
            )
        return ModelResult(
            provider=ProviderName.OPENAI,
            model=POLICY["model"],
            text="",
            parsed=value,
            request_id="fixture-model",
            usage=ModelUsage(100, 100),
        )

    return (
        SimpleNamespace(
            query=AsyncMock(side_effect=query),
            validate_target=AsyncMock(return_value=("https://example.com/", "example.com")),
        ),
        SimpleNamespace(generate=AsyncMock(side_effect=generate)),
    )


async def fixture(*, prepare=True, inputs=None, budget=10, modern=False):
    db, storage = MemoryDB(), HistoryStorage()
    db.run = replace(
        db.run, executor=KEY, created_at=datetime.now(UTC), input={**INPUTS, **(inputs or {})}
    )
    db.complete_keyword_plan_projection = AsyncMock()
    db.get_integration_connection = AsyncMock(return_value=None)
    definition = SPEC.definition
    if not modern:
        definition = {
            **definition,
            "keyword_policy": v1.POLICY,
            "keyword_instructions": v1.INSTRUCTIONS,
            "keyword_schemas": v1.SCHEMAS,
        }
    elif modern is True:
        definition = {
            **definition,
            "keyword_policy": v2.POLICY,
            "keyword_instructions": v2.INSTRUCTIONS,
            "keyword_schemas": v2.SCHEMAS,
        }
    elif modern == "v3":
        definition = {
            **definition,
            "keyword_policy": v3.POLICY,
            "keyword_instructions": v3.INSTRUCTIONS,
            "keyword_schemas": v3.SCHEMAS,
        }
    elif modern == "v4":
        from tin_lite import keyword_plan_v4 as v4

        definition = {
            **definition,
            "keyword_policy": v4.POLICY,
            "keyword_instructions": v4.INSTRUCTIONS,
            "keyword_schemas": v4.SCHEMAS,
        }
    storage.read_canonical_artifact = AsyncMock(return_value=canonical_json(definition))
    provider, model = providers()
    activities = KeywordPlanActivities(
        database=db,
        storage=storage,
        settings=SimpleNamespace(keyword_plan_max_cost_usd=budget, luna_api_key="fixture"),
        provider=provider,
        router=model,
    )
    if prepare:
        await activities.keyword_prepare(str(db.run.id))
    return activities, db, storage, provider, model


async def finish(activities, run_id):
    await activities.keyword_prepare(run_id)
    await activities.keyword_collect(run_id)
    for index in range(await activities.keyword_sample_count(run_id)):
        await activities.keyword_inspect({"run_id": run_id, "index": index})
    await activities.keyword_review(run_id)
    await activities.keyword_publish(run_id)


def test_catalog_pins_native_contract_and_supported_form():
    assert len({item.id for item in BUILTIN_WORKFLOWS}) == len(BUILTIN_WORKFLOWS)
    assert SPEC.executor == KEY and SPEC.review_policy is None
    assert SPEC.definition["keyword_policy"] == v5.POLICY
    assert SPEC.definition["system"] == "organic-traffic"
    assert registered_workflow_implementations()[KEY] is KeywordPlanWorkflow
    normalized = normalize_workflow_inputs(
        schema=SPEC.input_schema, inputs=INPUTS, project_id="00000000-0000-4000-8000-000000000001"
    )
    check_inputs(normalized)
    assert normalized["use_search_console"] is True and normalized["audit_run_id"] == ""


def test_normalization_preserves_distinctions_unknowns_and_provenance():
    assert phrases(["  API  tools", "api tools", "tool", "tools", "tools API"]) == [
        "API tools",
        "tool",
        "tools",
        "tools API",
    ]
    rows = keyword_rows(
        [
            provider_row(),
            {"keyword": "unknown"},
            {"keyword": "bad", "ranked_serp_element": "invalid"},
        ],
        source_id="target",
        market="US",
        observed_at="today",
    )
    assert len(rows) == 2 and rows[1]["search_volume"] is None
    assert rows[0]["keyword_difficulty"] == 15 and rows[0]["paid_competition"] == 0.8
    assert keyword_id("API tools", "US") != keyword_id("API tools", "GB")
    candidates, coverage = select_candidates({"target": rows, "other": rows}, limit=1)
    assert coverage["omitted"] == 1 and candidates[0]["observations"][0]["search_volume"] == 30
    assert safe_url("javascript:alert(1)") is None
    assert safe_url("https://secret:pass@example.com/") is None
    assert safe_url("http://127.0.0.1/") is None


def test_search_console_exact_property_and_country_scope():
    assert gsc_property_matches("sc-domain:example.com", "www.example.com")
    assert not gsc_property_matches("sc-domain:other.com", "example.com")
    assert not gsc_property_matches("https://example.com/blog/", "example.com")
    raw = [
        {"keys": ["scheduling", url, country], "clicks": 2, "impressions": 50, "position": 4.5}
        for url, country in [
            ("https://example.com/schedule", "usa"),
            ("https://example.com/schedule", "gbr"),
            ("https://other.example/schedule", "usa"),
        ]
    ]
    rows = gsc_rows(raw, target="example.com", market="US", observed_at="today")
    assert len(rows) == 1 and rows[0]["search_volume"] is None
    assert rows[0]["impressions"] == 50 and rows[0]["position"] == 4.5


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "invented", "primary", "metric"])
def test_review_rejects_invented_or_missing_assignments(mutation):
    candidates = [{"id": "a"}, {"id": "b"}]
    review = review_for(candidates)
    group = review["groups"][0]
    if mutation == "duplicate":
        group["keyword_ids"].append("a")
    elif mutation == "missing":
        group["keyword_ids"].pop()
    elif mutation == "invented":
        group["keyword_ids"].append("invented")
    elif mutation == "primary":
        group["primary_keyword_id"] = "invented"
    else:
        group["search_volume"] = 1000000
    with pytest.raises(ValueError):
        validate_review(review, candidates)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(ENDPOINTS))
async def test_live_adapter_uses_fixed_scope_and_bounds_without_retries(kind):
    value = {
        "ranked_relevant": {"host": "example.com", "seeds": ["scheduling"]},
        "overview": ["scheduling"],
        "overview_batch": ["scheduling"],
        "ad_traffic": {"keywords": ["scheduling"], "bid": 2.5},
    }.get(
        kind,
        "example.com"
        if kind in {"ranked", "competitors", "ads_search", "ranked_paid"}
        else "scheduling",
    )
    expected = request_for(kind, market="US", value=value, tag="fixture")
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == f"https://api.dataforseo.com/v3/{ENDPOINTS[kind]}"
        assert json.loads(request.content) == [expected]
        # The traffic forecast answers with aggregate rows, not an items envelope.
        result = (
            []
            if kind == "ad_traffic"
            else [
                {
                    "items": [],
                    "items_count": 0,
                    "total_count": 0,
                    "location_code": 2840,
                    "language_code": "en",
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "status_code": 20000,
                "tasks": [
                    {
                        "id": "test",
                        "status_code": 20000,
                        "cost": 0.01,
                        "data": expected,
                        "result": result,
                    }
                ],
            },
        )

    provider = KeywordData("fixture", "not-a-credential", transport=httpx.MockTransport(handler))
    result = await provider.query(kind, market="US", value=value, tag="fixture")
    assert result["items"] == [] and len(calls) == 1


@pytest.mark.asyncio
async def test_no_search_results_is_a_completed_empty_lookup():
    expected = request_for("ads_search", market="US", value="quiet.example", tag="fixture")

    def handler(request):
        return httpx.Response(
            200,
            json={
                "status_code": 20000,
                "tasks": [
                    {
                        "id": "test",
                        "status_code": 20100,
                        "status_message": "No Search Results.",
                        "cost": 0.002,
                        "result_count": 0,
                        "data": expected,
                        "result": None,
                    }
                ],
            },
        )

    provider = KeywordData("fixture", "x", transport=httpx.MockTransport(handler))
    result = await provider.query("ads_search", market="US", value="quiet.example", tag="fixture")
    assert result["items"] == [] and result["reported_cost_usd"] == "0.002"


def test_request_for_paid_kinds_are_bounded():
    with pytest.raises(ValueError):
        request_for("overview_batch", market="US", value=["k"] * 41, tag="t")
    with pytest.raises(ValueError):
        request_for("ad_traffic", market="US", value={"keywords": ["k"] * 41, "bid": 2}, tag="t")
    with pytest.raises(ValueError):
        request_for("ad_traffic", market="US", value={"keywords": ["k"], "bid": 0}, tag="t")
    with pytest.raises(ValueError):
        request_for("ad_traffic", market="US", value={"keywords": ["k"], "bid": True}, tag="t")
    forecast = request_for("ad_traffic", market="US", value={"keywords": ["k"], "bid": 3}, tag="t")
    assert forecast["match"] == "phrase" and forecast["bid"] == 3.0
    assert "language_code" not in request_for("ads_search", market="US", value="a.example", tag="t")
    assert request_for("ranked_paid", market="US", value="a.example", tag="t")["item_types"] == [
        "paid"
    ]


@pytest.mark.asyncio
async def test_ad_traffic_accepts_one_aggregate_row_and_rejects_more_than_requested():
    value = {"keywords": ["scheduling"], "bid": 2.5}
    expected = request_for("ad_traffic", market="US", value=value, tag="fixture")
    row = {"keyword": None, "bid": 2.5, "match": "phrase", "clicks": 12.5, "cost": 31.2}

    def respond(rows):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "status_code": 20000,
                    "tasks": [
                        {
                            "id": "test",
                            "status_code": 20000,
                            "cost": 0.09,
                            "data": expected,
                            "result": rows,
                        }
                    ],
                },
            )

        return KeywordData("fixture", "x", transport=httpx.MockTransport(handler))

    result = await respond([row]).query("ad_traffic", market="US", value=value, tag="fixture")
    assert result["items"] == [row] and result["reported_cost_usd"] == "0.09"
    with pytest.raises(DataForSEOError):
        await respond([row, row]).query("ad_traffic", market="US", value=value, tag="fixture")
    with pytest.raises(DataForSEOError):
        await respond(["row"]).query("ad_traffic", market="US", value=value, tag="fixture")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["scope", "target", "oversize", "cost", "http"])
async def test_live_adapter_fails_closed_without_retry(invalid):
    expected = request_for("ranked", market="US", value="example.com", tag="fixture")
    data = dict(expected)
    if invalid == "scope":
        data["location_code"] = 2826
    if invalid == "target":
        data["target"] = "other.example"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            500 if invalid == "http" else 200,
            json={
                "status_code": 20000,
                "tasks": [
                    {
                        "status_code": 20000,
                        "cost": "NaN" if invalid == "cost" else 0.01,
                        "data": data,
                        "result": [{"items": [{}] * (201 if invalid == "oversize" else 0)}],
                    }
                ],
            },
        )

    provider = KeywordData("fixture", "not-a-credential", transport=httpx.MockTransport(handler))
    with pytest.raises(DataForSEOError):
        await provider.query("ranked", market="US", value="example.com", tag="fixture")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_duplicate_execution_uses_saved_calls_and_one_atomic_commit():
    activities, db, storage, provider, model = await fixture()
    run_id = str(db.run.id)
    storage.repo.lose_response = True
    with pytest.raises(PublicationPendingError):
        await finish(activities, run_id)
    count = provider.query.await_count
    await finish(activities, run_id)
    assert count == provider.query.await_count == 7
    assert model.generate.await_count == 1 and storage.repo.writes == 1
    saved = storage.repo.trees[storage.repo.head]
    assert set(saved) == {"README.md", *paths(run_id).values()}
    inventory = json.loads(saved[paths(run_id)["keywords.json"]][1])
    evidence = json.loads(saved[paths(run_id)["evidence.json"]][1])
    assert evidence["inventory_sha256"] == digest(inventory)
    assert inventory["keywords"][0]["observations"][0]["search_volume"] == 30
    assert inventory["groups"][0]["existing_page_candidates"] == ["https://example.com/schedule"]
    assert "calendar" not in inventory and "rows" not in evidence["collection"]["target"]["value"]


@pytest.mark.asyncio
async def test_unknown_attempt_never_repurchased_and_fingerprint_is_enforced():
    activities, db, _, provider, _ = await fixture()
    run_id = str(db.run.id)
    provider.query.side_effect = TimeoutError("secret provider error")
    result = await activities._query(run_id, "target", "ranked", "example.com")
    assert result["status"] == "unknown" and "secret" not in json.dumps(result)
    assert await activities._query(run_id, "target", "ranked", "example.com") == result
    assert provider.query.await_count == 1
    with pytest.raises(ApplicationError, match="request changed"):
        await activities._query(run_id, "target", "ranked", "other.example")
    key = activities.key(run_id, "crashed")
    request = {"query": "unchanged"}
    db.effects[key] = EffectReceipt(
        key, KEY, "started", {"request_sha256": digest(request), "attempted_at": "before crash"}
    )
    call = AsyncMock()
    result = await activities._paid(run_id, "crashed", request, "0.10", call)
    assert result["status"] == "unknown" and call.await_count == 0


@pytest.mark.asyncio
async def test_budget_reserves_review_before_research_and_recovers_prepare_gap():
    activities, db, _, provider, _ = await fixture(budget=5)
    run_id = str(db.run.id)
    del db.effects[activities.key(run_id, "budget")]
    await activities.keyword_prepare(run_id)
    results = await asyncio.gather(
        *(activities._query(run_id, f"query:{i}", "ranked", "example.com") for i in range(20))
    )
    assert provider.query.await_count == 10
    assert sum(item["status"] == "unavailable" for item in results) == 10
    ledger = db.effects[activities.key(run_id, "budget")].result
    assert sum(Decimal(value) for value in ledger.values()) == 5 and ledger["review"] == "4.00"


@pytest.mark.asyncio
async def test_disabled_or_wrong_pinned_definition_never_calls_provider():
    activities, db, storage, provider, model = await fixture(prepare=False, budget=0)
    with pytest.raises(ApplicationError):
        await activities.keyword_prepare(str(db.run.id))
    activities.settings.keyword_plan_max_cost_usd = 10
    storage.read_canonical_artifact.return_value = canonical_json({})
    with pytest.raises(ApplicationError, match="pinned"):
        await activities.keyword_prepare(str(db.run.id))
    assert provider.query.await_count == model.generate.await_count == 0


@pytest.mark.asyncio
async def test_stopped_run_cannot_purchase_more_calls():
    activities, db, _, provider, _ = await fixture()
    db.run = replace(db.run, status=RunStatus.STOPPED)
    with pytest.raises(ApplicationError, match="not active"):
        await activities._query(str(db.run.id), "target", "ranked", "example.com")
    assert provider.query.await_count == 0


@pytest.mark.asyncio
async def test_optional_missing_search_console_does_not_block_research():
    activities, db, _, _, _ = await fixture(inputs={"use_search_console": True})
    await finish(activities, str(db.run.id))
    collection = await activities._result(str(db.run.id), "collection")
    assert collection["sources"]["gsc"]["status"] == "unavailable"
    assert any("Search Console" in note for note in collection["coverage"]["notes"])


@pytest.mark.asyncio
async def test_matching_search_console_is_pinned_cached_and_reaches_the_reviewer():
    activities, db, _, _, _ = await fixture(prepare=False, inputs={"use_search_console": True})
    db.get_integration_connection.return_value = SimpleNamespace(
        status="connected", configuration={"selected_site_url": "sc-domain:example.com"}
    )
    activities.integrations = SimpleNamespace(
        search_console_analytics=AsyncMock(
            return_value={
                "rows": [
                    {
                        "keys": ["consulting scheduling software", "https://example.com/", "usa"],
                        "clicks": 7,
                        "impressions": 90,
                        "position": 3.4,
                    }
                ]
            }
        )
    )
    run_id = str(db.run.id)
    await finish(activities, run_id)
    await finish(activities, run_id)
    read = activities.integrations.search_console_analytics
    assert read.await_count == 1
    assert read.call_args.kwargs["expected_site_url"] == "sc-domain:example.com"
    assert read.call_args.kwargs["dimensions"] == ("query", "page", "country")
    data = json.loads(activities.router.generate.call_args.args[1].messages[0].content)
    assert data["candidates"][0]["search_console_observation"]["impressions"] == 90
    # The integration gateway rechecks the current property immediately before its provider call.
    gateway = SimpleNamespace(
        _connection=AsyncMock(
            return_value=SimpleNamespace(
                configuration={"selected_site_url": "sc-domain:other.example"}
            )
        )
    )
    with pytest.raises(IntegrationAuthorizationError, match="changed"):
        await IntegrationService.search_console_analytics(
            gateway,
            project_id=db.run.project_id,
            start_date="2026-06-01",
            end_date="2026-08-29",
            expected_site_url="sc-domain:example.com",
        )


@pytest.mark.asyncio
async def test_seed_derivation_is_optional_receipted_and_used_once():
    activities, db, _, _, model = await fixture(inputs={"seed_phrases": []})
    for _ in range(2):
        await finish(activities, str(db.run.id))
    assert model.generate.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["seeds", "review"])
async def test_invalid_model_data_is_not_copied_into_temporal_errors(stage):
    activities, db, storage, _, model = await fixture(inputs={"seed_phrases": []})
    result = ModelResult(
        provider=ProviderName.OPENAI,
        model=POLICY["model"],
        text="",
        parsed={"private-output": "MUST NOT ENTER HISTORY"},
        request_id="fixture",
        usage=ModelUsage(),
    )
    if stage == "review":
        await activities.keyword_collect(str(db.run.id))
    model.generate.side_effect = None
    model.generate.return_value = result
    method = activities.keyword_collect if stage == "seeds" else activities.keyword_review
    for _ in range(2):
        with pytest.raises(ApplicationError, match="failed validation") as caught:
            await method(str(db.run.id))
        assert "MUST NOT" not in str(caught.value) and caught.value.__suppress_context__
    assert storage.repo.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_known_empty_is_not_an_unavailable_provider(unknown):
    activities, db, storage, provider, model = await fixture()
    if unknown:
        provider.query.side_effect = TimeoutError()
        with pytest.raises(ApplicationError, match="not fabricated"):
            await finish(activities, str(db.run.id))
        assert storage.repo.writes == 0
    else:
        provider.query.side_effect = None
        provider.query.return_value = {
            "items": [],
            "items_count": 0,
            "reported_cost_usd": "0.01",
            "provider_task_id": "empty-fixture",
        }
        await finish(activities, str(db.run.id))
        assert storage.repo.writes == 1
        assert (await activities._result(str(db.run.id), "review_validated")) == {
            "groups": [],
            "excluded": [],
        }
    assert model.generate.await_count == 0


@pytest.mark.asyncio
async def test_model_failure_does_not_purchase_a_replacement_or_publish():
    activities, db, storage, _, model = await fixture()
    run_id = str(db.run.id)
    await activities.keyword_collect(run_id)
    model.generate.side_effect = TimeoutError()
    for _ in range(2):
        with pytest.raises(ApplicationError, match="no replacement"):
            await activities.keyword_review(run_id)
    assert model.generate.await_count == 1 and storage.repo.writes == 0


@pytest.mark.asyncio
async def test_model_stages_wait_longer_than_the_client_default_for_large_verdicts():
    activities, db, _, _, model = await fixture(modern="v4", inputs={"seed_phrases": []})
    await finish(activities, str(db.run.id))
    waits = {
        call.args[1].output_schema_name: call.kwargs["timeout_seconds"]
        for call in model.generate.await_args_list
    }
    assert waits == {"keyword_seeds": None, "keyword_triage": 240, "keyword_review": 420}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason", "message"),
    [
        (
            TimeoutError("secret provider detail"),
            "provider_timeout",
            "the keyword review model call did not finish within 7 minutes",
        ),
        (
            ModelProviderError("OpenAI model request failed"),
            "provider_result_unavailable",
            "a model result could not be confirmed",
        ),
    ],
)
async def test_model_failure_names_its_cause_without_provider_text(error, reason, message):
    activities, db, _, _, model = await fixture()
    run_id = str(db.run.id)
    await activities.keyword_collect(run_id)
    model.generate.side_effect = error
    with pytest.raises(ApplicationError, match="no replacement"):
        await activities.keyword_review(run_id)
    failure = await activities._result(run_id, "failure")
    assert failure == {"code": "model_unavailable", "stage": "review", "reason": reason}
    receipt = await activities._result(run_id, "review")
    assert receipt["reason"] == reason and "secret" not in json.dumps(receipt)
    await activities.keyword_failure(run_id)
    text = db.project_failure.await_args.kwargs["error_message"]
    assert text == (
        f"Keyword planning stopped because {message}. "
        "Saved research was retained; no replacement call was purchased."
    )


@pytest.mark.parametrize(
    ("failure", "cause"),
    [
        ({"stage": "seeds", "reason": "provider_timeout"}, "did not finish in time"),
        ({"stage": "triage", "reason": "provider_timeout"}, "within 4 minutes"),
        ({"stage": "review", "reason": "provider_status_429"}, "rate-limited"),
        ({"stage": "review", "reason": "provider_status_400"}, "rejected the keyword review"),
        ({"stage": "review", "reason": "provider_connection"}, "connection"),
        ({"stage": "review", "reason": "invalid_result"}, "could not use"),
        ({"stage": "review", "reason": "spending_limit"}, "spending limit"),
        ({"stage": "review", "reason": "unconfirmed_previous_request"}, "earlier keyword review"),
        ({"stage": "review"}, "a model result could not be confirmed"),
    ],
)
def test_model_failure_cause_reads_only_fixed_labels(failure, cause):
    assert cause in model_failure_cause({"code": "model_unavailable", **failure})


def test_maximum_unicode_and_long_urls_remain_readable_with_explicit_omissions():
    rows = keyword_rows(
        [
            provider_row(
                f"{i} " + "界" * 115, url="https://example.com/" + str(i) + "/" + "x" * 1100
            )
            for i in range(600)
        ],
        source_id="target",
        market="US",
        observed_at="today",
    )
    candidates, coverage = select_candidates({"target": rows})
    assert len(canonical_json(candidates)) <= POLICY["candidate_bytes"]
    assert coverage["omitted"] > 0
    sample = serp_items(
        [
            {
                "type": "organic",
                "rank_group": i + 1,
                "url": "https://other.example/" + str(i) + "/" + "x" * 1100,
                "title": "界" * 160,
                "description": "界" * 240,
            }
            for i in range(10)
        ]
    )
    samples = {row["id"]: {"status": "completed", "items": sample} for row in candidates[:40]}
    assert len(sample) < 10 and overlaps(samples)[0]["shared_urls"] == len(sample)
    groups = []
    for i in range(80):
        selected = candidates[i::80]
        if selected:
            group = review_for(selected)["groups"][0]
            group.update(
                title="界" * 120,
                rationale="界" * 250,
                page_approach="界" * 200,
                evidence_needed="界" * 250,
            )
            groups.append(group)
    scope = {
        "url": "https://example.com/",
        "host": "example.com",
        "market": "US",
        "language": "en",
        "started_at": "today",
        "buyer_context": "界" * 2000,
    }
    data = review_input(scope=scope, candidates=candidates, samples=samples, coverage=coverage)
    assert len(canonical_json(data)) < POLICY["max_model_input_bytes"]
    # The independent review byte limit rejects an oversized otherwise-shaped response.
    review = {"groups": groups, "excluded": []}
    if len(canonical_json(review)) > 240_000:
        for group in groups:
            group["rationale"] = "Relevant buyer intent."
    documents = build_documents(
        run_id="00000000-0000-4000-8000-000000000001",
        project_id="00000000-0000-4000-8000-000000000002",
        definition_sha="d" * 40,
        scope=scope,
        candidates=candidates,
        samples=samples,
        review=review,
        coverage=coverage,
        evidence={},
    )
    for path, content in documents.items():
        assert 0 < len(content) <= LIMITS[path.rsplit("/", 1)[1]]
    assert "further groups" in next(
        value.decode() for path, value in documents.items() if path.endswith("PLAN.md")
    )
