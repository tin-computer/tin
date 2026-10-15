"""competitor.watch: the reviewed diff resource and report contract, synthetic data only.

No web, model or Tin calls run here. The procedure's history lives in the evidence block of its
own reports, so these tests pin how that block is read, diffed and carried forward.
"""

import copy
import json
import re
from pathlib import Path

import pytest

from tin_lite.workflow_prerequisites import parse_workflow_prerequisites
from tin_lite.workflow_qualification import Qualification, assess_output

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/competitor.watch"
RESOURCES = PACKAGE / "skills/competitor-watch"
FIXTURES = ROOT / "tests/fixtures/competitor_watch"
CASES = ROOT / "workflow_evals/competitor.watch/qualification.json"
NOW = "2026-09-24T08:00:00Z"
LAST_WEEK = "2026-09-17T08:00:00Z"


@pytest.fixture(scope="module")
def watch():
    path = RESOURCES / "SCHEMA.md"
    blocks = re.findall(r"```python\n(.*?)\n```", path.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 1
    namespace = {}
    # Only the fixed repository resource executes here, never uploaded author code.
    exec(compile(blocks[0], str(path), "exec"), namespace)  # noqa: S102
    return namespace


def tier(name, price, **overrides):
    base = {
        "name": name,
        "price_monthly": price,
        "price_annual_monthly_equivalent": None,
        "billing_unit": "per user",
        "seat_minimum": None,
        "cta_text": "Start free",
        "highlighted_features": [],
        "usage_limits": None,
    }
    base.update(overrides)
    return base


def competitor(**overrides):
    base = {
        "id": "rival.example",
        "name": "Rival",
        "source": "input",
        "pages": [
            {"url": "https://rival.example/pricing", "kind": "pricing", "status": "read"},
            {"url": "https://rival.example/changelog", "kind": "changelog", "status": "read"},
        ],
        "pricing": {
            "has_public_pricing": True,
            "currency": "USD",
            "free_trial": {"available": True, "days": 14, "credit_card_required": False},
            "tiers": [
                tier("Starter", 10, usage_limits="Up to 3 projects"),
                tier("Pro", 25, highlighted_features=["SSO", "Audit log"]),
                tier("Enterprise", None, cta_text="Contact sales"),
            ],
        },
        "changelog": [
            {"date": "2026-09-15", "title": "Bulk export"},
            {"date": "2026-09-08", "title": "Dark mode"},
        ],
    }
    base.update(overrides)
    return base


def recorded(watch, item, when=LAST_WEEK, previous=None):
    return watch["next_evidence"](previous, [item], when)


def changes_between(watch, before, after):
    previous = recorded(watch, before)
    current = recorded(watch, after, NOW)["competitors"][0]
    return watch["diff"](previous["competitors"][0], current)


def kinds(changes, material=None):
    return sorted(c["kind"] for c in changes if material is None or c["material"] is material)


# History ---------------------------------------------------------------------------------


def test_evidence_round_trips_through_the_report_fence(watch):
    evidence = recorded(watch, competitor())
    text = f"# Competitor watch\n\nStatus: baseline\n\n{watch['fence'](evidence)}\n"
    assert watch["read_evidence"](text) == evidence
    assert evidence["competitors"][0]["pricing_checked_at"] == LAST_WEEK


@pytest.mark.parametrize(
    "text",
    [
        None,
        "# no block at all",
        '```tin-competitor-watch\n{"version": 1, "checked_at": \n```',
        "```tin-competitor-watch\n[]\n```",
        '```tin-competitor-watch\n{"version": 2, "checked_at": "2026-09-17T08:00:00Z", '
        '"competitors": []}\n```',
    ],
)
def test_untrusted_or_truncated_history_is_discarded(watch, text):
    assert watch["read_evidence"](text) is None


def test_two_blocks_in_one_report_are_not_trusted(watch):
    block = watch["fence"](recorded(watch, competitor()))
    assert watch["read_evidence"](f"{block}\n\n{block}") is None


def test_latest_orders_by_checked_at_and_skips_malformed_reports(watch):
    older = watch["fence"](recorded(watch, competitor(), "2026-09-10T08:00:00Z"))
    newer = watch["fence"](recorded(watch, competitor(), LAST_WEEK))
    reports = {
        # Run ids sort in no useful order; the newest valid block must still win.
        "reports/competitor-watch/f0000000-0000-4000-8000-000000000000.md": older,
        "reports/competitor-watch/00000000-0000-4000-8000-000000000001.md": newer,
        "reports/competitor-watch/80000000-0000-4000-8000-000000000002.md": (
            "```tin-competitor-watch\n{truncated\n```"
        ),
    }
    path, evidence, skipped = watch["latest"](reports)
    assert path.endswith("000000000001.md")
    assert evidence["checked_at"] == LAST_WEEK
    assert skipped == ["reports/competitor-watch/80000000-0000-4000-8000-000000000002.md"]
    assert watch["latest"]({"a.md": "no block"}) == (None, None, ["a.md"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "https://rival.example/pricing"},
        {"source": "guess"},
        {"pages": []},
        {"pages": [{"url": "ftp://rival.example", "kind": "pricing", "status": "read"}]},
        {"pages": [{"url": "https://rival.example", "kind": "pricing", "status": "maybe"}]},
        {"changelog": [{"date": "Sept 3", "title": "Dark mode"}]},
        {"changelog": [{"date": None, "title": f"entry {i}"} for i in range(11)]},
    ],
)
def test_competitor_contract_rejects_values_outside_it(watch, overrides):
    with pytest.raises(ValueError):
        recorded(watch, competitor(**overrides))


