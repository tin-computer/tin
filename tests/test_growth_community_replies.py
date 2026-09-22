"""Offline fixture tests for the growth.community_replies contributed package.

No network access and no real model calls: ctx.models.generate is stubbed, matching the
pattern used for example.feedback_digest in test_public_workflows.py.
"""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import request_contract
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = Path(__file__).parents[1]
KEY = "growth.community_replies"
PACKAGE = ROOT / "workflow_packages" / KEY


def package():
    definition = json.loads((PACKAGE / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(PACKAGE / "main.py")))
    return module, definition


def stub(spec, script):
    """script maps step -> parsed output; also records every request payload."""
    calls = []

    async def generate(**payload):
        request_contract(spec, payload)
        calls.append(payload)
        output = script[payload["step"]]
        jsonschema.validate(output, payload["output_schema"])
        return {"parsed": output, "text": json.dumps(output)}

    return SimpleNamespace(models=SimpleNamespace(generate=generate)), calls


async def test_relevant_post_is_classified_and_drafted():
    module, definition = package()
    spec = validate_code_definition(definition)
    script = {
        "classify_relevance": {
            "items": [
                {"id": 0, "relevant": True, "reason": "They are asking for exactly this."},
                {"id": 1, "relevant": False, "reason": "Unrelated small talk."},
            ]
        },
        "draft_replies": {"items": [{"id": 0, "reply": "I make a tool for this, happy to help."}]},
    }
    context, calls = stub(spec, script)
    inputs = {
        "product_pitch": "A CLI that summarizes CSV exports for indie founders.",
        "posts": [
            "r/smallbusiness: does anyone know a quick way to total a CSV export?",
            "r/cats: my cat knocked over a plant again",
        ],
    }
    result = await module.run(context, inputs)
    assert [c["step"] for c in calls] == ["classify_relevance", "draft_replies"]
    assert calls[1]["data"]["posts"] == [{"id": 0, "text": inputs["posts"][0]}]
    validate_code_result(json.dumps(result).encode(), spec)
    assert "Worth a reply: 1" in result["content"]
    assert "I make a tool for this, happy to help." in result["content"]
    assert "Skipped: Unrelated small talk." in result["content"]


async def test_no_relevant_posts_skips_the_draft_step_entirely():
    module, definition = package()
    spec = validate_code_definition(definition)
    script = {
        "classify_relevance": {
            "items": [{"id": 0, "relevant": False, "reason": "Not about this problem."}]
        },
    }
    context, calls = stub(spec, script)
    result = await module.run(context, {"product_pitch": "A CSV tool.", "posts": ["hello world"]})
    assert [c["step"] for c in calls] == ["classify_relevance"]
    validate_code_result(json.dumps(result).encode(), spec)
    assert "Worth a reply: 0" in result["content"]
    assert "Draft reply" not in result["content"]


@pytest.mark.parametrize("bad_id", [1, 5])
async def test_relevance_must_preserve_every_id_exactly_once(bad_id):
    module, definition = package()
    spec = validate_code_definition(definition)
    script = {
        "classify_relevance": {"items": [{"id": bad_id, "relevant": True, "reason": "x"}]},
    }
    context, _ = stub(spec, script)
    with pytest.raises(ValueError, match="preserve every input ID"):
        await module.run(context, {"product_pitch": "A CSV tool.", "posts": ["only one post"]})


async def test_drafts_must_cover_every_relevant_post_exactly_once():
    module, definition = package()
    spec = validate_code_definition(definition)
    script = {
        "classify_relevance": {"items": [{"id": 0, "relevant": True, "reason": "Good fit."}]},
        "draft_replies": {"items": [{"id": 1, "reply": "wrong id"}]},
    }
    context, _ = stub(spec, script)
    with pytest.raises(ValueError, match="cover every relevant post"):
        await module.run(context, {"product_pitch": "A CSV tool.", "posts": ["only one post"]})
