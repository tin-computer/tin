import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import request_contract
from tin_lite.workflow_code import validate_code_definition, validate_code_result


ROOT = Path(__file__).parents[1] / "workflow_packages/growth.marketing_proof_gap"
MODULE = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))
DEFINITION = json.loads((ROOT / "workflow.json").read_text())["definition"]
SPEC = validate_code_definition(DEFINITION)


def inputs(**changes):
    value = {
        "project_id": "00000000-0000-4000-8000-000000000001",
        "claim_ids": ["claim-price", "claim-speed", "claim-trust"],
        "claim_texts": [
            "Teams save money by switching to us.",
            "Teams get setup in minutes.",
            "Customers can trust our security controls.",
        ],
        "claim_surfaces": ["pricing", "homepage", "sales_collateral"],
        "source_ids": ["review-1", "ticket-2", "conversation-3"],
        "source_texts": [
            "We saved money after switching and the pricing was clear.",
            "Setup took about 20 minutes for our team.",
            "The security documentation helped our procurement team trust the controls.",
        ],
        "source_types": ["review", "support_ticket", "conversation"],
    }
    value.update(changes)
    return value


def context(responses):
    calls = []

    async def generate(**payload):
        request_contract(SPEC, payload)
        calls.append(payload)
        result = responses.pop(0)
        if isinstance(result, dict) and ("parsed" in result or not result):
            return result
        schema = payload["output_schema"]
        jsonschema.validate(result, schema)
        return {"parsed": result, "text": json.dumps(result)}

    return SimpleNamespace(models=SimpleNamespace(generate=generate)), calls


def matches():
    return {
        "items": [
            {
                "claim_id": "claim-price",
                "source_id": "review-1",
                "evidence_excerpt": "saved money after switching",
                "direction": "supports",
            },
            {
                "claim_id": "claim-speed",
                "source_id": "ticket-2",
                "evidence_excerpt": "Setup took about 20 minutes",
                "direction": "supports",
            },
            {
                "claim_id": "claim-trust",
                "source_id": "conversation-3",
                "evidence_excerpt": "trust the controls",
                "direction": "supports",
            },
        ]
    }


def recommendations():
    return {
        "items": [
            {
                "claim_id": "claim-price",
                "target_surface": "pricing",
                "action": "Place the observed savings evidence beside the pricing claim.",
                "evidence_source_ids": ["review-1"],
            },
            {
                "claim_id": "claim-speed",
                "target_surface": "homepage",
                "action": "Add the setup evidence beside the setup claim.",
                "evidence_source_ids": ["ticket-2"],
            },
            {
                "claim_id": "claim-trust",
                "target_surface": "sales_collateral",
                "action": "Add the security documentation evidence to the sales proof section.",
                "evidence_source_ids": ["conversation-3"],
            },
        ]
    }


async def test_grounded_matches_produce_ranked_artifact():
    ctx, calls = context([matches(), recommendations()])
    result = await MODULE.run(ctx, inputs())
    validate_code_result(json.dumps(result).encode(), SPEC)

    artifact = json.loads(result["content"])
    assert artifact["status"] == "complete"
    assert artifact["coverage"]["claims_submitted"] == 3
    assert artifact["coverage"]["claims_with_evidence"] == 3
    assert artifact["claims"][0]["status"] == "weakly_supported"
    assert artifact["claims"][0]["rank"] == 1
    assert [call["step"] for call in calls] == [
        "match_claims_to_evidence",
        "recommend_proof_actions",
    ]


async def test_no_evidence_uses_deterministic_fallback_and_one_model_call():
    ctx, calls = context([{"items": []}])
    result = await MODULE.run(ctx, inputs())
    artifact = json.loads(result["content"])

    assert artifact["status"] == "no_evidence_matched"
    assert artifact["coverage"]["claims_with_evidence"] == 0
    assert all(
        claim["recommendation"]["evidence_source_ids"] == []
        for claim in artifact["claims"]
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        (
            {"claim_ids": ["same", "same"], "claim_texts": ["a", "b"],
             "claim_surfaces": ["homepage", "pricing"]},
            "unique",
        ),
        (
            {"claim_ids": ["bad id"], "claim_texts": ["a"], "claim_surfaces": ["homepage"]},
            "claim ID is invalid",
        ),
        (
            {"claim_ids": ["one"], "claim_texts": ["a"], "claim_surfaces": ["unknown"]},
            "claim surface is invalid",
        ),
        (
            {"source_ids": ["same", "same"], "source_texts": ["a", "b"],
             "source_types": ["review", "review"]},
            "unique",
        ),
    ],
)
async def test_invalid_input_is_rejected_before_model_call(changed, message):
    ctx, calls = context([])
    with pytest.raises(ValueError, match=message):
        await MODULE.run(ctx, inputs(**changed))
    assert calls == []


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            {"items": [{**matches()["items"][0], "source_id": "unknown"}]},
            "unknown source ID",
        ),
        (
            {"items": [{**matches()["items"][0], "evidence_excerpt": "not present"}]},
            "not present",
        ),
        (
            {"items": [matches()["items"][0], matches()["items"][0]]},
            "duplicates",
        ),
    ],
)
async def test_invalid_match_result_is_rejected(response, message):
    ctx, _ = context([response])
    with pytest.raises(ValueError, match=message):
        await MODULE.run(ctx, inputs())


async def test_recommendation_cannot_cite_unrelated_evidence():
    bad = recommendations()
    bad["items"][0]["evidence_source_ids"] = ["ticket-2"]
    ctx, _ = context([matches(), bad])

    with pytest.raises(ValueError, match="unrelated"):
        await MODULE.run(ctx, inputs())


@pytest.mark.parametrize(
    "response",
    [{}, {"parsed": {}}, {"parsed": {"items": "not-an-array"}}],
)
async def test_malformed_model_response_is_rejected(response):
    ctx, _ = context([response])
    with pytest.raises(ValueError, match="malformed"):
        await MODULE.run(ctx, inputs())


async def test_contradiction_status_is_code_owned():
    contradictory = matches()
    contradictory["items"][0]["direction"] = "contradicts"

    recs = recommendations()
    recs["items"][0]["action"] = "Soften the claim and attach the contradictory evidence for review."

    ctx, _ = context([contradictory, recs])
    result = await MODULE.run(ctx, inputs())
    artifact = json.loads(result["content"])

    claim = next(item for item in artifact["claims"] if item["claim_id"] == "claim-price")
    assert claim["status"] == "contradicted"
    assert claim["action_type"] == "remove_or_soften_claim"
