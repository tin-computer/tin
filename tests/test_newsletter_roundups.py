"""Reviewed ranking resource for outreach.newsletter_roundups; no web or model calls in CI."""

import json
import re
from pathlib import Path

import pytest

from tin_lite.workflow_prerequisites import parse_workflow_prerequisites

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/outreach.newsletter_roundups"
SKILL = PACKAGE / "skills/newsletter-roundups"
AS_OF = "2026-09-25"


@pytest.fixture(scope="module")
def scoring():
    text = (SKILL / "SCORING.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)\n```", text, re.S)
    assert len(blocks) == 1
    namespace = {}
    # Only the fixed repository resource executes here, never newsletter text from a run.
    exec(compile(blocks[0], str(SKILL / "SCORING.md"), "exec"), namespace)  # noqa: S102
    return namespace


def newsletter(name="Example Tools Weekly", **overrides):
    return {
        "name": name,
        "url": "https://exampletools.example.com/suggest",
        "newsletter_type": "tools_roundup",
        "open_verified": True,
        "requirements_confirmed": True,
        "route": "form",
        "fit": 3,
        **overrides,
    }


def test_manifest_declares_resources_context_and_a_run_owned_report():
    definition = json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))["definition"]
    assert definition["key"] == PACKAGE.name
    assert definition["system"] == "cold-outreach"
    procedure = definition["procedure"]
    on_disk = sorted(path.relative_to(PACKAGE).as_posix() for path in SKILL.rglob("*.md"))
    assert sorted(procedure["skill_files"]) == on_disk
    assert procedure["output"]["path_template"] == "reports/outreach/newsletters/{run_id}.md"
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
    for label in ("Pitching:", "Context:", "Submit via:", "## Pitched in earlier runs"):
        assert label in skill, label
    assert "tin-newsletter-state" in skill
    assert "tin-newsletter-state" in (SKILL / "SCORING.md").read_text(encoding="utf-8")
    # The email campaign contract does not fit tailored pitches; the skill says so.
    assert "outreach/email/SHORTLIST.csv" in skill


def test_shortlist_is_ranked_by_fit_then_name(scoring):
    newsletters = [
        newsletter("Loose Digest", fit=1),
        newsletter("Close Roundup", fit=3),
        newsletter("Another Close Roundup", fit=3),
        newsletter("Mid Weekly", fit=2),
    ]
    result = scoring["plan"](newsletters, as_of=AS_OF)
    assert [row["name"] for row in result["shortlist"]] == [
        "Another Close Roundup",
        "Close Roundup",
        "Mid Weekly",
        "Loose Digest",
    ]
    assert result["status"] == "complete"
    assert result["verdict"] == "fit"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"open_verified": False}, "open submissions not verified"),
        ({"requirements_confirmed": False}, "requirements not confirmed"),
    ],
)
def test_unverified_newsletters_are_excluded_with_a_reason(scoring, overrides, reason):
    result = scoring["plan"]([newsletter(**overrides)], as_of=AS_OF)
    assert result["shortlist"] == []
    assert reason in result["excluded"][0]["reason"]
    assert result["status"] == "no newsletters verified"
    assert result["verdict"] == "not a fit"


def test_type_preference_narrows_the_list(scoring):
    result = scoring["plan"](
        [newsletter(), newsletter("Industry Digest", newsletter_type="industry_digest")],
        as_of=AS_OF,
        newsletter_type_preference="industry_digest",
    )
    assert [row["newsletter_type"] for row in result["shortlist"]] == ["industry_digest"]
    assert "tools_roundup was not requested" in result["excluded"][0]["reason"]


def test_already_pitched_matches_names_loosely(scoring):
    result = scoring["plan"](
        [newsletter("Console.dev Weekly")], as_of=AS_OF, already_pitched=["console dev weekly"]
    )
    assert result["shortlist"] == []
    assert result["excluded"][0]["reason"] == "named in already_pitched"


