"""Reviewed scheduling resource for growth.buyer_moments; no web or model calls in CI."""

import json
import re
from pathlib import Path

import pytest

from tin_lite.workflow_prerequisites import parse_workflow_prerequisites
from tin_lite.workflow_qualification import Qualification

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/growth.buyer_moments"
SKILL = PACKAGE / "skills/buyer-moments"
CASES = ROOT / "workflow_evals/growth.buyer_moments/qualification.json"
AS_OF = "2026-09-26"
REPORT = "reports/growth/moments/0f4e5c1a-2b3c-4d5e-8f90-a1b2c3d4e5f6.md"


@pytest.fixture(scope="module")
def sched():
    text = (SKILL / "SCHEDULE.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)\n```", text, re.S)
    assert len(blocks) == 1
    namespace = {}
    # Only the fixed repository resource executes here, never text from a run.
    exec(compile(blocks[0], str(SKILL / "SCHEDULE.md"), "exec"), namespace)  # noqa: S102
    return namespace


def moment(name="Diwali", **overrides):
    return {
        "name": name,
        "market": "India",
        "kind": "festival",
        "date": "2026-11-08",
        "date_source": "https://example.gov.in/holidays-2026",
        "date_verified": True,
        "buyer_job": "Send customers a festive offer from a phone.",
        "fit": 3,
        "channels": ["search_page", "email", "forward_message"],
        "uses_offer": False,
        **overrides,
    }


def test_manifest_declares_resources_context_and_a_run_owned_report():
    definition = json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))["definition"]
    assert definition["key"] == PACKAGE.name
    procedure = definition["procedure"]
    on_disk = sorted(path.relative_to(PACKAGE).as_posix() for path in SKILL.rglob("*.md"))
    assert sorted(procedure["skill_files"]) == on_disk
    assert procedure["output"]["path_template"] == "reports/growth/moments/{run_id}.md"
    assert procedure["sandbox"] == {
        "profile": "isolated",
        "egress": "fenced",
        "timeout_seconds": 900,
    }
    schema = definition["input_schema"]
    assert schema["required"] == ["project_id"]
    for name, spec in schema["properties"].items():
        if spec.get("type") == "string" and name != "project_id":
            assert spec["maxLength"] <= 2000, name
            assert spec["default"] == "", name
    prerequisites = parse_workflow_prerequisites(definition["prerequisites"], input_schema=schema)
    assert all(p.level == "recommended" for p in prerequisites)
    assert {p.producer for p in prerequisites} == {
        "growth.onboarding_plan",
        "product.deep_dive",
        "product.code_map",
        "style.capture",
    }


def test_skill_report_labels_and_state_block_agree():
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for label in (
        "## Run sheet",
        "Date source:",
        "Buyer job:",
        "## Checkout readiness",
        "## Did it move?",
        "## Excluded moments",
        "utm_campaign",
    ):
        assert label in skill, label
    assert "tin-moments-state" in skill
    assert "tin-moments-state" in (SKILL / "SCHEDULE.md").read_text(encoding="utf-8")


def test_qualification_cases_parse_and_use_declared_inputs():
    contract = Qualification.model_validate(json.loads(CASES.read_text(encoding="utf-8")))
    definition = json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))["definition"]
    declared = set(definition["input_schema"]["properties"])
    for case in contract.cases:
        assert set(case.inputs) <= declared, case.id


def test_channels_are_scheduled_backwards_from_the_date(sched):
    result = sched["plan"]([moment()], AS_OF)
    steps = {step["channel"]: step for step in result["shortlist"][0]["plan"]}
    # 2026-11-08 minus 42 days live lead, minus 5 days of work.
    assert steps["search_page"] == {
        "channel": "search_page",
        "start_by": "2026-09-22",
        "ship_by": "2026-09-27",
        "timing": "tight",
    }
    assert steps["email"]["ship_by"] == "2026-11-01"
    assert steps["email"]["timing"] == "on time"
    assert [row["channel"] for row in result["run_sheet"]] == [
        "search_page",
        "email",
        "forward_message",
    ]


