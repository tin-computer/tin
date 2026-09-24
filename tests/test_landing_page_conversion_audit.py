import json
from pathlib import Path

import pytest

from tin_lite.community import validate_all

KEY = "growth.landing_page_conversion_audit"
PACKAGE = Path(__file__).resolve().parents[1] / "workflow_packages" / KEY


def test_manifest_and_output_contract():
    assert PACKAGE.exists()
    manifest = json.loads((PACKAGE / "workflow.json").read_text())
    definition = manifest["definition"]
    assert definition["title"] == "Audit a landing page for conversion and messaging"
    assert definition["key"] == KEY
    assert definition["executor"] == "codex.procedure"
    assert definition["procedure"]["output"]["kind"] == "project.artifact"
    assert definition["procedure"]["output"]["path"] == "reports/landing_page_conversion_audit.md"
    assert definition["procedure"]["output"]["media_type"] == "text/markdown"
    assert definition["input_schema"]["required"] == [
        "project_id",
        "landing_page_url",
        "target_customer",
        "primary_offer",
    ]
    properties = definition["input_schema"]["properties"]
    assert "landing_page_url" in properties
    assert "target_customer" in properties
    assert properties["landing_page_url"]["title"] == "Landing page URL"
    assert properties["target_customer"]["title"] == "Target customer"

    prompt = (PACKAGE / "PROMPT.md").read_text()
    skill = (PACKAGE / "skills" / "landing-page-conversion-audit" / "SKILL.md").read_text()

    assert "Do not invent testimonials" in prompt
    assert "Do not invent" in prompt
    assert "observed page content" in prompt.lower()
    assert "publicly accessible" in skill.lower()
    assert "Do not invent testimonials" in skill
    assert "Do not mix them together" in skill
    assert "one Markdown artifact" in skill
    assert "reports/landing_page_conversion_audit.md" in prompt
    assert "generic marketing advice" in skill.lower()

    assert definition["procedure"]["output"]["path"] == "reports/landing_page_conversion_audit.md"
    assert definition["procedure"]["output"]["path"].endswith(".md")
    assert definition["procedure"]["output"]["path"].count("/") == 1


@pytest.mark.asyncio
async def test_package_validates_cleanly():
    results = await validate_all()
    assert all(error is None for _, error in results)
