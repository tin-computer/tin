"""Static contract checks for the community signal scan contribution."""

import json
from pathlib import Path
from uuid import UUID

import pytest

from tin_lite.community import validate_files
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs
from tin_lite.workflow_qualification import Qualification, assess_output
from tin_lite.workflow_qualification_cli import check_checkout


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages" / "growth.community_signal_scan"
MANIFEST_PATH = PACKAGE / "workflow.json"
EVALUATION_PATH = ROOT / "workflow_evals" / "growth.community_signal_scan" / "qualification.json"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "community_signal_scan"


def manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def opportunity_section(report):
    marker = "## Opportunities"
    assert marker in report
    section = report.split(marker, 1)[1]
    return section.split("\n## ", 1)[0]


def test_required_input_bounds_and_required_fields_are_declared():
    definition = manifest()["definition"]
    schema = definition["input_schema"]

    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "project_id",
        "target_audience",
        "problem_to_find",
        "product_context",
    ]
    assert schema["properties"]["project_id"]["format"] == "uuid"
    assert schema["properties"]["target_audience"]["maxLength"] == 1000
    assert schema["properties"]["problem_to_find"]["maxLength"] == 2000
    assert schema["properties"]["product_context"]["maxLength"] == 2000


def test_optional_array_item_and_collection_bounds_are_declared():
    properties = manifest()["definition"]["input_schema"]["properties"]
    for name, item_limit in (("communities", 200), ("exclusions", 200)):
        field = properties[name]
        assert field["type"] == "array"
        assert field["maxItems"] > 0
        assert field["items"]["type"] == "string"
        assert field["items"]["maxLength"] == item_limit

    assert properties["communities"]["maxItems"] == 10
    assert properties["exclusions"]["maxItems"] == 20


def test_other_optional_text_and_numeric_inputs_are_bounded():
    properties = manifest()["definition"]["input_schema"]["properties"]
    assert properties["geography"]["maxLength"] == 200
    assert properties["language"]["maxLength"] == 100
    assert properties["language"]["default"] == "English"
    assert properties["recency_days"]["minimum"] == 1
    assert properties["recency_days"]["maximum"] == 365
    assert properties["recency_days"]["default"] == 90
    assert properties["max_opportunities"]["minimum"] == 1
    assert properties["max_opportunities"]["maximum"] == 20
    assert properties["max_opportunities"]["default"] == 8


def test_manifest_uses_codex_procedure_and_run_id_markdown_output():
    definition = manifest()["definition"]
    assert definition["key"] == "growth.community_signal_scan"
    assert definition["executor"] == "codex.procedure"
    assert definition["schedule_modes"] == ["on_demand"]
    assert definition["procedure"]["output"] == {
        "kind": "project.artifact",
        "path_template": "reports/community-signal-scan/{run_id}.md",
        "media_type": "text/markdown",
        "max_bytes": 250000,
    }


def test_package_has_no_messaging_posting_or_contact_integrations():
    definition = manifest()["definition"]
    assert "integration_requirements" not in definition
    assert "services" not in definition["procedure"]
    assert set(definition["procedure"]) == {
        "prompt_path",
        "skills_path",
        "skill_files",
        "entry_skill",
        "workspace",
        "sandbox",
        "output",
    }


def test_procedure_documents_no_external_actions_and_no_op_result():
    prompt = (PACKAGE / "PROMPT.md").read_text(encoding="utf-8").lower()
    skill = (
        PACKAGE / "skills" / "community-signal-scan" / "SKILL.md"
    ).read_text(encoding="utf-8").lower()
    combined = prompt + "\n" + skill
    normalized_skill = " ".join(skill.split())

    assert "never publish" in prompt
    assert "never" in combined and "contact a person" in prompt
    assert "no-op" in prompt
    assert "no suitable, verifiable opportunity was found" in normalized_skill
    assert "review" in skill


@pytest.mark.asyncio
async def test_package_passes_static_loader_validation_without_live_web_access():
    files = {
        path.relative_to(ROOT).as_posix(): path.read_bytes()
        for path in PACKAGE.rglob("*")
        if path.is_file()
    }
    definition, _ = await validate_files(
        files,
        definition_path="workflow_packages/growth.community_signal_scan/workflow.json",
    )
    assert definition["executor"] == "codex.procedure"


