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


def plan(scoring, *args, **kwargs):
    kwargs.setdefault("report_path", REPORT)
    return scoring["plan"](*args, **kwargs)


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
        "## Next steps",
        "## Picks",
        "**Why this show:**",
        "**Guest like you:**",
        "**How to reach them:**",
        "## Ladder",
        "## Who these shows reach",
        "## Shows you asked about",
        "## What could not be checked",
        "## Left out",
        "## About this run",
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
    result = plan(scoring, ARENAS, shows, AS_OF, max_pitches=3)
    assert [row["name"] for row in result["picks"]] == ["Buyers One", "Craft One", "Ev Sohbeti"]
    overflow = [row["name"] for row in result["excluded"]]
    assert overflow == ["Buyers Two", "Buyers Three"]
    assert result["excluded"][0]["reason"].startswith("more shows than the pitches you asked for")
    assert result["verdict"] == "thin"


def test_ladder_shows_wait_for_their_step_and_do_not_take_a_pick(scoring):
    shows = [
        show("Big Show", "craft", 1, "ladder", deadline="2026-10-11", similar_guest=None),
        show("Craft Two", "craft", 2),
    ]
    result = plan(scoring, ARENAS, shows, AS_OF, include_shows=["the big show"])
    assert [row["name"] for row in result["ladder"]] == ["Big Show"]
    assert [row["name"] for row in result["picks"]] == ["Craft Two"]
    assert result["include_outcomes"] == [{"name": "the big show", "outcome": "ladder"}]
    assert "Big Show" not in result["state"]["shows"].get("big show", {}).get("name", "")


def test_a_guest_fee_is_a_note_not_an_exclusion(scoring):
    paid = show(notes="Charges $300 for a guaranteed guest slot (its sponsor page).")
    result = plan(scoring, ARENAS, [paid], AS_OF)
    assert [row["name"] for row in result["picks"]] == ["Example Builders Show"]
    assert result["picks"][0]["notes"].startswith("Charges $300")


@pytest.mark.parametrize(
    ("overrides", "kwargs", "reason"),
    [
        ({"last_episode": "2026-04-01"}, {}, "no episode since 2026-04-01"),
        ({"last_episode": None}, {}, "latest episode date not verified"),
        ({}, {"already_pitched": ["example builders show"]}, "you already pitched it"),
        (
            {"route": "direct_message"},
            {"hard_nos": ["no_cold_email"]},
            "your growth plan rules out cold email",
        ),
    ],
)
def test_inactive_pitched_and_hard_no_shows_are_excluded_with_a_reason(
    scoring, overrides, kwargs, reason
):
    result = plan(scoring, ARENAS, [show(**overrides)], AS_OF, **kwargs)
    assert result["picks"] == []
    assert result["excluded"][0]["reason"].startswith(reason)
    assert result["status"] == "no shows verified" and result["verdict"] == "not a fit"


def test_a_show_found_twice_is_merged_with_every_method(scoring):
    shows = [
        show("Example Builders Show", "craft", 2, found_by=["lookalike_title"]),
        show("The Example Builders Show", "buyers", 1, found_by=["buyer_guests"]),
    ]
    result = plan(scoring, ARENAS, shows, AS_OF)
    assert len(result["picks"]) == 1 and result["duplicates_merged"] == 1
    assert result["picks"][0]["arena"] == "buyers"
    assert result["picks"][0]["found_by"] == ["buyer_guests", "lookalike_title"]


def test_gaps_name_missing_kinds_thin_arenas_and_unevaluated_asks(scoring):
    arenas = [ARENAS[0], ARENAS[2]]
    result = plan(scoring, arenas, [show()], AS_OF, include_shows=["Latent Space"])
    gaps = result["gaps"]
    assert "No shows for people in your own line of work were searched." in gaps
    assert "Small SaaS founders: searched only one way, so shows may be missing." in gaps
    assert "Turkish tech: no shows found." in gaps
    assert "You asked about Latent Space, and it was not checked." in gaps
    # The founder reads gaps: no arena ids or method names.
    assert not any("buyer_guests" in gap or gap.startswith("home:") for gap in gaps)


def test_a_show_pitched_in_an_earlier_run_is_never_pitched_again(scoring):
    first_report = "reports/outreach/podcasts/a.md"
    first = plan(scoring, ARENAS, [show()], "2026-09-10", report_path=first_report)
    earlier = scoring["merge_states"](
        [scoring["read_state"](json.loads(json.dumps(first["state"])))]
    )
    second = plan(
        scoring,
        ARENAS,
        [show(), show("New Show", "craft", 1)],
        AS_OF,
        previous=earlier,
        report_path="reports/outreach/podcasts/b.md",
    )
    assert [row["name"] for row in second["picks"]] == ["New Show"]
    assert second["excluded"][0]["reason"] == (
        f"pitched in an earlier run on 2026-09-10 ({first_report})"
    )
    assert set(second["state"]["shows"]) == {"example builders show", "new show"}


