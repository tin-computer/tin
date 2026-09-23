import json
from pathlib import Path

PKG_DIR = Path(__file__).parents[1] / "workflow_packages/growth.onboarding_friction_audit"

def test_manifest_is_valid():
    manifest_path = PKG_DIR / "workflow.json"
    assert manifest_path.exists()
    
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
        
    assert manifest["package_format"] == "tin-workflow-package-v1"
    assert manifest["definition"]["key"] == "growth.onboarding_friction_audit"
    assert manifest["definition"]["executor"] == "codex.procedure"
    assert "target_persona" in manifest["definition"]["input_schema"]["properties"]
    assert "maxLength" in manifest["definition"]["input_schema"]["properties"]["target_persona"]

def test_procedure_files_exist():
    prompt_path = PKG_DIR / "PROMPT.md"
    skill_path = PKG_DIR / "skills/onboarding-audit/SKILL.md"
    
    assert prompt_path.exists()
    assert skill_path.exists()
    
    prompt_content = prompt_path.read_text(encoding="utf-8")
    assert "onboarding-audit skill" in prompt_content
    assert "ONBOARDING_FRICTION_AUDIT.md" in prompt_content
    
    skill_content = skill_path.read_text(encoding="utf-8")
    assert "name: onboarding-audit" in skill_content
    assert "Time-to-First-Value" in skill_content

