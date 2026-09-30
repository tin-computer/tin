"""Offline tests for social.content_plan; no paid model or project services."""

import json
import runpy
from copy import deepcopy
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = REPOSITORY_ROOT / "workflow_packages" / "social.content_plan"
PROJECT_ID = "a0000000-0000-0000-0000-000000000001"
CONTEXT = (
    "Tin helps product teams turn project context into repeatable marketing workflows. "
    "Teams can review drafted files in the project before choosing to publish them. "
    "The current social workflow creates drafts only; it does not post to social platforms."
)
PLAN = {
    "audience": "Small product teams that need a steady marketing rhythm with limited time.",
    "objective": (
        "Help teams understand how to turn current product context into reviewable marketing work."
    ),
    "platform_reason": (
        "LinkedIn fits the professional team audience; X gives a concise "
        "place for one product-work observation at a time."
    ),
    "pillars": [
        {
            "name": "Practical workflow",
            "share": 40,
            "angle": "Explain a concrete marketing workflow step and its review point.",
        },
        {
            "name": "Product context",
            "share": 35,
            "angle": "Show how current product files inform a useful draft.",
        },
        {
            "name": "Editorial judgment",
            "share": 25,
            "angle": "Describe a decision a team should check before publishing.",
        },
    ],
    "calendar": [
        {
            "day": "Monday",
            "platform": "LinkedIn",
            "pillar": "Practical workflow",
            "idea": "Explain how a product team turns one context file into a reviewable draft.",
        },
        {
            "day": "Wednesday",
            "platform": "X",
            "pillar": "Product context",
            "idea": "Ask which product fact a team verifies before drafting a short update.",
        },
        {
            "day": "Friday",
            "platform": "LinkedIn",
            "pillar": "Editorial judgment",
            "idea": "Describe the decision to hold a weakly evidenced idea for later review.",
        },
    ],
    "engagement": (
        "After each draft, respond to relevant comments and note useful "
        "questions for the next week."
    ),
    "measurement": (
        "Record useful replies and profile visits weekly, then adjust the next set of topics."
    ),
    "constraints": (
        "Check product details against current files and verify "
        "any results or quotes before drafting."
    ),
}


def package():
    definition = json.loads((ROOT / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))
    return module, definition


class Files:
    def __init__(self, values):
        self.values = values
        self.reads = []

    def read_text(self, path):
        self.reads.append(path)
        if path not in self.values:
            raise FileNotFoundError(path)
        return self.values[path]


class Context:
    def __init__(self, values, spec, output=PLAN):
        self.files = Files(values)
        self.calls = []

        async def generate(**payload):
            request_contract(spec, payload)
            self.calls.append(payload)
            jsonschema.validate(output, payload["output_schema"])
            return {"parsed": output, "text": json.dumps(output)}

        self.models = SimpleNamespace(generate=generate)


def inputs(**overrides):
    return {"project_id": PROJECT_ID, **overrides}


def test_manifest_has_one_bounded_model_call_and_reviewable_file():
    _, definition = package()
    spec = validate_code_definition(definition)
    assert spec.output_path == "social/PLAN.md"
    assert spec.max_bytes == 12_000
    assert definition["schedule_modes"] == ["on_demand"]
    assert definition["human_review"]["eligible"] is True
    assert definition["input_schema"]["required"] == ["project_id"]
    assert [
        (route.model, route.max_calls, route.max_input_bytes, route.max_output_tokens)
        for route in spec.model_routes
    ] == [("gpt-6-sol", 1, 32_000, 4096)]


async def test_prioritized_file_produces_editable_calendar_without_blending_other_projects():
    module, definition = package()
    spec = validate_code_definition(definition)
    ctx = Context(
        {
            "context/product-marketing.md": CONTEXT,
            "reports/GROWTH_ONBOARDING_PLAN.md": "An older onboarding plan",
            "brand/BRAND.md": "Current brand, lower priority than curated product context",
            "BRAND.md": "Old unrelated brand",
            "wiki/INDEX.md": "Obsolete project",
            module.STYLE_PATH: "Use plain, short sentences. " * 300,
        },
        spec,
    )
    result = await module.run(ctx, inputs(context_text="Another old product"))
    assert result["path"] == "social/PLAN.md"
    assert ctx.files.reads == ["context/product-marketing.md", module.STYLE_PATH]
    assert ctx.calls[0]["data"]["product_context"] == CONTEXT
    assert len(ctx.calls[0]["data"]["writing_style"]) > 6_000
    assert ctx.calls[0]["step"] == "draft_social_content_plan"
    report = result["content"]
    assert "## Content pillars" in report
    assert "## Weekly calendar\n\n| Day | Platform | Pillar | Post idea |" in report
    assert report.count("| Monday |") == 1
    assert "160 of 180 minutes" in report
    assert "Old unrelated brand" not in report
    validate_code_result(json.dumps(result).encode(), spec)


