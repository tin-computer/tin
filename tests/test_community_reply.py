"""Offline checks for the bounded Hacker News community reply workflow."""

import asyncio
import importlib.util
from pathlib import Path

import pytest


MAIN = (
    Path(__file__).parents[1]
    / "workflow_packages/growth.community_reply_queue/main.py"
)
SPEC = importlib.util.spec_from_file_location("community_reply_queue", MAIN)
WORKFLOW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKFLOW)


def hit(*, story_id="100", object_id="101", text="We need help debugging slow API requests."):
    return {
        "objectID": object_id,
        "story_id": story_id,
        "story_title": "Ask HN: How do you debug slow API requests?",
        "comment_text": text,
        "created_at": "2026-09-22T12:00:00Z",
        "created_at_i": 1790078400,
        "points": 4,
        "num_comments": 6,
        "_tags": ["comment"],
    }


def review(candidate_index=0, *, mentions_product=False, reply=None):
    return {
        "parsed": {
            "candidates": [
                {
                    "candidate_index": candidate_index,
                    "fit_score": 84,
                    "why_this_fits": "The author asks for a concrete debugging approach.",
                    "reply_draft": reply
                    or "I would start by separating queue time from handler time.",
                    "mentions_product": mentions_product,
                    "review_check": "Read the parent thread and check whether it is still open.",
                }
            ]
        }
    }


class FakeServices:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses[len(self.calls) - 1]


class FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class Context(dict):
    def __init__(self, service_responses, model_response):
        super().__init__(created_at="2026-09-23T00:00:00+00:00")
        self.services = FakeServices(service_responses)
        self.models = FakeModels(model_response)


def inputs(**overrides):
    values = {
        "project_id": "00000000-0000-4000-8000-000000000001",
        "audience": "Developers maintaining small production APIs",
        "problem_queries": ["slow API debugging", "production error monitoring"],
        "lookback_days": 14,
        "product_name": "TraceKit",
        "product_summary": "TraceKit groups API errors by endpoint.",
        "notes": "Do not make unsupported claims.",
    }
    values.update(overrides)
    return values


def response(*hits):
    return {"status": 200, "data": {"hits": list(hits)}}


def test_search_is_bounded_deduplicated_and_requires_human_review():
    ctx = Context(
        [
            response(hit()),
            response(hit(text="A longer comment with more context about the slow API problem.")),
        ],
        review(),
    )
    result = asyncio.run(WORKFLOW.run(ctx, inputs()))

    assert len(ctx.services.calls) == 2
    assert [call["step"] for call in ctx.services.calls] == ["search_problem_1", "search_problem_2"]
    assert all(call["method"] == "GET" for call in ctx.services.calls)
    assert all(call["params"]["tags"] == "(story,comment)" for call in ctx.services.calls)
    assert all(call["params"]["hitsPerPage"] == 5 for call in ctx.services.calls)
    assert "created_at_i>" in ctx.services.calls[0]["params"]["numericFilters"]
    assert len(ctx.models.calls) == 1
    assert len(ctx.models.calls[0]["data"]["candidates"]) == 1
    assert result["path"] == "reports/COMMUNITY_REPLY_QUEUE.md"
    assert "https://news.ycombinator.com/item?id=100" in result["content"]
    assert "Human review before posting" in result["content"]
    assert "never posts or contacts anyone" in result["content"]


def test_empty_sample_skips_model_and_reports_no_qualified_discussions():
    ctx = Context([response(), response()], review())
    result = asyncio.run(WORKFLOW.run(ctx, inputs()))

    assert not ctx.models.calls
    assert "No discussions qualified" in result["content"]


def test_rejects_a_plausible_but_unusable_model_candidate_index():
    ctx = Context([response(hit()), response()], review(candidate_index=15))

    with pytest.raises(ValueError, match="changed or duplicated"):
        asyncio.run(WORKFLOW.run(ctx, inputs()))


def test_product_mention_requires_supported_summary_and_disclosure():
    ctx = Context([response(hit()), response()], review(mentions_product=True))

    with pytest.raises(ValueError, match="clear disclosure"):
        asyncio.run(WORKFLOW.run(ctx, inputs()))


def test_rejects_boolean_as_lookback_days():
    ctx = Context([], review())

    with pytest.raises(ValueError, match="integer from 1 through 30"):
        asyncio.run(WORKFLOW.run(ctx, inputs(lookback_days=True)))
        