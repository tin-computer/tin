import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEY = "growth.sunset_rescue"
PACKAGE = ROOT / "workflow_packages" / KEY
SKILL = PACKAGE / "skills" / "sunset-rescue"

WATCH_ONLY_REPORT = """# Sunset rescue: 2026-09-23

Status: watch_only
Chosen event: none this week

## The short version
Pocket's shutdown is closed; nothing cleared the vetoes.

## Events found
| Pocket | substitute | shutdown | closed |

## Scores
| Pocket | 3 | 3 | 0 | 1 | 0 | 0 | phase closed |

## Watch list
| Notion | free plan rumor | unconfirmed | 2026-10-01 | forum post |

## Evidence and assumptions
No vendor source for the Notion claim.
"""


def qualification():
    from tin_lite.workflow_qualification import Qualification

    return Qualification.model_validate_json(
        (ROOT / "workflow_evals" / KEY / "qualification.json").read_bytes()
    )


def case(case_id):
    return next(c for c in qualification().cases if c.id == case_id)


def test_every_skill_file_is_declared_and_referenced():
    procedure = json.loads((PACKAGE / "workflow.json").read_text())["definition"]["procedure"]
    on_disk = sorted(
        str(p.relative_to(PACKAGE)).replace("\\", "/") for p in (PACKAGE / "skills").rglob("*")
    )
    on_disk = [p for p in on_disk if p.endswith(".md")]
    assert sorted(procedure["skill_files"]) == on_disk
    method = (SKILL / "SKILL.md").read_text()
    for name in ("EVENTS.md", "SCORING.md", "REPORT.md"):
        assert name in method


def test_report_template_and_cases_agree_on_headings():
    template = (SKILL / "REPORT.md").read_text()
    for heading in case("ordinary").expect.contains:
        if heading.startswith("#"):
            assert heading in template


async def test_package_passes_the_shared_qualifier():
    from tin_lite.workflow_qualification import check_package

    files = {str(p.relative_to(ROOT)): p.read_bytes() for p in PACKAGE.rglob("*") if p.is_file()}
    files = {path.replace("\\", "/"): raw for path, raw in files.items()}
    checked = await check_package(files, f"workflow_packages/{KEY}/workflow.json", qualification())
    assert checked["cost"]["basis"] == "unmeasured"


@pytest.mark.parametrize("case_id", ["closed_event", "invented_event"])
def test_a_watch_only_report_passes_the_boundary_cases(case_id):
    from tin_lite.workflow_qualification import assess_output

    result = assess_output(case(case_id), status="succeeded", content=WATCH_ONLY_REPORT.encode())
    assert result["status"] == "passed"


@pytest.mark.parametrize(
    ("case_id", "wrong"),
    [
        ("closed_event", "Chosen event: Pocket shutdown, deadline 2025-07-08 (closed)"),
        ("invented_event", "Chosen event: Notion free_tier_removal, deadline 2026-10-01"),
    ],
)
def test_choosing_a_vetoed_event_fails(case_id, wrong):
    from tin_lite.workflow_qualification import assess_output

    report = WATCH_ONLY_REPORT.replace("Chosen event: none this week", wrong)
    result = assess_output(case(case_id), status="succeeded", content=report.encode())
    assert result["status"] == "failed"
