"""Offline contract and grounding tests for social.post_batch; no supplier calls."""

from __future__ import annotations

import hashlib
import json
import runpy
from copy import deepcopy
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import model_terms, request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = REPOSITORY_ROOT / "workflow_packages" / "social.post_batch"
SOURCE_RUN_ID = "a0000000-0000-0000-0000-000000000041"
ARTICLE = (
    "# Traceable analytics imports\n\n"
    "A useful analytics chart begins with data that can be checked. "
    "The CSV import keeps each row tied to its original transaction. "
    "Teams can inspect an imported row and compare it with the source record before "
    "they use the chart in a decision. The import records a validation result for "
    "each row and leaves invalid rows out of the chart.\n"
)
STYLE = "Use short, concrete sentences. Avoid hype and exclamation marks.\n"
EXCERPT = "The CSV import keeps each row tied to its original transaction."
POSTS = {
    "posts": [
        {
            "platform": "X",
            "body": (
                "A chart is easier to trust when each CSV row traces to its source transaction."
            ),
            "source_excerpt": EXCERPT,
        },
        {
            "platform": "LinkedIn",
            "body": (
                "A useful chart starts with rows you can check.\n\n"
                "The CSV import keeps each row connected to the original transaction, "
                "so the source record is there when a result needs a closer look."
            ),
            "source_excerpt": EXCERPT,
        },
        {
            "platform": "X",
            "body": (
                "Invalid imported rows stay out of the chart. Each row gets a validation result."
            ),
            "source_excerpt": (
                "The import records a validation result for each row and leaves invalid rows "
                "out of the chart."
            ),
        },
        {
            "platform": "LinkedIn",
            "body": (
                "Before a chart informs a decision, inspect a row behind it.\n\n"
                "Compare that row with its source record. The CSV import preserves the "
                "connection to the original transaction."
            ),
            "source_excerpt": (
                "Teams can inspect an imported row and compare it with the source record before "
                "they use the chart in a decision."
            ),
        },
    ]
}


def load_package():
    definition = json.loads((ROOT / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))
    return module, definition


def source(*, with_style=True, article=ARTICLE):
    result = {
        "source_run_id": SOURCE_RUN_ID,
        "source_revision": "a" * 40,
        "source_path": f"content/drafts/{SOURCE_RUN_ID}.md",
        "source_sha256": "b" * 64,
        "article": article,
        "article_sha256": hashlib.sha256(article.encode()).hexdigest(),
        "title": "Traceable analytics imports",
    }
    result["style"] = (
        {
            "path": ".agents/skills/writing-style/SKILL.md",
            "revision": "c" * 40,
            "sha256": hashlib.sha256(STYLE.encode()).hexdigest(),
            "content": STYLE,
        }
        if with_style
        else {"path": ".agents/skills/writing-style/SKILL.md", "revision": "a" * 40, "sha256": None}
    )
    return result


class Context(dict):
    def __init__(self, approved_article, spec, output=POSTS):
        super().__init__(approved_article=approved_article)
        self.calls = []

        async def generate(**payload):
            request_contract(spec, payload)
            self.calls.append(payload)
            jsonschema.validate(output, payload["output_schema"])
            return {"parsed": output, "text": json.dumps(output)}

        self.models = SimpleNamespace(generate=generate)


def inputs():
    return {"project_id": "a0000000-0000-0000-0000-000000000001", "source_run_id": SOURCE_RUN_ID}


def test_manifest_is_one_manual_approved_article_model_call():
    _, definition = load_package()
    spec = validate_code_definition(definition)
    assert definition["input_schema"]["required"] == ["project_id", "source_run_id"]
    assert definition["schedule_modes"] == ["on_demand"]
    assert definition["human_review"]["eligible"] is True
    assert definition["code"]["approved_article"] == {"input": "source_run_id"}
    assert not definition.get("integration_requirements")
    assert spec.output_path == "reports/SOCIAL_POST_BATCH.md"
    assert spec.max_bytes == 24_000
    assert [(route.name, route.model, route.max_calls) for route in spec.model_routes] == [
        ("draft", "gpt-6-sol", 1)
    ]
    assert model_terms(definition)["maximum_nanos"] > 0


