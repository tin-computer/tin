"""Offline checks for growth.competitor_review_miner — no model calls, no network requests."""

import json
import re
from pathlib import Path

PACKAGE_DIR = Path(__file__).parents[1] / "workflow_packages/growth.competitor_review_miner"
REPO_ROOT = Path(__file__).parents[1]
KEY = "growth.competitor_review_miner"
QUALIFICATION_FILE = REPO_ROOT / f"workflow_evals/{KEY}/qualification.json"

SUPPORTED_PLATFORMS = {"g2", "capterra", "trustpilot", "getapp", "software_advice", "producthunt"}

# ---------------------------------------------------------------------------
# Manifest checks
# ---------------------------------------------------------------------------


def test_manifest_key_matches_folder_name():
    manifest = json.loads((PACKAGE_DIR / "workflow.json").read_text())
    assert manifest["definition"]["key"] == KEY


def test_manifest_has_required_fields():
    manifest = json.loads((PACKAGE_DIR / "workflow.json").read_text())
    definition = manifest["definition"]
    assert manifest["package_format"] == "tin-workflow-package-v1"
    assert definition["executor"] == "codex.procedure"
    assert "on_demand" in definition["schedule_modes"]
    assert definition["title"].strip()
    assert definition["description"].strip()
    assert definition["version"]


def test_manifest_input_schema_has_required_properties():
    manifest = json.loads((PACKAGE_DIR / "workflow.json").read_text())
    schema = manifest["definition"]["input_schema"]
    props = schema["properties"]
    required = schema["required"]
    assert "project_id" in required
    assert "competitor_name" in required
    assert "review_platform" in required
    # reviews_text was removed in v2 in favour of web search
    assert "reviews_text" not in props
    assert "maxLength" in props["competitor_name"]
    assert set(props["review_platform"]["enum"]) == SUPPORTED_PLATFORMS
    assert "focus" in props


def test_manifest_procedure_paths_are_valid():
    manifest = json.loads((PACKAGE_DIR / "workflow.json").read_text())
    proc = manifest["definition"]["procedure"]
    assert (PACKAGE_DIR / proc["prompt_path"]).is_file()
    assert (PACKAGE_DIR / proc["skills_path"]).is_dir()
    for f in proc["skill_files"]:
        assert (PACKAGE_DIR / f).is_file(), f"Missing skill file: {f}"
    entry_skill_dir = PACKAGE_DIR / proc["skills_path"] / proc["entry_skill"]
    assert entry_skill_dir.is_dir()
    assert (entry_skill_dir / "SKILL.md").is_file()


def test_manifest_output_is_project_artifact():
    manifest = json.loads((PACKAGE_DIR / "workflow.json").read_text())
    output = manifest["definition"]["procedure"]["output"]
    assert output["kind"] == "project.artifact"
    assert output["path"].startswith("reports/")
    assert output["media_type"] == "text/markdown"
    assert output["max_bytes"] > 0


def test_manifest_sandbox_is_fenced_and_isolated():
    manifest = json.loads((PACKAGE_DIR / "workflow.json").read_text())
    sandbox = manifest["definition"]["procedure"]["sandbox"]
    assert sandbox["profile"] == "isolated"
    assert sandbox["egress"] == "fenced"
    assert sandbox["timeout_seconds"] <= 900


# ---------------------------------------------------------------------------
# SKILL.md checks
# ---------------------------------------------------------------------------


def test_skill_frontmatter_name_matches_directory():
    skill_path = PACKAGE_DIR / "skills/competitor-review-miner/SKILL.md"
    content = skill_path.read_text()
    match = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
    assert match, "SKILL.md missing frontmatter 'name:'"
    assert match.group(1).strip() == "competitor-review-miner"


def test_skill_frontmatter_description_is_non_empty():
    skill_path = PACKAGE_DIR / "skills/competitor-review-miner/SKILL.md"
    content = skill_path.read_text()
    match = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
    assert match, "SKILL.md missing frontmatter 'description:'"
    assert len(match.group(1).strip()) > 10


def test_skill_lists_all_supported_platforms():
    skill_text = (PACKAGE_DIR / "skills/competitor-review-miner/SKILL.md").read_text()
    for platform in SUPPORTED_PLATFORMS:
        assert platform in skill_text, f"SKILL.md missing platform: {platform}"