async def test_fallback_context_when_files_absent_and_single_platform():
    module, definition = package()
    one = deepcopy(PLAN)
    one["calendar"] = [one["calendar"][1]]
    ctx = Context({}, validate_code_definition(definition), one)
    result = await module.run(ctx, inputs(context_text=CONTEXT, hours_per_week=1, platforms="x"))
    assert ctx.calls[0]["data"]["context_source"] == "Supplied product context"
    assert ctx.calls[0]["data"]["slot_count"] == 1
    assert "| Wednesday | X |" in result["content"]
    assert "60 of 60 minutes" in result["content"]


async def test_onboarding_plan_precedes_brand_and_wiki_when_curated_context_is_absent():
    module, definition = package()
    ctx = Context(
        {
            "reports/GROWTH_ONBOARDING_PLAN.md": CONTEXT,
            "brand/BRAND.md": CONTEXT,
            "BRAND.md": "Old unrelated brand",
            "wiki/INDEX.md": "Obsolete code map",
        },
        validate_code_definition(definition),
    )
    await module.run(ctx, inputs())
    assert ctx.files.reads == [
        "context/product-marketing.md",
        "reports/GROWTH_ONBOARDING_PLAN.md",
        module.STYLE_PATH,
    ]
    assert ctx.calls[0]["data"]["product_context"] == CONTEXT
    assert ctx.calls[0]["data"]["context_source"] == "reports/GROWTH_ONBOARDING_PLAN.md"


async def test_adopted_brand_file_precedes_legacy_brand_and_wiki():
    module, definition = package()
    ctx = Context(
        {"brand/BRAND.md": CONTEXT, "BRAND.md": "Old brand", "wiki/INDEX.md": "Old wiki"},
        validate_code_definition(definition),
    )
    await module.run(ctx, inputs())
    assert ctx.calls[0]["data"]["context_source"] == "brand/BRAND.md"


async def test_missing_context_fails_before_paid_call():
    module, definition = package()
    ctx = Context({}, validate_code_definition(definition))
    with pytest.raises(ValueError, match="Add context/product-marketing.md"):
        await module.run(ctx, inputs())
    assert ctx.calls == []


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda p: p["pillars"][0].update(share=35), "total 100%"),
        (lambda p: p["calendar"][1].update(pillar="Invented pillar"), "absent from the plan"),
        (lambda p: p["calendar"][1].update(day="Monday"), "distinct"),
        (
            lambda p: p["calendar"][0].update(idea="See https://fake.example for details"),
            "unsupported",
        ),
        (lambda p: p["calendar"].pop(), "time budget"),
        (lambda p: p.update(constraints="x" * 480), "invalid constraints"),
        (
            lambda p: p.update(constraints="Verify the source before stating a signup-"),
            "unfinished constraints",
        ),
    ],
)
async def test_plausible_but_unusable_model_plan_is_rejected(change, message):
    module, definition = package()
    bad = deepcopy(PLAN)
    change(bad)
    ctx = Context({"BRAND.md": CONTEXT}, validate_code_definition(definition), bad)
    with pytest.raises(ValueError, match=message):
        await module.run(ctx, inputs())
    assert len(ctx.calls) == 1


def onboarding_plan(size=22_000):
    """Start here writes a plan this long on every onboarded project."""
    section = "## Channel\n\n" + CONTEXT + "\n\n"
    return ("# Growth plan\n\n" + section * (size // len(section) + 1))[:size]


async def test_long_onboarding_plan_gives_way_to_a_shorter_source():
    module, definition = package()
    spec = validate_code_definition(definition)
    ctx = Context(
        {"reports/GROWTH_ONBOARDING_PLAN.md": onboarding_plan(), "BRAND.md": CONTEXT}, spec
    )
    await module.run(ctx, inputs())
    assert ctx.calls[0]["data"]["context_source"] == "BRAND.md"
    ctx = Context({"reports/GROWTH_ONBOARDING_PLAN.md": onboarding_plan()}, spec)
    await module.run(ctx, inputs(context_text=CONTEXT))
    assert ctx.calls[0]["data"]["context_source"] == "Supplied product context"


async def test_long_onboarding_plan_alone_is_read_to_the_context_limit():
    module, definition = package()
    plan = onboarding_plan()
    ctx = Context({"reports/GROWTH_ONBOARDING_PLAN.md": plan}, validate_code_definition(definition))
    result = await module.run(ctx, inputs())
    data = ctx.calls[0]["data"]
    assert data["context_source"] == "reports/GROWTH_ONBOARDING_PLAN.md (first part)"
    assert len(data["product_context"]) <= module.MAX_CONTEXT_CHARS
    assert plan.startswith(data["product_context"])
    assert plan[len(data["product_context"])] == "\n"  # cut at a line, not mid-sentence
    assert result["path"] == "social/PLAN.md"


async def test_oversized_style_fails_before_paid_call():
    module, definition = package()
    spec = validate_code_definition(definition)
    ctx = Context({"BRAND.md": CONTEXT, module.STYLE_PATH: "s" * 10_001}, spec)
    with pytest.raises(ValueError, match="Writing-style guide"):
        await module.run(ctx, inputs())
    assert ctx.calls == []
