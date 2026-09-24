"""Tests for growth.hidden_channel_attribution.

Modeled on the test pattern used for example.feedback_digest in
tests/test_public_workflows.py: mock ctx.models.generate, run the async
entrypoint, and assert on validation behaviour.
"""

import json
import runpy
from types import SimpleNamespace

import pytest

from tin_lite.community import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT / "workflow_packages" / "growth.hidden_channel_attribution"


def load_module():
    return SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))


SAMPLE_RESPONSES = [
    "My friend Sarah told me about it",
    "Saw a post in the Indie Hackers Discord",
    "Someone shared it on LinkedIn",
]

# --- helpers ----------------------------------------------------------------


def make_ctx(classify_output, summarize_output):
    """Return a mock ctx whose models.generate returns canned payloads."""
    call_log = []

    async def generate(**payload):
        call_log.append(payload["step"])
        if payload["step"] == "classify_responses":
            out = classify_output
        else:
            out = summarize_output
        return {"parsed": out, "text": json.dumps(out)}

    ctx = SimpleNamespace(models=SimpleNamespace(generate=generate))
    return ctx, call_log


# --- valid path -------------------------------------------------------------


async def test_valid_classification_produces_report():
    module = load_module()
    classify_out = {
        "items": [
            {"id": 0, "category": "personal_referral"},
            {"id": 1, "category": "developer_community"},
            {"id": 2, "category": "social_media_post"},
        ]
    }
    summarize_out = {
        "points": [
            "Personal referral and developer community account for 2 of 3 signups.",
            "Social media posts are contributing organically without paid spend.",
        ]
    }
    ctx, call_log = make_ctx(classify_out, summarize_out)
    result = await module.run(ctx, {"responses": SAMPLE_RESPONSES, "project_id": "test"})

    # Both model steps were called in the right order
    assert call_log == ["classify_responses", "summarize_channels"]

    # Output has the right path and shape
    assert result["path"] == "reports/HIDDEN_CHANNEL_ATTRIBUTION.md"
    content = result["content"]
    assert "# Hidden channel attribution" in content
    assert "Responses analyzed: 3" in content
    assert "## Channel breakdown" in content
    assert "personal_referral: 1 of 3" in content
    assert "developer_community: 1 of 3" in content
    assert "social_media_post: 1 of 3" in content
    assert "## What this means" in content
    # Evidence quotes are picked in code (shortest per category), not invented
    assert "My friend Sarah told me about it" in content
    # Summary bullets appear
    assert "Personal referral" in content


# --- invalid ID: missing ID raises ValueError --------------------------------


async def test_missing_id_in_classification_raises():
    """If the model omits one input ID, run() must raise ValueError."""
    module = load_module()
    # ID 2 is missing — only returns 0 and 1
    classify_out = {
        "items": [
            {"id": 0, "category": "personal_referral"},
            {"id": 1, "category": "developer_community"},
            # id 2 missing
        ]
    }
    # summarize won't be reached, but provide a valid payload anyway
    summarize_out = {"points": ["placeholder"]}
    ctx, _ = make_ctx(classify_out, summarize_out)

    with pytest.raises(ValueError, match="preserve every input ID exactly once"):
        await module.run(ctx, {"responses": SAMPLE_RESPONSES, "project_id": "test"})


# --- invalid ID: duplicate ID raises ValueError -----------------------------


async def test_duplicate_id_in_classification_raises():
    """If the model duplicates an ID (and omits another), run() must raise ValueError."""
    module = load_module()
    # ID 0 is returned twice; ID 2 is missing
    classify_out = {
        "items": [
            {"id": 0, "category": "personal_referral"},
            {"id": 0, "category": "developer_community"},  # duplicate
            {"id": 1, "category": "social_media_post"},
        ]
    }
    summarize_out = {"points": ["placeholder"]}
    ctx, _ = make_ctx(classify_out, summarize_out)

    with pytest.raises(ValueError, match="preserve every input ID exactly once"):
        await module.run(ctx, {"responses": SAMPLE_RESPONSES, "project_id": "test"})


# --- empty input raises ValueError ------------------------------------------


async def test_all_whitespace_responses_raises():
    module = load_module()

    async def generate(**_):
        raise AssertionError("model should not be called for empty input")

    ctx = SimpleNamespace(models=SimpleNamespace(generate=generate))
    with pytest.raises(ValueError, match="No non-empty responses"):
        await module.run(ctx, {"responses": ["   ", "\t", ""], "project_id": "test"})
