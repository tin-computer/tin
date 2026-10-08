"""Reviewed bookkeeping resource for outreach.podcast_guest; no web, Podscan or model calls."""

import json
import re
from pathlib import Path

import pytest

from tin_lite.workflow_prerequisites import parse_workflow_prerequisites
from tin_lite.workflow_qualification import Qualification, assess_output

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/outreach.podcast_guest"
SKILL = PACKAGE / "skills/podcast-guest"
FIXTURES = ROOT / "tests/fixtures/podcast_guest"
CASES = ROOT / "workflow_evals/outreach.podcast_guest/qualification.json"
AS_OF = "2026-10-05"
REPORT = "reports/outreach/podcasts/7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f.md"
ARENAS = [
    {
        "id": "buyers",
        "label": "Small SaaS founders",
        "kind": "buyers",
        "language": "en",
        "why": "They use the product.",
    },
    {
        "id": "craft",
        "label": "Agent engineers",
        "kind": "craft",
        "language": "en",
        "why": "Few run agent fleets on real businesses.",
    },
    {
        "id": "home",
        "label": "Turkish tech",
        "kind": "home",
        "language": "tr",
        "why": "A US venture-backed startup engineer is rare there.",
    },
]


@pytest.fixture(scope="module")
def scoring():
    text = (SKILL / "SCORING.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)\n```", text, re.S)
    assert len(blocks) == 1
    namespace = {}
    # Only the fixed repository resource executes here, never show text from a run.
    exec(compile(blocks[0], str(SKILL / "SCORING.md"), "exec"), namespace)  # noqa: S102
    return namespace


def show(name="Example Builders Show", arena="buyers", rank=1, route="guest_form", **overrides):
    record = {
        "name": name,
        "url": "https://builders.example.com",
        "arena": arena,
        "language": "en",
        "last_episode": "2026-09-28",
        "found_by": ["buyer_guests"],
        "similar_guest": {
            "name": "Riley Example",
            "episode": "Running a company with agents",
            "url": "https://builders.example.com/ep/42",
            "date": "2026-08-14",
        },
        "route": route,
        "rank": rank,
        "reason": "Hosted a small agent company's founder last month.",
    }
    if route == "published_email":
        record.update(
            pitch_address="guests@builders.example.com",
            address_source="https://builders.example.com/guests",
        )
    if route == "ladder":
        record["ladder_step"] = "Speak at the hosts' conference first."
    record.update(overrides)
    return record


def fixture_text(name):
    # Normalise line endings so a Windows checkout (core.autocrlf) reads the same as CI.
    return (FIXTURES / name).read_bytes().decode("utf-8").replace("\r\n", "\n")


def case(case_id):
    contract = Qualification.model_validate(json.loads(CASES.read_text(encoding="utf-8")))
    return next(item for item in contract.cases if item.id == case_id)


def test_manifest_binds_podscan_and_declares_context_and_a_run_owned_report():
    definition = json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))["definition"]
    assert definition["key"] == PACKAGE.name
    procedure = definition["procedure"]
    on_disk = sorted(path.relative_to(PACKAGE).as_posix() for path in SKILL.rglob("*.md"))
    assert sorted(procedure["skill_files"]) == on_disk
    assert procedure["output"]["path_template"] == "reports/outreach/podcasts/{run_id}.md"
    assert procedure["sandbox"]["egress"] == "fenced"
    assert procedure["services"] == {
        "podcasts": {
            "provider_key": "managed.podscan",
            "max_calls": 30,
            "max_response_bytes": 24000,
        }
    }
    assert definition["integration_requirements"] == [
        {"provider_key": "managed.podscan", "capabilities": ["podcasts.read"], "required": True}
    ]
    schema = definition["input_schema"]
    assert schema["required"] == ["project_id", "founder_profile"]
    prerequisites = parse_workflow_prerequisites(definition["prerequisites"], input_schema=schema)
    assert {(p.path, p.producer) for p in prerequisites} == {
        ("wiki/INDEX.md", "product.deep_dive"),
        ("reports/GROWTH_ONBOARDING_PLAN.md", "growth.onboarding_plan"),
        (".agents/skills/writing-style/SKILL.md", "style.capture"),
    }


