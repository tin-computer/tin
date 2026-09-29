"""Offline weekly social batch checks against edited project files."""

from __future__ import annotations

import json
import runpy
from fnmatch import fnmatchcase
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = REPOSITORY_ROOT / "workflow_packages" / "social.post_batch"
PLAN = """# Social plan

## Weekly calendar

| Day | Platform | Pillar | Post idea |
| --- | --- | --- | --- |
| Monday | X | Product proof | Explain import traceability |
| Thursday | LinkedIn | Product proof | Explain validation choices |
"""
NOTES = (
    "The CSV import keeps every accepted row tied to its original transaction.\n"
    "Invalid rows receive a validation result and stay out of charts.\n"
    "A reviewer can inspect an imported row beside its source record.\n"
)
FIRST = {
    "posts": [
        {
            "platform": "X",
            "body": (
                "A traceable chart begins at the row. "
                "Imported CSV rows retain their source transaction."
            ),
            "source_excerpt": (
                "The CSV import keeps every accepted row tied to its original transaction."
            ),
        },
        {
            "platform": "LinkedIn",
            "body": (
                "Validation is visible at the row level.\n\n"
                "Invalid rows receive a result and stay out of charts, "
                "so reviewers can see what was excluded."
            ),
            "source_excerpt": "Invalid rows receive a validation result and stay out of charts.",
        },
    ]
}
SECOND = {
    "posts": [
        {
            "platform": "X",
            "body": (
                "Review an imported row beside its source record when a chart needs a closer look."
            ),
            "source_excerpt": "A reviewer can inspect an imported row beside its source record.",
        },
    ]
}


class Files:
    def __init__(self, values):
        self.values = values
        self.reads = []

    def read_text(self, path):
        self.reads.append(path)
        if path not in self.values:
            raise FileNotFoundError(path)
        return self.values[path]

    def glob(self, pattern):
        return [path for path in self.values if fnmatchcase(path, pattern)]


class Context:
    def __init__(self, files, spec, output, run_id):
        self.files = files
        self.values = {"run_id": run_id, "created_at": "2026-09-29T12:00:00+00:00"}
        self.calls = []

        async def generate(**payload):
            request_contract(spec, payload)
            self.calls.append(payload)
            jsonschema.validate(output, payload["output_schema"])
            return {"parsed": output, "text": json.dumps(output)}

        self.models = SimpleNamespace(generate=generate)

    def __getitem__(self, key):
        return self.values[key]


def package():
    definition = json.loads((ROOT / "workflow.json").read_text())["definition"]
    return SimpleNamespace(**runpy.run_path(str(ROOT / "main.py"))), validate_code_definition(
        definition
    )


def inputs(**kwargs):
    return {"project_id": "a0000000-0000-0000-0000-000000000001", "mode": "weekly", **kwargs}


async def test_second_batch_reads_edits_and_keeps_prior_review_annotations():
    module, spec = package()
    values = {
        "social/PLAN.md": PLAN,
        "context/social-updates.md": NOTES,
        "context/product-marketing.md": (
            "Product context, not evidence: imported rows are inspectable."
        ),
    }
    first = Context(Files(values), spec, FIRST, "a0000000-0000-0000-0000-000000000091")
    result1 = await module.run(first, inputs())
    assert len(first.calls) == 1
    assert first.calls[0]["data"]["slots"][0]["idea"] == "Explain import traceability"
    assert "Current material held for later" in result1["content"]
    assert "A reviewer can inspect" in result1["content"]
    assert "Status: Draft" in result1["content"]
    validate_code_result(
        json.dumps(result1).encode(),
        spec,
        created_at=__import__("datetime").datetime.fromisoformat(first["created_at"]),
    )

    reviewed = result1["content"].replace("Status: Draft", "Status: Kept for later")
    values[result1["path"]] = reviewed
    history = module._history(Files(values))
    assert len(history["bodies"]) == 2
    assert FIRST["posts"][0]["body"] in history["bodies"]
    assert "Validation is visible at the row level." in history["bodies"][1]
    assert FIRST["posts"][0]["source_excerpt"].casefold() in history["excerpts"]
    values["social/PLAN.md"] = PLAN.replace(
        "Explain import traceability", "Show source record review"
    )
    values["social/PLAN.md"] = values["social/PLAN.md"].replace(
        "| Thursday | LinkedIn | Product proof | Explain validation choices |\n", ""
    )
    second = Context(Files(values), spec, SECOND, "a0000000-0000-0000-0000-000000000092")
    result2 = await module.run(second, inputs())
    assert len(second.calls) == 1
    assert second.calls[0]["data"]["slots"][0]["idea"] == "Show source record review"
    assert second.calls[0]["data"]["unused_source_sentences"] == [
        "A reviewer can inspect an imported row beside its source record."
    ]
    assert result2["path"] != result1["path"]
    assert values[result1["path"]] == reviewed
    assert "Kept for later" not in result2["content"]
    values[result2["path"]] = result2["content"]
    third = Context(Files(values), spec, SECOND, "a0000000-0000-0000-0000-000000000093")
    result3 = await module.run(third, inputs())
    assert third.calls == []
    assert "No new source material" in result3["content"]
    assert "2 of 2" in result3["content"]


