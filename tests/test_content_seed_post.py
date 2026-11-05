"""Offline tests for the content.seed_post package: the step order, the copy checks, and that a
paid model result that cannot be used leaves a draft and a note rather than a failed run."""

from __future__ import annotations

import json
import re
import runpy
import shutil
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import model_terms, request_contract, validate_output_schema
from tin_lite.code_project_files import _glob_matches
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result

PACKAGE = REPOSITORY_ROOT / "workflow_packages" / "content.seed_post"
CREATED_AT = "2026-11-04T09:00:00+00:00"
SEED = (
    "# PR 491: Tin Analytics counts AI agents apart from people\n\n"
    "Merged 2026-10-23. Tin Analytics now sorts each visit by what made it: a person, an AI "
    "assistant fetching a page for someone, a crawler that trains a model, or a search bot. "
    "Founders see people and agents on separate lines. Mira from Acme asked for this on a call. "
    "Install: one script tag from https://tin.computer/analytics. The reporter reads server "
    "logs when a site has them.\n"
) * 3
BRAND = "# Brand\n\nTin Computer runs marketing for software founders. Start free: https://tin.computer/start\n"


def plan(**overrides):
    value = {
        "decision": "write",
        "decision_reason": "The seed explains a change founders can use.",
        "covered_by": "",
        "reader": "A software founder who reads their site analytics each week",
        "question": "How do I tell AI agent visits from people in my analytics?",
        "answer": " ".join(
            ["Sort every visit by what made it and count agents on their own line."] * 4
        ),
        "buyer_outcome": "Weekly numbers that show real readers and AI assistants apart",
        "seed_support": "The sorting rule and the install step",
        "private": ["Mira", "Acme"],
        "business_name": "Tin Computer",
        "call_to_action": "Start free: https://tin.computer/start",
        "slug": "tell-ai-agent-visits-from-people",
        "sections": [
            {
                "heading": "Four kinds of visit in your logs",
                "opening": "Each visit comes from a person, an assistant, a crawler or a bot.",
                "points": ["The four kinds [seed]"],
            },
            {
                "heading": "Separate lines for people and agents",
                "opening": "A weekly report reads clearly when agents sit on their own line.",
                "points": ["Separate lines [seed]"],
            },
        ],
        "worked_example": {
            "heading": "Reading a week of mixed traffic",
            "opening": "A small site can see its assistant visits grow while its people stay flat.",
            "format": "table",
            "case": "A week of visits sorted into the four kinds",
        },
        "checklist_heading": "Checks for your next weekly report",
        "checklist": [
            {"title": "Install the script", "line": "Add one script tag."},
            {"title": "Read the agent line", "line": "Compare it with last week."},
            {"title": "Keep crawlers apart", "line": "Training crawlers are not readers."},
        ],
        "sources": [],
        "related": [{"title": "Invented page", "url": "https://tin.computer/blog/made-up"}],
        "closing": {"heading": "Start counting your agent visits", "line": "Start free."},
        "heading_reading": "The headings move from kinds of visit to a weekly habit.",
        "opening_reading": "Each opening states its point.",
    }
    value.update(overrides)
    return value


def paragraph(topic, sentences=3):
    line = f"Tin Analytics sorts each {topic} visit by what made it, so your count stays true."
    return " ".join([line] * sentences)


def draft(*, dash=True, link=True, preface=""):
    body = [
        "# How do I tell AI agent visits from people in my analytics?",
        "",
        " ".join(["Sort every visit by what made it and count agents on their own line."] * 4),
        "",
        "## Four kinds of visit in your logs",
        "",
        paragraph("person"),
        "",
        (
            "Assistants fetch pages for someone — they are not readers."
            if dash
            else paragraph("assistant")
        ),
        "",
        "## Separate lines for people and agents",
        "",
        paragraph("crawler"),
        "",
        (
            "Read the [install notes](https://example.com/invented) before you start."
            if link
            else paragraph("bot")
        ),
        "",
        "## Reading a week of mixed traffic",
        "",
        "| Kind | Visits |\n|---|---|\n| People | 120 |\n| Assistants | 40 |",
        "",
        "## Checks for your next weekly report",
        "",
        "### Install the script",
        "",
        paragraph("script", 1),
        "",
        "## Start counting your agent visits",
        "",
        "Start free at [Tin Computer](https://tin.computer/start) and read your first week.",
    ]
    padding = [paragraph(f"week {n}") for n in range(12)]
    text = "\n".join(body[:8] + ["", *[p + "\n" for p in padding]] + body[8:])
    return preface + text + "\n"