def test_a_newsletter_drafted_in_an_earlier_run_is_never_pitched_again(scoring):
    first = scoring["plan"](
        [newsletter(), newsletter("Industry Digest", newsletter_type="industry_digest")],
        as_of="2026-09-10",
        report_path="reports/a.md",
    )
    earlier = scoring["merge_states"](
        [scoring["read_state"](json.loads(json.dumps(first["state"])))]
    )
    second = scoring["plan"](
        [
            newsletter(),
            newsletter("Industry Digest", newsletter_type="industry_digest"),
            newsletter("New Roundup"),
        ],
        as_of=AS_OF,
        previous=earlier,
        report_path="reports/b.md",
    )
    assert [row["name"] for row in second["shortlist"]] == ["New Roundup"]
    reasons = {row["name"]: row["reason"] for row in second["excluded"]}
    assert reasons["Example Tools Weekly"] == "pitch drafted on 2026-09-10 in reports/a.md"
    assert reasons["Industry Digest"] == "pitch drafted on 2026-09-10 in reports/a.md"
    # Memory is cumulative, so the next run only needs the newest block to remember all three.
    assert set(second["state"]["newsletters"]) == {
        "example tools weekly",
        "industry digest",
        "new roundup",
    }
    assert second["state"]["newsletters"]["new roundup"]["report"] == "reports/b.md"


def test_merge_keeps_the_first_report_that_drafted_a_newsletter(scoring):
    item = {"name": "Example Tools Weekly", "newsletter_type": "tools_roundup"}
    older = {
        "version": 1,
        "newsletters": {"k": {**item, "first_drafted": "2026-01-01", "report": "a"}},
    }
    newer = {
        "version": 1,
        "newsletters": {"k": {**item, "first_drafted": "2026-05-01", "report": "b"}},
    }
    merged = scoring["merge_states"]([newer, None, older])
    assert merged["newsletters"]["k"]["report"] == "a"


def test_hard_no_on_cold_email_drops_newsletters_without_a_stated_intake(scoring):
    newsletters = [
        newsletter("Form Digest"),
        newsletter("Published Address Digest", route="published_address"),
        newsletter("Writer Inbox Digest", route="direct_message"),
    ]
    allowed = scoring["plan"](newsletters, as_of=AS_OF)
    assert len(allowed["shortlist"]) == 3
    ruled = scoring["plan"](newsletters, as_of=AS_OF, hard_nos=["no_cold_email"])
    assert [row["name"] for row in ruled["shortlist"]] == [
        "Form Digest",
        "Published Address Digest",
    ]
    assert ruled["excluded"][0]["reason"].startswith("hard no: no cold email")


def test_the_cap_is_respected_and_overflow_stays_eligible_next_run(scoring):
    newsletters = [newsletter(f"Roundup {n}") for n in range(5)]
    result = scoring["plan"](newsletters, as_of=AS_OF, max_newsletters=3, report_path="r.md")
    assert len(result["shortlist"]) == 3
    overflow = [row for row in result["excluded"] if "over max_newsletters" in row["reason"]]
    assert len(overflow) == 2
    assert len(result["state"]["newsletters"]) == 3


def test_duplicates_are_counted_once(scoring):
    result = scoring["plan"]([newsletter(), newsletter("Example  Tools-Weekly")], as_of=AS_OF)
    assert len(result["shortlist"]) == 1
    assert result["duplicates_dropped"] == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "  "},
        {"url": "exampletools.example.com/suggest"},
        {"newsletter_type": "webinar"},
        {"open_verified": "yes"},
        {"route": "guessed_email"},
        {"fit": 4},
        {"fit": True},
    ],
)
def test_plausible_but_unusable_records_are_rejected(scoring, overrides):
    with pytest.raises(ValueError):
        scoring["plan"]([newsletter(**overrides)], as_of=AS_OF)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_newsletters": 2}, "max_newsletters"),
        ({"max_newsletters": 21}, "max_newsletters"),
        ({"newsletter_type_preference": "keynote"}, "newsletter_type_preference"),
        ({"hard_nos": ["no_podcasts"]}, "hard no"),
    ],
)
def test_run_inputs_outside_the_contract_are_rejected(scoring, kwargs, message):
    with pytest.raises(ValueError, match=message):
        scoring["plan"]([newsletter()], as_of=AS_OF, **kwargs)


@pytest.mark.parametrize(
    "state",
    [
        None,
        [],
        {"version": 2, "newsletters": {}},
        {"version": 1, "newsletters": []},
        {"version": 1, "newsletters": {"k": "drafted"}},
        {"version": 1, "newsletters": {"k": {"name": "X", "newsletter_type": "webinar"}}},
        {
            "version": 1,
            "newsletters": {
                "k": {
                    "name": "X",
                    "newsletter_type": "tools_roundup",
                    "first_drafted": "soon",
                    "report": "r",
                }
            },
        },
        {
            "version": 1,
            "newsletters": {
                "k": {
                    "name": "X",
                    "newsletter_type": "tools_roundup",
                    "first_drafted": "2026-01-01",
                }
            },
        },
    ],
)
def test_untrusted_earlier_state_is_discarded(scoring, state):
    assert scoring["read_state"](state) is None
