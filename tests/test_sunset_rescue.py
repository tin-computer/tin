"""Offline checks for competitor.sunset_rescue: package, cases and the report contract."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEY = "competitor.sunset_rescue"
PACKAGE = ROOT / "workflow_packages" / KEY
SKILL = PACKAGE / "skills" / "sunset-rescue"
BLOCK = re.compile(r"```json tin-sunset-rescue\n(.*?)\n```\s*\Z", re.S)

EVIDENCE = {
    "version": 1,
    "run_date": "2026-09-29",
    "status": "watch_only",
    "chosen": None,
    "kits_delivered": [],
    "watch_list": [
        {
            "tool": "Notion",
            "signal": "free plan rumor",
            "status": "unconfirmed",
            "reconsider_on": "2026-10-15",
            "source": "https://example.com/forum-post",
        }
    ],
    "searches": [{"query": '"Pocket" shutting down', "useful": True}],
}

WATCH_ONLY_REPORT = f"""# Sunset rescue: 2026-09-29

Status: watch_only
Chosen event: none this week

## The short version
Pocket's shutdown is closed; nothing cleared the vetoes.

## Events found
| Pocket | substitute | shutdown | 2025-05-22 | 2025-07-08 | 2025-10-08 | closed | vendor page |

## Scores
| Pocket | 3 | 3 | 0 | 1 | 0 | 0 | phase closed |
Demand unmeasured.

## Watch list
| Notion | free plan rumor | unconfirmed | 2026-10-15 | forum post |

## Evidence and assumptions
No vendor source for the Notion claim.

```json tin-sunset-rescue
{json.dumps(EVIDENCE, indent=2)}
```
"""


def evidence(report: str) -> dict:
    """The report's closing evidence block, checked the way the next run relies on it."""
    match = BLOCK.search(report)
    if match is None:
        raise ValueError("the report does not end with the evidence block")
    value = json.loads(match.group(1))
    if value.get("version") != 1 or value.get("status") not in {
        "rescue",
        "watch_only",
        "insufficient_context",
    }:
        raise ValueError("the evidence block has an unknown version or status")
    status_line = re.search(r"^Status: (\w+)$", report, re.M)
    if not status_line or status_line.group(1) != value["status"]:
        raise ValueError("the evidence block disagrees with the report's status")
    if (value["status"] == "rescue") != (value.get("chosen") is not None):
        raise ValueError("only a rescue names a chosen event")
    if value.get("chosen"):
        tool = value["chosen"]["tool"]
        if f"## Migration kit: {tool}" not in report:
            raise ValueError("a rescue must include its migration kit")
        unconfirmed = {w["tool"] for w in value["watch_list"] if w["status"] == "unconfirmed"}
        if tool in unconfirmed or value["chosen"].get("phase") == "closed":
            raise ValueError("a kit was built for an unconfirmed or closed event")
        if not str(value["chosen"].get("source", "")).startswith("https://"):
            raise ValueError("the chosen event has no primary source")
    for item in value["watch_list"]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.get("reconsider_on", "")):
            raise ValueError("every watch-list item needs a reconsider date")
    return value


def qualification():
    from tin_lite.workflow_qualification import Qualification

    return Qualification.model_validate_json(
        (ROOT / "workflow_evals" / KEY / "qualification.json").read_bytes()
    )


def case(case_id):
    return next(c for c in qualification().cases if c.id == case_id)


def test_manifest_asks_only_for_what_tin_cannot_know():
    definition = json.loads((PACKAGE / "workflow.json").read_text())["definition"]
    assert definition["key"] == KEY
    assert set(definition["input_schema"]["properties"]) == {
        "project_id",
        "known_event",
        "lookback_days",
    }
    assert definition["input_schema"]["required"] == ["project_id"]
    assert {p.get("path") or p.get("workflow") for p in definition["prerequisites"]} == {
        "reports/GROWTH_ONBOARDING_PLAN.md",
        "wiki/INDEX.md",
        "organic.keyword_plan",
    }
    assert definition["procedure"]["output"]["path_template"] == "reports/sunset-rescue/{run_id}.md"


def test_every_skill_file_is_declared_and_referenced():
    procedure = json.loads((PACKAGE / "workflow.json").read_text())["definition"]["procedure"]
    on_disk = sorted(
        str(p.relative_to(PACKAGE)).replace("\\", "/") for p in (PACKAGE / "skills").rglob("*.md")
    )
    assert sorted(procedure["skill_files"]) == on_disk
    method = (SKILL / "SKILL.md").read_text()
    for name in ("EVENTS.md", "SCORING.md", "REPORT.md"):
        assert name in method
    assert "at most 8 web searches" in (SKILL / "EVENTS.md").read_text()


def test_report_template_and_cases_agree_on_headings():
    template = (SKILL / "REPORT.md").read_text()
    for heading in case("ordinary").expect.contains:
        if heading.startswith(("#", "```")):
            assert heading in template


async def test_package_passes_the_shared_qualifier():
    from tin_lite.workflow_qualification import check_package

    files = {str(p.relative_to(ROOT)): p.read_bytes() for p in PACKAGE.rglob("*") if p.is_file()}
    files = {path.replace("\\", "/"): raw for path, raw in files.items()}
    checked = await check_package(files, f"workflow_packages/{KEY}/workflow.json", qualification())
    assert checked["cost"]["basis"] == "unmeasured"


@pytest.mark.parametrize("case_id", ["ordinary", "closed_event", "invented_event"])
def test_a_watch_only_report_passes_every_case(case_id):
    from tin_lite.workflow_qualification import assess_output

    result = assess_output(case(case_id), status="succeeded", content=WATCH_ONLY_REPORT.encode())
    assert result["status"] == "passed"
    assert evidence(WATCH_ONLY_REPORT)["status"] == "watch_only"


@pytest.mark.parametrize(
    ("case_id", "wrong"),
    [
        ("closed_event", "Chosen event: Pocket shutdown, deadline 2025-07-08 (closed)"),
        ("invented_event", "Chosen event: Notion free_tier_removal, deadline 2026-10-15"),
    ],
)
def test_choosing_a_vetoed_event_fails(case_id, wrong):
    from tin_lite.workflow_qualification import assess_output

    report = WATCH_ONLY_REPORT.replace("Chosen event: none this week", wrong)
    result = assess_output(case(case_id), status="succeeded", content=report.encode())
    assert result["status"] == "failed"


def test_plausible_but_unusable_reports_are_refused():
    # A kit built for the unconfirmed rumor: the report reads fine, the evidence does not.
    rumored = {
        **EVIDENCE,
        "status": "rescue",
        "chosen": {
            "tool": "Notion",
            "event_type": "free_tier_removal",
            "deadline": "2026-10-15",
            "phase": "final_stretch",
            "source": "https://example.com/forum-post",
        },
    }
    report = (
        WATCH_ONLY_REPORT.replace("Status: watch_only", "Status: rescue")
        .replace("## Watch list", "## Migration kit: Notion\n\n## Watch list")
        .replace(json.dumps(EVIDENCE, indent=2), json.dumps(rumored, indent=2))
    )
    with pytest.raises(ValueError, match="unconfirmed or closed"):
        evidence(report)
    # A report the next run cannot read: no evidence block at the end.
    with pytest.raises(ValueError, match="evidence block"):
        evidence(WATCH_ONLY_REPORT.split("```json")[0])
