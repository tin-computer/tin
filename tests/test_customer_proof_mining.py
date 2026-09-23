import json
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages" / "customer.proof_mining"


def load_module():
    import runpy

    return SimpleNamespace(**runpy.run_path(str(PACKAGE / "main.py")))


def inputs():
    return {
        "company_context": "A shared support workspace for small customer-success teams.",
        "audience": "Sales prospects and customer marketing readers",
        "evidence_json": json.dumps(
            [
                {
                    "source_id": "review-1",
                    "source_type": "review",
                    "source": "Customer review",
                    "date": "2026-09-01",
                    "text": "We cut weekly reporting from two hours to twenty minutes and finally saw every open issue in one place.",
                },
                {
                    "source_id": "interview-2",
                    "source_type": "interview",
                    "source": "Customer interview",
                    "date": "2026-09-02",
                    "text": "I worried setup would take too long, but the first team workspace was ready before lunch.",
                },
            ]
        ),
    }


def candidate(
    source_id="review-1",
    quote="We cut weekly reporting from two hours to twenty minutes",
    candidate_id="candidate-1",
    statement="A customer reported reducing weekly reporting from two hours to twenty minutes.",
    scope="single_customer",
):
    return {
        "id": candidate_id,
        "category": "measurable_outcome",
        "statement": statement,
        "scope": scope,
        "evidence_ids": [source_id],
        "evidence_quotes": [quote],
        "quote_candidate": quote,
        "use_cases": ["landing_page", "sales_deck"],
        "confidence": "high",
    }


@pytest.mark.parametrize(
    "bad_change,match",
    [
        (lambda item: item.update({"evidence_ids": ["missing"]}), "unknown evidence"),
        (lambda item: item.update({"evidence_quotes": ["An invented quote"]}), "exact substring"),
        (lambda item: item.update({"quote_candidate": "An embellished quote"}), "exact substring"),
        (lambda item: item.update({"statement": "Every customer saves time."}), "generalization"),
        (lambda item: item.update({"statement": "A customer saved 83% of the time."}), "number"),
    ],
)
def test_candidate_validation_preserves_source_links(bad_change, match):
    module = load_module()
    item = candidate()
    bad_change(item)
    with pytest.raises(ValueError, match=match):
        module._validate_candidates([item], {"review-1": json.loads(inputs()["evidence_json"])[0]})


@pytest.mark.asyncio
async def test_run_keeps_approved_proof_and_rejected_claims_bound_to_evidence():
    module = load_module()
    calls = []

    original = candidate()
    second = candidate(
        source_id="interview-2",
        quote="I worried setup would take too long",
        candidate_id="candidate-2",
        statement="A customer worried setup would take too long.",
    )

    async def generate_with_two(**payload):
        calls.append(payload)
        if payload["step"] == "extract_customer_proof":
            return {"parsed": {"candidates": [original, second]}}
        return {
            "parsed": {
                "approved_ids": ["candidate-1"],
                "rejected": [{"id": "candidate-2", "reason": "The supplied review fixture marks this candidate as unsuitable for the requested use."}],
            }
        }

    context = SimpleNamespace(models=SimpleNamespace(generate=generate_with_two))
    result = await module.run(context, inputs())
    assert "A customer reported reducing weekly reporting" in result["content"]
    assert "A customer worried setup would take too long" in result["content"]
    assert "review-1" in result["content"]
    assert [call["step"] for call in calls] == ["extract_customer_proof", "review_customer_proof"]


@pytest.mark.asyncio
async def test_run_rejects_review_that_does_not_classify_every_candidate():
    module = load_module()

    async def generate(**payload):
        if payload["step"] == "extract_customer_proof":
            return {"parsed": {"candidates": [candidate()]}}
        return {"parsed": {"approved_ids": [], "rejected": []}}

    context = SimpleNamespace(models=SimpleNamespace(generate=generate))
    with pytest.raises(ValueError, match="classify every candidate"):
        await module.run(context, inputs())