def test_a_channel_too_late_to_ship_is_dropped_not_rushed(sched):
    soon = moment("Bhai Dooj", date="2026-10-20")
    result = sched["plan"]([soon], AS_OF)
    row = result["shortlist"][0]
    assert "search_page" not in {step["channel"] for step in row["plan"]}
    assert row["missed"] == [{"channel": "search_page", "reason": "needed to ship by 2026-09-08"}]


def test_a_moment_with_no_channel_in_time_waits_for_next_year(sched):
    result = sched["plan"]([moment("Dussehra", date="2026-10-10", channels=["search_page"])], AS_OF)
    assert result["shortlist"] == []
    assert result["excluded"][0]["reason"] == "too late for every channel this year"
    assert result["status"] == "no moments verified"
    assert result["verdict"] == "not a fit"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"date_verified": False}, "not verified"),
        ({"date": "2026-09-01"}, "already passed"),
        ({"date": "2027-03-01"}, "after the horizon"),
        ({"fit": 1}, "loose fit"),
    ],
)
def test_unverified_past_distant_or_loose_moments_are_excluded(sched, overrides, reason):
    result = sched["plan"]([moment(**overrides)], AS_OF)
    assert result["shortlist"] == []
    assert reason in result["excluded"][0]["reason"]


def test_hard_nos_block_channels_and_discount_plans(sched):
    result = sched["plan"](
        [moment(channels=["email", "social_post", "forward_message"])],
        AS_OF,
        hard_nos=("no_founder_posting",),
    )
    row = result["shortlist"][0]
    assert [step["channel"] for step in row["plan"]] == ["email"]
    assert {miss["reason"] for miss in row["missed"]} == {"blocked by a hard no"}
    offer = sched["plan"]([moment(uses_offer=True)], AS_OF, hard_nos=("no_discounting",))
    assert "discount" in offer["excluded"][0]["reason"]
    with pytest.raises(ValueError, match="hard no"):
        sched["plan"]([moment()], AS_OF, hard_nos=("no_fun",))


def test_ranking_is_fit_first_then_most_urgent_start(sched):
    moments = [
        moment("Late strong", date="2026-12-20", fit=3),
        moment("Weak", date="2026-10-15", fit=2, channels=["email"]),
        moment("Early strong", date="2026-11-20", fit=3),
    ]
    result = sched["plan"](moments, AS_OF, max_moments=2)
    assert [row["name"] for row in result["shortlist"]] == ["Early strong", "Late strong"]
    assert result["excluded"] == [
        {"key": "weak@india@2026", "name": "Weak", "reason": "over max_moments"}
    ]
    assert result["verdict"] == "fit"


def test_memory_skips_prepared_moments_and_lists_past_ones_for_review(sched):
    first = sched["plan"]([moment()], "2026-09-01", report_path=REPORT)
    assert first["state"]["moments"]["diwali@india@2026"]["utm_campaign"] == "diwali-2026"
    trusted = sched["read_state"](json.loads(json.dumps(first["state"])))
    merged = sched["merge_states"]([trusted, None])
    again = sched["plan"]([moment()], AS_OF, previous=merged)
    assert again["shortlist"] == []
    assert again["prepared_before"][0]["report"] == REPORT
    later = sched["plan"]([], "2026-11-20", previous=merged)
    assert [item["key"] for item in later["review"]] == ["diwali@india@2026"]
    next_year = sched["plan"]([moment(date="2027-10-29")], "2027-09-01", previous=merged)
    assert [row["key"] for row in next_year["shortlist"]] == ["diwali@india@2027"]


def test_untrusted_state_and_bad_records_are_rejected(sched):
    assert sched["read_state"]({"version": 2, "moments": {}}) is None
    assert sched["read_state"]({"version": 1, "moments": {"bad": {}}}) is None
    with pytest.raises(ValueError, match="channel"):
        sched["plan"]([moment(channels=["billboard"])], AS_OF)
    with pytest.raises(ValueError, match="date_source"):
        sched["plan"]([moment(date_source="a friend said")], AS_OF)
    with pytest.raises(ValueError, match="missing"):
        sched["plan"]([{"name": "Diwali"}], AS_OF)
