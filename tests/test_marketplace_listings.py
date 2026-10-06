"""Reviewed partner-marketplace resources, synthetic rows only; no web or model calls in CI."""

import json
import re
from pathlib import Path

import pytest

from tin_lite.workflow_prerequisites import parse_workflow_prerequisites
from tin_lite.workflow_qualification import Qualification, assess_output

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/outreach.marketplace_listings"
RESOURCES = PACKAGE / "skills/partner-marketplaces"
FIXTURES = ROOT / "tests/fixtures/marketplace_listings"
CASES = ROOT / "workflow_evals/outreach.marketplace_listings/qualification.json"


def resource(name):
    path = RESOURCES / name
    blocks = re.findall(r"```python\n(.*?)\n```", path.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 1
    namespace = {}
    # Only fixed repository resources execute here, never uploaded author code.
    exec(compile(blocks[0], str(path), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture
def scoring():
    return resource("SCORING.md")


def gate(status="pass", size="small", rule="privacy policy"):
    return {"rule": rule, "status": status, "size": size}


def row(marketplace="github", **overrides):
    base = {
        "marketplace": marketplace,
        "shape": "user_authorized",
        "presence": "absent",
        "audience": 3,
        "competitors_listed": 2,
        "gates": [gate()],
        "effort": 1,
        "review_days": None,
    }
    base.update(overrides)
    return base


def test_readiness_follows_the_largest_open_gate(scoring):
    readiness = scoring["readiness"]
    assert readiness([gate(), gate()]) == "ready"
    assert readiness([gate(), gate("fail", "small")]) == "fix_first"
    assert readiness([gate(), gate("unknown", "small")]) == "fix_first"
    assert readiness([gate("unknown", "large"), gate("fail", "small")]) == "confirm"
    assert readiness([gate("unknown", "large"), gate("fail", "large")]) == "blocked"


@pytest.mark.parametrize(
    "gates",
    [[], None, [{"status": "pass", "size": "small"}], [gate("maybe")], [gate(size="huge")]],
)
def test_readiness_rejects_malformed_gates(scoring, gates):
    with pytest.raises(ValueError):
        scoring["readiness"](gates)


def test_score_is_the_documented_formula(scoring):
    score = scoring["score"]
    # 3 audience x (1 + 2 competitors) x ready 3 / effort 1
    assert score(row())["score"] == 27
    # competitors cap at three; fix_first weighs 2; effort 3 divides
    capped = row(competitors_listed=40, gates=[gate("fail")], effort=3, audience=2)
    assert score(capped)["score"] == pytest.approx(2 * 4 * 2 / 3, abs=0.01)
    assert score(row(gates=[gate("unknown", "large")]))["score"] == 9
    assert score(row(competitors_listed=0, audience=1))["score"] == 3


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"shape": "vendor_side"}, "not a storefront: vendor side"),
        ({"shape": "outbound_only"}, "not a storefront: outbound only"),
        ({"shape": "declared_only"}, "not a storefront: declared only"),
        ({"presence": "listed"}, "already listed"),
        ({"presence": "unknown"}, "presence unknown; recheck next run"),
        ({"gates": [gate("fail", "large")]}, "blocked by a large gate"),
    ],
)
def test_rows_that_must_never_be_picked(scoring, overrides, reason):
    result = scoring["score"](row(**overrides))
    assert result["score"] is None
    assert result["reason"] == reason


def test_a_billing_vendor_is_never_recommended_even_with_perfect_evidence(scoring):
    stripe = row("stripe", shape="vendor_side", competitors_listed=3, audience=3)
    picks, ranked = scoring["pick"]([stripe], 3)
    assert picks == []
    assert ranked[0]["reason"] == "not a storefront: vendor side"


@pytest.mark.parametrize(
    "overrides",
    [
        {"marketplace": "producthunt"},
        {"shape": "partner"},
        {"presence": "maybe"},
        {"audience": 0},
        {"audience": True},
        {"audience": 2.5},
        {"competitors_listed": -1},
        {"effort": 4},
        {"review_days": -2},
    ],
)
def test_score_rejects_values_outside_the_contract(scoring, overrides):
    with pytest.raises(ValueError):
        scoring["score"](row(**overrides))