REVIEW = {
    "findings": [
        {
            "where": "Four kinds of visit in your logs",
            "quote": "Assistants fetch pages for someone",
            "problem": "Uses a contrast formula",
            "fix": "State the claim directly",
        }
    ],
    "heading_reading": "The story holds.",
    "verdict": "small_edits",
}
FINAL = {
    "status": "ready_with_notes",
    "fact_checks": [
        {
            "claim": "120 people",
            "why": "The seed has no visit counts.",
            "question": "Which week do these counts come from?",
        }
    ],
    "notes": ["The worked example uses sample counts."],
}


class Files:
    def __init__(self, files, sections=None):
        self.files, self.sections = files, sections or {}

    def read_text(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def glob(self, pattern):
        return sorted(path for path in self.files if _glob_matches(path, pattern))

    def read_section(self, heading):
        if heading not in self.sections:
            raise FileNotFoundError(heading)
        return self.sections[heading]


def project_files(**extra):
    return Files(
        {
            "context/notes/2026-10-23-analytics.md": SEED,
            "brand/BRAND.md": BRAND,
            ".agents/skills/writing-style/SKILL.md": "Write short, literal sentences.",
            "content/drafts/2026-10-01-older-post.md": "# Older post about onboarding\n\nText.",
            **extra,
        },
        {"### Feature map": "- Analytics: shipped"},
    )


def load(root=PACKAGE):
    definition = json.loads((root / "workflow.json").read_text())["definition"]
    return SimpleNamespace(**runpy.run_path(str(root / "main.py"))), definition


def context(spec, outputs, calls, files=None):
    async def generate(**payload):
        request_contract(spec, payload)
        calls.append(payload)
        output = outputs[payload["step"]]
        if callable(output):
            output = output(payload)
        if isinstance(output, Exception):
            raise output
        if payload["output_schema"] is None:
            return {"text": output, "parsed": None}
        jsonschema.validate(output, payload["output_schema"])
        return {"text": json.dumps(output), "parsed": output}

    ctx = SimpleNamespace()
    value = {"run_id": "run-1", "created_at": CREATED_AT}

    class Context(dict):
        models = SimpleNamespace(generate=generate)

    ctx = Context(value)
    ctx.files = files or project_files()
    return ctx


def fix_dashes(payload):
    return {
        "paragraphs": [
            {"index": item["index"], "text": item["text"].replace(" — ", ". ").replace("—", ", ")}
            for item in payload["data"]["paragraphs"]
        ]
    }


def outputs(**overrides):
    value = {
        "angle": plan(),
        "draft": draft(),
        "review": REVIEW,
        "revise": lambda payload: payload["data"]["draft"],
        "copy_fix": fix_dashes,
        "final": FINAL,
    }
    value.update(overrides)
    return value


def inputs(**overrides):
    return {
        "project_id": "a0000000-0000-0000-0000-000000000001",
        "seed_path": "context/notes/2026-10-23-analytics.md",
        **overrides,
    }


def created():
    from datetime import datetime

    return datetime.fromisoformat(CREATED_AT)


def test_contract_routes_and_ceiling():
    module, definition = load()
    spec = validate_code_definition(definition)
    assert spec.timeout_seconds == 900 and spec.summary is True
    assert spec.output_path == "content/drafts/{date}-{slug}.md"
    assert {(r.name, r.model) for r in spec.model_routes} == {
        ("plan", "gpt-6.1-sol"),
        ("write", "gpt-6.1-sol"),
        ("review", "gpt-6.1-sol"),
    }
    assert all(r.max_input_bytes == module.INPUT_BYTES for r in spec.model_routes)
    for schema in (
        module.PLAN_SCHEMA,
        module.REVIEW_SCHEMA,
        module.FIX_SCHEMA,
        module.FINAL_SCHEMA,
    ):
        validate_output_schema(schema)
    maximum = model_terms(definition)["maximum_nanos"]
    assert 0 < maximum <= 4_000_000_000
    assert set(spec.files) == {
        "main.py",
        "recipe.json",
        "rules/guide.md",
        "rules/style.md",
        "rules/review.md",
        "rules/tells.txt",
    }


async def test_steps_in_order_and_a_clean_draft():
    module, definition = load()
    spec = validate_code_definition(definition)
    calls = []
    result = await module.run(context(spec, outputs(), calls), inputs())
    assert [c["step"] for c in calls] == ["angle", "draft", "review", "revise", "copy_fix", "final"]
    assert result["path"] == "content/drafts/2026-11-04-tell-ai-agent-visits-from-people.md"
    content = result["content"]
    assert content.startswith("# How do I tell AI agent visits from people in my analytics?\n")
    assert "—" not in content
    # An invented link keeps its words and loses its address; the site's own link stays.
    assert "https://example.com/invented" not in content and "install notes" in content
    assert "(https://tin.computer/start)" in content
    validate_code_result(json.dumps(result).encode(), spec, created_at=created())
    # The planner's invented related link never reaches the writer.
    assert calls[1]["data"]["plan"]["related"] == []
    # Business files and the style guide reach the planner; the seed is not read twice.
    paths = [item["path"] for item in calls[0]["data"]["business_files"]]
    assert paths == ["brand/BRAND.md", "wiki/INDEX.md Feature map"]
    assert calls[0]["data"]["style_guide"] == "Write short, literal sentences."
    assert calls[0]["data"]["existing_posts"] == [
        {"path": "content/drafts/2026-10-01-older-post.md", "title": "Older post about onboarding"}
    ]
    summary = result["summary"]
    assert summary["headline"].startswith("Draft ready, with items to check: How do I tell")
    sections = {s["heading"]: s["items"] for s in summary["sections"]}
    assert sections["Check before publishing"][0]["text"].startswith('"120 people"')
    assert any("removed 1 link" in item["text"] for item in sections["Notes"])
    assert "seed-post.r1" in sections["How it was written"][-1]["text"]


async def test_a_plan_that_breaks_heading_rules_gets_one_fix():
    module, definition = load()
    spec = validate_code_definition(definition)
    broken = plan(
        sections=[
            {
                "heading": "Why do agents visit?",
                "opening": "Agents fetch pages.",
                "points": ["x [seed]"],
            },
            {
                "heading": "Separate lines for people and agents",
                "opening": "Lines.",
                "points": ["y [seed]"],
            },
        ]
    )
    calls = []
    ctx = context(spec, outputs(angle=broken, plan_fix=plan()), calls)
    await module.run(ctx, inputs())
    assert [c["step"] for c in calls][:3] == ["angle", "plan_fix", "draft"]
    assert any("is a question" in p for p in calls[1]["data"]["problems"])


async def test_unusable_drafts_leave_the_plan_written_out():
    """A plausible model reply that is not a post: a preface and no H1, twice."""
    module, definition = load()
    spec = validate_code_definition(definition)
    calls = []
    chat = "Sure! Here is a short outline you could expand into a post about analytics."
    ctx = context(spec, outputs(draft=chat, draft_retry=chat), calls)
    result = await module.run(ctx, inputs())
    assert [c["step"] for c in calls] == ["angle", "draft", "draft_retry", "copy_fix", "final"]
    assert result["content"].startswith("# How do I tell AI agent visits from people")
    assert "## Four kinds of visit in your logs" in result["content"]
    notes = next(s for s in result["summary"]["sections"] if s["heading"] == "Notes")["items"]
    assert any("plan written out" in item["text"] for item in notes)


async def test_failed_steps_after_payment_keep_the_draft():
    module, definition = load()
    spec = validate_code_definition(definition)
    calls = []
    ctx = context(
        spec,
        outputs(
            review=ValueError("model_output_invalid"),
            revise="Too short.",
            final=ValueError("model_result_unconfirmed"),
        ),
        calls,
    )
    result = await module.run(ctx, inputs())
    assert result["content"].startswith("# How do I tell AI agent visits")
    assert "Tin Analytics sorts each week 3 visit" in result["content"]
    notes = [
        i["text"]
        for s in result["summary"]["sections"]
        if s["heading"] == "Notes"
        for i in s["items"]
    ]
    assert any("did not have the expected shape" in n for n in notes)
    assert any("draft stays" in n for n in notes)
    assert any("no confirmed result" in n for n in notes)


async def test_covered_or_thin_seed_leaves_a_note_and_no_post():
    module, definition = load()
    spec = validate_code_definition(definition)
    calls = []
    covered = plan(decision="covered", covered_by="content/drafts/2026-10-01-older-post.md")
    result = await module.run(context(spec, outputs(angle=covered), calls), inputs())
    assert [c["step"] for c in calls] == ["angle"]
    assert result["content"].startswith("# Not written: How do I tell")
    assert "content/drafts/2026-10-01-older-post.md" in result["content"]
    assert result["summary"]["headline"].startswith("No post written")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"seed_path": "", "seed_text": "too short"}, "at least 200 characters"),
        ({"seed_path": "context/notes/missing.md"}, "not in project Files"),
        (
            {"seed_path": "", "seed_text": SEED + " key sk-proj-" + "a" * 30},
            "looks like a credential",
        ),
        ({"seed_path": "../outside.md"}, "relative path"),
        ({"revise_path": "content/drafts/2026-10-01-older-post.md"}, "Add feedback"),
    ],
)
async def test_bad_input_is_refused_before_any_model_call(overrides, message):
    module, definition = load()
    spec = validate_code_definition(definition)
    calls = []
    with pytest.raises(ValueError, match=message):
        await module.run(context(spec, outputs(), calls), inputs(**overrides))
    assert calls == []


