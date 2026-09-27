"""Reviewed readiness resource for launch.show_hn; no web or model calls in CI."""

import json
import re
from pathlib import Path

import pytest

from tin_lite.workflow_prerequisites import parse_workflow_prerequisites

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/launch.show_hn"
SKILL = PACKAGE / "skills/show-hn-launch"
AS_OF = "2026-09-28"  # a Monday; the next preferred launch day is Tue 2026-09-29


@pytest.fixture(scope="module")
def rubric():
    text = (SKILL / "RUBRIC.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)\n```", text, re.S)
    assert len(blocks) == 1
    namespace = {}
    # Only the fixed repository resource executes here, never launch text from a run.
    exec(compile(blocks[0], str(SKILL / "RUBRIC.md"), "exec"), namespace)  # noqa: S102
    return namespace


def record(**overrides):
    return {
        "target_url": "https://example.dev/app",
        "access": "instant",
        "founder_present": True,
        "technical_story": 2,
        "novelty": 2,
        "honesty": 2,
        "title": "Show HN: A tiny profiler for async Python",
        "major_change_since_last": False,
        **overrides,
    }


# --- Manifest ---------------------------------------------------------------


def test_manifest_declares_resources_context_and_a_run_owned_report():
    definition = json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))["definition"]
    assert definition["key"] == PACKAGE.name
    assert definition["executor"] == "codex.procedure"
    procedure = definition["procedure"]
    on_disk = sorted(path.relative_to(PACKAGE).as_posix() for path in SKILL.rglob("*.md"))
    assert sorted(procedure["skill_files"]) == on_disk
    assert procedure["entry_skill"] == "show-hn-launch"
    assert procedure["output"]["path_template"] == "reports/launch/show-hn/{run_id}.md"
    assert procedure["sandbox"] == {
        "profile": "isolated",
        "egress": "fenced",
        "timeout_seconds": 900,
    }
    schema = definition["input_schema"]
    assert schema["required"] == ["project_id"]
    for name, spec in schema["properties"].items():
        if spec.get("type") == "string" and name != "project_id" and "enum" not in spec:
            assert spec["maxLength"] <= 2000, name
            assert spec["default"] == "", name
    prerequisites = parse_workflow_prerequisites(definition["prerequisites"], input_schema=schema)
    assert {(p.path, p.producer, p.level) for p in prerequisites} == {
        ("wiki/INDEX.md", "product.deep_dive", "recommended"),
        ("reports/GROWTH_ONBOARDING_PLAN.md", "growth.onboarding_plan", "recommended"),
        (".agents/skills/writing-style/SKILL.md", "style.capture", "recommended"),
    }


def test_skill_report_and_state_block_agree():
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for label in (
        "Showing:",
        "Context:",
        "Recommended post window:",
        "## Readiness",
        "## Blockers",
        "## Draft: first comment",
        "## Launch-day checklist",
    ):
        assert label in skill, label
    assert "tin-show-hn-state" in skill
    assert "tin-show-hn-state" in (SKILL / "RUBRIC.md").read_text(encoding="utf-8")
    # The skill must forbid the workflow from posting or voting on HN itself.
    assert "never" in skill.lower() and "upvote" in skill.lower()


# --- Verdict and scoring ----------------------------------------------------


def test_a_strong_instant_launch_is_ready_with_a_weekday_window(rubric):
    result = rubric["assess"](record(), AS_OF)
    assert result["verdict"] == "ready"
    assert result["status"] == "ready"
    assert result["score"] == 8 and result["max_score"] == 8
    assert result["window"] == {"date": "2026-09-29", "window": "13:00-15:00 UTC"}
    assert result["blockers"] == []


def test_a_middling_launch_is_almost(rubric):
    # instant(2) + story(1) + novelty(1) + honesty(1) = 5, no veto -> almost.
    result = rubric["assess"](record(technical_story=1, novelty=1, honesty=1), AS_OF)
    assert result["verdict"] == "almost"
    assert result["score"] == 5
    assert result["window"] is not None


def test_a_thin_launch_is_not_yet_with_no_window(rubric):
    # light_signup(1) + 1 + 0 + 0 = 2 -> below almost threshold.
    result = rubric["assess"](
        record(access="light_signup", technical_story=1, novelty=0, honesty=0), AS_OF
    )
    assert result["verdict"] == "not yet"
    assert result["window"] is None


def test_ready_needs_more_than_a_high_access_score(rubric):
    # instant(2) + story(2) + novelty(0) + honesty(2) = 6 clears the total, but novelty 0 blocks it.
    result = rubric["assess"](record(novelty=0), AS_OF)
    assert result["score"] == 6
    assert result["verdict"] == "almost"


# --- Blockers ---------------------------------------------------------------


def test_a_walled_product_is_blocked(rubric):
    result = rubric["assess"](record(access="walled"), AS_OF)
    assert result["verdict"] == "not yet"
    assert result["window"] is None
    assert any(b.startswith("walled") for b in result["blockers"])


def test_an_absent_founder_is_blocked(rubric):
    result = rubric["assess"](record(founder_present=False), AS_OF)
    assert result["verdict"] == "not yet"
    assert any(b.startswith("absent") for b in result["blockers"])