def test_pick_orders_by_score_then_review_time_then_id(scoring):
    rows = [
        row("slack", effort=2, review_days=50),
        row("github", review_days=None),
        row("notion", effort=2, review_days=10),
        row("hubspot", shape="vendor_side"),
        row("chrome", shape="distributed_artifact", competitors_listed=0),
    ]
    picks, ranked = scoring["pick"](rows, 3)
    assert [r["marketplace"] for r in picks] == ["github", "notion", "slack"]
    assert [r["marketplace"] for r in ranked] == ["github", "notion", "slack", "chrome", "hubspot"]
    top_one, _ = scoring["pick"](rows, 1)
    assert [r["marketplace"] for r in top_one] == ["github"]


def test_skipped_marketplaces_stay_in_the_table_but_are_not_picked(scoring):
    picks, ranked = scoring["pick"]([row("github"), row("slack", effort=2)], 3, skip=["github"])
    assert [r["marketplace"] for r in picks] == ["slack"]
    assert {r["marketplace"] for r in ranked} == {"github", "slack"}


@pytest.mark.parametrize(
    ("rows", "max_picks", "skip"),
    [
        ([row("github"), row("github")], 3, ()),
        ([row()], 0, ()),
        ([row()], 4, ()),
        ([row()], 2, ("product hunt",)),
    ],
)
def test_pick_rejects_duplicates_and_bad_bounds(scoring, rows, max_picks, skip):
    with pytest.raises(ValueError):
        scoring["pick"](rows, max_picks, skip)


def test_fit_counts_and_never_truncates(scoring):
    fit = scoring["fit"]
    name = "Reorder alerts for Shopify"
    assert fit(name, 30) == {"chars": len(name), "limit": 30, "ok": True}
    too_long = "Inventory reorder alerts for every Shopify store"
    assert fit(too_long, 30)["ok"] is False
    assert fit(too_long, None)["ok"] is True
    with pytest.raises(ValueError):
        fit("   ", 30)
    with pytest.raises(ValueError):
        fit(name, 0)


def test_state_round_trip_reports_what_changed(scoring):
    first_picks, first_ranked = scoring["pick"]([row("github"), row("slack", effort=2)], 1)
    first, changes = scoring["next_state"](None, first_ranked, first_picks)
    assert changes["new"] == ["github", "slack"]
    assert first["rows"]["github"] == {"presence": "absent", "picked_runs": 1}

    previous = scoring["read_state"](json.loads(json.dumps(first)))
    rows = [row("github", presence="listed"), row("slack", effort=2), row("notion", effort=2)]
    picks, ranked = scoring["pick"](rows, 2)
    second, changes = scoring["next_state"](previous, ranked, picks)
    assert changes["went_live"] == ["github"]
    assert changes["new"] == ["notion"]
    assert second["rows"]["slack"]["picked_runs"] == 1

    picks, ranked = scoring["pick"]([row("slack", effort=2)], 1)
    third, changes = scoring["next_state"](scoring["read_state"](second), ranked, picks)
    assert changes["still_waiting"] == [("slack", 2)]
    assert changes["dropped"] == ["github", "notion"]


@pytest.mark.parametrize(
    "state",
    [
        None,
        [],
        {"version": 2, "rows": {}},
        {"version": 1, "rows": []},
        {"version": 1, "rows": {"myspace": {"presence": "absent"}}},
        {"version": 1, "rows": {"github": {"presence": "gone"}}},
        {"version": 1, "rows": {"github": {"presence": "absent", "picked_runs": -1}}},
        {"version": 1, "rows": {"github": "absent"}},
    ],
)
def test_untrusted_previous_state_is_discarded(scoring, state):
    assert scoring["read_state"](state) is None


def test_reference_file_and_scorer_name_the_same_marketplaces(scoring):
    text = (RESOURCES / "MARKETPLACES.md").read_text(encoding="utf-8")
    entries = re.split(r"(?m)^## ", text)[1:]
    ids = [entry.split(" — ", 1)[0].strip() for entry in entries]
    assert ids == list(scoring["MARKETPLACES"])
    for entry in entries:
        for field in ("**Storefront for**", "**Hard gates**", "**Form**", "**Effort**"):
            assert field in entry, (entry.splitlines()[0], field)
        assert re.search(r"\*\*Rules source\*\*: https://", entry), entry.splitlines()[0]
        storefront = entry.split("**Storefront for**", 1)[1].split("\n- **", 1)[0]
        assert re.search(r"`(user_authorized|distributed_artifact)`", storefront)


