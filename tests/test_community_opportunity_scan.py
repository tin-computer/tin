from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages" / "community.opportunity_scan"


def load_manifest() -> dict:
    return json.loads((PACKAGE / "workflow.json").read_text())


def test_manifest_and_resources_match_package_contract():
    manifest = load_manifest()
    definition = manifest["definition"]

    assert manifest["package_format"] == "tin-workflow-package-v1"
    assert definition["key"] == "community.opportunity_scan"
    assert definition["executor"] == "codex.procedure"
    assert definition["schedule_modes"] == ["on_demand"]
    assert definition["input_schema"]["additionalProperties"] is False
    assert definition["input_schema"]["required"] == ["project_id"]

    procedure = definition["procedure"]
    assert procedure["prompt_path"] == "PROMPT.md"
    assert procedure["entry_skill"] == "community-opportunity-scan"
    assert procedure["skill_files"] == [
        "skills/community-opportunity-scan/SKILL.md"
    ]
    assert procedure["workspace"] == {"kind": "project.state"}
    assert procedure["sandbox"]["egress"] == "fenced"
    assert procedure["output"]["path"] == "reports/COMMUNITY_OPPORTUNITY_SCAN.md"

    for relative in [
        "PROMPT.md",
        "skills/community-opportunity-scan/SKILL.md",
    ]:
        assert (PACKAGE / relative).is_file(), relative


def test_prompt_and_skill_preserve_review_only_community_behavior():
    prompt = (PACKAGE / "PROMPT.md").read_text()
    skill = (PACKAGE / "skills/community-opportunity-scan/SKILL.md").read_text()
    combined = f"{prompt}\n{skill}"

    required_phrases = [
        "do not create accounts",
        "do not post replies",
        "verify community participation rules",
        "Never fabricate a thread",
        "Never auto-post",
        "do not recommend evading moderation",
        "do not invent an ICP",
    ]
    for phrase in required_phrases:
        assert phrase.lower() in combined.lower(), phrase
