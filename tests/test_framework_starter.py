"""Offline tests for growth.framework_starter: manifest bounds, context wiring, and cases."""

import json
from pathlib import Path

import pytest

from tin_lite.workflow_prerequisites import parse_workflow_prerequisites
from tin_lite.workflow_qualification import Qualification, assess_output

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/growth.framework_starter"
DEFINITION = json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))["definition"]
PROMPT = (PACKAGE / "PROMPT.md").read_text(encoding="utf-8")
SKILL = (PACKAGE / "skills/framework-starter/SKILL.md").read_text(encoding="utf-8")
FRAMEWORKS = (PACKAGE / "skills/framework-starter/FRAMEWORKS.md").read_text(encoding="utf-8")
REPORT = (PACKAGE / "skills/framework-starter/REPORT.md").read_text(encoding="utf-8")
CASES = Qualification.model_validate_json(
    (ROOT / "workflow_evals/growth.framework_starter/qualification.json").read_text(encoding="utf-8")
)


def case(case_id: str):
    return next(c for c in CASES.cases if c.id == case_id)


def test_only_project_id_is_required_and_every_text_input_is_bounded():
    schema = DEFINITION["input_schema"]
    assert schema["required"] == ["project_id"]
    assert schema["additionalProperties"] is False
    for name, spec in schema["properties"].items():
        if name != "project_id" and spec["type"] == "string":
            assert spec["maxLength"] <= 300
            assert spec["default"] == ""


def test_procedure_stays_isolated_fenced_and_writes_one_report():
    procedure = DEFINITION["procedure"]
    assert procedure["sandbox"] == {
        "profile": "isolated",
        "egress": "fenced",
        "timeout_seconds": 900,
    }
    assert procedure["output"]["path_template"] == "reports/growth/starters/{run_id}.md"
    assert DEFINITION["system"] == "platform"
    assert "starter" in DEFINITION["title"].lower()


def test_manifest_declares_every_resource_and_valid_prerequisites():
    on_disk = sorted(
        path.relative_to(PACKAGE).as_posix() for path in (PACKAGE / "skills").rglob("*.md")
    )
    assert sorted(DEFINITION["procedure"]["skill_files"]) == on_disk

    prerequisites = parse_workflow_prerequisites(
        DEFINITION["prerequisites"], input_schema=DEFINITION["input_schema"]
    )
    assert [(p.section, p.producer, p.level) for p in prerequisites] == [
        ("### Code map", "product.code_map", "required"),
        ("### Feature map", "product.deep_dive", "recommended"),
        (None, "growth.onboarding_plan", "recommended"),
        (None, "style.capture", "recommended"),
    ]


def test_skill_covers_frameworks_and_report_sections():
    for fw in ("nextjs", "fastapi", "vite-react", "express"):
        assert fw in FRAMEWORKS
    assert "tin-starter-state" in REPORT
    assert "tin-starter-state" in SKILL
    for forbidden in ("create repositories on GitHub", "publish packages", "make external network"):
        assert forbidden in PROMPT


def test_ordinary_qualification_case_passes():
    sample_report = """# Framework Starter: VectorDB + Next.js

Status: complete
Date: 2026-09-25
Framework: nextjs
Use case: Query vectors and return semantic search results in under 5 minutes.
Context: Code map: read, Feature map: read, growth plan: read, style guide: none found

A turnkey Next.js starter repository for developers building AI semantic search.

## 2. Repository Blueprint
Suggested repository name: `vector-search-nextjs-starter`

## 3. Environment & Configuration
### `.env.example`
```env
VECTOR_API_KEY=your_key_here
```

### Dependency Manifest (`package.json`)
```json
{
  "name": "vector-search-nextjs-starter"
}
```

```tin-starter-state
{
  "version": 1,
  "product": "VectorDB",
  "framework": "nextjs",
  "repo_name": "vector-search-nextjs-starter",
  "verified_at": "2026-09-25"
}
```
"""
    result = assess_output(case("ordinary"), status="succeeded", content=sample_report.encode("utf-8"))
    assert result["status"] == "passed", result["checks"]


def test_diagnostic_case_excludes_complete_state():
    diagnostic_report = """# Framework Starter

Status: diagnostic

No `### Code map` was found in `wiki/INDEX.md`. Run "Map the product from its code" (`product.code_map`) first.
"""
    result = assess_output(case("missing_code_map"), status="succeeded", content=diagnostic_report.encode("utf-8"))
    assert result["status"] == "passed", result["checks"]


def test_unsupported_framework_case_passes():
    unsupported_report = """# Framework Starter

Status: unsupported
Framework: ruby-on-rails-hotwire

The requested framework is currently not in FRAMEWORKS.md. Supported frameworks are nextjs, fastapi, vite-react, express.
"""
    result = assess_output(case("unsupported_framework"), status="succeeded", content=unsupported_report.encode("utf-8"))
    assert result["status"] == "passed", result["checks"]