def test_the_no_founder_posting_hard_no_blocks_the_launch(rubric):
    result = rubric["assess"](record(), AS_OF, hard_nos=["no_founder_posting"])
    assert result["verdict"] == "not yet"
    assert any("no founder posting" in b for b in result["blockers"])


def test_an_unknown_hard_no_is_rejected(rubric):
    with pytest.raises(ValueError, match="hard no"):
        rubric["assess"](record(), AS_OF, hard_nos=["no_billboards"])


# --- Timing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        ("2026-09-28", "2026-09-29"),  # Mon -> Tue
        ("2026-09-29", "2026-09-29"),  # Tue stays
        ("2026-10-02", "2026-10-06"),  # Fri -> next Tue
        ("2026-10-03", "2026-10-06"),  # Sat -> next Tue
    ],
)
def test_the_window_is_the_next_preferred_weekday(rubric, start, expected):
    from datetime import date

    result = rubric["recommend_window"](date.fromisoformat(start))
    assert result == {"date": expected, "window": "13:00-15:00 UTC"}


def test_a_past_available_date_never_times_the_launch_before_today(rubric):
    result = rubric["assess"](record(), AS_OF, earliest_available="2020-01-01")
    assert result["window"]["date"] >= AS_OF


def test_a_future_available_date_moves_the_window_out(rubric):
    result = rubric["assess"](record(), AS_OF, earliest_available="2026-10-05")  # Mon
    assert result["window"]["date"] == "2026-10-06"  # -> Tue


# --- Run-to-run memory ------------------------------------------------------


def test_the_same_target_is_not_relaunched_too_soon(rubric):
    first = rubric["assess"](record(), "2026-06-01", report_path="reports/a.md")
    earlier = rubric["merge_states"]([rubric["read_state"](json.loads(json.dumps(first["state"])))])
    second = rubric["assess"](record(), AS_OF, previous=earlier, report_path="reports/b.md")
    assert second["verdict"] == "not yet"
    assert any("repost too soon" in b and "reports/a.md" in b for b in second["blockers"])


def test_a_major_change_relaunch_is_allowed(rubric):
    first = rubric["assess"](record(), "2026-06-01", report_path="reports/a.md")
    earlier = first["state"]
    second = rubric["assess"](
        record(major_change_since_last=True), AS_OF, previous=earlier, report_path="reports/b.md"
    )
    assert second["verdict"] == "ready"
    assert second["blockers"] == []


def test_a_target_launched_outside_tin_is_blocked(rubric):
    result = rubric["assess"](record(), AS_OF, past_targets=["https://www.example.dev/app/"])
    assert result["verdict"] == "not yet"
    assert any("repost too soon" in b for b in result["blockers"])


def test_an_old_enough_launch_can_run_again(rubric):
    first = rubric["assess"](record(), "2024-01-01", report_path="reports/a.md")
    result = rubric["assess"](record(), AS_OF, previous=first["state"], report_path="reports/b.md")
    assert result["verdict"] == "ready"


def test_the_state_remembers_the_most_recent_preparation(rubric):
    older = {
        "version": 1,
        "targets": {
            "example.dev/app": {
                "url": "https://example.dev/app",
                "prepared": "2024-01-01",
                "report": "a",
            }
        },
    }
    newer = {
        "version": 1,
        "targets": {
            "example.dev/app": {
                "url": "https://example.dev/app",
                "prepared": "2025-01-01",
                "report": "b",
            }
        },
    }
    merged = rubric["merge_states"]([older, None, newer])
    assert merged["targets"]["example.dev/app"]["report"] == "b"


def test_target_slug_ignores_scheme_www_query_and_trailing_slash(rubric):
    a = rubric["target_slug"]("https://www.Example.dev/App/?ref=x#top")
    b = rubric["target_slug"]("https://example.dev/App")
    assert a == b == "example.dev/app"


# --- Rejecting plausible but unusable records -------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_url": "example.dev/app"},  # not https
        {"target_url": "http://example.dev"},  # not https
        {"access": "maybe"},  # unknown access
        {"founder_present": "yes"},  # not a bool
        {"technical_story": 3},  # out of range
        {"honesty": True},  # bool is not a score
        {"major_change_since_last": "no"},  # not a bool
        {"title": "A tiny profiler for async Python"},  # missing Show HN prefix
        {"title": "Show HN:"},  # empty description
        {"title": "Show HN: The BEST profiler ever"},  # ALL CAPS + superlative
        {"title": "Show HN: " + "x" * 80},  # over 80 chars
        {"title": "Show HN: My launch retrospective 2026"},  # ends with a year
    ],
)
def test_plausible_but_unusable_records_are_rejected(rubric, overrides):
    with pytest.raises(ValueError):
        rubric["assess"](record(**overrides), AS_OF)


@pytest.mark.parametrize(
    "state",
    [
        None,
        [],
        {"version": 2, "targets": {}},
        {"version": 1, "targets": []},
        {"version": 1, "targets": {"k": "prepared"}},
        {"version": 1, "targets": {"k": {"url": "nope", "prepared": "2026-01-01", "report": "r"}}},
        {
            "version": 1,
            "targets": {"k": {"url": "https://x.io", "prepared": "soon", "report": "r"}},
        },
        {"version": 1, "targets": {"k": {"url": "https://x.io", "prepared": "2026-01-01"}}},
    ],
)
def test_untrusted_earlier_state_is_discarded(rubric, state):
    assert rubric["read_state"](state) is None
