"""Offline checks for growth.audience_prospector package and qualification."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tin_lite.community import CheckoutStorage, ContributedPackage, discover, validate
from tin_lite.procedures import load_pinned_codex_procedure
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs
from tin_lite.workflow_qualification import Qualification
from tin_lite.workflow_qualification_cli import check_checkout

KEY = "growth.audience_prospector"
ROOT = Path(__file__).parents[1]
PACKAGE_DIR = ROOT / "workflow_packages" / KEY
MANIFEST_PATH = PACKAGE_DIR / "workflow.json"
QUALIFICATION_PATH = ROOT / "workflow_evals" / KEY / "qualification.json"

REQUIRED_TABLE_COLUMNS = [
    "Entity",
    "Type",
    "Verified URL",
    "Audience Fit",
    "Activity/Scale Evidence",
    "Verified Engagement Mechanism",
    "Evidence",
]

DISALLOWED_BROAD_ENTITIES = {
    "reddit",
    "linkedin",
    "twitter",
    "x",
    "facebook",
    "discord",
    "instagram",
    "meetup.com",
    "youtube",
    "tiktok",
}


# --- Report Parser & Validator ---


def parse_prospects_report(markdown_text: str) -> list[dict[str, str]]:
    """Parse the AUDIENCE_PROSPECTS.md table and enforce qualification rules."""
    lines = [line.strip() for line in markdown_text.strip().splitlines()]

    # Find the table header
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("|") and all(col in line for col in REQUIRED_TABLE_COLUMNS):
            header_idx = i
            break

    if header_idx is None:
        raise ValueError("Missing or invalid qualified opportunities table header")

    header_cols = [c.strip() for c in lines[header_idx].strip("|").split("|")]
    if header_cols != REQUIRED_TABLE_COLUMNS:
        raise ValueError(f"Table columns do not match required columns: {header_cols}")

    # Next line must be separator
    if header_idx + 1 >= len(lines) or not lines[header_idx + 1].startswith("|"):
        raise ValueError("Missing table separator line")

    rows = []
    for line in lines[header_idx + 2 :]:
        if not line.startswith("|") or line.startswith("##"):
            break
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) != len(REQUIRED_TABLE_COLUMNS):
            continue
        row = dict(zip(REQUIRED_TABLE_COLUMNS, cols, strict=True))
        rows.append(row)

    if len(rows) > 15:
        raise ValueError(f"Exceeds maximum of 15 qualified opportunities: found {len(rows)}")

    for row in rows:
        entity = row["Entity"].strip()
        if entity.lower() in DISALLOWED_BROAD_ENTITIES:
            raise ValueError(
                f"Disallowed broad platform '{entity}'; must be a specific named entity"
            )

        url = row["Verified URL"].strip()
        if not (url.startswith("https://") or url.startswith("http://")):
            raise ValueError(f"Entity '{entity}' has invalid or unverified URL: '{url}'")

        if not row["Audience Fit"].strip():
            raise ValueError(f"Entity '{entity}' is missing Audience Fit evidence")

        if not row["Verified Engagement Mechanism"].strip():
            raise ValueError(f"Entity '{entity}' is missing Verified Engagement Mechanism")

        evidence = row["Evidence"].strip()
        if not evidence:
            raise ValueError(f"Entity '{entity}' is missing Evidence")
        if not ("https://" in evidence or "http://" in evidence):
            raise ValueError(
                f"Entity '{entity}' Evidence column must contain at least one source URL"
            )

    return rows


# --- Package Tests ---


async def test_package_discovers_and_validates():
    packages = discover(ROOT / "workflow_packages")
    assert any(p.key == KEY for p in packages)

    target_pkg = ContributedPackage(key=KEY, path=PACKAGE_DIR)
    await validate(target_pkg, root=ROOT)


async def test_procedure_loader_contract():
    storage = CheckoutStorage(ROOT)
    procedure = await load_pinned_codex_procedure(
        storage=storage,
        repo_id="workflow_packages",
        commit_sha="checkout",
        definition_path=f"workflow_packages/{KEY}/workflow.json",
    )

    assert procedure.workflow_key == KEY
    assert procedure.entry_skill == "audience-prospector"
    assert "audience-prospector/SKILL.md" in procedure.skill_files
    assert procedure.output_path == "reports/AUDIENCE_PROSPECTS.md"
    assert procedure.output_media_type == "text/markdown"
    assert procedure.sandbox.profile == "isolated"
    assert procedure.sandbox.egress == "fenced"
    assert procedure.sandbox.timeout_seconds == 900
    assert len(procedure.prompt) > 0


def test_input_schema_validation():
    manifest = json.loads(MANIFEST_PATH.read_bytes())
    schema = manifest["definition"]["input_schema"]
    project_id = uuid4()

    # Valid inputs with all fields
    inputs = {
        "target_audience": "Undergraduate CS students",
        "geography": "United States",
        "constraints": "No paid sponsorships",
    }
    normalized = normalize_workflow_inputs(schema=schema, project_id=project_id, inputs=inputs)
    assert normalized["target_audience"] == "Undergraduate CS students"
    assert normalized["geography"] == "United States"
    assert normalized["constraints"] == "No paid sponsorships"

    # Valid inputs with defaults applied
    minimal_inputs = {"target_audience": "Undergraduate CS students"}
    normalized_min = normalize_workflow_inputs(
        schema=schema, project_id=project_id, inputs=minimal_inputs
    )
    assert normalized_min["target_audience"] == "Undergraduate CS students"
    assert normalized_min["geography"] == ""
    assert normalized_min["constraints"] == ""

    # Reject project_id supplied in user inputs
    with pytest.raises(WorkflowInputError, match="project_id is bound"):
        normalize_workflow_inputs(
            schema=schema, project_id=project_id, inputs={"project_id": str(project_id)}
        )

    # Reject missing target_audience
    with pytest.raises(WorkflowInputError):
        normalize_workflow_inputs(schema=schema, project_id=project_id, inputs={})

    # Reject empty target_audience (minLength: 1)
    with pytest.raises(WorkflowInputError):
        normalize_workflow_inputs(
            schema=schema, project_id=project_id, inputs={"target_audience": ""}
        )

    # Reject extra unknown properties
    with pytest.raises(WorkflowInputError):
        normalize_workflow_inputs(
            schema=schema,
            project_id=project_id,
            inputs={"target_audience": "CS Students", "unknown_field": "val"},
        )


async def test_qualification_evaluation_contract():
    assert QUALIFICATION_PATH.exists()
    qualification = Qualification.model_validate_json(QUALIFICATION_PATH.read_bytes())

    assert qualification.version == 1
    assert len(qualification.cases) >= 2
    assert len(qualification.rubric) >= 4

    report = await check_checkout(ROOT, f"workflow_packages/{KEY}/workflow.json")
    assert report["shape"]["status"] == "passed"
    assert report["shape"]["executor"] == "codex.procedure"
    assert report["workflow"] == KEY


# --- Report Quality & Anti-Hallucination Tests ---

TABLE_HEADER = (
    "| Entity | Type | Verified URL | Audience Fit | "
    "Activity/Scale Evidence | Verified Engagement Mechanism | Evidence |"
)
TABLE_SEP = "|---|---|---|---|---|---|---|"


def test_synthetic_valid_report_passes():
    row1 = (
        "| ACM Student Chapter at UIUC | Student Association | https://acm.illinois.edu "
        "| Undergraduate CS majors | Active weekly schedule (Fall 2026) "
        "| Open invitation for tech talks | https://acm.illinois.edu/corporate - talk intake |"
    )
    row2 = (
        "| HackMIT | Hackathon | https://hackmit.org "
        "| 1,000+ student developers | Annual hackathon September 2026 "
        "| Mentor submission form | https://hackmit.org/mentor - applications open July 2026 |"
    )
    sample_report = f"""# Audience Prospects: Undergraduate CS Students

