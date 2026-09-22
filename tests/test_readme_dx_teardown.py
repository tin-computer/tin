"""Offline validation test for the growth.readme_dx_teardown workflow package."""

import json
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "workflow_packages" / "growth.readme_dx_teardown"


class TestReadmeDxTeardownWorkflow(unittest.TestCase):
    def test_readme_dx_teardown_manifest(self):
        manifest_path = PACKAGE_ROOT / "workflow.json"
        self.assertTrue(manifest_path.exists(), "workflow.json must exist in package root")
        
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("package_format"), "tin-workflow-package-v1")
        
        definition = manifest.get("definition", {})
        self.assertEqual(definition.get("key"), "growth.readme_dx_teardown")
        self.assertEqual(definition.get("executor"), "codex.procedure")
        self.assertEqual(definition.get("version"), "1.0.0")
        self.assertEqual(definition.get("schedule_modes"), ["on_demand"])
        
        schema = definition.get("input_schema", {})
        self.assertEqual(schema.get("type"), "object")
        self.assertIn("project_id", schema.get("required", []))
        self.assertIn("repository", schema.get("required", []))
        
        procedure = definition.get("procedure", {})
        self.assertEqual(procedure.get("prompt_path"), "PROMPT.md")
        self.assertTrue((PACKAGE_ROOT / procedure["prompt_path"]).exists())
        
        for skill_file in procedure.get("skill_files", []):
            self.assertTrue((PACKAGE_ROOT / skill_file).exists(), f"Missing declared skill file: {skill_file}")

    def test_readme_dx_teardown_prompt_and_skill_content(self):
        prompt_text = (PACKAGE_ROOT / "PROMPT.md").read_text(encoding="utf-8")
        self.assertIn("readme-dx-teardown", prompt_text)
        self.assertIn("README_DX_TEARDOWN.md", prompt_text)

        skill_path = PACKAGE_ROOT / "skills" / "readme-dx-teardown" / "SKILL.md"
        self.assertTrue(skill_path.exists())
        skill_text = skill_path.read_text(encoding="utf-8")
        self.assertIn("name: readme-dx-teardown", skill_text)
        self.assertIn("Time-to-First-Hello-World", skill_text)


if __name__ == "__main__":
    unittest.main()
