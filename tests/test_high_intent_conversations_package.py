import json
from pathlib import Path


PACKAGE_DIR = (
    Path(__file__).resolve().parents[1]
    / "workflow_packages"
    / "growth.high_intent_conversations"
)


def load_manifest():
    return json.loads((PACKAGE_DIR / "workflow.json").read_text(encoding="utf-8"))


def test_high_intent_conversations_package_contract():
    manifest = load_manifest()
    definition = manifest["definition"]
    procedure = definition["procedure"]

    assert manifest["package_format"] == "tin-workflow-package-v1"
    assert definition["key"] == "growth.high_intent_conversations"
    assert definition["executor"] == "codex.procedure"
    assert definition["schedule_modes"] == ["on_demand"]

    properties = definition["input_schema"]["properties"]

    for required_input in (
        "project_id",
        "product_context",
        "target_customer",
        "problem",
    ):
        assert required_input in properties

    assert (PACKAGE_DIR / procedure["prompt_path"]).is_file()

    entry_skill = procedure["entry_skill"]
    assert entry_skill == "high-intent-conversations"

    expected_skill = f"skills/{entry_skill}/SKILL.md"
    assert expected_skill in procedure["skill_files"]
    assert (PACKAGE_DIR / expected_skill).is_file()

    output = procedure["output"]
    assert output["kind"] == "project.artifact"
    assert output["path"] == "reports/HIGH_INTENT_CONVERSATIONS.md"
    assert output["media_type"] == "text/markdown"

    prompt = (PACKAGE_DIR / procedure["prompt_path"]).read_text(encoding="utf-8")
    skill = (PACKAGE_DIR / expected_skill).read_text(encoding="utf-8")

    assert "public conversations" in prompt
    assert "Do not post, comment, contact, message" in prompt
    assert "Do not fabricate" in prompt

    assert "concrete evidence of the stated problem" in skill
    assert "Clearly distinguish observed evidence from interpretation." in skill
   