def test_skill_defines_required_analysis_buckets():
    skill_text = (PACKAGE_DIR / "skills/competitor-review-miner/SKILL.md").read_text()
    required_buckets = [
        "Pain Points",
        "Loved Features",
        "Switching Triggers",
        "Pricing Signals",
        "Missed Use Cases",
    ]
    for bucket in required_buckets:
        assert bucket in skill_text, f"SKILL.md missing bucket: {bucket}"


def test_skill_defines_report_sections():
    skill_text = (PACKAGE_DIR / "skills/competitor-review-miner/SKILL.md").read_text()
    required_sections = [
        "Signal summary",
        "Pain Points",
        "Loved Features",
        "Switching Triggers",
        "Pricing Signals",
        "Missed Use Cases",
        "Three growth moves",
        "Evidence notes",
    ]
    for section in required_sections:
        assert section in skill_text, f"SKILL.md missing section: {section}"


def test_skill_enforces_anti_hallucination_rule():
    skill_text = (PACKAGE_DIR / "skills/competitor-review-miner/SKILL.md").read_text()
    assert "verbatim" in skill_text.lower(), "SKILL.md should require verbatim quotes from reviews"
    assert "paraphrased" in skill_text.lower(), (
        "SKILL.md should require (paraphrased) label if exact quote is not used"
    )


def test_skill_restricts_safe_files_and_forbids_sensitive_files():
    skill_text = (PACKAGE_DIR / "skills/competitor-review-miner/SKILL.md").read_text()
    prompt_text = (PACKAGE_DIR / "PROMPT.md").read_text()
    for text in (skill_text, prompt_text):
        assert ".env" in text
        assert "credentials" in text
        assert "explicitly safe" in text


def test_skill_defines_diagnostic_report_format():
    skill_text = (PACKAGE_DIR / "skills/competitor-review-miner/SKILL.md").read_text()
    assert "Competitor Review Miner — Diagnostic Report" in skill_text
    assert "Status: invalid input" in skill_text
    assert "Status: not found" in skill_text
    assert "Status: insufficient data" in skill_text


def test_skill_caps_collection_at_30_reviews():
    skill_text = (PACKAGE_DIR / "skills/competitor-review-miner/SKILL.md").read_text()
    assert "30" in skill_text, "SKILL.md should mention the 30 review collection cap"


def test_skill_describes_site_scoped_search():
    skill_text = (PACKAGE_DIR / "skills/competitor-review-miner/SKILL.md").read_text()
    # The skill must use site:-scoped search to stay bounded to one platform
    assert "site:" in skill_text, "SKILL.md should use site:-scoped web search queries"


# ---------------------------------------------------------------------------
# PROMPT.md checks
# ---------------------------------------------------------------------------


def test_prompt_mentions_web_search():
    prompt = (PACKAGE_DIR / "PROMPT.md").read_text()
    assert "web search" in prompt.lower(), "PROMPT.md should mention web search"


def test_prompt_forbids_dangerous_actions():
    prompt = (PACKAGE_DIR / "PROMPT.md").read_text()
    assert "untrusted" in prompt.lower()
    assert "do not publish" in prompt.lower()
    assert "credentials" in prompt.lower()


def test_prompt_names_correct_output_path():
    manifest = json.loads((PACKAGE_DIR / "workflow.json").read_text())
    declared_path = manifest["definition"]["procedure"]["output"]["path"]
    prompt = (PACKAGE_DIR / "PROMPT.md").read_text()
    assert declared_path in prompt


# ---------------------------------------------------------------------------
# Community validator check
# ---------------------------------------------------------------------------


async def test_package_passes_community_validator():
    from tin_lite.community import ContributedPackage, validate

    package = ContributedPackage(key=KEY, path=PACKAGE_DIR)
    # Should not raise; CI runs the same check via `uv run tin-lite validate-community`
    await validate(package)


async def test_all_declared_skill_files_exist_on_disk():
    manifest = json.loads((PACKAGE_DIR / "workflow.json").read_text())
    for skill_file in manifest["definition"]["procedure"]["skill_files"]:
        target = PACKAGE_DIR / skill_file
        assert target.is_file(), f"Declared skill_file not found on disk: {skill_file}"


# ---------------------------------------------------------------------------
# Workflow qualification checks
# ---------------------------------------------------------------------------


