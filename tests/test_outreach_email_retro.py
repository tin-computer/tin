"""Offline tests for the contributed outreach.email_retro package."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.workflow_code import validate_code_definition, validate_code_result

FIXTURES = Path(__file__).parent / "fixtures" / "outreach.email_retro"

STEP = "interpret_cohorts"

LEDGER = (
    "recipient,segment,subject,touch,status,replied_at\n"
    "ada@example.com,Designers,Quick question about {{name}}'s handoff,1,sent,\n"
    "ada@example.com,Designers,Quick question about {{name}}'s handoff,2,sent,2026-09-21\n"
    "grace@example.com,Designers,Quick question about {{name}}'s handoff,1,sent,2026-09-20\n"
    "alan@example.com,Marketers,Quick question about {{name}}'s handoff,1,bounced,\n"
)
SHORTLIST = (
    "email,segment,source\n"
    "ada@example.com,Designers,github\n"
    "grace@example.com,Designers,referral\n"
    "alan@example.com,Marketers,referral\n"
)

MODEL_ITEMS = [
    {
        "cohort": "Designers / Quick question about {{name}}'s handoff",
        "pattern": "bounce_rate_too_high",
        "evidence": "Bounce rate 33% across the campaign (1 of 3 recipients).",
        "recommended_action": "Verify addresses before the next send.",
    },
    {
        "cohort": "Marketers / Quick question about {{name}}'s handoff",
        "pattern": "reply_rate_too_low",
        "evidence": "Reply rate 0% for this cohort (0 of 1).",
        "recommended_action": "Rewrite the first line for this segment.",
    },
    {
        "cohort": "Designers / Quick question about {{name}}'s handoff",
        "pattern": "consistent_performance",
        "evidence": "Both designers replied within two touches.",
        "recommended_action": "Keep this subject for the next designer cohort.",
    },
]


def package(key="outreach.email_retro"):
    root = Path(f"workflow_packages/{key}")
    definition = json.loads((root / "workflow.json").read_text(encoding="utf-8"))["definition"]
    module = SimpleNamespace(**runpy.run_path(str(root / "main.py")))
    return module, definition


def generate_stub(spec, calls, output):
    async def generate(**payload):
        from tin_lite.code_models import request_contract

        request_contract(spec, payload)
        calls.append(payload)
        if payload["step"] == STEP:
            jsonschema.validate(output, payload["output_schema"])
            return {"parsed": output, "text": json.dumps(output)}
        raise AssertionError(payload["step"])

    return generate


def model_output(items=MODEL_ITEMS):
    return {"items": items}


async def test_retro_computes_rates_and_validates_model_output():
    module, definition = package()
    spec = validate_code_definition(definition)
    calls = []
    context = SimpleNamespace(
        models=SimpleNamespace(generate=generate_stub(spec, calls, model_output()))
    )
    result = await module.run(
        context, {"ledger_csv": LEDGER, "shortlist_csv": SHORTLIST}
    )
    assert [call["step"] for call in calls] == [STEP]
    data = calls[0]["data"]
    designers = next(row for row in data if row["segment"] == "Designers")
    assert designers["reply_rate"] == 1.0 and designers["mean_touches_to_reply"] == 1.5
    marketers = next(row for row in data if row["segment"] == "Marketers")
    assert marketers["reply_rate"] == 0.0 and marketers["bounce_rate"] == 1.0
    validate_code_result(json.dumps(result).encode(), spec)
    assert result["path"] == "reports/EMAIL_RETRO.md"
    assert "Designers" in result["content"] and "Reply rate overall: 67%" in result["content"]
    assert "3 of 3 shortlist emails appear in the ledger" in result["content"]


async def test_fallback_when_model_returns_zero_items():
    module, definition = package()
    spec = validate_code_definition(definition)
    calls = []
    context = SimpleNamespace(
        models=SimpleNamespace(generate=generate_stub(spec, calls, model_output([])))
    )
    result = await module.run(context, {"ledger_csv": LEDGER, "shortlist_csv": SHORTLIST})
    assert len(calls) == 1
    validate_code_result(json.dumps(result).encode(), spec)
    assert "deterministic rule-based fallback" in result["content"]
    assert "segment performs better" in result["content"]
    assert "bounce rate too high" in result["content"]


async def test_fallback_when_model_claims_are_unsupported():
    module, definition = package()
    spec = validate_code_definition(definition)
    calls = []

    ledger = (
        "recipient,segment,subject,touch,status,replied_at\n"
        "ada@example.com,Designers,Hello,1,sent,\n"
        "grace@example.com,Designers,Hello,1,sent,\n"
    )
    unsupported = [
        {
            "cohort": "Designers / Hello",
            "pattern": "subject_earned_replies",
            "evidence": "Reply rate 0% for this subject, so this subject clearly earned replies.",
            "recommended_action": "Keep the subject.",
        },
        {
            "cohort": "Marketers / B",
            "pattern": "segment_performs_better",
            "evidence": "Reply rate 0% versus 0%.",
            "recommended_action": "Target marketers more.",
        },
    ]
    context = SimpleNamespace(
        models=SimpleNamespace(generate=generate_stub(spec, calls, model_output(unsupported)))
    )
    result = await module.run(context, {"ledger_csv": ledger, "shortlist_csv": SHORTLIST})
    assert len(calls) == 1
    validate_code_result(json.dumps(result).encode(), spec)
    assert "deterministic rule-based fallback" in result["content"]
    assert "reply rate too low" in result["content"]


@pytest.mark.parametrize(
    "ledger,shortlist",
    [
        ("recipient,segment,subject\nada@example.com,Designers,S\n", SHORTLIST),
        ("recipient,segment,subject,touch,status,replied_at\n", SHORTLIST),
        ("touch,status\n1,sent\n", SHORTLIST),
        (
            "recipient,segment,subject,touch,status,replied_at\nnot-an-email,Designers,S,1,sent,\n",
            SHORTLIST,
        ),
        (
            "recipient,segment,subject,touch,status,replied_at\nada@example.com,Designers,S,1,sent,\n",
            "email,segment\nzed@other.com,Designers\n",
        ),
    ],
)
async def test_invalid_inputs_raise_value_errors(ledger, shortlist):
    module, _ = package()
    with pytest.raises(ValueError):
        await module.run(None, {"ledger_csv": ledger, "shortlist_csv": shortlist})


async def test_pinned_fixture_regenerates_the_saved_reviewer_report():
    """The reviewer-facing sample report stays reproducible from the committed fixtures."""
    module, definition = package()
    spec = validate_code_definition(definition)
    calls = []
    context = SimpleNamespace(
        models=SimpleNamespace(
            generate=generate_stub(
                spec,
                calls,
                model_output(
                    [
                        {
                            "cohort": "Designers / Quick question about your Figma handoff",
                            "pattern": "segment_performs_better",
                            "evidence": (
                                "Designers replied at 100% (2 of 2) versus 0% (0 of 1) "
                                "for Marketers."
                            ),
                            "recommended_action": (
                                "Weight the next shortlist toward designers; rewrite the "
                                "marketer opener."
                            ),
                        },
                        {
                            "cohort": "Marketers / Grow your pipeline without tools",
                            "pattern": "bounce_rate_too_high",
                            "evidence": "Bounce rate 100% for this cohort (1 of 1).",
                            "recommended_action": (
                                "Verify marketer addresses before the next send."
                            ),
                        },
                    ]
                ),
            )
        )
    )
    result = await module.run(
        context,
        {
            "ledger_csv": (FIXTURES / "sample_ledger.csv").read_text(encoding="utf-8"),
            "shortlist_csv": (FIXTURES / "sample_shortlist.csv").read_text(encoding="utf-8"),
        },
    )
    saved = (FIXTURES / "sample_report.md").read_text(encoding="utf-8")
    assert result["content"] == saved
    assert result["path"] == "reports/EMAIL_RETRO.md"
