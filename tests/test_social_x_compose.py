"""Offline package checks; no provider calls or project writes."""

import json
import runpy
import sys
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result
from tin_lite.x_posts import validate_draft
from tin_lite.x_text import weighted_length

ROOT = REPOSITORY_ROOT / "workflow_packages" / "social.x_compose"
PROJECT_ID = "a0000000-0000-0000-0000-000000000061"
FACT = "We built a workflow that returns exact JSON post drafts through MCP for review."
POST = {
    "text": "We built a workflow that returns exact JSON post drafts through MCP for review.",
    "readiness": "ready",
    "support_ids": ["s1"],
    "editor_notes": "Check the current file before posting.",
    "attachments": [],
    "missing_assets": [],
}


def package():
    sys.path.insert(0, str(ROOT))
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        module = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))
    finally:
        sys.dont_write_bytecode = previous
        sys.path.pop(0)
    definition = json.loads((ROOT / "workflow.json").read_text())["definition"]
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

    def glob(self, path):
        return [path] if path in self.values else []


class Context(dict):
    def __init__(self, values, spec, output=None):
        super().__init__(
            run_id="12345678-1234-4678-9234-567812345678",
            created_at="2026-09-29T12:00:00Z",
        )
        self.files = Files(values)
        self.calls = []
        self.output = output or {"posts": [POST]}

        async def generate(**payload):
            request_contract(spec, payload)
            self.calls.append(payload)
            jsonschema.validate(self.output, payload["output_schema"])
            return {"parsed": deepcopy(self.output), "text": json.dumps(self.output)}

        self.models = SimpleNamespace(generate=generate)


def inputs(**overrides):
    return {"project_id": PROJECT_ID, "direction": FACT, **overrides}


def test_manifest_and_shared_counter():
    _, definition = package()
    spec = validate_code_definition(definition)
    assert definition["schedule_modes"] == ["on_demand"]
    assert definition["human_review"]["eligible"] is False
    assert spec.output_path == "social/x-drafts/{date}-{slug}.json"
    assert [(r.model, r.max_calls, r.max_input_bytes) for r in spec.model_routes] == [
        ("gpt-6-sol", 1, 32000)
    ]
    assert (ROOT / "x_text.py").read_bytes() == (
        REPOSITORY_ROOT / "src/tin_lite/x_text.py"
    ).read_bytes()


async def test_plan_free_singleton_uses_exact_current_fact_and_json_artifact():
    module, definition = package()
    spec = validate_code_definition(definition)
    ctx = Context({"context/product-marketing.md": "Tin is a project workflow service."}, spec)
    result = await module.run(ctx, inputs())
    artifact = json.loads(result["content"])
    assert result["path"].startswith("social/x-drafts/2026-09-29-")
    assert artifact["posts"][0]["id"] == "p1"
    assert artifact["posts"][0]["text"] == POST["text"]
    assert artifact["posts"][0]["support"] == [{"source_path": "direction", "excerpt": FACT}]
    assert artifact["account_id"] == ""
    assert ctx.calls[0]["step"] == "compose_x_posts"
    validate_code_result(
        json.dumps(result).encode(),
        spec,
        created_at=datetime.fromisoformat(ctx["created_at"].replace("Z", "+00:00")),
    )


async def test_plan_is_topic_guidance_not_a_source_citation():
    module, definition = package()
    spec = validate_code_definition(definition)
    ctx = Context({"social/PLAN.md": "Claim: we doubled revenue.\n"}, spec)
    result = await module.run(ctx, inputs(plan_path="social/PLAN.md"))
    assert "doubled revenue" in ctx.calls[0]["data"]["optional_plan"]
    assert all(
        unit["source_path"] != "social/PLAN.md" for unit in ctx.calls[0]["data"]["source_units"]
    )
    assert json.loads(result["content"])["posts"][0]["text"] == FACT


