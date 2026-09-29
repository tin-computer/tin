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
    BuyerPanel,
    ContentReview,
    PanelValidation,
    payload,
)
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
        ("panel", policy["max_panel_attempts"], BuyerPanel, False),
        ("interpret", policy["max_panel_attempts"] * questions, None, False),
        ("validate", policy["max_panel_attempts"], PanelValidation, False),
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
            + policy["max_output_tokens"] * rate["output"]
            + searches * CARD["web_search_call_nanos"]
        )
    # v10 asks at most 8 questions three times with web search and once without, plus a
    # review of the top pages: 28 searched and 61 unsearched calls, about $1.92.
    assert Decimal("1.9") < total < AUDIT_MAXIMUM_USD
    terms = service_terms(SPECS["organic.audit"].definition)
    assert terms["maximum_nanos"] == AUDIT_MAXIMUM_USD * NANOS_PER_DOLLAR == 2 * NANOS_PER_DOLLAR


def test_content_plan_share_covers_its_one_model_call():
    pages = {
        "pages": [
            {"page_id": f"p{index:03d}", "status": "inspected", "source_id": "page:" + "0" * 64}
            for index in range(1, editorial.POLICY["max_pages"] + 1)
        ]
    }
    aliases = {f"s{index}": "keyword:" + "x" * 40 for index in range(400)}
    tokens = (
        editorial.POLICY["max_input_bytes"]
        + len(editorial.INSTRUCTIONS.encode())
        + len(json.dumps(editorial.bound_schema(pages, aliases)).encode())
    )
    rate = LUNA["long_context"]
    bound = usd(
        tokens * rate["cache_write"] + editorial.POLICY["max_output_tokens"] * rate["output"]
    )
    assert bound < Decimal("0.10") < CONTENT_PLAN_SHARE_USD


@pytest.mark.parametrize(
    ("inputs", "dollars"),
    [
        ({}, 15),  # keyword $2 + audit $2 + plan $1 + draft $5 + PR adaptation $5
        ({"content_delivery": "draft_only"}, 10),
        ({"technical_fix": True, "repository_serves_site": True, "expected_repository": "o/r"}, 20),
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
    assert terms["maximum_nanos"] == dollars * NANOS_PER_DOLLAR