async def test_workflow_qualification_contract():
    from tin_lite.workflow_packages import decode_workflow_source
    from tin_lite.workflow_qualification import Qualification, check_package

    assert QUALIFICATION_FILE.is_file(), f"Qualification file missing: {QUALIFICATION_FILE}"
    contract = Qualification.model_validate_json(QUALIFICATION_FILE.read_bytes())
    assert contract.version == 1
    case_ids = {case.id for case in contract.cases}
    assert "ordinary_reviews" in case_ids
    assert "alternate_platform" in case_ids
    assert "not_found_diagnostic" in case_ids
    assert "insufficient_data_diagnostic" in case_ids
    assert "invalid_input_diagnostic" in case_ids
    assert len(contract.cases) == 5
    assert len(contract.rubric) >= 2

    raw_manifest = (PACKAGE_DIR / "workflow.json").read_bytes()
    manifest_rel = f"workflow_packages/{KEY}/workflow.json"
    source = decode_workflow_source(raw_manifest, definition_path=manifest_rel)
    files = {manifest_rel: raw_manifest}
    for res in source.resource_paths.values():
        files[res] = (REPO_ROOT / res).read_bytes()

    report = await check_package(files, manifest_rel, contract)
    assert report["shape"]["status"] == "passed"
    assert report["cost"]["configured_ceiling_usd"] == "5"
    assert report["cost"]["basis"] == "unmeasured"
    assert report["safety"]["status"] == "review_required"


async def test_qualification_cli_checkout():
    from tin_lite.workflow_qualification_cli import check_checkout

    manifest_rel = f"workflow_packages/{KEY}/workflow.json"
    await check_checkout(REPO_ROOT, manifest_rel)


def test_qualification_fixtures_evaluation():
    from tin_lite.workflow_qualification import Qualification, assess_output

    contract = Qualification.model_validate_json(QUALIFICATION_FILE.read_bytes())
    cases_by_id = {c.id: c for c in contract.cases}

    ordinary_output = (
        "# Competitor review intelligence: Intercom\n\n"
        "**Platform:** g2\n"
        "## Signal summary\n| Bucket | Reviews | Score |\n\n"
        "## Pain Points\n- Pricing jumped without notice\n\n"
        "## Loved Features\n- Easy to set up live chat\n\n"
        "## Switching Triggers\n- Switched away because of cost\n\n"
        "## Pricing Signals\n- Very expensive at scale\n\n"
        "## Missed Use Cases\n- No native CRM integration\n\n"
        "## Three growth moves\n1. Target switched Intercom users.\n\n"
        "## Evidence notes\n- Platform searched: g2\n- Reviews collected: 15\n"
    )
    result_ord = assess_output(
        cases_by_id["ordinary_reviews"],
        status="succeeded",
        content=ordinary_output.encode(),
    )
    assert result_ord["status"] == "passed"

    alternate_output = (
        "# Competitor review intelligence: HubSpot\n\n"
        "**Platform:** capterra\n"
        "## Signal summary\n| Bucket | Reviews | Score |\n\n"
        "## Pain Points\n- Steep learning curve for admins\n\n"
        "## Loved Features\n- Excellent free tier\n\n"
        "## Switching Triggers\n- Moved from HubSpot due to cost\n\n"
        "## Pricing Signals\n- Enterprise pricing is prohibitive\n\n"
        "## Missed Use Cases\n- Limited reporting customisation\n\n"
        "## Three growth moves\nHubSpot migration plays.\n\n"
        "## Evidence notes\n- Platform searched: capterra\n- Reviews collected: 20\n"
    )
    result_alt = assess_output(
        cases_by_id["alternate_platform"],
        status="succeeded",
        content=alternate_output.encode(),
    )
    assert result_alt["status"] == "passed"

    diag_not_found = (
        "# Competitor Review Miner — Diagnostic Report\n\n"
        "Status: not found\n\n"
        'Reason: No listing found for "xXxNoSuchProduct999xXx" on trustpilot.'
    )
    result_not_found = assess_output(
        cases_by_id["not_found_diagnostic"],
        status="succeeded",
        content=diag_not_found.encode(),
    )
    assert result_not_found["status"] == "passed"

    diag_insufficient = (
        "# Competitor Review Miner — Diagnostic Report\n\n"
        "Status: insufficient data\n\n"
        'Reason: Fewer than 2 reviews found for "BrandNewStealthApp" on producthunt.'
    )
    result_insufficient = assess_output(
        cases_by_id["insufficient_data_diagnostic"],
        status="succeeded",
        content=diag_insufficient.encode(),
    )
    assert result_insufficient["status"] == "passed"

    diag_invalid_input = (
        "# Competitor Review Miner — Diagnostic Report\n\n"
        "Status: invalid input\n\n"
        "Reason: competitor_name must be a company or product name, not a URL."
    )
    result_invalid = assess_output(
        cases_by_id["invalid_input_diagnostic"],
        status="succeeded",
        content=diag_invalid_input.encode(),
    )
    assert result_invalid["status"] == "passed"


