"""organic.site_architecture: its contract with the technical fix, checked offline."""

import json
import re
from datetime import date
from pathlib import Path

from tin_lite import planned_url_changes as planned
from tin_lite.workflow_qualification import Qualification

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/organic.site_architecture"
MEASUREMENT = PACKAGE / "skills/site-architecture/MEASUREMENT.md"


def definition():
    return json.loads((PACKAGE / "workflow.json").read_text())["definition"]


def test_it_reads_the_repository_and_writes_the_path_the_technical_fix_reads():
    procedure = definition()["procedure"]
    assert procedure["workspace"] == {
        "kind": "github.repository",
        "provider_key": "infra.github",
        "capabilities": ["contents.read"],
    }
    assert procedure["output"]["path"] == planned.ARCHITECTURE_PATH
    required = {r["provider_key"] for r in definition()["integration_requirements"]}
    assert required == {"infra.github", "analytics.gsc"}


def test_the_documented_redirects_block_is_what_the_hook_reads():
    example = re.search(
        r"`redirects\.json`.*?```json\n(.*?)\n```", MEASUREMENT.read_text(), re.S
    ).group(1)
    block = json.loads(example)
    block["generated"] = "2026-09-28"
    report = (
        "<!-- redirects.json:start -->\n```json\n"
        + json.dumps(block)
        + "\n```\n<!-- redirects.json:end -->"
    )
    changes = planned.read_changes({planned.ARCHITECTURE_PATH: report}, date(2026, 9, 29))
    assert changes == [
        {
            "source": "organic.site_architecture",
            "kind": "redirect",
            "from": "/features/reports",
            "to": "/product/reports",
            "reason": "url_change: /features moves to /product",
            "confirmed": True,
        }
    ]


def test_the_prompt_no_longer_hands_redirects_to_the_founder():
    text = (PACKAGE / "PROMPT.md").read_text() + (
        PACKAGE / "skills/site-architecture/SKILL.md"
    ).read_text()
    assert "by hand" not in text.replace("check by hand", "")
    assert "the next technical fix asks about each" in text
    assert "Approval status" not in text


def test_qualification_cases_cover_a_truncated_read_and_a_missing_date():
    raw = (ROOT / "workflow_evals/organic.site_architecture/qualification.json").read_text()
    cases = {case.id: case for case in Qualification.model_validate_json(raw).cases}
    assert {"truncated_gsc", "follow_up_without_date", "thin_data_stop"} <= set(cases)
    assert "Search Console before and after" in cases["follow_up_without_date"].expect.excludes
