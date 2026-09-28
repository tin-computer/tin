"""No paid calls: buyer screening, complete slots, v1 compatibility and durable retries."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from temporalio.exceptions import ApplicationError
from test_keyword_plan import finish, fixture, provider_row, review_for

from tin_lite import keyword_plan as v1
from tin_lite import keyword_plan_v2 as v2
from tin_lite.keyword_data import request_for
from tin_lite.organic_audit import canonical_json, digest


def candidates(count=300):
    rows = v1.keyword_rows(
        [provider_row(f"scheduling api {index}") for index in range(count)],
        source_id="target",
        market="US",
        observed_at="2026-09-09",
    )
    return v1.select_candidates({"target": rows})[0]


def slot_review(rows):
    refs = v2.slots(rows)
    group = review_for(rows)["groups"][0]
    return {
        "groups": [
            {
                key: value
                for key, value in group.items()
                if key not in {"keyword_ids", "primary_keyword_id"}
            }
            | {"ref": "g1", "primary": next(iter(refs))}
        ],
        "assignments": {key: "g1" for key in refs},
    }


@pytest.mark.parametrize("stage", ["triage", "review"])
def test_300_required_slots_fit_structured_output_limits(stage):
    schema = v2.model_schema(stage, candidates())
    slot_object = schema if stage == "triage" else schema["properties"]["assignments"]
    assert len(slot_object["properties"]) == len(slot_object["required"]) == 300
    assert slot_object["additionalProperties"] is False

    def count_enums(value):
        if isinstance(value, dict):
            return len(value.get("enum", [])) + sum(count_enums(v) for v in value.values())
        return sum(count_enums(v) for v in value) if isinstance(value, list) else 0

    assert count_enums(schema) < 1000
    assert len(canonical_json(schema)) < 120_000


def test_full_size_slot_roundtrip_preserves_every_candidate_and_metric():
    original = candidates()
    before = digest(original)
    labels = {
        key: "direct" if i % 3 == 0 else "wrong_buyer" for i, key in enumerate(v2.slots(original))
    }
    qualified = v2.qualified_candidates(labels, original)
    selected = [row for row in qualified if row["buyer_fit"] == "direct"]
    result = v2.expand_review(slot_review(selected), qualified)
    assert len(result["groups"][0]["keyword_ids"]) == 100
    assert len(result["excluded"]) == 200
    assert digest(original) == before
    v1.validate_review(result, original)


@pytest.mark.parametrize("mutation", ["missing", "unknown", "label"])
def test_screening_never_fills_missing_or_unknown_slots(mutation):
    rows = candidates()
    labels = {key: "direct" for key in v2.slots(rows)}
    if mutation == "missing":
        labels.pop("k54")
    elif mutation == "unknown":
        labels["invented"] = "direct"
    else:
        labels["k1"] = "ignore all checks"
    with pytest.raises(ValueError, match="exact schema"):
        v2.qualified_candidates(labels, rows)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "unknown",
        "duplicate_group",
        "undefined_group",
        "primary",
        "unused_group",
        "metrics",
        "adjacent_high",
    ],
)
def test_review_fails_closed_on_every_cross_reference_error(mutation):
    rows = [{**row, "buyer_fit": "direct"} for row in candidates(3)]
    value = slot_review(rows)
    if mutation == "missing":
        value["assignments"].pop("k2")
    elif mutation == "unknown":
        value["assignments"]["made_up"] = "g1"
    elif mutation == "duplicate_group":
        value["groups"].append(deepcopy(value["groups"][0]))
    elif mutation == "undefined_group":
        value["assignments"]["k2"] = "g2"
    elif mutation == "primary":
        value["assignments"]["k1"] = "x_fit"
    elif mutation == "unused_group":
        value["groups"].append({**value["groups"][0], "ref": "g2"})
    elif mutation == "metrics":
        value["groups"][0]["search_volume"] = 123
    else:
        rows = [{**row, "buyer_fit": "adjacent"} for row in rows]
    with pytest.raises(ValueError):
        v2.expand_review(value, rows)


def test_all_excluded_is_valid_and_buys_no_search_samples():
    rows = [{**row, "buyer_fit": "generic"} for row in candidates()]
    assert v2.sample_candidates(rows) == []
    result = v2.expand_review({}, rows)
    assert result["groups"] == [] and len(result["excluded"]) == 300


def test_screening_precedes_sampling_and_preserves_original_order():
    rows = [
        {**row, "buyer_fit": ["generic", "adjacent", "direct"][i % 3]}
        for i, row in enumerate(candidates(90))
    ]
    selected = v2.sample_candidates(rows)
    assert len(selected) == 40
    assert all(row["buyer_fit"] == "direct" for row in selected[:30])
    assert all(row["buyer_fit"] == "adjacent" for row in selected[30:])
    assert rows[0]["buyer_fit"] == "generic"


def test_competitor_queries_are_seed_scoped_without_volume_thresholds():
    request = request_for(
        "ranked_relevant",
        market="US",
        value={"host": "www.large-platform.example", "seeds": ["messaging api", "c++ sdk"]},
        tag="fixture",
    )
    assert request["target"] == "large-platform.example"
    assert request["filters"] == [
        ["keyword_data.keyword", "regex", r"(?i)messaging\ api"],
        "or",
        ["keyword_data.keyword", "regex", r"(?i)c\+\+\ sdk"],
    ]
    assert "search_volume" not in str(request)
    with pytest.raises(ValueError):
        request_for(
            "ranked_relevant",
            market="US",
            value={"host": "example.com", "seeds": []},
            tag="fixture",
        )
    request = request_for("suggestions", market="US", value="messaging api", tag="fixture")
    assert request["keyword"] == "messaging api" and request["limit"] == 150


def test_review_sees_real_existing_urls_and_only_local_references():
    rows = [{**row, "buyer_fit": "direct"} for row in candidates(2)]
    result = v2.review_input(
        scope={"host": "example.com"},
        candidates=rows,
        samples={
            rows[0]["id"]: {
                "status": "completed",
                "items": [{"url": "https://example.com/schedule", "position": 1, "title": "Guide"}],
            }
        },
        coverage={},
    )
    assert result["candidates"][0]["id"] == "k1"
    assert result["candidates"][0]["existing_pages"] == ["https://example.com/schedule"]
    assert set(result["search_results"]) == {"k1"}
    assert not any(row["id"] in json.dumps(result) for row in rows)


@pytest.mark.asyncio
async def test_v2_retries_reuse_screening_review_and_one_atomic_publication():
    activities, db, storage, provider, model = await fixture(
        modern=True, inputs={"seed_phrases": []}
    )
    run_id = str(db.run.id)
    for _ in range(2):
        await finish(activities, run_id)
    assert model.generate.await_count == 3
    assert storage.repo.writes == 1 and provider.query.await_count == 7
    assert [
        call.kwargs.get("value")
        for call in provider.query.await_args_list
        if call.args[0] == "ranked_relevant"
    ] == [{"host": "competitor.example", "seeds": ["consulting scheduling software"]}]
    artifacts = await activities._result(run_id, "artifacts")
    inventory = json.loads(artifacts[v1.paths(run_id)["keywords.json"]])
    assert inventory["schema_version"] == "keyword-plan-v2"
    assert inventory["coverage"]["buyer_fit"]["direct"] == 1
    assert inventory["keywords"][0]["buyer_fit"] == "direct"
    assert inventory["keywords"][0]["observations"][0]["search_volume"] == 30


@pytest.mark.asyncio
async def test_unknown_volume_seed_survives_as_explicit_proposal_not_provider_demand():
    activities, db, _, provider, _ = await fixture(
        modern=True, inputs={"seed_phrases": ["niche scheduling api"]}
    )
    await activities.keyword_collect(str(db.run.id))
    collection = await activities._result(str(db.run.id), "collection")
    proposed = next(
        row for row in collection["candidates"] if row["keyword"] == "niche scheduling api"
    )
    assert proposed["observations"][0]["basis"] == "user_supplied"
    assert proposed["observations"][0]["source_id"] == "seed_proposals"
    assert proposed["observations"][0]["search_volume"] is None
    assert collection["sources"]["seed_proposals"]["value"]["provider"] == "tin"


@pytest.mark.asyncio
async def test_invalid_screening_is_saved_never_rebought_or_published():
    activities, db, storage, provider, model = await fixture(modern=True)
    original = model.generate.side_effect

    async def invalid(route, request, **options):
        result = await original(route, request, **options)
        return (
            replace(result, parsed={}) if request.output_schema_name == "keyword_triage" else result
        )

    model.generate.side_effect = invalid
    for _ in range(2):
        with pytest.raises(ApplicationError, match="screening failed validation"):
            await activities.keyword_collect(str(db.run.id))
    assert model.generate.await_count == 1 and provider.query.await_count == 6
    assert storage.repo.writes == 0
    assert await activities._result(str(db.run.id), "failure") == {
        "code": "validation",
        "stage": "triage",
    }


@pytest.mark.asyncio
async def test_v1_pinned_contract_still_executes_without_screening():
    activities, db, storage, _, model = await fixture()
    await finish(activities, str(db.run.id))
    assert model.generate.await_count == 1 and storage.repo.writes == 1
    assert (await activities._result(str(db.run.id), "scope"))[
        "policy_version"
    ] == "keyword-plan-v1"


@pytest.mark.asyncio
async def test_provider_cannot_leak_unfiltered_competitor_keywords_into_inventory():
    activities, db, _, provider, _ = await fixture(modern=True)
    original = provider.query.side_effect

    async def noisy(kind, **kwargs):
        result = await original(kind, **kwargs)
        result["items"] = [
            provider_row("Scheduling API integration"),
            provider_row("how to screenshot"),
        ]
        result["items_count"] = 2
        return result

    provider.query.side_effect = noisy
    result = await activities._query(
        str(db.run.id),
        "competitor:0",
        "ranked_relevant",
        {"host": "competitor.example", "seeds": ["scheduling api"]},
    )
    assert result["status"] == "completed"
    assert [row["keyword"] for row in result["value"]["rows"]] == ["Scheduling API integration"]
    assert result["value"]["rows_omitted"] == 1