# ---------------------------------------------------------------------------
# Input validation semantics & unusable result handling
# ---------------------------------------------------------------------------


def test_supported_platforms_match_manifest_enum():
    manifest = json.loads((PACKAGE_DIR / "workflow.json").read_text())
    props = manifest["definition"]["input_schema"]["properties"]
    schema_enum = set(props["review_platform"]["enum"])
    assert schema_enum == SUPPORTED_PLATFORMS


def test_unusable_model_result_handling_and_rejections():
    """Verify that unusable results pass diagnostic cases and are rejected by ordinary cases."""
    from tin_lite.workflow_qualification import Qualification, assess_output

    contract = Qualification.model_validate_json(QUALIFICATION_FILE.read_bytes())
    cases_by_id = {c.id: c for c in contract.cases}

    # 1. Not-found diagnostic passes the not_found case
    not_found_output = (
        "# Competitor Review Miner — Diagnostic Report\n\n"
        "Status: not found\n\n"
        'Reason: No listing found for "xXxNoSuchProduct999xXx" on trustpilot.\n'
    )
    result = assess_output(
        cases_by_id["not_found_diagnostic"],
        status="succeeded",
        content=not_found_output.encode(),
    )
    assert result["status"] == "passed"

    # 2. Not-found diagnostic fails ordinary_reviews because ordinary excludes diagnostic status
    result_ord_rejected = assess_output(
        cases_by_id["ordinary_reviews"],
        status="succeeded",
        content=not_found_output.encode(),
    )
    assert result_ord_rejected["status"] == "failed"

    # 3. Insufficient data diagnostic passes insufficient_data case
    insufficient_output = (
        "# Competitor Review Miner — Diagnostic Report\n\n"
        "Status: insufficient data\n\n"
        'Reason: Fewer than 2 reviews found for "BrandNewStealthApp" on producthunt.\n'
    )
    result_insuff = assess_output(
        cases_by_id["insufficient_data_diagnostic"],
        status="succeeded",
        content=insufficient_output.encode(),
    )
    assert result_insuff["status"] == "passed"

    # 4. Insufficient data fails ordinary_reviews
    assert (
        assess_output(
            cases_by_id["ordinary_reviews"],
            status="succeeded",
            content=insufficient_output.encode(),
        )["status"]
        == "failed"
    )

    # 5. Invalid input diagnostic passes invalid_input case
    invalid_input_output = (
        "# Competitor Review Miner — Diagnostic Report\n\n"
        "Status: invalid input\n\n"
        "Reason: competitor_name cannot be a URL.\n"
    )
    result_invalid = assess_output(
        cases_by_id["invalid_input_diagnostic"],
        status="succeeded",
        content=invalid_input_output.encode(),
    )
    assert result_invalid["status"] == "passed"

    # 6. Diagnostic case rejects report that includes normal headings (e.g. ## Three growth moves)
    contaminated_diagnostic = (
        "# Competitor Review Miner — Diagnostic Report\n\n"
        "Status: not found\n\n"
        "## Three growth moves\n"
        "Contaminated section\n"
    )
    result_contaminated = assess_output(
        cases_by_id["not_found_diagnostic"],
        status="succeeded",
        content=contaminated_diagnostic.encode(),
    )
    assert result_contaminated["status"] == "failed"

    # 7. Null/missing content fails assessment
    assert (
        assess_output(
            cases_by_id["not_found_diagnostic"],
            status="succeeded",
            content=None,
        )["status"]
        == "failed"
    )
