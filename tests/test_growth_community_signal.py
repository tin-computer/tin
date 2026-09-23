import json
from pathlib import Path

from tin_lite.procedures import validate_codex_procedure_definition


ROOT = Path(__file__).resolve().parents[1] / "workflow_packages" / "growth.community_signal"


def test_manifest_matches_public_procedure_contract():
    definition = json.loads((ROOT / "workflow.json").read_text(encoding="utf-8"))[
        "definition"
    ]

    procedure = definition["procedure"]

    # Workflow packages use package-relative paths. The lower-level
    # procedure validator expects paths in the normalized procedures/ tree.
    normalized = {
        **definition,
        "procedure": {
            **procedure,
            "prompt_path": "procedures/growth.community_signal/PROMPT.md",
            "skills_path": "procedures/growth.community_signal/skills",
            "skill_files": [
                "procedures/growth.community_signal/skills/community-signal/SKILL.md"
            ],
        },
    }

    spec = validate_codex_procedure_definition(normalized)

    assert spec.prompt_path == "procedures/growth.community_signal/PROMPT.md"
    assert spec.skills_path == "procedures/growth.community_signal/skills"
    assert spec.skill_files == (
        "procedures/growth.community_signal/skills/community-signal/SKILL.md",
    )