**Geographic Scope:** United States
**Constraints Applied:** No paid sponsorships
**Qualified Opportunities Found:** 2

## Qualified Opportunities

{TABLE_HEADER}
{TABLE_SEP}
{row1}
{row2}

## Disqualified Entities & Observations
- Disqualified r/technology due to broad non-student audience.
"""
    rows = parse_prospects_report(sample_report)
    assert len(rows) == 2
    assert rows[0]["Entity"] == "ACM Student Chapter at UIUC"
    assert rows[1]["Entity"] == "HackMIT"


def test_report_rejects_broad_unbounded_platform():
    row = (
        "| Reddit | Community | https://reddit.com | Students discuss tech "
        "| Millions of users | Post content | https://reddit.com - public forum |"
    )
    bad_report = f"""# Audience Prospects: CS Students

## Qualified Opportunities

{TABLE_HEADER}
{TABLE_SEP}
{row}
"""
    with pytest.raises(ValueError, match="Disallowed broad platform 'Reddit'"):
        parse_prospects_report(bad_report)


def test_report_rejects_padding_beyond_fifteen():
    table_rows = "\n".join(
        f"| Club {i} | Association | https://club{i}.edu | CS students "
        f"| 50 members | Open guest talk | https://club{i}.edu/about |"
        for i in range(1, 17)
    )
    padded_report = f"""# Audience Prospects: CS Students

## Qualified Opportunities

{TABLE_HEADER}
{TABLE_SEP}
{table_rows}
"""
    with pytest.raises(ValueError, match="Exceeds maximum of 15 qualified opportunities"):
        parse_prospects_report(padded_report)


def test_report_rejects_invalid_url():
    row = (
        "| UIUC ACM | Club | not-a-valid-url | CS students "
        "| 200 members | Guest lecture | https://uiuc.edu/evidence |"
    )
    invalid_url_report = f"""# Audience Prospects: CS Students

## Qualified Opportunities

{TABLE_HEADER}
{TABLE_SEP}
{row}
"""
    with pytest.raises(ValueError, match="invalid or unverified URL"):
        parse_prospects_report(invalid_url_report)


def test_report_rejects_missing_source_url_in_evidence():
    row = (
        "| UIUC ACM | Student Association | https://acm.illinois.edu | CS students "
        "| 200 members | Guest lecture intake | Official site confirms guest lectures |"
    )
    no_url_evidence_report = f"""# Audience Prospects: CS Students

## Qualified Opportunities

{TABLE_HEADER}
{TABLE_SEP}
{row}
"""
    with pytest.raises(ValueError, match="Evidence column must contain at least one source URL"):
        parse_prospects_report(no_url_evidence_report)


def test_report_rejects_missing_engagement_mechanism():
    row = (
        "| UIUC ACM | Student Association | https://acm.illinois.edu | CS students "
        "| 200 members | | https://acm.illinois.edu/about |"
    )
    missing_mechanism_report = f"""# Audience Prospects: CS Students

## Qualified Opportunities

{TABLE_HEADER}
{TABLE_SEP}
{row}
"""
    with pytest.raises(ValueError, match="missing Verified Engagement Mechanism"):
        parse_prospects_report(missing_mechanism_report)