@pytest.mark.parametrize("with_style", [True, False])
async def test_valid_batch_uses_full_pinned_source_and_renders_reviewable_drafts(with_style):
    module, definition = load_package()
    spec = validate_code_definition(definition)
    chosen = source(with_style=with_style)
    ctx = Context(chosen, spec)

    result = await module.run(ctx, inputs())

    assert len(ctx.calls) == 1
    assert ctx.calls[0]["step"] == "draft_approved_article_social_posts"
    assert ctx.calls[0]["data"]["article"] == ARTICLE
    assert ctx.calls[0]["data"]["style"] == (STYLE if with_style else "")
    assert result["path"] == "reports/SOCIAL_POST_BATCH.md"
    report = result["content"]
    assert report.count("Source excerpt from the approved article:") == 4
    assert "## 1. First — X" in report
    assert "## 2. Next — LinkedIn" in report
    assert "## 3. Later — X" in report
    assert "## 4. Last — LinkedIn" in report
    assert chosen["article_sha256"] in report
    assert "does not post or schedule" in report
    validate_code_result(json.dumps(result).encode(), spec)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda p: p["posts"][0].update(
                source_excerpt="A claim absent from the approved article."
            ),
            "absent",
        ),
        (lambda p: p["posts"][0].update(body="The import is 92% faster."), "number"),
        (
            lambda p: p["posts"][0].update(body='A customer called it "perfect for everyone".'),
            "quote",
        ),
        (lambda p: p["posts"][0].update(body="Read it at https://unknown.example/story"), "link"),
        (lambda p: p["posts"][0].update(body="We doubled our results."), "first-person"),
        (lambda p: p["posts"][0].update(body="é" * 121), "X length"),
        (lambda p: p["posts"][1].update(body="a" * 1401), "LinkedIn length"),
        (lambda p: p["posts"][0].update(platform="LinkedIn"), "target X"),
        (lambda p: p["posts"][2].update(body=p["posts"][0]["body"]), "repeats"),
        (lambda p: p["posts"][0].update(body="a" * 1500), "clipped"),
    ],
)
async def test_plausible_but_unusable_model_output_is_rejected(change, message):
    module, definition = load_package()
    output = deepcopy(POSTS)
    change(output)
    # Some cases reach the code guard after strict schema; clipping reaches its
    # maxLength exactly and remains schema-valid, so it must be rejected here.
    ctx = Context(source(), validate_code_definition(definition), output=output)
    with pytest.raises(ValueError, match=message):
        await module.run(ctx, inputs())


async def test_missing_or_mismatched_approved_source_fails_before_model_call():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    for chosen in (None, {**source(), "source_run_id": "a0000000-0000-0000-0000-000000000099"}):
        ctx = Context(chosen, spec)
        with pytest.raises(ValueError, match="approved article"):
            await module.run(ctx, inputs())
        assert ctx.calls == []


async def test_incomplete_or_tampered_source_fails_before_model_call():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    tampered = {**source(), "article_sha256": "0" * 64}
    ctx = Context(tampered, spec)
    with pytest.raises(ValueError, match="digest"):
        await module.run(ctx, inputs())
    assert ctx.calls == []

    long_source = source(article=ARTICLE + "Additional verified context. " * 1200)
    ctx = Context(long_source, spec)
    with pytest.raises(ValueError, match="no source was cut"):
        await module.run(ctx, inputs())
    assert ctx.calls == []


async def test_tampered_pinned_style_fails_before_model_call():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    chosen = source()
    chosen["style"] = {**chosen["style"], "sha256": "0" * 64}
    ctx = Context(chosen, spec)
    with pytest.raises(ValueError, match="style guide digest"):
        await module.run(ctx, inputs())
    assert ctx.calls == []


async def test_unicode_expansion_is_bounded_before_requesting_a_model():
    module, definition = load_package()
    chosen = source(article=ARTICLE + "é" * 4500)
    ctx = Context(chosen, validate_code_definition(definition))
    with pytest.raises(ValueError, match="no source was cut"):
        await module.run(ctx, inputs())
    assert ctx.calls == []


async def test_wrapped_source_sentence_is_cited_without_clipping():
    module, definition = load_package()
    wrapped = ARTICLE.replace("keeps each row", "keeps each\nrow")
    ctx = Context(source(article=wrapped), validate_code_definition(definition))
    result = await module.run(ctx, inputs())
    assert EXCERPT in result["content"]


async def test_source_excerpt_cannot_stop_at_a_wrapped_line():
    module, definition = load_package()
    output = deepcopy(POSTS)
    output["posts"][0]["source_excerpt"] = "The CSV import keeps each row tied to its original"
    ctx = Context(source(), validate_code_definition(definition), output=output)
    with pytest.raises(ValueError, match="complete source sentence"):
        await module.run(ctx, inputs())


@pytest.mark.parametrize("text", ["é", "漢", "😀", '"', "\\", "a"])
def test_preflight_admitted_text_fits_actual_gateway_serialization(text):
    module, definition = load_package()
    spec = validate_code_definition(definition)
    for count in (500, 1500, 3000, 4500, 6000, 12000):
        data = {"article": ARTICLE + text * count, "title": "Test", "style": STYLE}
        try:
            module._request_fits(data)
        except ValueError:
            continue
        request_contract(
            spec,
            {
                "route": "draft",
                "step": "draft_approved_article_social_posts",
                "instructions": module.INSTRUCTIONS,
                "data": data,
                "output_schema": module.SCHEMA,
            },
        )
