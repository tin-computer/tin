"""Reviewed decision resource for growth.feedback_to_fix; no provider or model calls in CI."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/growth.feedback_to_fix"
RESOURCES = PACKAGE / "skills/feedback-to-fix"


def resource(name):
    path = RESOURCES / name
    blocks = re.findall(r"```python\n(.*?)\n```", path.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 1
    namespace = {}
    # Only the fixed repository resource executes here, never repository or model text.
    exec(compile(blocks[0], str(path), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture
def decide():
    return resource("THEMES.md")["choose_action"]


def quote(author, n=1):
    return {
        "author": author,
        "url": f"https://www.reddit.com/r/example/comments/abc/post/c{author}{n}/",
        "text": "the first screen is confusing, too many options",
    }


def theme(theme_id="t1", authors=("a1", "a2"), **overrides):
    return {
        "id": theme_id,
        "kind": "unclear_what_it_is",
        "quotes": [quote(a) for a in authors],
        "copy_files": ["web/index.html"],
        "supported": True,
        **overrides,
    }


def test_two_different_people_on_editable_copy_produce_a_patch(decide):
    assert decide([theme()]) == {
        "outcome": "patch",
        "theme": "t1",
        "reported": [],
        "reason": "",
    }


def test_one_person_saying_it_three_times_is_not_a_theme(decide):
    repeated = theme(authors=("a1", "a1", "a1"))
    decided = decide([repeated])
    assert decided["outcome"] == "insufficient_data"
    assert decided["theme"] is None


def test_no_themes_is_an_honest_insufficient_data_result(decide):
    decided = decide([])
    assert decided["outcome"] == "insufficient_data"
    assert "2 different people" in decided["reason"]


def test_the_theme_with_most_people_wins_and_the_rest_are_reported(decide):
    small = theme("t1", authors=("a1", "a2"))
    large = theme("t2", authors=("a1", "a2", "a3"), kind="objection")
    decided = decide([small, large])
    assert (decided["outcome"], decided["theme"], decided["reported"]) == ("patch", "t2", ["t1"])


def test_ties_follow_the_kind_order_then_the_id(decide):
    question = theme("t1", kind="unanswered_question")
    unclear = theme("t2", kind="unclear_what_it_is")
    assert decide([question, unclear])["theme"] == "t2"
    nine = theme("t9", copy_files=["web/a.html"])
    three = theme("t3", copy_files=["web/b.html"])
    assert decide([nine, three])["theme"] == "t3"


def test_a_bigger_theme_that_cannot_be_patched_does_not_block_a_smaller_one(decide):
    product = theme("t1", authors=("a1", "a2", "a3"), kind="product_change")
    copy = theme("t2", authors=("a4", "a5"))
    decided = decide([product, copy])
    assert (decided["outcome"], decided["theme"], decided["reported"]) == ("patch", "t2", ["t1"])


def test_two_one_person_themes_on_the_same_copy_become_one_theme_of_two_people(decide):
    first = theme("t1", authors=("a1",))
    second = theme("t2", authors=("a2",))
    decided = decide([first, second])
    assert (decided["outcome"], decided["theme"], decided["reported"]) == ("patch", "t1", [])


def test_the_same_person_in_two_themes_on_one_file_is_still_one_person(decide):
    decided = decide([theme("t1", authors=("a1",)), theme("t2", authors=("a1",))])
    assert decided["outcome"] == "insufficient_data"


@pytest.mark.parametrize(
    "other",
    [
        {"kind": "objection"},
        {"copy_files": ["web/pricing.html"]},
        {"copy_files": []},
        {"kind": "product_change"},
    ],
)
def test_themes_are_not_merged_across_kinds_files_or_product_changes(decide, other):
    first = theme("t1", authors=("a1",))
    second = theme("t2", authors=("a2",), **other)
    assert decide([first, second])["outcome"] == "insufficient_data"


def test_a_merged_theme_is_supported_only_when_every_part_is(decide):
    first = theme("t1", authors=("a1",))
    second = theme("t2", authors=("a2",), supported=False)
    decided = decide([first, second])
    assert decided["outcome"] == "report_only"
    assert "does not show" in decided["reason"]


def test_merged_copy_files_still_respect_the_file_budget(decide):
    first = theme("t1", authors=("a1",), copy_files=["a", "b"])
    second = theme("t2", authors=("a2",), copy_files=["b", "c", "d"])
    decided = decide([first, second])
    assert decided["outcome"] == "report_only"
    assert "more than 3 files" in decided["reason"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"kind": "product_change"}, "product change"),
        ({"supported": False}, "does not show"),
        ({"copy_files": []}, "no existing page copy"),
        ({"copy_files": ["a", "b", "c", "d"]}, "more than 3 files"),
    ],
)
def test_a_real_theme_copy_cannot_fix_is_reported_not_patched(decide, overrides, reason):
    decided = decide([theme(**overrides)])
    assert decided["outcome"] == "report_only"
    assert decided["theme"] is None and decided["reported"] == ["t1"]
    assert reason in decided["reason"]


@pytest.mark.parametrize(
    "broken",
    [
        None,
        [None],
        [theme(kind="praise")],
        [theme(supported="yes")],
        [theme(copy_files="web/index.html")],
        [theme(copy_files=[""])],
        [theme(id="")],
        [theme("t1"), theme("t1")],
        [theme(quotes=[])],
        [theme(quotes=[{**quote("a1"), "url": ""}])],
        [theme(quotes=[{**quote("a1"), "url": "http://example.test/x"}])],
        [theme(quotes=[{**quote("a1"), "text": "   "}])],
        [theme(quotes=[{**quote("a1"), "text": "x" * 401}])],
        [theme(quotes=[{**quote("a1"), "author": ""}])],
        [theme(quotes=["they said it was confusing"])],
    ],
)
def test_unusable_facts_are_rejected_rather_than_guessed(decide, broken):
    with pytest.raises(ValueError):
        decide(broken)


def test_manifest_declares_every_shipped_resource():
    definition = json.loads((PACKAGE / "workflow.json").read_text())["definition"]
    procedure = definition["procedure"]
    shipped = sorted(
        str(path.relative_to(PACKAGE)) for path in (PACKAGE / "skills").rglob("*") if path.is_file()
    )
    assert sorted(procedure["skill_files"]) == shipped
    assert definition["key"] == PACKAGE.name
    assert procedure["output"]["kind"] == "github.pull_request"
    assert procedure["output"]["allow_no_change"] is True
    assert procedure["output"]["max_files"] == 3


def test_the_skill_uses_the_resource_unchanged_and_the_prompt_keeps_its_promises():
    skill = " ".join((RESOURCES / "SKILL.md").read_text().split())
    prompt = " ".join((PACKAGE / "PROMPT.md").read_text().split())
    assert "Run `choose_action` from THEMES.md unchanged" in skill
    assert "A quote from a report that changed nothing counts again" in skill
    assert "Put each point in its own theme" in skill
    assert "Do not merge people yourself" in skill
    result = " ".join((RESOURCES / "RESULT.md").read_text().split())
    assert "empty unless the outcome is `patch`" in result
    assert "`Seen once`" in result
    for promise in (
        "never write a username",
        "plain Markdown link",
        "no citation markers",
        "Never reply, post",
        "merge or deploy",
    ):
        assert promise in prompt


def test_the_result_headings_are_the_ones_the_skill_promises():
    text = (RESOURCES / "RESULT.md").read_text()
    assert re.findall(r"^## (.+)$", text, re.M) == [
        "Outcome",
        "Themes",
        "Evidence",
        "The change",
        "Not patched",
        "Sources read",
        "Verification",
        "Not changed",
    ]
    assert "reports/feedback-to-fix/{run_id}/RESULT.md" in text
    assert "tin-feedback-to-fix" in text
