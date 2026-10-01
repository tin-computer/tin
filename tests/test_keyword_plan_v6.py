"""Keyword policy v6 reservations and the organic ceilings they allow, from list prices."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError
from test_keyword_plan import fixture
from test_keyword_plan_concurrency import RUN_ID, track

from tin_lite import keyword_plan_v2 as v2
from tin_lite import keyword_plan_v5 as v5
from tin_lite import keyword_plan_v6 as v6
from tin_lite import organic_system
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.executor_gates import keyword_plan_gate, organic_system_gate
from tin_lite.keyword_plan import POLICY as V1
from tin_lite.service_pricing import CARD, NANOS_PER_DOLLAR

SPECS = {spec.key: spec for spec in BUILTIN_WORKFLOWS}
RESERVATIONS = {
    "labs_reservation_usd",
    "serp_reservation_usd",
    "seed_reservation_usd",
    "triage_reservation_usd",
    "review_reservation_usd",
}
# DataForSEO list prices checked September 29, 2026.
LABS_TASK, LABS_ITEM, SERP_PAGE = Decimal("0.012"), Decimal("0.00012"), Decimal("0.002")
LUNA = CARD["models"]["gpt-6-luna"]


def usd(nanos):
    return Decimal(nanos) / NANOS_PER_DOLLAR


def worst_run(policy):
    """Every reservation a run can make: model seeds, discovered competitors, full samples."""
    lookups = 2 + V1["max_competitors"] + 1 + 2 * V1["max_seeds"]
    return (
        sum(Decimal(policy[f"{stage}_reservation_usd"]) for stage in ("seed", "triage", "review"))
        + lookups * Decimal(policy["labs_reservation_usd"])
        + V1["max_serps"] * Decimal(policy["serp_reservation_usd"])
    )


def test_v6_changes_only_reservations_and_the_floor_and_leaves_v5_alone():
    changed = RESERVATIONS | {"version", "minimum_ceiling_usd"}
    assert {k: v for k, v in v6.POLICY.items() if k not in changed} == {
        k: v for k, v in v5.POLICY.items() if k not in changed
    }
    assert v6.INSTRUCTIONS == v5.INSTRUCTIONS and v6.SCHEMAS == v5.SCHEMAS
    assert v6.seed_values is v5.seed_values
    # Pinned v5 runs keep their reservations, fingerprints and $5 floor.
    assert {key: v5.POLICY[key] for key in RESERVATIONS} == {
        "labs_reservation_usd": "0.10",
        "serp_reservation_usd": "0.03",
        "seed_reservation_usd": "0.50",
        "triage_reservation_usd": "0.50",
        "review_reservation_usd": "4.00",
    }
    assert "minimum_ceiling_usd" not in v5.POLICY
    assert worst_run(v5.POLICY) == Decimal("8.40")


def test_v6_reservations_cover_list_prices_and_token_bounds():
    largest = max(V1["footprint_rows"], V1["idea_rows"], V1["related_rows"], V1["max_seeds"], 5)
    assert LABS_TASK + largest * LABS_ITEM == Decimal("0.036")
    assert Decimal(v6.POLICY["labs_reservation_usd"]) >= Decimal("1.25") * Decimal("0.036")
    assert Decimal(v6.POLICY["serp_reservation_usd"]) >= 2 * SERP_PAGE
    slots = [{"id": f"kw_{i:016x}", "keyword": "k", "buyer_fit": "direct"} for i in range(300)]
    for stage, key in (("seeds", "seed"), ("triage", "triage"), ("review", "review")):
        schema = v6.SCHEMAS[stage] if stage == "seeds" else v2.model_schema(stage, slots)
        # One token per byte of capped input, instructions and schema, at long-context rates.
        tokens = (
            V1["max_model_input_bytes"]
            + len(v6.INSTRUCTIONS[stage].encode())
            + len(json.dumps(schema).encode())
        )
        assert tokens > CARD["long_context_above_input_tokens"]
        rate = LUNA["long_context"]
        bound = usd(
            tokens * max(rate["input"], rate["cache_write"])
            + v6.POLICY[f"{key}_output_tokens"] * rate["output"]
        )
        assert bound < Decimal(v6.POLICY[f"{key}_reservation_usd"]), stage


def test_v6_worst_run_fits_its_floor_and_the_catalog_defaults():
    assert worst_run(v6.POLICY) == Decimal("1.65") <= Decimal(v6.POLICY["minimum_ceiling_usd"])
    keyword = SPECS["organic.keyword_plan"]
    # v7 (batched screening) succeeded v6 as the pinned default and keeps its $2 floor.
    assert keyword.definition["keyword_policy"]["minimum_ceiling_usd"] == "2"
    assert keyword.version_label == "0.7.0"
    limit = keyword.input_schema["properties"]["max_cost_usd"]
    assert limit["minimum"] == limit["default"] == 2
    system = organic_system.INPUT_SCHEMA["properties"]["keyword_max_cost_usd"]
    assert system["minimum"] == system["default"] == 2
    assert SPECS["organic.traffic_system"].version_label == "0.5.0"


@pytest.mark.asyncio
async def test_v6_run_at_its_floor_refuses_nothing():
    activities, db, storage, provider, model = await fixture(
        prepare=False, modern="current", budget=2, inputs={"seed_phrases": [], "max_cost_usd": 2}
    )
    db.run = replace(db.run, id=RUN_ID)
    track(provider)
    run_id = str(RUN_ID)
    await activities.keyword_prepare(run_id)
    await activities.keyword_collect(run_id)
    count = await activities.keyword_sample_count(run_id)
    await activities.keyword_inspect_batch(run_id)
    await activities.keyword_review(run_id)
    await activities.keyword_publish(run_id)
    assert count > 10 and storage.repo.writes == 1
    refused = [
        key
        for key, receipt in db.effects.items()
        if key.startswith(f"keyword:{run_id}:")
        and (receipt.result or {}).get("reason") == "spending_limit"
    ]
    assert refused == []
    ledger = db.effects[activities.key(run_id, "budget")].result
    assert sum(Decimal(value) for value in ledger.values()) <= 2
    assert ledger["review"] == "0.15" and ledger["serp:0"] == "0.005"


@pytest.mark.asyncio
async def test_older_policies_keep_their_five_dollar_floor():
    activities, db, *_ = await fixture(
        prepare=False, modern="v4", budget=2, inputs={"max_cost_usd": 2}
    )
    with pytest.raises(ApplicationError, match=r"at least \$5"):
        await activities.keyword_prepare(str(db.run.id))
    activities, db, *_ = await fixture(
        prepare=False, modern="current", budget=2, inputs={"max_cost_usd": 2}
    )
    await activities.keyword_prepare(str(db.run.id))
    activities, db, *_ = await fixture(
        prepare=False, modern="current", budget=1, inputs={"max_cost_usd": 2}
    )
    with pytest.raises(ApplicationError, match=r"at least \$2"):
        await activities.keyword_prepare(str(db.run.id))


@pytest.mark.asyncio
async def test_v6_lookup_above_its_reservation_stops_research():
    activities, db, _, provider, _ = await fixture(modern="current", budget=2)
    query = provider.query.side_effect

    async def overpriced(kind, **kwargs):
        return {**await query(kind, **kwargs), "reported_cost_usd": "0.06"}

    provider.query.side_effect = overpriced
    with pytest.raises(ApplicationError, match="pinned reservation"):
        await activities._query(str(db.run.id), "target", "ranked", "example.com")


def test_keyword_gate_uses_the_v6_floor():
    ready = SimpleNamespace(
        dataforseo_login="login",
        dataforseo_password="fixture",  # noqa: S106
        luna_api_key="key",
    )
    assert keyword_plan_gate(SimpleNamespace(**vars(ready), keyword_plan_max_cost_usd=2)) is None
    assert "$2" in keyword_plan_gate(SimpleNamespace(**vars(ready), keyword_plan_max_cost_usd=1.5))
    system = SimpleNamespace(
        **vars(ready),
        keyword_plan_max_cost_usd=2,
        organic_audit_max_cost_usd=2,
        content_plan_max_cost_usd=1,
    )
    assert organic_system_gate(system) is None