def test_the_pitch_block_holds_one_email_per_published_address_pick(scoring):
    picks = plan(
        scoring,
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
        {"hook_episode": {"title": "Ep 9", "url": "https://x.example/9", "date": "2026-09-01"}},
        {"hook_episode": {"title": "Ep 9", "url": "x", "date": "2026-09-01", "detail": "d"}},
    ],
)
def test_plausible_but_unusable_records_are_rejected(scoring, overrides):
    record = show(**overrides)
    if overrides.get("route") == "published_email" and "address_source" in overrides:
        record["address_source"] = overrides["address_source"]
    with pytest.raises(ValueError):
        plan(scoring, ARENAS, [record], AS_OF)


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
        plan(scoring, ARENAS, [show()], AS_OF, **args)


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
        plan(scoring, arenas, [], AS_OF)


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
    state = re.search(r"```json tin-podcast-state\n(.*?)\n```", text, re.S).group(1)
    parsed = scoring["read_state"](json.loads(state))
    assert parsed is not None
    picks_section = text.split("## Picks", 1)[1].split("\n## ", 1)[0]
    picked = re.findall(r"(?m)^### \d+\. (.+)$", picks_section)
    pitched_here = [s["name"] for s in parsed["shows"].values() if s["report"] == REPORT]
    assert sorted(picked) == sorted(pitched_here)
    block = json.loads(re.search(r"```json tin-podcast-pitches\n(.*?)\n```", text, re.S).group(1))
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
    for label in ("## Who these shows reach", "(home, Turkish)", "**Guest like you:**"):
        assert f"contains:{contains.index(label)}" in failed, label


def test_methods_tried_count_toward_coverage_even_when_they_found_nothing(scoring):
    arenas = [{**ARENAS[0], "tried": ["buyer_guests", "transcript_topic"]}]
    result = plan(scoring, arenas, [show()], AS_OF)
    assert not any("fewer than two methods" in gap for gap in result["gaps"])
    with pytest.raises(ValueError, match="tried"):
        plan(scoring, [{**ARENAS[0], "tried": ["guessing"]}], [show()], AS_OF)


def test_a_show_without_guests_is_listed_as_excluded(scoring):
    result = plan(scoring, ARENAS, [show(takes_guests=False)], AS_OF)
    assert result["picks"] == []
    assert result["excluded"][0]["reason"] == "does not host guests"


def test_state_records_the_report_path_inside_the_project(scoring):
    sandbox = "/home/user/project/" + REPORT
    result = plan(scoring, ARENAS, [show()], AS_OF, report_path=sandbox)
    assert result["state"]["shows"]["example builders show"]["report"] == REPORT
    with pytest.raises(ValueError, match="report_path"):
        plan(scoring, ARENAS, [show()], AS_OF, report_path="")
    with pytest.raises(ValueError, match="report_path"):
        plan(scoring, ARENAS, [show()], AS_OF, report_path="notes/podcasts.md")


def test_earlier_state_with_a_sandbox_path_is_read_as_the_project_path(scoring):
    item = {"name": "X", "route": "guest_form", "first_pitched": "2026-10-21"}
    state = {"version": 1, "shows": {"x": {**item, "report": "/home/user/project/" + REPORT}}}
    assert scoring["read_state"](state)["shows"]["x"]["report"] == REPORT


def test_a_pitch_may_open_on_a_checked_episode_other_than_the_similar_guest(scoring):
    hook = {
        "title": "Why a CISO doesn't trust the approve button",
        "url": "https://builders.example.com/ep/104",
        "date": "2026-09-22",
        "detail": "The CISO explains why one-click approvals stop being a control.",
    }
    result = plan(scoring, ARENAS, [show(hook_episode=hook)], AS_OF)
    assert result["picks"][0]["hook_episode"] == hook


def test_the_report_reads_as_separate_lines_with_its_data_folded_away():
    from tin_lite.documents import render_markdown

    text = fixture_text("synthetic_report.md")
    html = render_markdown(text).html
    # Each field renders on its own: a bullet, not one paragraph of joined lines.
    assert "<li><strong>Listeners:</strong>" in html
    assert "<strong>Listeners:</strong>" not in html.split("<li><strong>Listeners:</strong>")[0]
    assert html.count('<details class="md-document-metadata md-document-data">') == 2
    # The founder-facing part names no method or arena ids.
    readable = text.split("## About this run", 1)[0]
    assert not re.search(r"buyer_guests|lookalike_|transcript_topic|Searched with", readable)