@pytest.mark.asyncio
async def test_synthetic_qualification_cases_pass_repository_fixture_checker():
    qualification = Qualification.model_validate_json(EVALUATION_PATH.read_text(encoding="utf-8"))
    fixtures = (FIXTURE_DIR / "outputs.json").read_bytes()
    report = await check_checkout(
        ROOT,
        "workflow_packages/growth.community_signal_scan/workflow.json",
        fixtures=fixtures,
    )

    assert report["workflow"] == "growth.community_signal_scan"
    assert report["evaluation"]["status"] == "fixture_assertions_passed"
    assert [case.id for case in qualification.cases] == [
        "useful_conversation",
        "unverifiable_snippet",
        "irrelevant_conversation",
    ]
    assert all(case["status"] == "passed" for case in report["evaluation"]["cases"])


def test_useful_synthetic_conversation_is_an_accepted_reviewable_opportunity():
    fixture_set = json.loads((FIXTURE_DIR / "conversations.json").read_text(encoding="utf-8"))
    outputs = json.loads((FIXTURE_DIR / "outputs.json").read_text(encoding="utf-8"))
    useful = fixture_set["useful_conversation"]
    report = outputs["useful_conversation"]["content"]
    opportunities = opportunity_section(report)

    assert useful["source_url"] in opportunities
    assert useful["title"] in opportunities
    assert useful["first_person_problem_evidence"] in opportunities
    for expected in (
        "Audience fit: high",
        "Problem fit: high",
        "Product fit: plausible",
        "Review Status: review",
    ):
        assert expected in opportunities


def test_unverifiable_snippet_is_excluded_and_reported_as_a_limitation():
    fixture_set = json.loads((FIXTURE_DIR / "conversations.json").read_text(encoding="utf-8"))
    outputs = json.loads((FIXTURE_DIR / "outputs.json").read_text(encoding="utf-8"))
    snippet = fixture_set["unverifiable_snippet"]
    report = outputs["unverifiable_snippet"]["content"]
    opportunities = opportunity_section(report)

    assert snippet["source_url"] not in opportunities
    assert snippet["title"] not in opportunities
    assert "could not be opened or verified" in report
    assert "## Limitations" in report
    case = Qualification.model_validate_json(EVALUATION_PATH.read_text()).cases[1]
    assert assess_output(case, status="succeeded", content=report.encode())["status"] == "passed"


def test_irrelevant_keyword_overlap_is_rejected_from_opportunities():
    fixture_set = json.loads((FIXTURE_DIR / "conversations.json").read_text(encoding="utf-8"))
    outputs = json.loads((FIXTURE_DIR / "outputs.json").read_text(encoding="utf-8"))
    irrelevant = fixture_set["irrelevant_conversation"]
    report = outputs["irrelevant_conversation"]["content"]
    opportunities = opportunity_section(report)

    assert irrelevant["source_url"] not in opportunities
    assert irrelevant["title"] not in opportunities
    assert "excluded because it concerns gardening, not SaaS onboarding" in report
    assert "No suitable, verifiable conversation was found" in report


@pytest.mark.parametrize(
    "changes",
    [
        {"target_audience": ""},
        {"problem_to_find": ""},
        {"product_context": ""},
        {"recency_days": 0},
        {"recency_days": 366},
        {"max_opportunities": 0},
        {"max_opportunities": 21},
    ],
)
def test_invalid_inputs_are_rejected_by_tins_input_validator(changes):
    schema = manifest()["definition"]["input_schema"]
    inputs = {
        "target_audience": "early-stage SaaS founders",
        "problem_to_find": (
            "Founders struggle to understand why trial users abandon onboarding."
        ),
        "product_context": (
            "Fictional analytics product that helps teams identify onboarding drop-off."
        ),
    }
    inputs.update(changes)

    with pytest.raises(WorkflowInputError):
        normalize_workflow_inputs(schema=schema, project_id=UUID(int=1), inputs=inputs)