@pytest.mark.parametrize(
    "pricing",
    [
        {"has_public_pricing": True, "tiers": []},
        {"has_public_pricing": False, "tiers": []},
        {"has_public_pricing": True, "tiers": [tier("Pro", None)]},
        {"has_public_pricing": True, "tiers": [tier("Pro", 5), tier("pro", 6)]},
        {"has_public_pricing": True, "tiers": [tier("Pro", -1)]},
        {"has_public_pricing": True, "tiers": [tier("Pro", True)]},
        {"has_public_pricing": True, "tiers": [tier("Pro", 5, seat_minimum=0)]},
        {"has_public_pricing": True, "tiers": [tier("Pro", 5, highlighted_features=["x"] * 7)]},
        {"has_public_pricing": "yes", "tiers": [tier("Pro", 5)]},
    ],
)
def test_pricing_contract_rejects_empty_or_invented_extractions(watch, pricing):
    # An empty extraction is how a JavaScript shell looks when misread; it must be `empty`.
    with pytest.raises(ValueError):
        recorded(watch, competitor(pricing=pricing))


def test_competitor_bounds(watch):
    many = [competitor(id=f"rival{i}.example") for i in range(6)]
    with pytest.raises(ValueError):
        watch["next_evidence"](None, many, NOW)
    with pytest.raises(ValueError):
        watch["next_evidence"](None, [competitor(), competitor(id="www.rival.example")], NOW)
    with pytest.raises(ValueError):
        watch["next_evidence"](None, [competitor()], "2026-09-24 08:00")


# Diff --------------------------------------------------------------------------------------


def test_first_check_has_no_changes_and_is_a_baseline(watch):
    changes, not_compared = watch["diff"](None, recorded(watch, competitor())["competitors"][0])
    assert (changes, not_compared) == ([], [])
    assert watch["status"](None, changes, True) == "baseline"
    assert watch["status"](None, [], False) == "diagnostic"


def test_an_unchanged_page_is_a_quiet_week(watch):
    changes, not_compared = changes_between(watch, competitor(), competitor())
    assert changes == [] and not_compared == []
    previous = recorded(watch, competitor())
    assert watch["status"](previous, changes, True) == "no-change"


def test_real_pricing_moves_are_material(watch):
    after = competitor()
    pricing = copy.deepcopy(after["pricing"])
    pricing["tiers"][0]["price_monthly"] = 12
    pricing["tiers"][1]["seat_minimum"] = 3
    pricing["tiers"][2] = tier("Business", 40)
    pricing["free_trial"]["credit_card_required"] = True
    after["pricing"] = pricing
    changes, _ = changes_between(watch, competitor(), after)
    assert kinds(changes, material=True) == [
        "free_trial",
        "price",
        "seat_minimum",
        "tier_added",
        "tier_removed",
    ]
    price = next(c for c in changes if c["kind"] == "price")
    assert (price["subject"], price["before"], price["after"]) == ("Starter price_monthly", 10, 12)
    assert watch["status"](recorded(watch, competitor()), changes, True) == "changes"


def test_cosmetic_rewording_is_not_material(watch):
    after = competitor()
    pricing = copy.deepcopy(after["pricing"])
    pricing["tiers"][0]["usage_limits"] = "Create up to 3 projects"
    pricing["tiers"][0]["cta_text"] = "Try it free"
    pricing["tiers"][0]["name"] = "starter"  # case-only rename is the same tier
    pricing["tiers"][0]["price_monthly"] = 10.001
    after["pricing"] = pricing
    changes, _ = changes_between(watch, competitor(), after)
    assert kinds(changes) == ["copy", "copy"]
    assert kinds(changes, material=True) == []


