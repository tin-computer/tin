import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import request_contract
from tin_lite.workflow_code import validate_code_definition, validate_code_result


ROOT = Path(__file__).parents[1] / "workflow_packages/growth.customer_objection_mining"
MODULE = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))
DEFINITION = json.loads((ROOT / "workflow.json").read_text())["definition"]
SPEC = validate_code_definition(DEFINITION)


def inputs(**changes):
    value = {
        "source_ids": ["review-1", "ticket-2", "conversation-3"],
        "source_texts": [
            "The price feels too high for a small team.",
            "We cannot justify the price until we see a comparison with our current tool.",
            "I need a clear comparison before I can get approval.",
        ],
        "source_types": ["review", "support_ticket", "conversation"],
    }
    value.update(changes)
    return value


def extraction_items():
    return [
        {
            "source_id": "review-1",
            "evidence_excerpt": "price feels too high",
            "objection": "The price is hard to justify for a small team.",
            "strength": "explicit",
        },
        {
            "source_id": "ticket-2",
            "evidence_excerpt": "see a comparison with our current tool",
            "objection": "Buyers need comparison evidence before they can justify cost.",
            "strength": "explicit",
        },
        {
            "source_id": "conversation-3",
            "evidence_excerpt": "clear comparison before I can get approval",
            "objection": "Buyers need comparison evidence before they can justify cost.",
            "strength": "implicit",
        },
    ]


def grouped(
    source_ids=("ticket-2", "conversation-3"), surface="comparison_page", evidence_ids=None
):
    return {
        "limitations": ["The sample is small and does not establish market-wide prevalence."],
        "objections": [
            {
                "id": "comparison_proof",
                "label": "Need comparison proof",
                "objection": "Buyers need comparison evidence before approving a purchase.",
                "source_ids": list(source_ids),
                "surfaces": [surface],
                "recommendations": [
                    {
                        "surface": surface,
                        "action": "Add a factual comparison section addressing approval concerns.",
                        "evidence_source_ids": ",".join(evidence_ids or source_ids),
                    }
                ],
            }
        ],
    }


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


async def test_normal_mixed_source_case_is_ranked_and_json_valid():
    ctx, calls = context([{"items": extraction_items()}, grouped()])
    result = await MODULE.run(ctx, inputs())
    validate_code_result(json.dumps(result).encode(), SPEC)
    artifact = json.loads(result["content"])
    objection = artifact["objections"][0]
    assert artifact["status"] == "complete"
    assert objection["frequency"] == 2
    assert objection["evidence_quality"] == "mixed"
    assert objection["coverage"] == "cross_source_type"
    assert objection["rank"] == 1
    assert [call["step"] for call in calls] == [
        "extract_purchase_objections",
        "group_and_map_objections",
    ]
    assert len(result["content"].encode()) <= 45000


async def test_insufficient_evidence_has_explicit_limitation():
    ctx, calls = context([{"items": []}])
    result = await MODULE.run(ctx, inputs())
    artifact = json.loads(result["content"])
    assert artifact["status"] == "insufficient_evidence"
    assert artifact["objections"] == []
    assert artifact["coverage"]["limitations"]
    assert len(calls) == 1


