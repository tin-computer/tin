"""Organic ceilings near three times measured cost, never below one run's worst case."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from tin_lite import content_plan_editorial as editorial
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_audit import AUDIT_POLICY
from tin_lite.organic_audit_ai import (
    AnswerGrade,
    BuyerPanelV15,
    ContentReview,
    PanelReviewV15,
    payload,
)
from tin_lite.organic_audit_engines import per_question_usd
from tin_lite.service_pricing import (
    AUDIT_MAXIMUM_USD,
    CARD,
    CONTENT_PLAN_SHARE_USD,
    NANOS_PER_DOLLAR,
    service_terms,
)
from tin_lite.workflow_inputs import normalize_workflow_inputs

SPECS = {spec.key: spec for spec in BUILTIN_WORKFLOWS}
LUNA = CARD["models"]["gpt-6-luna"]


def usd(nanos):
    return Decimal(nanos) / NANOS_PER_DOLLAR


def test_audit_ceiling_covers_every_call_at_every_bound():
    policy, total = AUDIT_POLICY, Decimal(AUDIT_POLICY["crawl_reservation_usd"])
    questions = policy["max_questions"]
    calls = (
        ("research", policy["max_research_attempts"], None, True),
        ("answer", questions * policy["repetitions"] + policy["brand_checks"], None, True),
        ("panel", policy["max_panel_attempts"], BuyerPanelV15, False),
        ("interpret", policy["max_panel_attempts"] * questions, None, False),
        # v11 reviews each question: the schema names rejected questions and why.
        ("validate", policy["max_panel_attempts"], PanelReviewV15, False),
        ("judge_graded", questions * policy["repetitions"], AnswerGrade, False),
        # One answer per question without web search, and its grade. Its input is the
        # question (at most 400 characters); the grade reads an answer of at most 32 KB.
        ("answer_memory", questions, None, False),
        ("judge_graded", questions, AnswerGrade, False),
        # One review of at most five page outlines with capped fields.
        ("content_review", 1, ContentReview, False),
    )
    bounds = {"answer_memory": 400, "content_review": 25_000}
    rate = LUNA["standard"]
    for index, (stage, count, schema, search) in enumerate(calls):
        size = bounds.get(stage, 33_000 if index == 7 else 60_000)
        data = (
            {"website": "https://example.com/", "focus_hint": "x" * 59_000}
            if stage == "research"
            else "x" * size
        )
        request = payload(stage=stage, data=data, schema=schema, market="US", search=search)
        searches = policy["max_tool_calls"] if search else 0
        tokens = len(json.dumps(request).encode()) + searches * 16_384
        assert tokens < CARD["long_context_above_input_tokens"]
        total += count * usd(
            tokens * max(rate["input"], rate["cache_write"])
            + request["max_output_tokens"] * rate["output"]
            + searches * CARD["web_search_call_nanos"]
        )
    # v15 asks at most 16 questions three times with web search and once without, plus a
    # review of the top pages: 52 searched and 117 unsearched calls, about $3.56. Earlier
    # policies asked at most eight and keep the $2 ceiling.
    maximum = Decimal(policy["billing_maximum_usd"])
    assert Decimal("3.5") < total < maximum == 4 > AUDIT_MAXIMUM_USD
    # v13 also asks the same questions on six AI engines within their own pinned ceiling.
    engines = per_question_usd(policy) * questions
    assert engines <= Decimal(policy["ai_engines_max_cost_usd"]) == 2
    terms = service_terms(SPECS["organic.audit"].definition)
    assert terms["maximum_nanos"] == (maximum + 2) * NANOS_PER_DOLLAR
    assert total + engines < maximum + 2


def test_content_plan_share_covers_its_one_model_call():
    pages = {
        "pages": [
            {"page_id": f"p{index:03d}", "status": "inspected", "source_id": "page:" + "0" * 64}
            for index in range(1, editorial.V8_POLICY["max_pages"] + 1)
        ]
    }
    aliases = {f"s{index}": "keyword:" + "x" * 40 for index in range(400)}
    tokens = (
        editorial.V8_POLICY["max_input_bytes"]
        + len(editorial.V8_INSTRUCTIONS.encode())
        + len(json.dumps(editorial.bound_schema(pages, aliases)).encode())
    )
    rate = LUNA["long_context"]
    bound = usd(
        tokens * rate["cache_write"] + editorial.V8_POLICY["max_output_tokens"] * rate["output"]
    )
    assert bound < Decimal("0.10") < CONTENT_PLAN_SHARE_USD


@pytest.mark.parametrize(
    ("inputs", "dollars"),
    [
        # organic-traffic-v8 (2026-10-08 calibration): the keyword limit + $12, plus $3 for
        # page delivery and $3 for technical fixes (both on by default); about four times the
        # children's p90s, and above any one child's own ceiling.
        ({}, 20),
        ({"technical_fix": False}, 17),
        ({"content_delivery": "draft_only"}, 17),
        ({"content_delivery": "draft_only", "technical_fix": False}, 14),
        (
            {"technical_fix": True, "repository_serves_site": True, "expected_repository": "o/r"},
            20,
        ),
        ({"keyword_max_cost_usd": 9}, 27),  # a founder's higher keyword limit raises it
    ],
)
def test_traffic_system_ceiling_uses_the_new_defaults(inputs, dollars):
    spec = SPECS["organic.traffic_system"]
    normalized = normalize_workflow_inputs(
        schema=spec.input_schema,
        project_id="00000000-0000-4000-8000-000000000001",
        inputs={
            "site_url": "https://example.com/",
            "market": "US",
            "buyer_context": "Scheduling software for small consulting teams.",
            "start_date": "2026-10-01",
            **inputs,
        },
    )
    terms = service_terms(spec.definition, inputs=normalized)
    assert terms["maximum_nanos"] == int(dollars * NANOS_PER_DOLLAR)


def test_the_v8_ceiling_is_about_four_times_its_estimate_and_holds_every_child():
    from tin_lite.codex_api_pricing import api_terms
    from tin_lite.service_pricing import TRAFFIC_SYSTEM_V8_USD
    from tin_lite.workflow_estimates import estimate_nanos

    spec = SPECS["organic.traffic_system"]
    inputs = {"keyword_max_cost_usd": 2, "technical_fix": True, "content_delivery": "auto"}
    maximum = service_terms(spec.definition, inputs=inputs)["maximum_nanos"]
    estimate = estimate_nanos(spec.definition, inputs, maximum)
    assert (maximum, estimate) == (20 * NANOS_PER_DOLLAR, 4_900_000_000)
    assert Decimal("3.5") < Decimal(maximum) / estimate < Decimal("4.5")
    # Every child fits on its own, without the keyword limit: audit v15 $6, the planning
    # agent $6, the draft $6, website.change $4 and the refresh $3.
    children = [
        service_terms(SPECS["organic.audit"].definition)["maximum_nanos"],
        service_terms(SPECS["content.plan"].definition)["maximum_nanos"],
        *(
            api_terms(SPECS[key].definition, child=True)["maximum_nanos"]
            for key in ("content.generate", "website.change", "content.refresh")
        ),
    ]
    assert children == [n * NANOS_PER_DOLLAR for n in (6, 6, 6, 4, 3)]
    assert all(child <= TRAFFIC_SYSTEM_V8_USD * NANOS_PER_DOLLAR for child in children)