async def test_revision_applies_feedback_to_the_same_slug():
    module, definition = load()
    spec = validate_code_definition(definition)
    previous = draft(dash=False, link=False)
    files = project_files(**{"content/drafts/2026-11-01-agent-visits.md": previous})
    calls = []

    def revised(payload):
        assert payload["data"]["feedback"] == "Shorter closing."
        return payload["data"]["post"].replace("read your first week", "start")

    ctx = context(spec, outputs(feedback_revise=revised), calls, files)
    result = await module.run(
        ctx,
        inputs(
            seed_path="",
            revise_path="content/drafts/2026-11-01-agent-visits.md",
            feedback="Shorter closing.",
        ),
    )
    # The four-sentence answer paragraph is over the limit, so one fix call runs.
    assert [c["step"] for c in calls] == ["feedback_revise", "copy_fix", "final"]
    assert result["path"] == "content/drafts/2026-11-04-agent-visits.md"
    assert "and start." in result["content"]


async def test_a_recipe_copy_changes_the_steps(tmp_path):
    """A variant is a copy of the package with another recipe.json (and routes)."""
    root = tmp_path / "content.seed_post"
    shutil.copytree(PACKAGE, root)
    recipe = json.loads((root / "recipe.json").read_text())
    (root / "recipe.json").write_text(
        json.dumps({**recipe, "id": "seed-post.single", "pipeline": "single", "final_read": False})
    )
    module, definition = load(root)
    spec = validate_code_definition(definition)
    calls = []
    result = await module.run(context(spec, outputs(draft=draft(dash=False)), calls), inputs())
    assert [c["step"] for c in calls] == ["draft", "copy_fix"]
    assert result["path"].endswith("-how-do-i-tell-ai-agent-visits-from.md")
    assert re.search(r"Recipe seed-post\.single", json.dumps(result["summary"]))


