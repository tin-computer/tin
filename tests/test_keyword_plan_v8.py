"""Keyword policy v8: v7's batched screening with caps sized as runaway guards.

Runs a59cded8 and de1a4d3e stopped screening at exactly 5,000 output tokens after 3.5-4.2k
tokens of reasoning. v7 (8,000, retry 16,000) is deployed and stays as it is; v8 raises the
screening caps to 32,000 and 64,000 and changes nothing else.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal

import pytest
from temporalio.exceptions import ApplicationError
from test_keyword_plan import fixture
from test_keyword_plan_v6 import worst_run
from test_keyword_plan_v7 import SCREENING, collected, label

from tin_lite import keyword_plan_v2 as v2
from tin_lite import keyword_plan_v6 as v6
from tin_lite import keyword_plan_v7 as v7
from tin_lite import keyword_plan_v8 as v8
from tin_lite import organic_system
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.executor_gates import KEYWORD_DEFAULT_USD, KEYWORD_MINIMUM_USD
from tin_lite.keyword_plan_activities import CONTRACTS, MINIMUM_CEILING, triage_reservations
from tin_lite.model_providers import MessageRole, ModelMessage, ModelRequest
from tin_lite.organic_audit import canonical_json
from tin_lite.service_pricing import CARD, NANOS_PER_DOLLAR

LUNA = CARD["models"]["gpt-6-luna"]
CAPS = {
    "triage_output_tokens",
    "triage_retry_output_tokens",
    "triage_reservation_usd",
    "triage_retry_reservation_usd",
}

# ---------------------------------------------------------------- the pinned contracts


def test_v8_changes_only_screening_caps_and_leaves_v7_alone():
    assert {k: v for k, v in v8.POLICY.items() if k not in CAPS | {"version"}} == {
        k: v for k, v in v7.POLICY.items() if k not in CAPS | {"version"}
    }
    assert v8.POLICY["version"] == "keyword-plan-v8"
    assert v8.POLICY["triage_batch_size"] == 50
    assert v8.INSTRUCTIONS == v7.INSTRUCTIONS and v8.SCHEMAS == v7.SCHEMAS
    assert v8.seed_values is v7.seed_values
    # v7 is deployed: its caps and reservations are what its pinned runs send and hold.
    assert v7.POLICY["version"] == "keyword-plan-v7"
    assert v7.POLICY["triage_output_tokens"] == 8000
    assert v7.POLICY["triage_retry_output_tokens"] == 16000
    assert v7.POLICY["triage_reservation_usd"] == v7.POLICY["triage_retry_reservation_usd"]
    assert v7.POLICY["triage_reservation_usd"] == "0.02"
    # Seeds and review keep v6's caps in every later policy.
    for policy in (v6.POLICY, v7.POLICY, v8.POLICY):
        assert policy["seed_output_tokens"] == 2000
        assert policy["review_output_tokens"] == 24000
    assert set(v8.POLICY) - set(v6.POLICY) <= SCREENING
    # Every earlier policy stays supported by the worker.
    for policy in (v2.POLICY, v6.POLICY, v7.POLICY, v8.POLICY):
        assert CONTRACTS[policy["version"]][0] is policy
    keyword = next(spec for spec in BUILTIN_WORKFLOWS if spec.key == "organic.keyword_plan")
    assert keyword.definition["keyword_policy"] == v8.POLICY
    assert keyword.version_label == "0.8.0"


def test_v8_caps_stay_inside_the_route_and_reservations_cover_their_bounds():
    policy = v8.POLICY
    assert policy["triage_output_tokens"] == 32_000
    assert policy["triage_retry_output_tokens"] == 2 * policy["triage_output_tokens"] == 64_000
    # GPT-6 Luna allows 128,000 output tokens per response.
    assert policy["triage_retry_output_tokens"] <= v8.MODEL_OUTPUT_LIMIT == 128_000
    # The recorder counts request bytes plus 4,096 as input tokens; that stays standard band.
    tokens = policy["triage_max_request_bytes"] + 4096
    assert tokens <= CARD["long_context_above_input_tokens"]
    rate = LUNA["standard"]
    worst = {}
    for output, reservation in (
        (policy["triage_output_tokens"], policy["triage_reservation_usd"]),
        (policy["triage_retry_output_tokens"], policy["triage_retry_reservation_usd"]),
    ):
        worst[output] = Decimal(
            tokens * max(rate["input"], rate["cache_write"]) + output * rate["output"]
        ) / Decimal(NANOS_PER_DOLLAR)
        assert worst[output] < Decimal(reservation)
    assert worst[32_000] == Decimal("0.026512")
    assert worst[64_000] == Decimal("0.042512")


def test_v8_worst_run_fits_its_floor_and_the_catalog_defaults():
    policy = v8.POLICY
    assert v7.batch_count(policy) == 6
    batched = Decimal(policy["triage_reservation_usd"]) + Decimal(
        policy["triage_retry_reservation_usd"]
    )
    total = (
        worst_run(policy)
        - Decimal(policy["triage_reservation_usd"])
        + v7.batch_count(policy) * batched
    )
    # Seeds $0.10, review $0.15, screening $0.45, 22 lookups $1.10 and 40 samples $0.20.
    assert total == Decimal("2.00") <= Decimal(policy["minimum_ceiling_usd"])
    assert MINIMUM_CEILING == Decimal(policy["minimum_ceiling_usd"]) == Decimal("2")
    assert KEYWORD_MINIMUM_USD == 2 and KEYWORD_DEFAULT_USD == 2
    keyword = next(spec for spec in BUILTIN_WORKFLOWS if spec.key == "organic.keyword_plan")
    limit = keyword.input_schema["properties"]["max_cost_usd"]
    assert limit["minimum"] == limit["default"] == 2
    system = organic_system.INPUT_SCHEMA["properties"]["keyword_max_cost_usd"]
    assert system["minimum"] == system["default"] == 2
    # Screening's share held back before research: one first attempt per batch.
    assert triage_reservations(policy) == [(f"triage:{index}", "0.03") for index in range(6)]


def test_the_largest_possible_v8_batch_fits_the_request_bound():
    """Bounded inputs (2,000-character contexts, 120-character keywords) never hit the cap."""
    scope = {
        "url": "https://" + "a" * 240 + ".example/",
        "host": "a" * 240 + ".example",
        "market": "US",
        "language": "en",
        "buyer_context": "é" * 2000,
        "started_at": "2026-10-02T00:00:00+00:00",
        "max_cost_usd": "2",
        "policy_version": v8.POLICY["version"],
        "integrations": {
            "status": "observed",
            "connections": [{"provider": "custom.api." + "x" * 60, "status": "connected"}] * 50,
            "truncated": True,
            "meaning": "Availability only.",
        },
        "audit": {"context_excerpt": '"' * 2000, "paths": {"AUDIT.md": "r" * 200}},
        "gsc": {"status": "unavailable", "reason": "x" * 200},
    }
    batch = [
        {"id": f"kw_{index:016x}", "keyword": '"' * 120}
        for index in range(v8.POLICY["triage_batch_size"])
    ]
    request = ModelRequest(
        messages=(
            ModelMessage(
                role=MessageRole.USER,
                content=canonical_json(v2.triage_input(scope, batch)).decode(),
            ),
        ),
        system=v8.INSTRUCTIONS["triage"],
        output_schema=v2.model_schema("triage", batch),
        output_schema_name="keyword_triage",
        max_output_tokens=v8.POLICY["triage_retry_output_tokens"],
    )
    size = len(json.dumps(asdict(request), ensure_ascii=False).encode())
    assert size < v8.POLICY["triage_max_request_bytes"]


# ---------------------------------------------------------------- runs


async def test_new_runs_pin_v8_and_screen_with_its_caps():
    activities, db, _storage, _provider, _model, calls = await collected("current")
    run_id = str(db.run.id)
    assert (await activities._result(run_id, "scope"))["policy_version"] == "keyword-plan-v8"
    await activities.keyword_collect(run_id)
    collection = await activities._result(run_id, "collection")
    assert len(collection["candidates"]) > 2 * v8.POLICY["triage_batch_size"]
    assert calls and all(len(keywords) <= 50 and tokens == 32_000 for tokens, keywords in calls)
    assert all(row["buyer_fit"] == label(row["keyword"]) for row in collection["candidates"])
    ledger = db.effects[activities.key(run_id, "budget")].result
    assert all(ledger[f"triage:{index}"] == "0.03" for index in range(len(calls)))


async def test_v8_merges_to_exactly_what_v7_returns():
    results = {}
    for version in ("v7", "current"):
        activities, db, _storage, _provider, _model, calls = await collected(version)
        await activities.keyword_collect(str(db.run.id))
        results[version] = (await activities._result(str(db.run.id), "collection"), calls)
    (v7_collection, v7_calls), (v8_collection, v8_calls) = results["v7"], results["current"]
    assert [tokens for tokens, _ in v7_calls] == [8000] * len(v7_calls)
    assert [tokens for tokens, _ in v8_calls] == [32_000] * len(v8_calls)
    assert sorted(keywords for _, keywords in v7_calls) == sorted(
        keywords for _, keywords in v8_calls
    )

    def labels(collection):
        return [(row["id"], row["buyer_fit"]) for row in collection["candidates"]]

    assert labels(v8_collection) == labels(v7_collection)


async def test_a_cut_off_v8_batch_is_asked_once_more_with_twice_the_cap(monkeypatch):
    monkeypatch.setattr("tin_lite.keyword_plan_activities.TRIAGE_CONCURRENCY", 1)
    activities, db, _storage, _provider, _model, calls = await collected(
        "current", cut=lambda request, count: count == 1
    )
    run_id = str(db.run.id)
    await activities.keyword_collect(run_id)
    collection = await activities._result(run_id, "collection")
    assert collection["triage_stages"][:3] == ["triage:0", "triage:0:retry", "triage:1"]
    assert [tokens for tokens, _ in calls[:3]] == [32_000, 64_000, 32_000]
    assert calls[0][1] == calls[1][1]  # The same batch, unchanged.
    ledger = db.effects[activities.key(run_id, "budget")].result
    assert ledger["triage:0"] == "0.03" and ledger["triage:0:retry"] == "0.045"
    first = await activities._result(run_id, "triage:0")
    assert first["status"] == "unknown" and first["reason"] == "output_truncated"
    assert await activities._result(run_id, "failure") is None


async def test_a_v8_batch_cut_off_twice_fails_named(monkeypatch):
    monkeypatch.setattr("tin_lite.keyword_plan_activities.TRIAGE_CONCURRENCY", 1)
    activities, db, *_rest, calls = await collected(
        "current", cut=lambda request, count: count in {1, 2}
    )
    run_id = str(db.run.id)
    with pytest.raises(ApplicationError, match="stopped at its output limit"):
        await activities.keyword_collect(run_id)
    assert [tokens for tokens, _ in calls] == [32_000, 64_000]
    failure = await activities._result(run_id, "failure")
    assert failure["code"] == "output_truncated" and failure["batch"] == 0
    await activities.keyword_failure(run_id)
    message = db.project_failure.await_args.kwargs["error_message"]
    assert "screening batch 1 of" in message and "so did its retry with twice the limit" in message


async def test_v8_run_at_its_two_dollar_floor_refuses_nothing():
    activities, db, *_ = await fixture(
        prepare=False, modern="current", budget=2, inputs={"max_cost_usd": 2}
    )
    await activities.keyword_prepare(str(db.run.id))
    activities, db, *_ = await fixture(
        prepare=False, modern="current", budget=1.99, inputs={"max_cost_usd": 2}
    )
    with pytest.raises(ApplicationError, match=r"at least \$2"):
        await activities.keyword_prepare(str(db.run.id))
