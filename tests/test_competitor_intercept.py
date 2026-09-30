"""Offline tests for growth.competitor_intercept."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PACKAGE = Path(__file__).parent.parent / "workflow_packages" / "growth.competitor_intercept"


def load_main():
    spec = importlib.util.spec_from_file_location("competitor_intercept_main", PACKAGE / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = load_main()
build_queries = mod.build_queries
run = mod.run

BASE_INPUTS = {
    "project_id": "00000000-0000-0000-0000-000000000001",
    "competitor_name": "Acme CRM",
    "your_product_name": "Relay",
    "your_one_liner": "Relay is a lightweight CRM for freelancers who hate spreadsheets.",
}

SCORED_THREADS = [
    {
        "query_index": 0,
        "title": "Acme CRM keeps crashing on export — anyone else?",
        "score": 5,
        "reason": "Strong complaint, no accepted answer",
        "complaint_summary": "User loses export progress due to crashes.",
    },
    {
        "query_index": 2,
        "title": "Left Acme CRM for spreadsheets — worth it?",
        "score": 4,
        "reason": "Clear intent to switch",
        "complaint_summary": "User is evaluating alternatives.",
    },
    {
        "query_index": 1,
        "title": "Acme CRM pricing question",
        "score": 2,
        "reason": "Not a complaint",
        "complaint_summary": "User asking about pricing tiers.",
    },
]

DRAFTED_REPLIES = [
    {
        "query_index": 0,
        "reply_text": (
            "Export bugs are brutal when you're in the middle of a deal. "
            "We ran into the same thing and ended up building Relay, "
            "a simpler CRM for freelancers — happy to share if it helps."
        ),
        "tone_note": "Opens with empathy before any mention of the product.",
    },
    {
        "query_index": 2,
        "reply_text": (
            "Spreadsheets work until they don't. "
            "We made Relay for exactly this — lightweight, no bloat, "
            "built for solo operators."
        ),
        "tone_note": "Validates the frustration rather than dismissing it.",
    },
]


def make_ctx(score_result, draft_result):
    ctx = MagicMock()
    ctx.models.generate = AsyncMock(
        side_effect=[
            {"parsed": score_result},
            {"parsed": draft_result},
        ]
    )
    return ctx


def test_build_queries_no_pain_points():
    queries = build_queries("Acme CRM", None)
    assert len(queries) >= 5
    for q in queries:
        assert "reddit_url" in q
        assert "hn_url" in q
        assert "Acme CRM" in q["term"]
        assert q["reddit_url"].startswith("https://www.reddit.com/search/")
        assert q["hn_url"].startswith("https://hn.algolia.com/")


def test_build_queries_with_pain_points():
    queries = build_queries("Acme CRM", ["pricing", "slow support"])
    terms = [q["term"] for q in queries]
    assert any("pricing" in t for t in terms)
    assert any("slow support" in t for t in terms)


def test_build_queries_indices_unique():
    queries = build_queries("X", ["a", "b", "c"])
    indices = [q["index"] for q in queries]
    assert indices == list(range(len(queries)))


@pytest.mark.asyncio
async def test_run_happy_path():
    ctx = make_ctx(
        score_result={"threads": SCORED_THREADS},
        draft_result={"drafts": DRAFTED_REPLIES},
    )
    result = await run(ctx, BASE_INPUTS)
    assert result["path"] == "reports/COMPETITOR_INTERCEPT.md"
    content = result["content"]
    assert "Acme CRM" in content
    assert "Relay" in content
    assert "Acme CRM keeps crashing" in content
    assert "pricing question" not in content
    assert "Export bugs are brutal" in content
    assert "All search queries" in content
    assert "reddit.com" in content


@pytest.mark.asyncio
async def test_run_with_pain_points():
    inputs = {**BASE_INPUTS, "pain_points": ["no API", "expensive"]}
    ctx = make_ctx(
        score_result={"threads": SCORED_THREADS},
        draft_result={"drafts": DRAFTED_REPLIES},
    )
    result = await run(ctx, inputs)
    content = result["content"]
    assert "no API" in content or "expensive" in content


@pytest.mark.asyncio
async def test_run_no_qualifying_threads():
    ctx = make_ctx(
        score_result={"threads": [{"query_index": 0, "title": "General discussion", "score": 1, "reason": "Not a complaint", "complaint_summary": "General chat."}]},
        draft_result={"drafts": []},
    )
    result = await run(ctx, BASE_INPUTS)
    assert "No threads scoring 3 or above" in result["content"]


@pytest.mark.asyncio
async def test_run_score_step_returns_non_list_raises():
    ctx = MagicMock()
    ctx.models.generate = AsyncMock(return_value={"parsed": {"threads": "not a list"}})
    with pytest.raises(ValueError, match="list of threads"):
        await run(ctx, BASE_INPUTS)


@pytest.mark.asyncio
async def test_run_draft_step_returns_non_list_raises():
    ctx = MagicMock()
    ctx.models.generate = AsyncMock(
        side_effect=[
            {"parsed": {"threads": SCORED_THREADS}},
            {"parsed": {"drafts": "not a list"}},
        ]
    )
    with pytest.raises(ValueError, match="list of drafts"):
        await run(ctx, BASE_INPUTS)


@pytest.mark.asyncio
async def test_run_draft_missing_for_thread_graceful():
    ctx = make_ctx(
        score_result={"threads": [SCORED_THREADS[0]]},
        draft_result={"drafts": []},
    )
    result = await run(ctx, BASE_INPUTS)
    assert "No reply drafted" in result["content"]


@pytest.mark.asyncio
async def test_run_output_keys():
    ctx = make_ctx(
        score_result={"threads": SCORED_THREADS},
        draft_result={"drafts": DRAFTED_REPLIES},
    )
    result = await run(ctx, BASE_INPUTS)
    assert set(result.keys()) == {"path", "content"}
    assert isinstance(result["content"], str)
    assert len(result["content"].encode()) < 32_000