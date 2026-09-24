"""organic.error_pages package contract and case assertions; no provider/model calls in CI."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/organic.error_pages"
CASES = ROOT / "workflow_evals/organic.error_pages/qualification.json"

GOOD = """# Error pages

status: complete
repository: acme/cli at 1a2b3c4
docs_base_url: https://example.com/docs/errors

## Summary

Nine user-visible errors, six without an answer. Publish `DATABASE_URL is not set` first.

## Funnel

| station | in | out | dropped because |
|---|---|---|---|
| harvest | – | 14 | – |

## Error inventory

| # | anchor | surface | path:line | coverage | demand | reach | gap | self_fixable | score |
|---|---|---|---|---|---|---|---|---|---|
| 1 | DATABASE_URL is not set | cli | cli/init.py:41 | uncovered | third_party | 3 | 4 | 2 | 24 |

## Drafted pages

### Error: DATABASE_URL is not set

- URL: https://example.com/docs/errors/database-url-is-not-set

## Link-back changes

```diff
-    sys.exit("Error: DATABASE_URL is not set")
+    sys.exit("Error: DATABASE_URL is not set. See https://example.com/docs/errors/database-url-is-not-set")
```

## Not read

- web/: no user-facing errors outside the CLI.
"""


def contract():
    from tin_lite.workflow_qualification import Qualification

    return Qualification.model_validate_json(CASES.read_bytes())


def case(case_id):
    return next(c for c in contract().cases if c.id == case_id)


def test_manifest_is_a_read_only_repository_procedure():
    definition = json.loads((PACKAGE / "workflow.json").read_text())["definition"]
    procedure = definition["procedure"]
    assert procedure["workspace"]["capabilities"] == ["contents.read"]
    assert procedure["output"]["kind"] == "project.artifact"
    assert definition["integration_requirements"][0]["capabilities"] == ["contents.read"]


def test_every_skill_resource_is_referenced_by_the_entry_skill():
    skill = (PACKAGE / "skills/error-pages/SKILL.md").read_text()
    assert "SCORING.md" in skill
    assert "REPORT.md" in skill


def test_report_template_carries_every_heading_the_cases_assert():
    template = (PACKAGE / "skills/error-pages/REPORT.md").read_text()
    for c in contract().cases:
        for phrase in c.expect.contains:
            if phrase.startswith("#"):
                assert phrase in template


async def test_package_and_cases_pass_the_static_qualifier():
    from tin_lite.workflow_qualification import check_package

    files = {str(p.relative_to(ROOT)): p.read_bytes() for p in PACKAGE.rglob("*") if p.is_file()}
    checked = await check_package(
        files, "workflow_packages/organic.error_pages/workflow.json", contract()
    )
    assert checked["shape"]["status"] == "passed"
    assert checked["safety"]["status"] == "review_required"
    assert {item["id"] for item in checked["evaluation"]["rubric"]} >= {"verbatim", "arithmetic"}


def test_useful_report_passes_the_ordinary_case():
    from tin_lite.workflow_qualification import assess_output

    result = assess_output(case("ordinary"), status="succeeded", content=GOOD.encode())
    assert result["status"] == "passed"


@pytest.mark.parametrize(
    ("case_id", "content"),
    [
        # Plausible but unusable: every heading is present, but the run ignored the supplied
        # docs URL, so none of the drafted links or diffs can ship.
        (
            "ordinary",
            GOOD.replace("https://example.com/docs/errors", "https://<docs-site>/errors"),
        ),
        # Plausible but unusable: with no docs URL supplied, the run invented a domain.
        ("no_docs_url", GOOD),
        # A run that stopped before drafting anything.
        ("ordinary", GOOD.split("## Drafted pages")[0]),
    ],
)
def test_plausible_but_unusable_reports_fail(case_id, content):
    from tin_lite.workflow_qualification import assess_output

    result = assess_output(case(case_id), status="succeeded", content=content.encode())
    assert result["status"] == "failed"


def test_failed_run_without_artifact_fails():
    from tin_lite.workflow_qualification import assess_output

    assert assess_output(case("ordinary"), status="failed", content=None)["status"] == "failed"
