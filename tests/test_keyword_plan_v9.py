"""Keyword policy v9: credible competitors, screening before selection, honest priorities.

Across 36 production runs, discovery chose youtube.com, facebook.com, reddit.com and namesakes
as competitors; candidates were capped at 300 before screening (one run kept 2 direct of 817
collected); and 22 of 69 high-priority groups had no measured demand in any member.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from decimal import Decimal

import pytest
from test_keyword_plan import fixture, provider_row
from test_keyword_plan_v6 import worst_run
from test_keyword_plan_v7 import label, screening, widen

from tin_lite import keyword_plan_v2 as v2
from tin_lite import keyword_plan_v8 as v8
from tin_lite import keyword_plan_v9 as v9
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.keyword_data import ENDPOINTS, request_for
from tin_lite.keyword_plan import build_documents, keyword_id
from tin_lite.keyword_plan_activities import (
    CONTRACTS,
    MINIMUM_CEILING,
    screen_batches,
    triage_reservations,
)
from tin_lite.model_providers import MessageRole, ModelMessage, ModelRequest
from tin_lite.organic_audit import canonical_json
from tin_lite.service_pricing import CARD, NANOS_PER_DOLLAR

LUNA = CARD["models"]["gpt-6-luna"]
CHANGED = {
    "version",
    "screen_candidates",
    "triage_output_tokens",
    "triage_retry_output_tokens",
    "triage_max_request_bytes",
    "triage_reservation_usd",
    "triage_retry_reservation_usd",
    "competitor_discovery_rows",
    "competitor_min_shared_keywords",
    "selection",
    "high_priority",
}

# ---------------------------------------------------------------- the pinned contracts


def test_v9_is_pinned_and_leaves_v8_alone():
    assert {k: v for k, v in v9.POLICY.items() if k not in CHANGED} == {
        k: v for k, v in v8.POLICY.items() if k not in CHANGED
    }
    assert v9.POLICY["version"] == "keyword-plan-v9"
    assert v9.SCHEMAS == v8.SCHEMAS and v9.seed_values is v8.seed_values
    assert v9.INSTRUCTIONS["triage"] == v8.INSTRUCTIONS["triage"]
    assert v9.INSTRUCTIONS["seeds"].startswith(v8.INSTRUCTIONS["seeds"])
    # The sentence that let unmeasured niches be high priority is gone; the rule replaces it.
    assert "high priority with unknown volume" in v8.INSTRUCTIONS["review"]
    assert "high priority with unknown volume" not in v9.INSTRUCTIONS["review"]
    assert "measured_demand true" in v9.INSTRUCTIONS["review"]
    # v8 is deployed: its caps are what its pinned runs send.
    assert v8.POLICY["triage_output_tokens"] == 32_000
    assert "screen_candidates" not in v8.POLICY
    assert CONTRACTS[v9.POLICY["version"]][0] is v9.POLICY
    assert CONTRACTS[v8.POLICY["version"]][0] is v8.POLICY
    keyword = next(spec for spec in BUILTIN_WORKFLOWS if spec.key == "organic.keyword_plan")
    assert keyword.definition["keyword_policy"] == v9.POLICY
    assert keyword.definition["keyword_instructions"] == v9.INSTRUCTIONS
    assert keyword.version_label == "0.9.0"


def test_v9_caps_cover_the_largest_screening_output_and_reservations_cover_their_bounds():
    policy = v9.POLICY
    # The largest screening output seen in production was 2,320 tokens for 50 keywords.
    assert policy["triage_output_tokens"] >= 5 * 2320
    assert policy["triage_retry_output_tokens"] == 2 * policy["triage_output_tokens"]
    assert policy["triage_retry_output_tokens"] <= v8.MODEL_OUTPUT_LIMIT
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
    assert worst[12_000] == Decimal("0.013012")
    assert worst[24_000] == Decimal("0.019012")


def test_v9_worst_run_fits_the_two_dollar_floor():
    policy = v9.POLICY
    assert screen_batches(policy) == 12 and screen_batches(v8.POLICY) == 6
    per_batch = Decimal(policy["triage_reservation_usd"]) + Decimal(
        policy["triage_retry_reservation_usd"]
    )
    total = (
        worst_run(policy)
        - Decimal(policy["triage_reservation_usd"])
        + screen_batches(policy) * per_batch
    )
    # Seeds $0.10, review $0.15, screening $0.408, 22 lookups $1.10 and 40 samples $0.20.
    assert total == Decimal("1.958") <= Decimal(policy["minimum_ceiling_usd"]) == MINIMUM_CEILING
    assert triage_reservations(policy) == [(f"triage:{index}", "0.014") for index in range(12)]


def test_the_largest_possible_v9_batch_fits_its_request_bound():
    scope = {
        "url": "https://" + "a" * 240 + ".example/",
        "host": "a" * 240 + ".example",
        "market": "US",
        "language": "en",
        "buyer_context": "é" * 2000,
        "started_at": "2026-10-06T00:00:00+00:00",
        "max_cost_usd": "2",
        "policy_version": v9.POLICY["version"],
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
        for index in range(v9.POLICY["triage_batch_size"])
    ]
    request = ModelRequest(
        messages=(
            ModelMessage(
                role=MessageRole.USER,
                content=canonical_json(v2.triage_input(scope, batch)).decode(),
            ),
        ),
        system=v9.INSTRUCTIONS["triage"],
        output_schema=v2.model_schema("triage", batch),
        output_schema_name="keyword_triage",
        max_output_tokens=v9.POLICY["triage_retry_output_tokens"],
    )
    assert (
        len(json.dumps(asdict(request), ensure_ascii=False).encode())
        < v9.POLICY["triage_max_request_bytes"]
    )


def test_wide_discovery_asks_the_same_endpoint_for_twenty_domains():
    request = request_for("competitors_wide", market="US", value="example.com", tag="t")
    assert ENDPOINTS["competitors_wide"] == ENDPOINTS["competitors"]
    assert request["limit"] == v9.POLICY["competitor_discovery_rows"] == 20
    assert request_for("competitors", market="US", value="example.com", tag="t")["limit"] == 5


# ---------------------------------------------------------------- competitors


def test_discovery_drops_platforms_namesakes_and_thin_overlap():
    items = [
        {"domain": "youtube.com", "intersections": 900},
        {"domain": "www.reddit.com", "intersections": 400},
        {"domain": "brightlog.one", "intersections": 40},
        {"domain": "amazon.co.uk", "intersections": 300},
        {"domain": "lookalike-music.com", "intersections": 1},
        {"domain": "rival-one.example", "intersections": 120},
        {"domain": "brightlog.to", "intersections": 999},
        {"domain": "rival-two.example"},  # No overlap count: kept.
        {"domain": "rival-one.example", "intersections": 120},
        {"domain": "rival-three.example", "intersections": 3},
        {"domain": "rival-four.example", "intersections": 50},
        {"bad": "row"},
    ]
    kept, dropped = v9.competitor_domains(items, target="www.brightlog.to", limit=3)
    assert kept == ["rival-one.example", "rival-two.example", "rival-three.example"]
    assert dropped == [
        {"domain": "youtube.com", "reason": "general_platform"},
        {"domain": "reddit.com", "reason": "general_platform"},
        {"domain": "brightlog.one", "reason": "namesake"},
        {"domain": "amazon.co.uk", "reason": "general_platform"},
        {"domain": "lookalike-music.com", "reason": "few_shared_keywords"},
    ]
    # Only general platforms: no competitor footprint is bought.
    assert v9.competitor_domains(items[:2], target="brightlog.to", limit=3)[0] == []


async def test_v9_runs_buy_footprints_only_for_credible_competitors():
    activities, db, _storage, provider, _model = await fixture(
        modern="current", inputs={"seed_phrases": []}
    )
    original = provider.query.side_effect

    async def query(kind, **kwargs):
        if kind == "competitors_wide":
            items = [
                {"domain": "youtube.com", "intersections": 500},
                {"domain": "example.net", "intersections": 30},
                {"domain": "rival.example", "intersections": 12},
            ]
            return {
                "items": items,
                "items_count": 3,
                "total_count": 3,
                "reported_cost_usd": "0.01",
                "provider_task_id": "fixture-only",
            }
        return await original(kind, **kwargs)

    provider.query.side_effect = query
    run_id = str(db.run.id)
    await activities.keyword_collect(run_id)
    kinds = [call.args[0] for call in provider.query.await_args_list]
    assert "competitors_wide" in kinds and "competitors" not in kinds
    footprints = [
        call.kwargs["value"]["host"]
        for call in provider.query.await_args_list
        if call.args[0] == "ranked_relevant"
    ]
    assert footprints == ["rival.example"]
    collection = await activities._result(run_id, "collection")
    assert collection["competitors"] == ["rival.example"]
    discovery = collection["sources"]["competitors"]["value"]
    assert discovery["dropped"] == [
        {"domain": "youtube.com", "reason": "general_platform"},
        {"domain": "example.net", "reason": "namesake"},
    ]


# ---------------------------------------------------------------- screening, then selection


def rows(source, words):
    return [{**provider_keyword(word), "source_id": source} for word in words]


def provider_keyword(word):
    return {"id": keyword_id(word, "US"), "keyword": word, "search_volume": 10}


def test_selection_takes_screened_direct_keywords_first_across_sources():
    sources = {
        "suggestions:0": rows("suggestions:0", [f"broad {i}" for i in range(8)] + ["fit a"]),
        "related:0": rows("related:0", [f"other {i}" for i in range(8)] + ["fit b"]),
    }
    pool, collected = v9.screening_pool(sources, limit=100)
    assert collected == 18 and len(pool) == 18
    # Round-robin by source, as v1's selection.
    assert [row["keyword"] for row in pool[:4]] == ["broad 0", "other 0", "broad 1", "other 1"]
    fits = {row["id"]: "wrong_buyer" for row in pool}
    fits[keyword_id("fit a", "US")] = fits[keyword_id("fit b", "US")] = "direct"
    fits[keyword_id("other 3", "US")] = "adjacent"
    candidates, coverage = v9.select_screened(sources, fits, limit=4)
    assert {row["keyword"] for row in candidates} == {"fit a", "fit b", "other 3", "broad 0"}
    assert all(row["buyer_fit"] == fits[row["id"]] for row in candidates)
    assert all(row["observations"] for row in candidates)
    assert coverage["unique_collected"] == 18 and coverage["screened"] == 18
    assert coverage["selected"] == 4 and coverage["omitted"] == 14
    assert coverage["buyer_fit"] == {
        "direct": 2,
        "adjacent": 1,
        "wrong_buyer": 15,
        "generic": 0,
        "unclear": 0,
    }
    assert coverage["selected_buyer_fit"]["wrong_buyer"] == 1
    # An unscreened keyword is never selected.
    candidates, _ = v9.select_screened(sources, {keyword_id("fit a", "US"): "direct"}, limit=4)
    assert [row["keyword"] for row in candidates] == ["fit a"]


async def test_v9_screens_more_than_it_reviews_and_keeps_the_best_fits():
    # Model-proposed seeds: four of them, each with suggestions and related keywords.
    activities, db, _storage, provider, model = await fixture(
        modern="current", inputs={"seed_phrases": []}
    )
    widen(provider)
    calls = screening(model)
    run_id = str(db.run.id)
    await activities.keyword_collect(run_id)
    collection = await activities._result(run_id, "collection")
    screened = [keyword for _tokens, keywords in calls for keyword in keywords]
    assert all(tokens == 12_000 and len(keywords) <= 50 for tokens, keywords in calls)
    assert len(screened) == len(set(screened)) > 300
    candidates = collection["candidates"]
    assert len(candidates) == 300
    direct = [keyword for keyword in screened if label(keyword) == "direct"]
    kept = {row["keyword"] for row in candidates}
    assert set(direct) <= kept
    assert all(row["buyer_fit"] == label(row["keyword"]) for row in candidates)
    coverage = collection["coverage"]
    assert coverage["screened"] == len(screened)
    assert coverage["buyer_fit"]["direct"] == len(direct)
    assert coverage["selected"] == 300
    assert any("screened for buyer fit" in note for note in coverage["notes"])
    ledger = db.effects[activities.key(run_id, "budget")].result
    assert all(ledger[f"triage:{index}"] == "0.014" for index in range(len(calls)))


# ---------------------------------------------------------------- priorities


def review_value(candidates, priority="high"):
    slots = v2.slots([row for row in candidates if row["buyer_fit"] in v2.ELIGIBLE])
    return {
        "groups": [
            {
                "ref": "g1",
                "primary": "k1",
                "title": "Scheduling for consultants",
                "intent": "commercial",
                "priority": priority,
                "rationale": "Consultants compare scheduling tools for client bookings.",
                "page_approach": "Compare scheduling options for small consulting teams.",
                "evidence_needed": "Gather product screenshots and real booking examples.",
            }
        ],
        "assignments": dict.fromkeys(slots, "g1"),
    }


def candidate(word, *, volume=None, impressions=None, fit="direct"):
    observation = {"source_id": "seed_proposals", "search_volume": volume}
    if impressions is not None:
        observation |= {"source_id": "gsc", "impressions": impressions}
    return {
        "id": keyword_id(word, "US"),
        "keyword": word,
        "buyer_fit": fit,
        "observations": [observation],
    }


@pytest.mark.parametrize(
    "members,lowered",
    [
        ([candidate("niche phrase"), candidate("other phrase", volume=0)], True),
        ([candidate("niche phrase"), candidate("measured phrase", volume=90)], False),
        ([candidate("niche phrase", impressions=40)], False),
    ],
)
def test_high_priority_requires_measured_demand(members, lowered):
    review, adjusted = v9.expand_review(review_value(members), members)
    assert review["groups"][0]["priority"] == ("medium" if lowered else "high")
    assert adjusted == ([members[0]["id"]] if lowered else [])
    # Other priorities pass through untouched.
    review, adjusted = v9.expand_review(review_value(members, "low"), members)
    assert review["groups"][0]["priority"] == "low" and adjusted == []


def test_review_input_names_measured_demand():
    members = [
        candidate("niche phrase"),
        candidate("measured phrase", volume=90),
        candidate("ignored", volume=900, fit="wrong_buyer"),
    ]
    scope = {"host": "example.com", "url": "https://example.com/"}
    data = v9.review_input(scope=scope, candidates=members, samples={}, coverage={})
    assert [(row["keyword"], row["measured_demand"]) for row in data["candidates"]] == [
        ("niche phrase", False),
        ("measured phrase", True),
    ]


async def test_an_unmeasured_high_group_is_published_as_medium_with_a_note(monkeypatch):
    activities, db, _storage, provider, model = await fixture(modern="current")
    published = {}

    def capture(**kwargs):
        published.update(build_documents(**kwargs))
        return published

    monkeypatch.setattr("tin_lite.keyword_plan_activities.build_documents", capture)
    original_query, original_generate = provider.query.side_effect, model.generate.side_effect

    async def query(kind, **kwargs):
        result = await original_query(kind, **kwargs)
        if kind not in {"competitors_wide", "serp"}:
            # The database knows the keywords but measures no volume for any of them.
            unmeasured = provider_row()
            unmeasured["keyword_data"]["keyword_info"]["search_volume"] = None
            result["items"] = [unmeasured]
        return result

    async def generate(route, request, *, timeout_seconds=None):
        result = await original_generate(route, request, timeout_seconds=timeout_seconds)
        if request.output_schema_name == "keyword_review":
            assert all(
                row["measured_demand"] is False
                for row in json.loads(request.messages[0].content)["candidates"]
            )
            assert result.parsed["groups"][0]["priority"] == "high"
        return replace(result)

    provider.query.side_effect, model.generate.side_effect = query, generate
    run_id = str(db.run.id)
    await activities.keyword_collect(run_id)
    for index in range(await activities.keyword_sample_count(run_id)):
        await activities.keyword_inspect({"run_id": run_id, "index": index})
    await activities.keyword_review(run_id)
    review = await activities._result(run_id, "review_validated")
    assert [group["priority"] for group in review["groups"]] == ["medium"]
    assert (await activities._result(run_id, "review_priority"))["lowered"]
    await activities.keyword_publish(run_id)
    plan = next(value for path, value in published.items() if path.endswith("PLAN.md")).decode()
    assert "· medium priority ·" in plan and "· high priority ·" not in plan
    assert "no member has measured search volume" in plan
