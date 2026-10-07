"""Audit policy v15 drafts up to sixteen questions from public pages and Search Console searches.

It no longer reads organic.prompt_panel. v14 is on main and may deploy at any time, so its
policy, instructions and schemas stay exactly as main shipped them. Offline only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_organic_audit import activities_fixture, panel_fixture, response
from test_organic_audit_panel import interpretation, research, review
from test_prompt_panel import audit_with, panel_row, published

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_audit import (
    AUDIT_POLICY,
    PANEL_PREPARATION_POLICY_KEYS,
    V14_AUDIT_POLICY,
    audit_policy,
    digest,
)
from tin_lite.organic_audit_ai import ai_contract, ai_schemas
from tin_lite.organic_audit_completion import NEUTRAL_KEYS
from tin_lite.organic_audit_panel import search_queries
from tin_lite.public_workflows import PUBLIC_WORKFLOWS
from tin_lite.service_pricing import NANOS_PER_DOLLAR, service_terms

V14 = "organic-audit-v14"
V15 = "organic-audit-v15"
# What main shipped for v14 at 6191d27, before v15 existed.
V14_POLICY_DIGEST = "ba2725514bd78ff3fbb9cfbed749700a4759e8ed55a9a093b08ed9b89cd8da75"
V14_CONTRACT_DIGEST = "8e4247192536b81a59d29fd43a50678ed50a809d53633b4699cc41f6ee4a7a3f"
V14_SCHEMAS_DIGEST = "7c0ac931e8e612cd09b2a9a55b1425f145fb71c5af613eb1b15ac02c70a1ada8"
JOBS = [
    "Planning team work",
    "Tracking team goals",
    "Running team reviews",
    "Sharing team updates",
]


def four_job_panel(jobs=JOBS):
    base = panel_fixture()
    return {
        **base,
        "questions": [
            {**q, "job": job, "question": q["question"].rstrip("?") + f" when {job.lower()}?"}
            for job in jobs
            for q in base["questions"]
        ],
    }


def searches():
    rows = [
        ["team planning software", "https://example.com/", 1, 300, 8.0],
        ["team planning software", "https://example.com/pricing", 0, 200, 12.0],
        ["acme login", "https://example.com/", 40, 900, 1.0],
        ["example com", "https://example.com/", 30, 800, 1.0],
        ["weekly goal tracker", "https://example.com/goals", 2, 120, 9.0],
    ]
    return {"status": "completed", "value": {"queries": rows}}


async def drafted(*, panel=None, calls=None, with_searches=True):
    activities, db, _, _ = await activities_fixture()
    run_id = str(db.run.id)
    if with_searches:
        await activities._save(run_id, "search_console_queries", searches())
    activities.responses = SimpleNamespace(
        create=AsyncMock(
            side_effect=calls
            or [
                research(),
                response(json.dumps(panel or four_job_panel()), search=False),
                *[interpretation() for _ in range(16)],
                review(),
            ]
        )
    )
    count = await activities.organic_prepare_panel(run_id)
    requests = [call.args[0] for call in activities.responses.create.await_args_list]
    return activities, db, run_id, count, requests


def test_v14_is_exactly_what_main_shipped():
    v14 = audit_policy(V14)
    assert v14 is V14_AUDIT_POLICY
    assert digest(v14) == V14_POLICY_DIGEST
    assert digest(ai_contract(V14)) == V14_CONTRACT_DIGEST
    assert digest(ai_schemas(V14)) == V14_SCHEMAS_DIGEST


def test_v15_is_the_default_and_raises_the_question_limit_to_sixteen():
    assert AUDIT_POLICY["version"] == V15 and audit_policy() is AUDIT_POLICY
    assert {k: v for k, v in AUDIT_POLICY.items() if k != "version"} == {
        **{k: v for k, v in V14_AUDIT_POLICY.items() if k != "version"},
        "prompt_panel": False,
        "max_panel_jobs": 4,
        "max_questions": 16,
        "question_interpretation_concurrency": 8,
        "panel_max_output_tokens": 12_000,
        "search_console_questions": 40,
        "ai_engines_max_cost_usd": "2.00",
        "billing_maximum_usd": "4.00",
    }
    # How a new panel is drafted and the billed ceiling: the same answers and grading, so an
    # answer completion may cross them.
    assert {
        "question_interpretation_concurrency",
        "panel_max_output_tokens",
        "search_console_questions",
    } <= PANEL_PREPARATION_POLICY_KEYS
    assert {k: v for k, v in AUDIT_POLICY.items() if k not in NEUTRAL_KEYS} == {
        k: v for k, v in V14_AUDIT_POLICY.items() if k not in NEUTRAL_KEYS
    }
    for stage in ("answer", "answer_memory", "judge_graded", "content_review"):
        assert ai_contract(V15)[stage] == ai_contract(V14)[stage]
    assert ai_contract(V15)["panel"].startswith(ai_contract(V14)["panel"])
    assert set(ai_schemas(V15)) == {
        "PanelValidation",
        "AnswerJudgment",
        "AnswerGrade",
        "ContentReview",
        "BuyerPanelV15",
        "PanelReviewV15",
    }
    workflow = next(w for w in BUILTIN_WORKFLOWS if w.key == "organic.audit")
    assert workflow.version_label == "0.11.0"
    assert workflow.definition["audit_policy"] == AUDIT_POLICY
    assert workflow.definition["audit_instructions"] == ai_contract(V15)
    assert workflow.definition["audit_schemas"] == ai_schemas(V15)
    # $4 for the audit's own calls and $2 for the engines; v14 keeps $2 and $1.
    definition = {"executor": "organic.audit"}
    v14 = service_terms({**definition, "audit_policy": V14_AUDIT_POLICY})
    v15 = service_terms({**definition, "audit_policy": AUDIT_POLICY})
    assert v14["maximum_nanos"] == 3 * NANOS_PER_DOLLAR
    assert v15["maximum_nanos"] == 6 * NANOS_PER_DOLLAR


def test_new_setups_no_longer_offer_the_prompt_panel():
    panel = next(w for w in PUBLIC_WORKFLOWS if w.key == "organic.prompt_panel")
    assert panel.public_discovery is False and panel.public_mcp is None


def test_the_draft_reads_the_most_searched_queries_without_the_sites_own_name():
    # Impressions are summed over pages, and "example com" names the host. A search for the
    # product's name ("acme login") is left to the model, which knows the name.
    assert search_queries(searches(), ("example.com",), 40) == [
        {"query": "acme login", "impressions": 900},
        {"query": "team planning software", "impressions": 500},
        {"query": "weekly goal tracker", "impressions": 120},
    ]
    assert search_queries(searches(), ("example.com",), 1) == [
        {"query": "acme login", "impressions": 900}
    ]
    assert search_queries(None, ("example.com",), 40) == []
    assert search_queries({"status": "unavailable"}, ("example.com",), 40) == []


@pytest.mark.asyncio
async def test_v15_drafts_four_jobs_and_asks_all_sixteen():
    activities, _, run_id, count, requests = await drafted()
    # Sixteen questions, three answers with web search and one without: 64.
    assert count == 64
    panel = await activities._result(run_id, "panel")
    assert len(panel["questions"]) == 16 and panel["repetitions"] == 3
    assert list(dict.fromkeys(q["job"] for q in panel["questions"])) == JOBS
    preparation = await activities._result(run_id, "panel_preparation")
    assert preparation["method"] == "public_research_then_blind_question_review"
    draft = requests[1]
    assert draft["text"]["format"]["name"] == "BuyerPanelV15"
    assert draft["max_output_tokens"] == 12_000
    assert json.loads(draft["input"])["search_console_queries"] == search_queries(
        searches(), ("example.com",), 40
    )
    assert "search_console_queries" in draft["instructions"]
    # Every question is read blind, then one review names the questions to drop.
    assert len(requests) == 1 + 1 + 16 + 1
    assert requests[-1]["text"]["format"]["name"] == "PanelReviewV15"
    assert all(r["max_output_tokens"] == AUDIT_POLICY["max_output_tokens"] for r in requests[2:])


@pytest.mark.asyncio
async def test_without_search_console_the_draft_reads_public_research_alone():
    _, _, _, count, requests = await drafted(with_searches=False)
    assert count == 64
    assert "search_console_queries" not in json.loads(requests[1]["input"])


@pytest.mark.asyncio
async def test_a_draft_with_five_jobs_is_redrafted_not_trimmed():
    # Plausible but unusable: twenty questions, past the schema's sixteen. The correction
    # asks again; the run never asks a panel the schema rejects.
    five = four_job_panel([*JOBS, "Booking team rooms"])
    calls = [
        research(),
        response(json.dumps(five), search=False),
        response(json.dumps(four_job_panel()), search=False),
        *[interpretation() for _ in range(16)],
        review(),
    ]
    activities, _, run_id, count, requests = await drafted(calls=calls)
    assert count == 64
    attempts = (await activities._result(run_id, "panel_preparation"))["attempts"]
    assert [a["reason"] for a in attempts[1:]] == ["panel_questions_invalid", None]
    assert json.loads(requests[2]["input"])["correction"]["reason"] == "panel_questions_invalid"


@pytest.mark.asyncio
async def test_v15_never_reads_a_published_prompt_panel(monkeypatch):
    content = await published(monkeypatch)
    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    await audit_with(db, storage, [panel_row(content)])
    activities.responses = SimpleNamespace(
        create=AsyncMock(
            side_effect=[
                research(),
                response(json.dumps(four_job_panel()), search=False),
                *[interpretation() for _ in range(16)],
                review(),
            ]
        )
    )
    assert await activities.organic_prepare_panel(run_id) == 64
    saved = await activities._result(run_id, "panel")
    assert "origin" not in saved and saved["name"] == "Acme"
    assert await activities._result(run_id, "prompt_panel") is None
