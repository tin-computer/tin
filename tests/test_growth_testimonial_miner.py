"""Offline tests for the growth.testimonial_miner code package.

Mirrors the pattern used for example.feedback_digest in test_public_workflows.py:
load the real manifest and main.py from disk, validate the manifest against the
code contract, and drive `run` with a stub `ctx.models.generate` so no network
or model credentials are needed.
"""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import request_contract
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = Path(__file__).resolve().parent.parent / "workflow_packages" / "growth.testimonial_miner"


def load():
    definition = json.loads((ROOT / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))
    return module, definition


def make_context(extract_response, brief_response=None):
    spec = validate_code_definition(load()[1])
    calls = []

    async def generate(**payload):
        request_contract(spec, payload)
        calls.append(payload)
        output = extract_response if payload["step"] == "extract_quotes" else brief_response
        jsonschema.validate(output, payload["output_schema"])
        return {"parsed": output, "text": json.dumps(output)}

    return SimpleNamespace(models=SimpleNamespace(generate=generate)), calls, spec


BASE_CSV = (
    "text,source,visibility\n"
    "We cut onboarding time from two weeks to two days,call with Priya Sept 2026,private\n"
    "Way easier to use than the last tool we tried,G2 review Aug 2026,public\n"
    "great tool!,support chat,private\n"
)


async def test_valid_run_produces_theme_sections_and_permission_asks():
    module, definition = load()
    extract_response = {
        "items": [
            {
                "id": 0,
                "theme": "time_saved",
                "usable": True,
                "quote": "cut onboarding time from two weeks to two days",
            },
            {
                "id": 1,
                "theme": "ease_of_use",
                "usable": True,
                "quote": "way easier to use than the last tool we tried",
            },
            {"id": 2, "theme": "other", "usable": False, "quote": "n/a"},
        ]
    }
    brief_response = {
        "theme_notes": [
            {"theme": "time_saved", "placement": "pricing page"},
            {"theme": "ease_of_use", "placement": "homepage hero"},
        ],
        "permission_asks": [
            "Ask Priya (Sept 2026 call) for permission to quote the onboarding-time line."
        ],
    }
    context, calls, spec = make_context(extract_response, brief_response)
    result = await module.run(context, {"product_name": "Acme", "feedback_csv": BASE_CSV})
    assert [c["step"] for c in calls] == ["extract_quotes", "write_brief"]
    assert "Time saved" in result["content"]
    assert "Ease of use" in result["content"]
    assert "Needs permission before public use" in result["content"]
    assert "Clear to use" in result["content"]
    assert "Ask Priya" in result["content"]
    assert "great tool" not in result["content"]  # non-usable item excluded
    validate_code_result(json.dumps(result).encode(), spec)


async def test_no_usable_quotes_skips_the_second_model_call_and_says_so():
    module, definition = load()
    extract_response = {"items": [{"id": 0, "theme": "other", "usable": False, "quote": "n/a"}]}
    context, calls, spec = make_context(extract_response)
    lone_row = "text,source,visibility\ngreat tool!,support chat,private\n"
    result = await module.run(context, {"product_name": "Acme", "feedback_csv": lone_row})
    assert [c["step"] for c in calls] == ["extract_quotes"]  # brief route never called
    assert "No item in this batch" in result["content"]
    validate_code_result(json.dumps(result).encode(), spec)


async def test_duplicate_rows_are_collapsed_before_scoring():
    module, definition = load()
    duplicate_csv = (
        "text,source,visibility\n"
        "We cut onboarding time from two weeks to two days,call with Priya,private\n"
        "We cut onboarding time from two weeks to two days,a different chat,private\n"
    )
    extract_response = {
        "items": [
            {
                "id": 0,
                "theme": "time_saved",
                "usable": True,
                "quote": "cut onboarding time from two weeks to two days",
            },
        ]
    }
    brief_response = {
        "theme_notes": [{"theme": "time_saved", "placement": "pricing page"}],
        "permission_asks": [],
    }
    context, calls, spec = make_context(extract_response, brief_response)
    result = await module.run(context, {"product_name": "Acme", "feedback_csv": duplicate_csv})
    assert "After de-dup: 1" in result["content"]
    assert len(calls[0]["data"]) == 1


async def test_extraction_must_preserve_every_id_exactly_once():
    module, definition = load()
    extract_response = {"items": [{"id": 99, "theme": "other", "usable": False, "quote": "n/a"}]}
    context, _, _ = make_context(extract_response)
    lone_row = "text,source,visibility\ngreat tool!,support chat,private\n"
    with pytest.raises(ValueError, match="preserve every input ID"):
        await module.run(context, {"product_name": "Acme", "feedback_csv": lone_row})


async def test_pii_is_scrubbed_before_it_ever_reaches_the_model():
    module, definition = load()
    row = (
        "text,source,visibility\n"
        '"Email me at dana@example.com or call 555-123-4567, it just works",email,private\n'
    )
    extract_response = {
        "items": [{"id": 0, "theme": "other", "usable": True, "quote": "it just works"}]
    }
    brief_response = {
        "theme_notes": [{"theme": "other", "placement": "footer"}],
        "permission_asks": [],
    }
    context, calls, _ = make_context(extract_response, brief_response)
    await module.run(context, {"product_name": "Acme", "feedback_csv": row})
    sent_text = calls[0]["data"][0]["text"]
    assert "dana@example.com" not in sent_text
    assert "555-123-4567" not in sent_text
    assert "[redacted email]" in sent_text
    assert "[redacted phone]" in sent_text


@pytest.mark.parametrize(
    "bad_csv",
    [
        "text,source\nhello,there\n",  # missing visibility column
        "text,source,visibility\n,somewhere,public\n",  # empty text
        "text,source,visibility\nhi,somewhere,maybe\n",  # bad visibility value
    ],
)
async def test_malformed_csv_is_rejected(bad_csv):
    module, _ = load()
    context, _, _ = make_context({"items": []})
    with pytest.raises(ValueError):
        await module.run(context, {"product_name": "Acme", "feedback_csv": bad_csv})


def test_manifest_is_a_valid_code_definition():
    _, definition = load()
    validate_code_definition(definition)