def test_skill_report_labels_and_blocks_agree():
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for label in (
        "Founder:",
        "## Arenas",
        "## Picks",
        "Similar guest:",
        "Send via:",
        "## Ladder",
        "## Shows you asked about",
        "## Gaps",
        "## Excluded candidates",
    ):
        assert label in skill, label
    scoring_text = (SKILL / "SCORING.md").read_text(encoding="utf-8")
    for block in ("tin-podcast-pitches", "tin-podcast-state"):
        assert block in skill
    assert "tin-podcast-state" in scoring_text
    search = (SKILL / "SEARCH.md").read_text(encoding="utf-8")
    for operation in (
        "people.search",
        "people.appearances",
        "episodes.search",
        "podcasts.search",
        "podcasts.get",
        "podcasts.episodes",
        "charts.top",
    ):
        assert f"`{operation}`" in search, operation


def test_search_guide_names_only_operations_tin_exposes():
    from tin_lite import podscan

    search = (SKILL / "SEARCH.md").read_text(encoding="utf-8")
    named = set(re.findall(r"^\| `([a-z]+\.[a-z_]+)` \|", search, re.M))
    assert named == set(podscan.OPERATIONS)


def test_picks_are_balanced_across_arenas_not_ranked_on_one_scale(scoring):
    shows = [
        show("Buyers One", "buyers", 1),
        show("Buyers Two", "buyers", 2),
        show("Buyers Three", "buyers", 3),
        show("Craft One", "craft", 1, found_by=["lookalike_title"]),
        show("Ev Sohbeti", "home", 1, language="tr", found_by=["country_chart"]),
    ]
    result = scoring["plan"](ARENAS, shows, AS_OF, max_pitches=3)
    assert [row["name"] for row in result["picks"]] == ["Buyers One", "Craft One", "Ev Sohbeti"]
    overflow = [row["name"] for row in result["excluded"]]
    assert overflow == ["Buyers Two", "Buyers Three"]
    assert result["excluded"][0]["reason"] == "over max_pitches; eligible next run"
    assert result["verdict"] == "thin"


def test_ladder_shows_wait_for_their_step_and_do_not_take_a_pick(scoring):
    shows = [
        show("Big Show", "craft", 1, "ladder", deadline="2026-10-11", similar_guest=None),
        show("Craft Two", "craft", 2),
    ]
    result = scoring["plan"](ARENAS, shows, AS_OF, include_shows=["the big show"])
    assert [row["name"] for row in result["ladder"]] == ["Big Show"]
    assert [row["name"] for row in result["picks"]] == ["Craft Two"]
    assert result["include_outcomes"] == [{"name": "the big show", "outcome": "ladder"}]
    assert "Big Show" not in result["state"]["shows"].get("big show", {}).get("name", "")


def test_a_guest_fee_is_a_note_not_an_exclusion(scoring):
    paid = show(notes="Charges $300 for a guaranteed guest slot (its sponsor page).")
    result = scoring["plan"](ARENAS, [paid], AS_OF)
    assert [row["name"] for row in result["picks"]] == ["Example Builders Show"]
    assert result["picks"][0]["notes"].startswith("Charges $300")


@pytest.mark.parametrize(
    ("overrides", "kwargs", "reason"),
    [
        ({"last_episode": "2026-04-01"}, {}, "no episode since 2026-04-01"),
        ({"last_episode": None}, {}, "latest episode date not verified"),
        ({}, {"already_pitched": ["example builders show"]}, "named in already_pitched"),
        ({"route": "direct_message"}, {"hard_nos": ["no_cold_email"]}, "hard no: no cold email"),
    ],
)
def test_inactive_pitched_and_hard_no_shows_are_excluded_with_a_reason(
    scoring, overrides, kwargs, reason
):
    result = scoring["plan"](ARENAS, [show(**overrides)], AS_OF, **kwargs)
    assert result["picks"] == []
    assert result["excluded"][0]["reason"].startswith(reason)
    assert result["status"] == "no shows verified" and result["verdict"] == "not a fit"