async def test_weekly_rejects_plausible_unsupported_result():
    module, spec = package()
    output = {
        "posts": [
            {
                "platform": "X",
                "body": "Imports are 47% faster.",
                "source_excerpt": (
                    "The CSV import keeps every accepted row tied to its original transaction."
                ),
            },
            FIRST["posts"][1],
        ]
    }
    ctx = Context(
        Files({"social/PLAN.md": PLAN, "context/social-updates.md": NOTES}),
        spec,
        output,
        "a0000000-0000-0000-0000-000000000094",
    )
    with pytest.raises(ValueError, match="number"):
        await module.run(ctx, inputs())


async def test_weekly_shows_calendar_gaps_when_material_is_thin():
    module, spec = package()
    output = {"posts": [FIRST["posts"][0]]}
    ctx = Context(
        Files({"social/PLAN.md": PLAN, "context/social-updates.md": NOTES.splitlines()[0]}),
        spec,
        output,
        "a0000000-0000-0000-0000-000000000095",
    )
    result = await module.run(ctx, inputs())
    assert "Calendar slots awaiting material" in result["content"]
    assert "Thursday — LinkedIn" in result["content"]
    assert len(ctx.calls) == 1


def test_mixed_sentence_and_bullet_notes_remain_eligible():
    module, _ = package()
    notes = (
        "The import retains a source transaction for each accepted row.\n"
        "- Reviewers can compare one imported row with its source record\n"
    )
    assert module._unused_sentences(notes, set()) == [
        "The import retains a source transaction for each accepted row.",
        "Reviewers can compare one imported row with its source record",
    ]


async def test_raw_material_adds_to_current_notes_and_fragment_is_rejected():
    module, spec = package()
    raw = "A reviewer can inspect an imported row beside its source record."
    output = {
        "posts": [
            {
                "platform": "X",
                "body": "Inspect an imported row beside its source record.",
                "source_excerpt": "reviewer can inspect an imported row beside its source record.",
            }
        ]
    }
    one_slot = PLAN.replace(
        "| Thursday | LinkedIn | Product proof | Explain validation choices |\n", ""
    )
    ctx = Context(
        Files({"social/PLAN.md": one_slot, "context/social-updates.md": NOTES.splitlines()[0]}),
        spec,
        output,
        "a0000000-0000-0000-0000-000000000096",
    )
    with pytest.raises(ValueError, match="complete unused source statement"):
        await module.run(ctx, inputs(raw_material=raw))
    assert raw in ctx.calls[0]["data"]["unused_source_sentences"]


async def test_report_discloses_combined_file_and_caller_notes():
    module, spec = package()
    one_slot = PLAN.replace(
        "| Thursday | LinkedIn | Product proof | Explain validation choices |\n", ""
    )
    ctx = Context(
        Files({"social/PLAN.md": one_slot, "context/social-updates.md": NOTES}),
        spec,
        {"posts": [FIRST["posts"][0]]},
        "a0000000-0000-0000-0000-000000000097",
    )
    result = await module.run(
        ctx, inputs(raw_material="New current review notes from the founder.")
    )
    assert (
        "Source material: `context/social-updates.md` and caller-supplied notes"
        in result["content"]
    )
