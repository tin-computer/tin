"""Offline tests for the contributed growth.churn_reason_digest package.

Mirrors the shape of test_public_workflows.py's example() helper and its
two-model-step test: load the package source with runpy (never import it),
validate the manifest with the same loader used at run time, and drive
`run()` with a fake ctx.models.generate that checks each request against the
declared output schema, the way a real model call would be checked before
it is trusted.
"""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = Path(__file__).parents[1] / "workflow_packages" / "growth.churn_reason_digest"

VALID_CSV = (
    "reason,mrr_cents\n"
    "Too expensive for what we get,4900\n"
    "Switched to a cheaper competitor,3900\n"
    "Needed SSO and it was not available,9900\n"
    "Support never replied to our ticket,1900\n"
    "We simply stopped using the product,1500\n"
)


def package():
    definition = json.loads((ROOT / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))
    return module, definition


def test_manifest_loads_under_the_real_code_contract():
    _, definition = package()
    spec = validate_code_definition(definition)
    assert spec.entrypoint == "main.py"
    assert {route.name for route in spec.model_routes} == {"classify", "recommend"}


def fake_generate(*, classify_by_id, action_by_top_id, seen):
    async def generate(**payload):
        seen.append(payload)
        jsonschema.Draft202012Validator.check_schema(payload["output_schema"])
        if payload["step"] == "classify_reasons":
            output = {
                "items": [
                    {"id": item["id"], "category": classify_by_id[item["id"]]}
                    for item in payload["data"]
                ]
            }
        else:
            assert payload["step"] == "recommend_actions"
            output = {
                "actions": [
                    {"id": item["id"], "action": action_by_top_id[item["id"]]}
                    for item in payload["data"]
                ]
            }
        jsonschema.validate(output, payload["output_schema"])
        return {"parsed": output, "text": json.dumps(output)}

    return generate


async def test_ranks_by_revenue_and_drafts_one_action_per_top_reason():
    module, definition = package()
    spec = validate_code_definition(definition)
    seen = []
    classify_by_id = {
        0: "price",
        1: "competitor",
        2: "missing_feature",
        3: "support",
        4: "unused",
    }
    # Highest MRR at risk: missing_feature (9900), then price (4900), then competitor (3900).
    action_by_top_id = {
        0: "Ship SSO, the single most cited blocker, and notify the lost accounts.",
        1: "Publish a pricing page that undercuts the named competitor's plan.",
        2: "Offer the churned accounts a lower-tier plan before they compare prices elsewhere.",
    }
    context = SimpleNamespace(
        models=SimpleNamespace(
            generate=fake_generate(
                classify_by_id=classify_by_id, action_by_top_id=action_by_top_id, seen=seen
            )
        )
    )
    result = await module.run(context, {"csv_text": VALID_CSV})
    validate_code_result(json.dumps(result).encode(), spec)

    assert [call["step"] for call in seen] == ["classify_reasons", "recommend_actions"]
    content = result["content"]
    assert "Cancellations analyzed: 5" in content
    assert "Total monthly revenue at risk: $221.00" in content
    # Missing feature (the $99 row) has the most revenue at risk and must rank first.
    assert content.index("Missing feature") < content.index("Price")
    assert content.index("Price") < content.index("Switched to a competitor")
    assert "Ship SSO" in content
    assert 'Example: "Needed SSO and it was not available"' in content


async def test_classification_must_preserve_every_id_exactly_once():
    module, _ = package()

    async def generate(**payload):
        if payload["step"] == "classify_reasons":
            # Drops id 4 and duplicates id 0 instead - must be rejected before any aggregation.
            return {
                "parsed": {"items": [{"id": 0, "category": "price"}] * len(payload["data"])},
                "text": "{}",
            }
        raise AssertionError("recommend_actions must not run after a rejected classification")

    context = SimpleNamespace(models=SimpleNamespace(generate=generate))
    with pytest.raises(ValueError, match="preserve every input ID"):
        await module.run(context, {"csv_text": VALID_CSV})


@pytest.mark.parametrize(
    "csv_text,match",
    [
        ("reason,mrr_cents\n,100\n", "reason must be"),
        ("reason,mrr_cents\nToo pricey,-5\n", "mrr_cents must be"),
        ("reason,mrr_cents\nToo pricey,12.50\n", "mrr_cents must be"),
        ("mrr_cents\n100\n", "Use exactly the columns"),
        ("reason,mrr_cents,extra\nToo pricey,100,x\n", "Use exactly the columns"),
        ("reason,mrr_cents\n", "Provide 1-30"),
    ],
)
def test_rejects_malformed_csv_before_any_model_call(csv_text, match):
    module, _ = package()
    with pytest.raises(ValueError, match=match):
        module._parse_rows(csv_text)
