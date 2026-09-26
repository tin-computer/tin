"""Offline contract checks for the pricing-friction-test community package."""

import json
from pathlib import Path


ROOT = Path(__file__).parents[1] / "workflow_packages" / "growth.pricing_friction_test"


def test_manifest_declares_bounded_posthog_procedure():
    manifest = json.loads((ROOT / "workflow.json").read_text())
    definition = manifest["definition"]

    assert manifest["package_format"] == "tin-workflow-package-v1"
    assert definition["key"] == "growth.pricing_friction_test"
    assert definition["executor"] == "codex.procedure"
    assert definition["schedule_modes"] == ["on_demand"]

    inputs = definition["input_schema"]["properties"]
    assert inputs["project_id"]["format"] == "uuid"
    assert inputs["pricing_page_url"]["maxLength"] == 2048
    assert inputs["focus"]["maxLength"] == 2000
    assert inputs["reporting_days"]["maximum"] == 90

    integration = definition["integration_requirements"][0]
    assert integration["provider_key"] == "analytics.posthog"
    assert set(integration["capabilities"]) == {"query.read", "definitions.read"}

    service = definition["procedure"]["services"]["analytics"]
    assert service["provider_key"] == "analytics.posthog"
    assert service["max_calls"] == 8
    assert definition["procedure"]["output"]["path"] == "reports/PRICING_FRICTION_TEST.md"


def test_declared_resources_exist_and_output_is_single_report():
    manifest = json.loads((ROOT / "workflow.json").read_text())
    definition = manifest["definition"]
    assert (ROOT / definition["procedure"]["prompt_path"]).is_file()

    for resource in definition["procedure"]["skill_files"]:
        assert (ROOT / resource).is_file()

    output = definition["procedure"]["output"]
    assert output["kind"] == "project.artifact"
    assert output["media_type"] == "text/markdown"
    assert output["max_bytes"] <= 64000
