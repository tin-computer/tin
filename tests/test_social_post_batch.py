"""Offline contract and grounding tests for social.post_batch; no supplier calls."""

from __future__ import annotations

import hashlib
import json
import runpy
from copy import deepcopy
from fnmatch import fnmatchcase
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import model_terms, request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = REPOSITORY_ROOT / "workflow_packages" / "social.post_batch"
PROJECT_ID = "a0000000-0000-0000-0000-000000000001"
ARTICLE_PATH = "content/drafts/article.md"
ARTICLE = (
    "# Traceable analytics imports\n\n"
    "A useful analytics chart begins with data that can be checked. "
    "The CSV import keeps each row tied to its original transaction. "
    "Teams can inspect an imported row and compare it with the source record before "
    "they use the chart in a decision. The import records a validation result for "
    "each row and leaves invalid rows out of the chart.\n"
)
STYLE_PATH = ".agents/skills/writing-style/SKILL.md"
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


class Files:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.reads = []
        self.globs = []

    def read_text(self, path):
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        value = self.files[path]
        if not isinstance(value, str):
            raise ValueError("file is not valid UTF-8 text")
        return value

    def glob(self, pattern):
        self.globs.append(pattern)
        return [path for path in self.files if fnmatchcase(path, pattern)]


class Context:
    def __getitem__(self, key):
        return self.context[key]

    def __init__(self, files, spec, output=POSTS):
        self.files = files
        self.calls = []
        self.context = {
            "run_id": "a0000000-0000-0000-0000-000000000099",
            "created_at": "2026-09-29T12:00:00+00:00",
        }

        async def generate(**payload):
            request_contract(spec, payload)
            self.calls.append(payload)
            jsonschema.validate(output, payload["output_schema"])
            return {"parsed": output, "text": json.dumps(output)}

        self.models = SimpleNamespace(generate=generate)


def inputs(**kwargs):
    return {"project_id": PROJECT_ID, **kwargs}


def test_manifest_uses_project_files_with_optional_plain_text_and_one_model_call():
    _, definition = load_package()
    spec = validate_code_definition(definition)
    assert definition["version"] == "3.0.0"
    assert definition["input_schema"]["required"] == ["project_id"]
    assert definition["input_schema"]["properties"]["article_path"]["maxLength"] == 512
    assert definition["input_schema"]["properties"]["article_text"]["maxLength"] == 20000
    assert definition["schedule_modes"] == ["on_demand"]
    assert definition["human_review"]["eligible"] is True
    assert "approved_article" not in definition["code"]
    assert not definition.get("integration_requirements")
    assert spec.output_path == "social/posts/{date}-{slug}.md"
    assert spec.max_bytes == 24_000
    assert [(route.name, route.model, route.max_calls) for route in spec.model_routes] == [
        ("draft", "gpt-6-sol", 1)
    ]
    assert model_terms(definition)["maximum_nanos"] > 0


@pytest.mark.parametrize("with_style", [True, False])
async def test_single_project_article_is_auto_selected_and_reviewable(with_style):
    module, definition = load_package()
    spec = validate_code_definition(definition)
    files = {ARTICLE_PATH: ARTICLE}
    if with_style:
        files[STYLE_PATH] = STYLE
    ctx = Context(Files(files), spec)

    result = await module.run(ctx, inputs())

    assert len(ctx.calls) == 1
    assert ctx.calls[0]["step"] == "draft_article_social_posts"
    assert ctx.calls[0]["data"]["article"] == ARTICLE
    assert ctx.calls[0]["data"]["style"] == (STYLE if with_style else "")
    assert result["path"] == "social/posts/2026-09-29-a0000000000000000000000000000099.md"
    report = result["content"]
    assert report.count("Source excerpt from the article:") == 4
    assert "## 1. First — X" in report
    assert "## 2. Next — LinkedIn" in report
    assert "## 3. Later — X" in report
    assert "## 4. Last — LinkedIn" in report
    assert hashlib.sha256(ARTICLE.encode()).hexdigest() in report
    assert "does not post or schedule" in report
    assert "approved" not in report.casefold()
    validate_code_result(
        json.dumps(result).encode(),
        spec,
        created_at=__import__("datetime").datetime.fromisoformat(ctx["created_at"]),
    )


async def test_named_file_wins_over_text_fallback():
    module, definition = load_package()
    named = ARTICLE.replace("Traceable analytics imports", "Named article")
    alternate = ARTICLE.replace("Traceable analytics imports", "Fallback article")
    ctx = Context(Files({ARTICLE_PATH: named}), validate_code_definition(definition))
    result = await module.run(ctx, inputs(article_path=ARTICLE_PATH, article_text=alternate))
    assert ctx.calls[0]["data"]["article"] == named
    assert "From article: Named article" in result["content"]
    assert "Caller-supplied" not in result["content"]


async def test_missing_named_file_uses_supplied_text():
    module, definition = load_package()
    ctx = Context(Files(), validate_code_definition(definition))
    result = await module.run(
        ctx,
        inputs(article_path="content/drafts/missing.md", article_text=ARTICLE),
    )
    assert ctx.calls[0]["data"]["article"] == ARTICLE
    assert "Caller-supplied article text" in result["content"]
    assert hashlib.sha256(ARTICLE.encode()).hexdigest() in result["content"]