def test_a_limit_with_new_numbers_is_material(watch):
    after = competitor()
    after["pricing"] = copy.deepcopy(after["pricing"])
    after["pricing"]["tiers"][0]["usage_limits"] = "Up to 1 project"
    changes, _ = changes_between(watch, competitor(), after)
    assert kinds(changes, material=True) == ["usage_limits"]


def test_switching_to_sales_led_is_material_but_cta_copy_is_not(watch):
    after = competitor()
    after["pricing"] = copy.deepcopy(after["pricing"])
    after["pricing"]["tiers"][1]["cta_text"] = "Book a demo"
    changes, _ = changes_between(watch, competitor(), after)
    assert kinds(changes, material=True) == ["sales_motion"]


def test_a_feature_moved_up_tier_is_material_while_new_highlights_are_not(watch):
    after = competitor()
    pricing = copy.deepcopy(after["pricing"])
    pricing["tiers"][1]["highlighted_features"] = ["Audit log", "AI triage"]
    pricing["tiers"][2]["highlighted_features"] = ["SSO"]
    after["pricing"] = pricing
    changes, _ = changes_between(watch, competitor(), after)
    moved = next(c for c in changes if c["kind"] == "feature_moved")
    assert (moved["subject"], moved["before"], moved["after"]) == ("SSO", "Pro", "Enterprise")
    assert moved["material"] is True
    added = next(c for c in changes if c["kind"] == "feature_added")
    assert added["subject"] == "AI triage" and added["material"] is False


def test_a_currency_switch_suppresses_price_comparison(watch):
    after = competitor()
    pricing = copy.deepcopy(after["pricing"])
    pricing["currency"] = "EUR"
    pricing["tiers"][0]["price_monthly"] = 9
    after["pricing"] = pricing
    changes, _ = changes_between(watch, competitor(), after)
    # A geo-rendered page is not a price cut.
    assert kinds(changes) == ["currency"]
    assert kinds(changes, material=True) == []


def test_new_changelog_entries_are_listed_but_not_escalated(watch):
    after = competitor(
        changelog=[
            {"date": "2026-09-22", "title": "AI triage"},
            *competitor()["changelog"],
            {"date": "2026-09-01", "title": "Older entry that scrolled into view"},
        ]
    )
    changes, _ = changes_between(watch, competitor(), after)
    assert [(c["kind"], c["subject"], c["material"]) for c in changes] == [
        ("changelog_new", "AI triage", False)
    ]
    assert watch["status"](recorded(watch, competitor()), changes, True) == "no-change"
    promoted = watch["promote"](changes[0], "Matches watch_for: AI triage; Feature map lacks it.")
    assert promoted["material"] is True
    assert watch["status"](recorded(watch, competitor()), [promoted], True) == "changes"


def test_promotion_needs_a_reason_and_a_promotable_change(watch):
    change = {"kind": "changelog_new", "material": False, "reason": None}
    with pytest.raises(ValueError):
        watch["promote"](change, "  ")
    with pytest.raises(ValueError):
        watch["promote"]({**change, "kind": "copy"}, "looks important")


def test_an_unreadable_page_is_not_compared_and_carries_forward(watch):
    previous = recorded(watch, competitor())
    shell = competitor(
        pages=[
            {"url": "https://rival.example/pricing", "kind": "pricing", "status": "empty"},
            {"url": "https://rival.example/changelog", "kind": "changelog", "status": "read"},
        ],
        pricing=None,
    )
    current = recorded(watch, shell, NOW)["competitors"][0]
    changes, not_compared = watch["diff"](previous["competitors"][0], current)
    assert changes == [] and not_compared == ["pricing"]
    assert watch["status"](previous, changes, True) == "no-change"

    carried = watch["next_evidence"](previous, [shell], NOW)["competitors"][0]
    assert carried["pricing"] == previous["competitors"][0]["pricing"]
    assert carried["pricing_checked_at"] == LAST_WEEK
    assert carried["changelog_checked_at"] == NOW
    # Next week the page renders again with a real change; the diff is against the carried data.
    after = competitor()
    after["pricing"] = copy.deepcopy(after["pricing"])
    after["pricing"]["tiers"][1]["price_monthly"] = 30
    following = recorded(watch, after, "2026-10-01T08:00:00Z")["competitors"][0]
    changes, _ = watch["diff"](carried, following)
    assert kinds(changes, material=True) == ["price"]


def test_a_never_read_section_stays_empty_without_a_date(watch):
    evidence = recorded(watch, competitor(changelog=None))
    assert evidence["competitors"][0]["changelog"] is None
    assert evidence["competitors"][0]["changelog_checked_at"] is None


# Package contract ----------------------------------------------------------------------------


def manifest():
    return json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))["definition"]


