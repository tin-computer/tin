"""
Offline tests for directories.submission_pack.

Pattern mirrors test_public_workflows.py: use runpy to load main.py
without importing it as a module (the directory name contains a dot).
"""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent / "workflow_packages" / "directories.submission_pack"


def load_module():
    return SimpleNamespace(**runpy.run_path(str(PACKAGE_ROOT / "main.py")))


def base_inputs(**overrides):
    defaults = {
        "project_id": "00000000-0000-4000-8000-000000000001",
        "product_name": "Acme Analytics",
        "tagline": "Real-time analytics for lean SaaS teams. See what matters, skip the noise.",
        "website_url": "https://acmeanalytics.example.com",
        "short_description": (
            "Acme Analytics gives SaaS founders a single dashboard that surfaces "
            "activation drop-offs, feature adoption gaps, and revenue signals — "
            "without a data team."
        ),
        "pricing_summary": "Free up to 1,000 events/day. Paid from $19/mo.",
        "category_tags": ["Analytics", "SaaS", "B2B", "Dashboard", "Startups"],
        "founder_email": "founder@acmeanalytics.example.com",
        "target_directories": [],
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


def test_all_directories_produces_valid_output():
    mod = load_module()
    result = mod.run(None, base_inputs())
    assert result["path"] == "reports/DIRECTORY_SUBMISSION_PACK.md"
    content = result["content"]
    assert "# Directory submission pack" in content
    assert "Product Hunt" in content
    assert "G2" in content
    assert "Capterra" in content
    assert "Submission tracker" in content
    assert "Acme Analytics" in content
    assert "https://acmeanalytics.example.com" in content


def test_output_contains_one_section_per_directory():
    mod = load_module()
    fixture = json.loads((PACKAGE_ROOT / "fixtures" / "directories.json").read_text())
    result = mod.run(None, base_inputs())
    for d in fixture:
        assert f"## {d['name']}" in result["content"], f"Missing section for {d['name']}"


def test_target_filter_includes_only_selected():
    mod = load_module()
    result = mod.run(None, base_inputs(target_directories=["Product Hunt", "BetaList"]))
    content = result["content"]
    assert "## Product Hunt" in content
    assert "## BetaList" in content
    assert "## G2" not in content
    assert "## Capterra" not in content


def test_no_tags_runs_cleanly():
    mod = load_module()
    result = mod.run(None, base_inputs(category_tags=[]))
    assert result["path"] == "reports/DIRECTORY_SUBMISSION_PACK.md"
    assert "none provided" in result["content"]


def test_no_pricing_omits_pricing_field():
    mod = load_module()
    result = mod.run(None, base_inputs(pricing_summary=""))
    # Pricing field should not appear in sections (not just be empty)
    content = result["content"]
    assert "**Pricing:**" not in content


def test_tagline_trimmed_for_producthunt():
    """Product Hunt tagline limit is 60. Tagline over 60 chars should be trimmed."""
    mod = load_module()
    long_tagline = "A" * 80 + " suffix words here"  # 80+ chars, well above 60
    result = mod.run(None, base_inputs(tagline=long_tagline))
    content = result["content"]
    # Warning should appear in Product Hunt section
    ph_start = content.index("## Product Hunt")
    ph_end = content.index("---", ph_start)
    ph_section = content[ph_start:ph_end]
    assert "trimmed" in ph_section.lower() or "\u2026" in ph_section


def test_tracker_table_present_with_all_directories():
    mod = load_module()
    result = mod.run(None, base_inputs())
    content = result["content"]
    assert "## Submission tracker" in content
    assert "Not started" in content


def test_g2_warns_when_email_missing():
    mod = load_module()
    result = mod.run(None, base_inputs(
        target_directories=["G2"],
        founder_email="",
    ))
    content = result["content"]
    assert "email" in content.lower()


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


def test_rejects_unknown_directory():
    mod = load_module()
    with pytest.raises(ValueError, match="Unknown directories"):
        mod.run(None, base_inputs(target_directories=["NonExistentDirectory"]))


def test_rejects_http_url():
    mod = load_module()
    with pytest.raises(ValueError, match="https://"):
        mod.run(None, base_inputs(website_url="http://insecure.example.com"))


def test_rejects_empty_product_name():
    mod = load_module()
    with pytest.raises(ValueError):
        mod.run(None, base_inputs(product_name="   "))


def test_rejects_short_tagline():
    mod = load_module()
    with pytest.raises(ValueError):
        mod.run(None, base_inputs(tagline="Too short"))


def test_rejects_overlong_tagline():
    mod = load_module()
    with pytest.raises(ValueError, match="tagline"):
        mod.run(None, base_inputs(tagline="A" * 121))


def test_rejects_overlong_product_name():
    mod = load_module()
    with pytest.raises(ValueError, match="product_name"):
        mod.run(None, base_inputs(product_name="A" * 61))


# ---------------------------------------------------------------------------
# Fixture integrity
# ---------------------------------------------------------------------------


def test_fixture_is_valid_json_with_required_keys():
    raw = (PACKAGE_ROOT / "fixtures" / "directories.json").read_text()
    directories = json.loads(raw)
    assert isinstance(directories, list)
    assert len(directories) >= 5, "Fixture must have at least 5 directories"
    required_keys = {"name", "submission_url", "da", "requires_email", "field_spec"}
    spec_keys = {"name_max", "tagline_max", "description_max"}
    for d in directories:
        missing = required_keys - d.keys()
        assert not missing, f"{d.get('name', '?')} missing keys: {missing}"
        missing_spec = spec_keys - d["field_spec"].keys()
        assert not missing_spec, f"{d['name']} field_spec missing: {missing_spec}"
        assert d["field_spec"]["name_max"] >= 1
        assert d["field_spec"]["tagline_max"] >= 1
        assert d["field_spec"]["description_max"] >= d["field_spec"].get("description_min", 0)


def test_all_directory_names_unique():
    raw = (PACKAGE_ROOT / "fixtures" / "directories.json").read_text()
    names = [d["name"] for d in json.loads(raw)]
    assert len(names) == len(set(names)), "Duplicate directory names in fixture"