async def test_unsafe_named_path_fails_before_file_read_or_model_call():
    module, definition = load_package()
    files = Files()
    ctx = Context(files, validate_code_definition(definition))
    with pytest.raises(ValueError, match="safe relative file path"):
        await module.run(ctx, inputs(article_path="../private.md", article_text=ARTICLE))
    assert files.reads == []
    assert ctx.calls == []


async def test_unreadable_or_nonregular_named_file_does_not_fall_back_to_text():
    module, definition = load_package()

    class UnsafeFiles(Files):
        def read_text(self, path):
            self.reads.append(path)
            if path == ARTICLE_PATH:
                raise ValueError("file too large or not regular")
            raise FileNotFoundError(path)

    files = UnsafeFiles()
    ctx = Context(files, validate_code_definition(definition))
    with pytest.raises(ValueError, match="not regular"):
        await module.run(ctx, inputs(article_path=ARTICLE_PATH, article_text=ARTICLE))
    assert ctx.calls == []


async def test_one_legacy_public_article_is_a_fallback_candidate():
    module, definition = load_package()
    ctx = Context(
        Files({"reports/PUBLIC_ARTICLE.md": ARTICLE}), validate_code_definition(definition)
    )
    await module.run(ctx, inputs())
    assert ctx.calls[0]["data"]["article"] == ARTICLE


async def test_multiple_files_refuse_arbitrary_selection_but_accept_text_fallback():
    module, definition = load_package()
    files = Files({ARTICLE_PATH: ARTICLE, "content/articles/second.md": ARTICLE})
    ctx = Context(files, validate_code_definition(definition))
    with pytest.raises(ValueError, match="Several project articles"):
        await module.run(ctx, inputs())
    assert ctx.calls == []

    ctx = Context(files, validate_code_definition(definition))
    await module.run(ctx, inputs(article_text=ARTICLE))
    assert ctx.calls[0]["data"]["article"] == ARTICLE


async def test_unselected_legacy_file_is_not_read_when_text_resolves_ambiguity():
    module, definition = load_package()

    class LegacyUnreadableFiles(Files):
        def read_text(self, path):
            if path == "reports/PUBLIC_ARTICLE.md":
                raise ValueError("legacy file is not regular")
            return super().read_text(path)

    files = LegacyUnreadableFiles(
        {
            ARTICLE_PATH: ARTICLE,
            "content/articles/second.md": ARTICLE,
            "reports/PUBLIC_ARTICLE.md": "not read",
        }
    )
    ctx = Context(files, validate_code_definition(definition))
    await module.run(ctx, inputs(article_text=ARTICLE))
    assert ctx.calls[0]["data"]["article"] == ARTICLE
    assert "reports/PUBLIC_ARTICLE.md" not in files.reads


async def test_generated_files_are_excluded_from_auto_selection():
    module, definition = load_package()
    files = Files(
        {
            "content/drafts/article.md": ARTICLE,
            "content/drafts/article.generation.md": "internal data",
        }
    )
    ctx = Context(files, validate_code_definition(definition))
    await module.run(ctx, inputs())
    assert ctx.calls[0]["data"]["article"] == ARTICLE


async def test_no_file_or_fallback_fails_before_model_call():
    module, definition = load_package()
    ctx = Context(Files(), validate_code_definition(definition))
    with pytest.raises(ValueError, match="No project article"):
        await module.run(ctx, inputs())
    assert ctx.calls == []


async def test_current_style_guide_is_read_from_pinned_project_files():
    module, definition = load_package()
    ctx = Context(
        Files({ARTICLE_PATH: ARTICLE, STYLE_PATH: STYLE}), validate_code_definition(definition)
    )
    result = await module.run(ctx, inputs())
    assert STYLE_PATH in ctx.files.reads
    assert ctx.calls[0]["data"]["style"] == STYLE
    assert hashlib.sha256(STYLE.encode()).hexdigest() in result["content"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda p: p["posts"][0].update(source_excerpt="A claim absent from the article."),
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
    ctx = Context(Files({ARTICLE_PATH: ARTICLE}), validate_code_definition(definition), output)
    with pytest.raises(ValueError, match=message):
        await module.run(ctx, inputs())


async def test_article_and_style_limits_fail_before_model_call():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    huge_article = ARTICLE + "Additional verified context. " * 1200
    ctx = Context(Files({ARTICLE_PATH: huge_article}), spec)
    with pytest.raises(ValueError, match="exceed one model request"):
        await module.run(ctx, inputs())
    assert ctx.calls == []

    ctx = Context(Files(), spec)
    with pytest.raises(ValueError, match="20,000 characters"):
        await module.run(ctx, inputs(article_text="a" * 20001))
    assert ctx.calls == []


async def test_wrapped_source_sentence_is_cited_without_clipping():
    module, definition = load_package()
    wrapped = ARTICLE.replace("keeps each row", "keeps each\nrow")
    ctx = Context(Files({ARTICLE_PATH: wrapped}), validate_code_definition(definition))
    result = await module.run(ctx, inputs())
    assert EXCERPT in result["content"]


async def test_source_excerpt_cannot_stop_at_a_wrapped_line():
    module, definition = load_package()
    output = deepcopy(POSTS)
    output["posts"][0]["source_excerpt"] = "The CSV import keeps each row tied to its original"
    ctx = Context(Files({ARTICLE_PATH: ARTICLE}), validate_code_definition(definition), output)
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
                "step": "draft_article_social_posts",
                "instructions": module.INSTRUCTIONS,
                "data": data,
                "output_schema": module.SCHEMA,
            },
        )
