import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/growth.earned_media_hook_finder"
EVAL = ROOT / "workflow_evals/growth.earned_media_hook_finder/qualification.json"


def test_manifest_declares_bounded_browser_procedure_and_practical_inputs():
    manifest = json.loads((PACKAGE / "workflow.json").read_bytes())
    definition = manifest["definition"]
    assert manifest["package_format"] == "tin-workflow-package-v1"
    assert definition["key"] == "growth.earned_media_hook_finder"
    assert definition["executor"] == "codex.procedure"
    assert definition["schedule_modes"] == ["on_demand"]
    assert definition["input_schema"]["required"] == ["project_id", "company_url"]
    properties = definition["input_schema"]["properties"]
    assert properties["company_url"]["format"] == "uri"
    assert properties["specific_story_url"]["format"] == "uri"
    assert properties["news_window_hours"]["default"] == 72
    assert properties["news_window_hours"]["minimum"] == 24
    assert properties["news_window_hours"]["maximum"] == 336
    assert definition["procedure"]["sandbox"] == {
        "profile": "browser", "egress": "open", "timeout_seconds": 900
    }
    assert definition["procedure"]["output"]["path_template"] == "reports/earned-media/{run_id}.md"


def test_prompt_and_skill_preserve_the_marketing_scope_and_refusal_contract():
    prompt = (PACKAGE / "PROMPT.md").read_text()
    skill = (PACKAGE / "skills/earned-media-hook-finder/SKILL.md").read_text()
    combined = prompt + "\n" + skill
    for phrase in [
        "No strong media opportunity found", "specific_story_url", "news_window_hours",
        "Do not send email", "Never invent", "Relevance: 25 points", "Credibility: 25 points",
        "Timeliness: 20 points", "Distinctiveness: 20 points", "Media usefulness: 10 points",
        "score of at least 70", "Removal test", "## Information gap", "## Suggested pitch",
    ]:
        assert phrase in combined
    assert "journalist@example.com" not in skill


def test_qualification_covers_success_specific_story_and_refusal_cases():
    contract = json.loads(EVAL.read_bytes())
    assert {case["id"] for case in contract["cases"]} == {
        "ordinary_opportunity", "specific_story", "no_strong_opportunity"
    }
    rubric_ids = {rubric["id"] for rubric in contract["rubric"]}
    assert {"grounding", "timeliness", "usefulness", "refusal"} <= rubric_ids
    assert "No strong media opportunity found" in contract["cases"][2]["expect"]["contains"]


@pytest.mark.asyncio
async def test_package_passes_community_validation():
    from tin_lite.community import ContributedPackage, validate

    package = ContributedPackage(key="growth.earned_media_hook_finder", path=PACKAGE)
    assert await validate(package, root=ROOT) is None