async def test_matching_account_guide_used_and_mismatch_ignored():
    module, definition = package()
    spec = validate_code_definition(definition)
    guide = "X account ID: 12345\nUse dry humor and short lines."
    values = {module.STYLE_PATH: guide}
    matched = Context(values, spec)
    result = await module.run(matched, inputs(account_id="12345"))
    assert json.loads(result["content"])["account_id"] == "12345"
    assert matched.calls[0]["data"]["x_writing_style"] == guide
    other = Context(values, spec)
    result = await module.run(other, inputs(account_id="67890"))
    assert json.loads(result["content"])["account_id"] == "67890"
    assert other.calls[0]["data"]["x_writing_style"] == ""


async def test_image_and_video_attachments_have_typed_paths():
    module, definition = package()
    spec = validate_code_definition(definition)
    image = deepcopy(POST)
    image["attachments"] = [
        {"type": "image", "path": "assets/screen.png", "alt_text": "A draft preview in Tin"}
    ]
    ctx = Context({"assets/screen.png": b"PNG"}, spec, {"posts": [image]})
    result = await module.run(ctx, inputs(asset_paths=["assets/screen.png"]))
    assert json.loads(result["content"])["posts"][0]["attachments"] == image["attachments"]
    video = deepcopy(POST)
    video["attachments"] = [{"type": "video", "path": "assets/demo.mp4", "alt_text": ""}]
    ctx = Context({"assets/demo.mp4": b"MP4"}, spec, {"posts": [video]})
    result = await module.run(ctx, inputs(asset_paths=["assets/demo.mp4"]))
    assert json.loads(result["content"])["posts"][0]["attachments"] == video["attachments"]


async def test_partial_batch_explains_gap_and_duplicate_batch_fails():
    module, definition = package()
    spec = validate_code_definition(definition)
    ctx = Context({}, spec)
    result = await module.run(ctx, inputs(post_count=3))
    assert len(json.loads(result["content"])["posts"]) == 1
    assert json.loads(result["content"])["posts"][0]["editor_notes"]
    duplicate = Context({}, spec, {"posts": [POST, POST]})
    with pytest.raises(ValueError, match="repeats"):
        await module.run(duplicate, inputs(post_count=2))


@pytest.mark.parametrize(
    "change, message",
    [
        ({"support_ids": ["s999"]}, "nonexistent"),
        ({"support_ids": []}, "cite"),
        ({"text": "We built 50 workflows overnight."}, "unsupported number"),
        ({"text": "We built a workflow. “We doubled revenue”"}, "unsupported quotation"),
        ({"text": "x" * 281}, "280 weighted"),
        (
            {"attachments": [{"type": "image", "path": "assets/fake.png", "alt_text": "fake"}]},
            "selected",
        ),
        ({"readiness": "needs_asset", "missing_assets": []}, "name the missing"),
    ],
)
async def test_plausible_unusable_model_outputs_rejected(change, message):
    module, definition = package()
    spec = validate_code_definition(definition)
    post = {**deepcopy(POST), **change}
    ctx = Context({}, spec, {"posts": [post]})
    with pytest.raises(ValueError, match=message):
        await module.run(ctx, inputs())


async def test_missing_evidence_and_oversized_guide_fail_before_paid_call():
    module, definition = package()
    spec = validate_code_definition(definition)
    ctx = Context({}, spec)
    with pytest.raises(ValueError, match="concrete current fact"):
        await module.run(ctx, inputs(direction="what next?"))
    assert not ctx.calls
    guide = "X account ID: 12345\n" + "Be precise about what shipped.\n" * 1000
    ctx = Context({module.STYLE_PATH: guide}, spec)
    with pytest.raises(ValueError, match="24000-byte"):
        await module.run(ctx, inputs(account_id="12345"))
    assert not ctx.calls