def test_manifest_declares_every_resource_and_its_boundaries():
    definition = manifest()
    procedure = definition["procedure"]
    assert definition["key"] == "competitor.watch"
    assert definition["schedule_modes"] == ["on_demand", "weekly"]
    on_disk = sorted(
        path.relative_to(PACKAGE).as_posix() for path in (PACKAGE / "skills").rglob("*.md")
    )
    assert sorted(procedure["skill_files"]) == on_disk
    assert procedure["sandbox"]["profile"] == "isolated"
    assert procedure["sandbox"]["egress"] == "fenced"
    assert procedure["output"]["path_template"] == "reports/competitor-watch/{run_id}.md"
    assert "path" not in procedure["output"]


def test_inputs_are_optional_and_bounded():
    schema = manifest()["input_schema"]
    assert schema["required"] == ["project_id"]
    assert schema["additionalProperties"] is False
    for name, spec in schema["properties"].items():
        if spec.get("type") == "string" and name != "project_id":
            assert "maxLength" in spec, name
        if spec.get("type") == "array":
            assert spec["maxItems"] <= 10 and spec["items"]["maxLength"] <= 500, name
            assert spec["default"] == [], name
    assert schema["properties"]["max_competitors"]["maximum"] == 5


def test_prerequisites_are_recommended_context_not_gates():
    definition = manifest()
    prerequisites = parse_workflow_prerequisites(
        definition["prerequisites"], input_schema=definition["input_schema"]
    )
    assert {p.level for p in prerequisites} == {"recommended"}
    assert [p.upstream for p in prerequisites] == [
        "growth.onboarding_plan",
        "product.deep_dive",
        "organic.keyword_plan",
    ]


def test_skill_layout_and_schema_agree():
    skill = (RESOURCES / "SKILL.md").read_text(encoding="utf-8")
    layout = (RESOURCES / "LAYOUT.md").read_text(encoding="utf-8")
    schema = (RESOURCES / "SCHEMA.md").read_text(encoding="utf-8")
    assert "```tin-competitor-watch" in layout and 'FENCE = "tin-competitor-watch"' in schema
    for heading in (
        "## What changed",
        "## Suggested responses",
        "## Also seen",
        "## Current pricing",
        "## Not read",
        "## Sources",
    ):
        assert heading in layout, heading
    for workflow in ("research.deep_dive", "content.plan"):
        assert f"`{workflow}`" in layout, workflow
    # Pages go through the content plan; content.generate drafts them.
    assert "content.public_article" not in layout
    for source in (
        "reports/competitor-watch/",
        "reports/GROWTH_ONBOARDING_PLAN.md",
        "### Feature map",
        "reports/keyword-plan/",
        "reports/paid-ads/",
    ):
        assert source in skill, source
    # Procedures can only write their declared output; no side snapshot survives a run.
    assert "data/competitor_watch" not in skill + layout + schema


# Qualification cases -------------------------------------------------------------------------


def case(case_id):
    contract = Qualification.model_validate(json.loads(CASES.read_text(encoding="utf-8")))
    return next(item for item in contract.cases if item.id == case_id)


def fixture(name):
    return (FIXTURES / name).read_bytes().decode("utf-8").replace("\r\n", "\n")


def test_cases_cover_the_unusable_inputs():
    contract = Qualification.model_validate(json.loads(CASES.read_text(encoding="utf-8")))
    ids = {item.id for item in contract.cases}
    assert {"js_rendered_pricing", "malformed_prior_evidence", "no_competitors_known"} <= ids
    properties = manifest()["input_schema"]["properties"]
    for item in contract.cases:
        assert set(item.inputs) <= set(properties) - {"project_id"}, item.id


def test_a_quiet_week_report_passes_and_diffs_cleanly(watch):
    text = fixture("quiet_week.md")
    result = assess_output(case("quiet_week"), status="succeeded", content=text.encode())
    assert result["status"] == "passed", result["checks"]
    previous = watch["read_evidence"](fixture("previous_week.md"))
    current = watch["read_evidence"](text)
    changes, _ = watch["diff"](previous["competitors"][0], current["competitors"][0])
    assert kinds(changes) == ["changelog_new"]
    assert watch["status"](previous, changes, True) == "no-change"


def test_a_plausible_but_unusable_report_fails_the_js_case(watch):
    # Reads a JavaScript shell as "all plans removed" and invents an urgent response.
    text = fixture("unusable_js_shell.md")
    result = assess_output(case("js_rendered_pricing"), status="succeeded", content=text.encode())
    assert result["status"] == "failed"
    failed = {check["check"] for check in result["checks"] if not check["passed"]}
    assert {"contains:0", "contains:1", "excludes:0", "excludes:2"} <= failed
    # The contract refuses the same mistake, so the next run never diffs against it.
    assert watch["read_evidence"](text) is None