def test_manifest_declares_every_resource_and_a_valid_prerequisite():
    manifest = json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))
    definition = manifest["definition"]
    procedure = definition["procedure"]
    on_disk = sorted(
        path.relative_to(PACKAGE).as_posix() for path in (PACKAGE / "skills").rglob("*.md")
    )
    assert sorted(procedure["skill_files"]) == on_disk
    prerequisites = parse_workflow_prerequisites(
        definition["prerequisites"], input_schema=definition["input_schema"]
    )
    assert [(p.section, p.producer, p.level) for p in prerequisites] == [
        ("### Code map", "product.code_map", "required"),
        ("### Feature map", "product.deep_dive", "recommended"),
        (None, "growth.onboarding_plan", "recommended"),
        (None, "style.capture", "recommended"),
    ]
    assert procedure["output"]["path_template"] == "reports/outreach/marketplaces/{run_id}.md"
    # The group is now "Outreach", not just cold outreach, so marketplace listings belong in it.
    assert definition["system"] == "cold-outreach"
    for name, spec in definition["input_schema"]["properties"].items():
        if spec.get("type") == "string" and name != "project_id":
            assert "maxLength" in spec, name


def test_report_layout_and_skill_agree_on_the_state_block():
    skill = (RESOURCES / "SKILL.md").read_text(encoding="utf-8")
    report = (RESOURCES / "REPORT.md").read_text(encoding="utf-8")
    assert "tin-listings-state" in skill and "```tin-listings-state" in report
    for heading in (
        "## Submit this week",
        "## Fix first",
        "## Since last run",
        "## Shelf check",
        "## Not a storefront",
        "## No marketplace in the reference",
        "## Evidence",
    ):
        assert heading in report


def fixture_text(name):
    # Normalise line endings so a Windows checkout (core.autocrlf) reads the same as CI.
    return (FIXTURES / name).read_bytes().decode("utf-8").replace("\r\n", "\n")


def case(case_id):
    contract = Qualification.model_validate(json.loads(CASES.read_text(encoding="utf-8")))
    return next(item for item in contract.cases if item.id == case_id)


def test_the_real_tin_report_passes_the_ordinary_case(scoring):
    # Output of hosted run 450c9353 on 2026-09-23, the second run on the same project, from
    # v1.0.0: it re-recommended GitHub. v1.1.0 reports it as waiting instead (tested above).
    text = fixture_text("tin_2026-09-23.md")
    result = assess_output(case("ordinary"), status="succeeded", content=text.encode("utf-8"))
    assert result["status"] == "passed", result["checks"]
    state = re.search(r"```tin-listings-state\n(.*?)\n```", text, re.S).group(1)
    assert scoring["read_state"](json.loads(state))["rows"]["github"]["picked_runs"] == 2
    assert "Recommended again: github (2 runs)" in text
    assert len(text.split("## Shelf check", 1)[0]) < 6000


def test_a_plausible_but_unusable_report_fails_the_ordinary_case(scoring):
    # Recommends Stripe because Tin bills with it, and calls a timed-out search "absent".
    report = (FIXTURES / "unusable_report.md").read_bytes()
    result = assess_output(case("ordinary"), status="succeeded", content=report)
    assert result["status"] == "failed"
    failed = {check["check"] for check in result["checks"] if not check["passed"]}
    assert {"contains:4", "contains:8", "excludes:1"} <= failed
    # The scorer refuses the same mistake before any report is written.
    stripe = row("stripe", shape="vendor_side")
    assert scoring["score"](stripe)["score"] is None
    assert scoring["score"](row(presence="unknown"))["score"] is None


def test_equal_scores_go_to_the_shorter_review_even_against_alphabetical_order(scoring):
    rows = [row("notion", effort=2, review_days=50), row("slack", effort=2, review_days=10)]
    picks, _ = scoring["pick"](rows, 2)
    assert [r["marketplace"] for r in picks] == ["slack", "notion"]


def test_copy_exactly_at_the_limit_fits(scoring):
    assert scoring["fit"]("x" * 30, 30)["ok"] is True
    assert scoring["fit"]("x" * 31, 30)["ok"] is False


def test_only_real_changes_are_reported_between_runs(scoring):
    previous = scoring["read_state"](
        {
            "version": 1,
            "rows": {
                "mcp_registry": {"presence": "listed", "picked_runs": 0},
                "github": {"presence": "absent", "picked_runs": 0},
            },
        }
    )
    rows = [row("mcp_registry", presence="listed"), row("github")]
    picks, ranked = scoring["pick"](rows, 1)
    state, changes = scoring["next_state"](previous, ranked, picks)
    # Listed before and still listed is not news; a first-time pick is not "still waiting".
    assert changes["went_live"] == []
    assert changes["still_waiting"] == []
    assert state["rows"]["github"]["picked_runs"] == 1