def test_a_show_found_twice_is_merged_with_every_method(scoring):
    shows = [
        show("Example Builders Show", "craft", 2, found_by=["lookalike_title"]),
        show("The Example Builders Show", "buyers", 1, found_by=["buyer_guests"]),
    ]
    result = scoring["plan"](ARENAS, shows, AS_OF)
    assert len(result["picks"]) == 1 and result["duplicates_merged"] == 1
    assert result["picks"][0]["arena"] == "buyers"
    assert result["picks"][0]["found_by"] == ["buyer_guests", "lookalike_title"]


def test_gaps_name_missing_kinds_thin_arenas_and_unevaluated_asks(scoring):
    arenas = [ARENAS[0], ARENAS[2]]
    result = scoring["plan"](arenas, [show()], AS_OF, include_shows=["Latent Space"])
    assert "no craft arena was searched" in result["gaps"]
    assert "buyers: fewer than two methods tried (buyer_guests)" in result["gaps"]
    assert "home: no candidates found" in result["gaps"]
    assert "asked-for show not evaluated: Latent Space" in result["gaps"]


def test_a_show_pitched_in_an_earlier_run_is_never_pitched_again(scoring):
    first = scoring["plan"](ARENAS, [show()], "2026-09-10", report_path="reports/a.md")
    earlier = scoring["merge_states"](
        [scoring["read_state"](json.loads(json.dumps(first["state"])))]
    )
    second = scoring["plan"](
        ARENAS,
        [show(), show("New Show", "craft", 1)],
        AS_OF,
        previous=earlier,
        report_path="reports/b.md",
    )
    assert [row["name"] for row in second["picks"]] == ["New Show"]
    assert second["excluded"][0]["reason"] == "pitched on 2026-09-10 in reports/a.md"
    assert set(second["state"]["shows"]) == {"example builders show", "new show"}


def test_the_pitch_block_holds_one_email_per_published_address_pick(scoring):
    picks = scoring["plan"](
        ARENAS,
        [show(route="published_email"), show("Form Show", "craft", 1)],
        AS_OF,
    )["picks"]
    draft = {
        "show": "Example Builders Show",
        "subject": "Guest idea",
        "body": "Hi.",
        "follow_up": "Following up.",
    }
    block = scoring["pitch_block"](picks, [draft])
    assert block["pitches"][0]["to"] == "guests@builders.example.com"
    assert block["pitches"][0]["address_source"] == "https://builders.example.com/guests"
    with pytest.raises(ValueError, match="no draft"):
        scoring["pitch_block"](picks, [])
    with pytest.raises(ValueError, match="not a published_email pick"):
        scoring["pitch_block"](picks, [draft, {**draft, "show": "Form Show"}])
    with pytest.raises(ValueError, match="subject"):
        scoring["pitch_block"](picks, [{**draft, "subject": "x" * 121}])


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "  "},
        {"url": "builders.example.com"},
        {"arena": "investors"},
        {"language": "English"},
        {"last_episode": "last week"},
        {"found_by": []},
        {"found_by": ["vibes"]},
        {"route": "guessed_email"},
        {"rank": 0},
        {"rank": True},
        {"route": "published_email", "pitch_address": "the host"},
        {"route": "published_email", "pitch_address": "a@b.co", "address_source": None},
        {"pitch_address": "guests@builders.example.com"},
        {"route": "ladder", "ladder_step": ""},
        {"similar_guest": {"name": "Riley", "episode": "x", "url": "x", "date": "2026-01-01"}},
    ],
)
def test_plausible_but_unusable_records_are_rejected(scoring, overrides):
    record = show(**overrides)
    if overrides.get("route") == "published_email" and "address_source" in overrides:
        record["address_source"] = overrides["address_source"]
    with pytest.raises(ValueError):
        scoring["plan"](ARENAS, [record], AS_OF)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"max_pitches": 2}, "max_pitches"),
        ({"max_pitches": 21}, "max_pitches"),
        ({"hard_nos": ["no_podcasts"]}, "hard no"),
    ],
)
def test_run_inputs_outside_the_contract_are_rejected(scoring, args, message):
    with pytest.raises(ValueError, match=message):
        scoring["plan"](ARENAS, [show()], AS_OF, **args)