async def test_a_lab_copy_picks_an_arm_and_its_routes(tmp_path):
    """One lab copy holds several arms; the `arm` input picks one, and an arm can send the
    writing steps to another declared route, such as a second model."""
    root = tmp_path / "custom.seed_post_lab"
    shutil.copytree(PACKAGE, root)
    manifest = json.loads((root / "workflow.json").read_text())
    definition = manifest["definition"]
    definition["key"] = "custom.seed_post_lab"
    routes = definition["code"]["model_routes"]
    routes["astra"] = {**routes["write"], "model": "gpt-6-astra", "max_calls": 3}
    definition["input_schema"]["properties"]["arm"] = {
        "type": "string",
        "enum": ["steps", "astra-writes"],
        "default": "steps",
    }
    (root / "workflow.json").write_text(json.dumps(manifest))
    recipe = json.loads((root / "recipe.json").read_text())
    recipe["arms"] = {"steps": {}, "astra-writes": {"routes": {"write": "astra"}}}
    (root / "recipe.json").write_text(json.dumps(recipe))
    module, definition = load(root)
    spec = validate_code_definition(definition)
    calls = []
    result = await module.run(context(spec, outputs(), calls), inputs(arm="astra-writes"))
    routes_used = {(c["step"], c["route"]) for c in calls}
    assert routes_used == {
        ("angle", "plan"),
        ("draft", "astra"),
        ("review", "review"),
        ("revise", "astra"),
        ("copy_fix", "astra"),
        ("final", "review"),
    }
    assert "seed-post.r1/astra-writes" in json.dumps(result["summary"])
    with pytest.raises(ValueError, match="Unknown arm"):
        await module.run(context(spec, outputs(), []), inputs(arm="luna"))