def test_limits_in_the_tin_report_come_from_the_reference_file():
    reference = (RESOURCES / "MARKETPLACES.md").read_text(encoding="utf-8")
    github = reference.split("## github — ", 1)[1].split("\n## ", 1)[0]
    report = fixture_text("tin_2026-09-23.md")
    table = report.split("### 1. GitHub Marketplace", 1)[1].split("### 2.", 1)[0]
    limits = {int(n) for n in re.findall(r"\| \d+ / (\d+) \|", table)}
    assert limits == {80, 250, 255, 1000}
    for limit in limits:
        assert f"{limit:,}" in github or str(limit) in github, limit


def test_a_listing_prepared_in_an_earlier_run_is_not_prepared_again(scoring):
    first_picks, first_ranked = scoring["pick"]([row("github"), row("slack", effort=2)], 1)
    first, _ = scoring["next_state"](
        None, first_ranked, first_picks, checked="2026-09-17", report="reports/outreach/a.md"
    )
    assert first["rows"]["github"]["picked_on"] == "2026-09-17"
    assert first["rows"]["github"]["picked_in"] == "reports/outreach/a.md"
    assert "picked_on" not in first["rows"]["slack"]

    previous = scoring["merge_states"]([scoring["read_state"](json.loads(json.dumps(first)))])
    rows = [row("github"), row("slack", effort=2)]
    picks, ranked = scoring["pick"](rows, 1, previous=previous)
    assert [r["marketplace"] for r in picks] == ["slack"]
    github = next(r for r in ranked if r["marketplace"] == "github")
    assert github["waiting"] is True
    assert github["reason"] == "recommended on 2026-09-17 in reports/outreach/a.md; waiting on you"
    second, changes = scoring["next_state"](
        previous, ranked, picks, checked="2026-09-24", report="reports/outreach/b.md"
    )
    assert changes["still_waiting"] == [("github", 1)]
    # The packet stays where it was first written.
    assert second["rows"]["github"]["picked_in"] == "reports/outreach/a.md"
    assert second["rows"]["slack"]["picked_in"] == "reports/outreach/b.md"

    # Once it goes live it leaves the waiting list.
    picks, ranked = scoring["pick"]([row("github", presence="listed")], 1, previous=second)
    _, changes = scoring["next_state"](second, ranked, picks, checked="2026-10-01")
    assert changes["went_live"] == ["github"]
    assert changes["still_waiting"] == []


def test_every_earlier_report_folds_into_one_memory(scoring):
    older = {
        "version": 1,
        "checked": "2026-09-10",
        "rows": {
            "github": {
                "presence": "absent",
                "picked_runs": 1,
                "picked_on": "2026-09-10",
                "picked_in": "reports/outreach/a.md",
            }
        },
    }
    newer = {
        "version": 1,
        "checked": "2026-09-17",
        "rows": {
            "github": {"presence": "listed", "picked_runs": 0},
            "slack": {"presence": "unknown", "picked_runs": 0},
        },
    }
    states = [scoring["read_state"](newer), None, scoring["read_state"](older)]
    merged = scoring["merge_states"](states)
    assert merged["checked"] == "2026-09-17"
    assert merged["rows"]["github"] == {
        "presence": "listed",
        "picked_runs": 1,
        "picked_on": "2026-09-10",
        "picked_in": "reports/outreach/a.md",
    }
    assert merged["rows"]["slack"]["presence"] == "unknown"
    assert scoring["merge_states"]([None]) is None


@pytest.mark.parametrize(
    "state",
    [
        {"version": 1, "checked": "last week", "rows": {}},
        {"version": 1, "rows": {"github": {"presence": "absent", "picked_on": "2026-9-1"}}},
        {"version": 1, "rows": {"github": {"presence": "absent", "picked_in": ""}}},
    ],
)
def test_untrusted_memory_fields_discard_the_block(scoring, state):
    assert scoring["read_state"](state) is None


def test_the_family_leaves_sending_to_the_founder():
    skill = (RESOURCES / "SKILL.md").read_text(encoding="utf-8")
    assert "outreach/email/SHORTLIST.csv" in skill
    assert "GROWTH_ONBOARDING_PLAN.md" in skill
    assert ".agents/skills/writing-style/SKILL.md" in skill