@pytest.mark.parametrize(
    "arenas",
    [
        [],
        [{**ARENAS[0], "kind": "investors"}],
        [{**ARENAS[0], "id": "Buyers!"}],
        [ARENAS[0], ARENAS[0]],
    ],
)
def test_untrustworthy_arenas_are_rejected(scoring, arenas):
    with pytest.raises(ValueError):
        scoring["plan"](arenas, [], AS_OF)


@pytest.mark.parametrize(
    "state",
    [
        None,
        [],
        {"version": 2, "shows": {}},
        {"version": 1, "shows": []},
        {"version": 1, "shows": {"x": "pitched"}},
        {
            "version": 1,
            "shows": {
                "other": {
                    "name": "X",
                    "route": "guest_form",
                    "first_pitched": "2026-01-01",
                    "report": "r",
                }
            },
        },
        {
            "version": 1,
            "shows": {
                "x": {"name": "X", "route": "ladder", "first_pitched": "2026-01-01", "report": "r"}
            },
        },
        {
            "version": 1,
            "shows": {
                "x": {"name": "X", "route": "guest_form", "first_pitched": "soon", "report": "r"}
            },
        },
    ],
)
def test_untrusted_earlier_state_is_discarded(scoring, state):
    assert scoring["read_state"](state) is None


def test_a_plausible_report_passes_the_ordinary_case(scoring):
    text = fixture_text("synthetic_report.md")
    result = assess_output(case("ordinary"), status="succeeded", content=text.encode("utf-8"))
    assert result["status"] == "passed", result["checks"]
    state = re.search(r"```tin-podcast-state\n(.*?)\n```", text, re.S).group(1)
    parsed = scoring["read_state"](json.loads(state))
    assert parsed is not None
    picks_section = text.split("## Picks", 1)[1].split("\n## ", 1)[0]
    picked = re.findall(r"(?m)^### \d+\. (.+)$", picks_section)
    pitched_here = [s["name"] for s in parsed["shows"].values() if s["report"] == REPORT]
    assert sorted(picked) == sorted(pitched_here)
    block = json.loads(re.search(r"```tin-podcast-pitches\n(.*?)\n```", text, re.S).group(1))
    emails = [p for p in block["pitches"]]
    assert {p["show"] for p in emails} <= set(picked)
    turkish = next(p for p in emails if p["language"] == "tr")
    assert "Merhaba" in turkish["body"]
    assert "Verdict: fit\n" in text


def test_a_plausible_but_unusable_report_fails_the_ordinary_case():
    # Ranked by size on one scale, guessed addresses, invented credentials, non-English shows
    # and paid shows dropped, no arenas, gaps or memory.
    report = (FIXTURES / "unusable_report.md").read_bytes()
    result = assess_output(case("ordinary"), status="succeeded", content=report)
    assert result["status"] == "failed"
    failed = {check["check"] for check in result["checks"] if not check["passed"]}
    contains = case("ordinary").expect.contains
    for label in ("## Arenas", "(home, tr)", "Similar guest:", "```tin-podcast-state"):
        assert f"contains:{contains.index(label)}" in failed, label


def test_methods_tried_count_toward_coverage_even_when_they_found_nothing(scoring):
    arenas = [{**ARENAS[0], "tried": ["buyer_guests", "transcript_topic"]}]
    result = scoring["plan"](arenas, [show()], AS_OF)
    assert not any("fewer than two methods" in gap for gap in result["gaps"])
    with pytest.raises(ValueError, match="tried"):
        scoring["plan"]([{**ARENAS[0], "tried": ["guessing"]}], [show()], AS_OF)


def test_a_show_without_guests_is_listed_as_excluded(scoring):
    result = scoring["plan"](ARENAS, [show(takes_guests=False)], AS_OF)
    assert result["picks"] == []
    assert result["excluded"][0]["reason"] == "does not host guests"
