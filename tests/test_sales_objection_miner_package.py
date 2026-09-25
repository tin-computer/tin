"""Offline contract checks for the contributed sales-objection workflow."""

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
KEY = "growth.sales_objection_miner"
PACKAGE = ROOT / "workflow_packages" / KEY
MANIFEST = PACKAGE / "workflow.json"
QUALIFICATION = ROOT / "workflow_evals" / KEY / "qualification.json"


def test_manifest_matches_package_shape():
    manifest = json.loads(MANIFEST.read_text())
    definition = manifest["definition"]
    assert manifest["package_format"] == "tin-workflow-package-v1"
    assert definition["key"] == KEY
    assert definition["executor"] == "codex.procedure"
    assert definition["schedule_modes"] == ["on_demand"]
    assert definition["procedure"]["prompt_path"] == "PROMPT.md"
    assert definition["procedure"]["entry_skill"] == "sales-objection-miner"
    assert definition["procedure"]["skill_files"] == [
        "skills/sales-objection-miner/SKILL.md"
    ]
    assert definition["procedure"]["output"]["path"] == "reports/SALES_OBJECTION_BRIEF.md"


def test_inputs_are_bounded_and_project_id_is_required():
    schema = json.loads(MANIFEST.read_text())["definition"]["input_schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["project_id", "objections"]
    assert schema["properties"]["objections"]["maxItems"] == 40
    assert schema["properties"]["objections"]["items"]["maxLength"] == 1200
    assert schema["properties"]["audience"]["maxLength"] == 1000
    assert schema["properties"]["product_context"]["maxLength"] == 3000


def test_skill_and_prompt_define_marketing_output_and_no_external_effects():
    prompt = (PACKAGE / "PROMPT.md").read_text()
    skill = (PACKAGE / "skills" / "sales-objection-miner" / "SKILL.md").read_text()
    combined = prompt + "\n" + skill
    for heading in (
        "Executive summary",
        "Objection clusters",
        "Messaging gaps",
        "Recommended marketing changes",
        "Test plan",
        "Evidence and limitations",
    ):
        assert heading in combined
    assert "Do not contact prospects" in prompt
    assert "Do not contact prospects" not in skill  # The skill stays method-focused.
    assert "personal identifiers" in combined


def test_qualification_contains_normal_mixed_and_insufficient_cases():
    qualification = json.loads(QUALIFICATION.read_text())
    assert qualification["version"] == 1
    cases = {case["id"]: case for case in qualification["cases"]}
    assert set(cases) == {"ordinary", "mixed", "insufficient"}
    assert len(cases["ordinary"]["inputs"]["objections"]) == 6
    assert len(cases["mixed"]["inputs"]["objections"]) == 4
    assert len(cases["insufficient"]["inputs"]["objections"]) == 3
    assert "insufficient" in cases["insufficient"]["expect"]["contains"][0]