async def test_dense_grouped_objections_fit_the_declared_artifact_cap():
    source_ids = [
        f"conversation-{index:02d}-enterprise-purchasing-committee-evidence-record"
        for index in range(1, 13)
    ]
    source_texts = [
        (
            f"Buyer {index} needs clear implementation, pricing, security, and comparison "
            "evidence before the purchasing committee can approve a conversion."
        )
        for index in range(1, 13)
    ]
    source_types = ["conversation", "review", "support_ticket"] * 4

    extracted = [
        {
            "source_id": source_id,
            "evidence_excerpt": "evidence before the purchasing committee can approve a conversion",
            "objection": "The purchasing committee lacks the evidence needed to approve conversion.",
            "strength": "explicit",
        }
        for source_id in source_ids
    ]

    action_base = (
        "State the concrete buyer concern, show the relevant product evidence, name the "
        "verification path, distinguish supported claims from assumptions, and give the "
        "buying committee a concise next step for approval. Preserve the cited customer "
        "language in supporting notes, explain the operational consequence of delaying "
        "the decision, identify the specific proof a reviewer can inspect, and make clear "
        "which product behavior is documented rather than promised."
    )[:454]

    label = (
        "Committee evidence gap: documented proof for implementation, pricing, security, "
        "and comparison review across the purchasing process"
    )[:160]

    objection = (
        "Buyers need concrete implementation, pricing, security, and comparison evidence "
        "before their purchasing committee can approve this conversion. They need to "
        "understand the operational change, see which claims are supported by product "
        "documentation, compare the current workflow with the alternative, and identify "
        "a reviewable next step for technical, financial, and risk approval."
    )[:500]

    objections = []

    for index in range(10):
        objections.append(
            {
                "id": f"committee_evidence_{index}",
                "label": label,
                "objection": objection,
                "source_ids": source_ids[:6],
                "surfaces": ["homepage", "pricing", "comparison_page"],
                "recommendations": [
                    {
                        "surface": surface,
                        "action": f"{action_base} Focus this recommendation on {surface}.",
                        "evidence_source_ids": ",".join(source_ids[:6]),
                    }
                    for surface in ["homepage", "pricing", "comparison_page"]
                ],
            }
        )

    ctx, _ = context(
        [
            {"items": extracted},
            {"limitations": [], "objections": objections},
        ]
    )

    result = await MODULE.run(
        ctx,
        inputs(
            source_ids=source_ids,
            source_texts=source_texts,
            source_types=source_types,
        ),
    )

    size = len(result["content"].encode())

    assert 32000 < size < 45000
    validate_code_result(json.dumps(result).encode(), SPEC)


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        (
            {
                "source_ids": ["same", "same"],
                "source_texts": ["a", "b"],
                "source_types": ["review", "review"],
            },
            "unique",
        ),
        (
            {
                "source_ids": ["bad id"],
                "source_texts": ["a"],
                "source_types": ["review"],
            },
            "source ID is invalid",
        ),
        (
            {
                "source_ids": ["one"],
                "source_texts": ["a"],
                "source_types": ["unknown"],
            },
            "source type is invalid",
        ),
    ],
)
async def test_invalid_input_is_rejected_before_model_call(changed, message):
    ctx, calls = context([])
    with pytest.raises(ValueError, match=message):
        await MODULE.run(ctx, inputs(**changed))
    assert calls == []


@pytest.mark.parametrize(
    ("model_items", "message"),
    [
        ([{**extraction_items()[0], "source_id": "unknown"}], "unknown source ID"),
        ([{**extraction_items()[0], "evidence_excerpt": "not in the source"}], "not present"),
        ([{**extraction_items()[0], "evidence_excerpt": "   "}], "empty"),
        (
            [
                {**extraction_items()[0]},
                {**extraction_items()[0]},
                {**extraction_items()[0]},
            ],
            "more than two",
        ),
    ],
)
async def test_invalid_extraction_evidence_is_rejected(model_items, message):
    ctx, _ = context([{"items": model_items}])
    with pytest.raises(ValueError, match=message):
        await MODULE.run(ctx, inputs())


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"items": extraction_items()}, "unsupported surfaces"),
        ({"items": extraction_items()}, "unrelated source evidence"),
        ({"items": extraction_items()}, "grouped objection source IDs must be unique"),
    ],
)
async def test_invalid_grouping_is_rejected(payload, message):
    if message == "unsupported surfaces":
        group = grouped(surface="landing_page")
    elif message == "unrelated source evidence":
        group = grouped(evidence_ids=("review-1",))
    else:
        group = grouped(source_ids=("ticket-2", "ticket-2"))
    ctx, _ = context([payload, {"parsed": group}])
    with pytest.raises(ValueError, match=message):
        await MODULE.run(ctx, inputs())


@pytest.mark.parametrize("response", [{}, {"parsed": {}}, {"parsed": {"items": "not-an-array"}}])
async def test_malformed_or_empty_model_response_is_rejected(response):
    ctx, _ = context([response])
    with pytest.raises(ValueError, match="malformed"):
        await MODULE.run(ctx, inputs())
