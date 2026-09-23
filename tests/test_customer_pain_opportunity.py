"""Offline contract checks for the supplied-evidence growth opportunity procedure."""

import json
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/growth.customer_pain_opportunity"
MANIFEST_PATH = PACKAGE / "workflow.json"


def package_files():
    manifest = json.loads(MANIFEST_PATH.read_text())
    procedure = manifest["definition"]["procedure"]
    return manifest, procedure


def test_package_contract_is_bounded_and_emits_one_markdown_artifact():
    manifest, procedure = package_files()
    definition = manifest["definition"]

    assert manifest["package_format"] == "tin-workflow-package-v1"
    assert definition["key"] == PACKAGE.name
    assert definition["executor"] == "codex.procedure"
    assert definition["schedule_modes"] == ["on_demand"]
    assert procedure["output"] == {
        "kind": "project.artifact",
        "path": "reports/CUSTOMER_PAIN_GROWTH_OPPORTUNITY.md",
        "media_type": "text/markdown",
        "max_bytes": 60000,
    }
    assert procedure["skill_files"] == ["skills/customer-pain-opportunity/SKILL.md"]

    schema = definition["input_schema"]
    assert schema["additionalProperties"] is False
    for name in ("project_id", "product_description", "target_customer", "evidence_bundle"):
        assert name in schema["required"]
    assert schema["properties"]["evidence_bundle"]["maxLength"] == 24000


def test_schema_accepts_bounded_evidence_bundle_and_preserves_supplied_ids():
    manifest, _ = package_files()
    schema = manifest["definition"]["input_schema"]
    valid = {
        "project_id": "0d289321-f7c5-4bea-9ad1-bf33fefcb919",
        "product_description": "Bookkeeping for independent designers.",
        "target_customer": "Independent graphic designers.",
        "evidence_bundle": json.dumps(
            [
                {
                    "evidence_id": "E-01",
                    "source_url": "https://community.example/thread/1",
                    "platform": "Community forum",
                    "date": "2026-04-03",
                    "excerpt": "I spend hours tracking down client receipts before tax time.",
                }
            ]
        ),
    }
    jsonschema.validate(valid, schema)

    overlong = json.loads(json.dumps(valid))
    overlong["evidence_bundle"] = "x" * 24001
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(overlong, schema)

    assert json.loads(valid["evidence_bundle"])[0]["evidence_id"] == "E-01"


def test_skill_preserves_grounding_and_explicit_four_factor_ranking():
    skill = (PACKAGE / "skills/customer-pain-opportunity/SKILL.md").read_text()
    prompt = (PACKAGE / "PROMPT.md").read_text()
    normalized_skill = " ".join(skill.lower().split())
    for required_text in (
        "Evidence strength",
        "Problem clarity",
        "Product fit",
        "Actionability",
        "Do not invent a single numeric score",
        "Distinct URLs are a practical proxy",
        "Do not claim prevalence",
        "preserve each ID exactly",
        "source URL",
    ):
        assert " ".join(required_text.lower().split()) in normalized_skill
    assert "Do not browse the web" in prompt
    assert "reports/CUSTOMER_PAIN_GROWTH_OPPORTUNITY.md" in prompt


def test_qualification_case_uses_synthetic_evidence_and_traceable_expectations():
    evaluation_path = ROOT / "workflow_evals/growth.customer_pain_opportunity/qualification.json"
    evaluation = json.loads(evaluation_path.read_text())
    assert evaluation["version"] == 1
    assert evaluation["cases"]
    assert all("inputs" in case and "expect" in case for case in evaluation["cases"])
    ordinary = next(case for case in evaluation["cases"] if case["id"] == "ordinary")
    assert "E-01" in ordinary["inputs"]["evidence_bundle"]
    assert "Evidence strength" in ordinary["expect"]["contains"]
    assert "Actionability" in ordinary["expect"]["contains"]

    weak_fit = next(case for case in evaluation["cases"] if case["id"] == "weak_product_fit")
    assert "Adjacent" not in weak_fit["expect"]["contains"]
    for required in (
        "E-10",
        "sample",
        "Product-fit uncertainty",
        "The supplied description does not establish that LedgerLeaf solves this pain.",
    ):
        assert required in weak_fit["expect"]["contains"]

    duplicate = next(case for case in evaluation["cases"] if case["id"] == "duplicate_evidence_id")
    duplicate_records = json.loads(duplicate["inputs"]["evidence_bundle"])
    assert [record["evidence_id"] for record in duplicate_records] == ["E-20", "E-20"]
    assert all(url in duplicate["expect"]["contains"] for url in (
        "https://forum.example.test/pain/receipts",
        "https://community.example.test/pain/records",
    ))
    assert "duplicate evidence ID" in duplicate["expect"]["contains"]
    assert "conclusions withheld pending corrected evidence IDs" in duplicate["expect"]["contains"]