async def test_large_valid_context_and_guide_select_complete_relevant_units():
    module, definition = package()
    spec = validate_code_definition(definition)
    target = "MCP now returns the current project file revision in the X draft preview."
    filler = [
        f"The onboarding page explains a general buyer workflow step number {index}."
        for index in range(500)
    ]
    context = "\n".join(filler + [target])
    assert 35_000 < len(context.encode()) < 64_000
    guide = "X account ID: 12345\n## Explicit preferences\n" + "\n".join(
        f"- Keep observation {index} concrete and brief." for index in range(480)
    )
    assert 8_000 < len(guide.encode()) < 24_000
    direction = "Write about MCP returning the current file revision in an X preview."
    selected, _ = module._source_units("reports/GROWTH_ONBOARDING_PLAN.md", context)
    target_id = f"s{len(selected) + 1}"  # Direction contributes s1.
    post = {
        **deepcopy(POST),
        "text": "MCP now returns the current project file revision in the X draft preview.",
        "support_ids": [target_id],
    }
    ctx = Context(
        {"reports/GROWTH_ONBOARDING_PLAN.md": context, module.STYLE_PATH: guide},
        spec,
        {"posts": [post]},
    )
    result = await module.run(ctx, inputs(direction=direction, account_id="12345"))
    data = ctx.calls[0]["data"]
    assert any(unit["excerpt"] == target for unit in data["source_units"])
    assert data["source_coverage"]["source_units_selected"] < 502
    assert data["source_coverage"]["guide_statements_selected"] < 480
    assert "## Explicit preferences" in data["x_writing_style"]
    artifact = json.loads(result["content"])
    assert artifact["posts"][0]["support"] == [
        {"source_path": "reports/GROWTH_ONBOARDING_PLAN.md", "excerpt": target}
    ]
    assert "unselected statements were not reviewed" in artifact["posts"][0]["editor_notes"]


async def test_large_explicit_evidence_selects_relevant_fact_without_truncating_it():
    module, definition = package()
    spec = validate_code_definition(definition)
    target = "The MCP preview preserves the selected screenshot order in the draft."
    evidence = "\n".join(
        [
            f"An unrelated operational note records routine task number {index}."
            for index in range(800)
        ]
        + [target]
    )
    assert 40_000 < len(evidence.encode()) < 64_000
    ctx = Context({"notes/release.md": evidence}, spec)
    result = await module.run(
        ctx,
        inputs(
            direction="Explain how the MCP preview preserves screenshot order.",
            evidence_paths=["notes/release.md"],
        ),
    )
    assert any(unit["excerpt"] == target for unit in ctx.calls[0]["data"]["source_units"])
    assert (
        "unselected statements were not reviewed"
        in json.loads(result["content"])["posts"][0]["editor_notes"]
    )


def test_weighted_counter_common_and_adversarial_cases():
    assert weighted_length("Hi http://test.co") == 26
    assert weighted_length("界") == 2
    assert weighted_length("e\u0301") == 1  # NFC
    assert weighted_length("https://example.com/" + "a" * 500 + ".") == 24
    assert weighted_length("http://bad_host.example.com") > 23
    assert weighted_length("example.com") == 23
    assert weighted_length("https://x.xyz") >= 23
    assert weighted_length("tin.computer") == 23
    assert weighted_length("x" * 267 + " https://x.xyz") > 280
    assert weighted_length("x" * 269 + " tin.computer") > 280
    unknown = "https://verylongsubdomain.example.completely/a" * 8
    assert weighted_length(unknown) >= len(unknown)
    false_prefix = "https://verylongsubdomain.com.invalid"
    assert weighted_length(false_prefix) >= len(false_prefix)
    punctuated = "https://example.com/short,not-url-text"
    assert weighted_length(punctuated) >= 23 + len(",not-url-text")
    assert weighted_length("https://example.com,https://x.xyz") >= 47
    assert weighted_length("1️⃣") >= 2
    assert weighted_length("👨‍👩‍👧‍👦") >= 2
    assert weighted_length("🇺🇸") >= 2


@pytest.mark.parametrize("tail", ["https://x.xyz", "tin.computer"])
def test_trusted_draft_rejects_short_url_or_bare_domain_overflow(tail):
    draft = {
        "schema_version": "tin.social.x_draft.v1",
        "account_id": "",
        "posts": [
            {
                "id": "p1",
                "text": "x" * 267 + " " + tail,
                "readiness": "ready",
                "support": [],
                "editor_notes": "",
                "attachments": [],
                "missing_assets": [],
            }
        ],
    }
    with pytest.raises(ValueError, match="character limit"):
        validate_draft(draft)
