"""Reviewed decision resource and plan contract for qa.feedback_to_fix; no provider or model
calls in CI."""

import json
import re
from pathlib import Path

import pytest

from tin_lite import feedback_fix_plan

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/qa.feedback_to_fix"
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


def target(path="web/index.html", start=10, end=12):
    return {"path": path, "start": start, "end": end}


def theme(theme_id="t1", authors=("a1", "a2"), **overrides):
    return {
        "id": theme_id,
        "kind": "unclear_what_it_is",
        "quotes": [quote(a) for a in authors],
        "targets": [target()],
        "supported": True,
        **overrides,
    }


def test_two_different_people_on_editable_copy_produce_a_patch(decide):
    assert decide([theme()]) == {
        "outcome": "patch",
        "theme": "t1",
        "files": ["web/index.html"],
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
    nine = theme("t9", targets=[target("web/a.html")])
    three = theme("t3", targets=[target("web/b.html")])
    assert decide([nine, three])["theme"] == "t3"


def test_a_bigger_theme_that_cannot_be_patched_does_not_block_a_smaller_one(decide):
    product = theme("t1", authors=("a1", "a2", "a3"), kind="product_change")
    copy = theme("t2", authors=("a4", "a5"))
    decided = decide([product, copy])
    assert (decided["outcome"], decided["theme"], decided["reported"]) == ("patch", "t2", ["t1"])


def test_two_one_person_themes_on_the_same_copy_become_one_theme_of_two_people(decide):
    first = theme("t1", authors=("a1",))
    second = theme("t2", authors=("a2",), targets=[target(start=12, end=14)])
    decided = decide([first, second])
    assert (decided["outcome"], decided["theme"], decided["reported"]) == ("patch", "t1", [])


def test_different_points_in_one_landing_page_file_are_not_one_theme(decide):
    # Most small sites keep the whole landing page in one file: the hero and the pricing note
    # further down are different copy, so one voice each is still one voice each.
    hero = theme("t1", authors=("a1",), targets=[target("app/page.tsx", 10, 14)])
    pricing = theme("t2", authors=("a2",), targets=[target("app/page.tsx", 220, 226)])
    assert decide([hero, pricing])["outcome"] == "insufficient_data"


def test_the_same_person_in_two_themes_on_one_file_is_still_one_person(decide):
    decided = decide([theme("t1", authors=("a1",)), theme("t2", authors=("a1",))])
    assert decided["outcome"] == "insufficient_data"


@pytest.mark.parametrize(
    "other",
    [
        {"kind": "objection"},
        {"targets": [target("web/pricing.html")]},
        {"targets": [target(start=13, end=20)]},
        {"targets": []},
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
    first = theme("t1", authors=("a1",), targets=[target("a"), target("b")])
    second = theme("t2", authors=("a2",), targets=[target("b"), target("c"), target("d")])
    decided = decide([first, second])
    assert decided["outcome"] == "report_only"
    assert "more than 3 files" in decided["reason"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"kind": "product_change"}, "product change"),
        ({"supported": False}, "does not show"),
        ({"targets": []}, "no existing page copy"),
        ({"targets": [target(path) for path in "abcd"]}, "more than 3 files"),
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
        [theme(targets="web/index.html")],
        [theme(targets=["web/index.html"])],
        [theme(targets=[target(path="")])],
        [theme(targets=[target(start=0)])],
        [theme(targets=[target(start=5, end=4)])],
        [theme(targets=[{"path": "a", "start": "1", "end": 2}])],
        [theme(id="")],
        [theme("t1"), theme("t1")],
        [theme(quotes=[])],
        [theme(quotes=[{**quote("a1"), "url": ""}])],
        [theme(quotes=[{**quote("a1"), "url": "http://example.test/x"}])],
        [theme(quotes=[{**quote("a1"), "url": "https://"}])],
        [theme(quotes=[{**quote("a1"), "url": "https:///comments/abc"}])],
        [theme(quotes=[{**quote("a1"), "url": "https://."}])],
        [theme(quotes=[{**quote("a1"), "url": "https://exa mple.test/x"}])],
        [theme(quotes=[{**quote("a1"), "url": None}])],
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
    assert definition["key"] == PACKAGE.name == feedback_fix_plan.WORKFLOW_KEY
    assert definition["system"] == "product-qa"
    # The repository Tin keeps is the one selected on GitHub; the founder types none.
    assert set(definition["input_schema"]["properties"]) == {
        "project_id",
        "platforms",
        "lookback_days",
    }
    # It reads the repository and writes a plan; website.change writes the site.
    assert procedure["workspace"]["capabilities"] == ["contents.read"]
    assert definition["integration_requirements"][0]["capabilities"] == ["contents.read"]
    assert procedure["output"] == {
        "kind": "project.artifact",
        "path_template": feedback_fix_plan.PATH_TEMPLATE,
        "media_type": "text/markdown",
        "max_bytes": 200000,
        "validator": feedback_fix_plan.VALIDATOR,
    }


def test_the_skill_uses_the_resource_unchanged_and_the_prompt_keeps_its_promises():
    skill = " ".join((RESOURCES / "SKILL.md").read_text().split())
    prompt = " ".join((PACKAGE / "PROMPT.md").read_text().split())
    assert "Run `choose_action` from THEMES.md unchanged" in skill
    assert "`after` wording is already in the repository: it shipped" in skill
    assert "A plan without a patch excludes nothing" in skill
    assert "Record a one-person observation as its own theme" in skill
    assert "Put each point in its own theme" in skill
    assert "Do not merge people yourself" in skill
    assert "Open at most 30 threads" in skill
    plan = " ".join((RESOURCES / "PLAN.md").read_text().split())
    assert "`Seen once`" in plan
    assert "never by retyping a file" in plan
    for promise in (
        "list one-person comments under `Seen once` and never act on them",
        "open at most 30",
        "never write a username",
        "plain Markdown link",
        "no citation markers",
        "Never reply, post",
        "open a pull request, merge or deploy",
    ):
        assert promise in prompt


def test_the_plan_headings_are_the_ones_the_skill_promises():
    text = (RESOURCES / "PLAN.md").read_text()
    assert re.findall(r"^## (.+)$", text, re.M) == [
        "Outcome",
        "Themes",
        "Evidence",
        "The change",
        "Not patched",
        "Sources read",
        "Verification",
        "Not changed",
        "Patch",
    ]
    assert "reports/feedback-to-fix/{run_id}/PLAN.md" in text
    assert feedback_fix_plan.SCHEMA in text


# --- The plan contract website.change applies -------------------------------------------

PAGE = "export default function Home() {\n  return <h1>Type the books you read.</h1>;\n}\n"


def patch(**overrides):
    return {
        "schema": "feedback-fix-patch/1",
        "repository": "owner/site",
        "base_ref": "main",
        "base_sha": "9" * 40,
        "route": "/",
        "summary": "The hero says you type the book you are reading.",
        "kind": "unclear_what_it_is",
        "people": 2,
        "quotes": [
            "https://www.reddit.com/r/SideProject/comments/abc/x/c1/",
            "https://news.ycombinator.com/item?id=1",
        ],
        "edits": [
            {
                "path": "src/app/page.tsx",
                "before": "A reading typing gym.",
                "after": "Type the books you read.",
            }
        ],
        "files": [{"path": "src/app/page.tsx", "action": "update", "content": PAGE}],
        "caps": {"max_files": 3},
        **overrides,
    }


def plan(found=None, *, outcome="patch", reason=""):
    text = "# Copy fix\n\n## Outcome\n\n`" + outcome + "` for owner/site.\n\n"
    if found is not None:
        text += (
            "## Patch\n\n<!-- feedback-fix-patch.json:start -->\n```json\n"
            + json.dumps(found)
            + "\n```\n<!-- feedback-fix-patch.json:end -->\n\n"
        )
    summary = json.dumps({"outcome": outcome, "reason": reason})
    return text + f"```json feedback-to-fix\n{summary}\n```\n"


def test_a_patch_plan_parses_and_a_plan_without_one_names_its_reason():
    parsed = feedback_fix_plan.parse(plan(patch()))
    assert parsed["patch"]["files"][0]["path"] == "src/app/page.tsx"
    empty = feedback_fix_plan.parse(
        plan(outcome="insufficient_data", reason="no theme has 2 different people")
    )
    assert empty["patch"] is None and empty["summary"]["outcome"] == "insufficient_data"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (plan(), "exactly one feedback-fix-patch.json"),
        (plan(outcome="report_only"), "names its reason"),
        (plan(patch(), outcome="report_only", reason="x"), "Only a patch outcome"),
        (plan(patch(people=1)), "2 different people"),
        (plan(patch(kind="product_change")), "unclear product"),
        (plan(patch(quotes=["https://a.test/1"])), "2 to 30 quotes"),
        (plan(patch(quotes=["http://a.test/1", "https://b.test/2"])), "2 to 30 quotes"),
        (plan(patch(files=[{**patch()["files"][0], "action": "create"}])), "never creates"),
        (
            plan(patch(files=[{**patch()["files"][0], "path": f"p{i}.tsx"} for i in range(4)])),
            "one to 3 files",
        ),
        (
            plan(patch(files=[{"path": "package.json", "action": "update", "content": "{}"}])),
            "must not change package.json",
        ),
        (
            plan(patch(edits=[{**patch()["edits"][0], "after": "Not in the file."}])),
            "not in src/app/page.tsx",
        ),
        (plan(patch(edits=[{**patch()["edits"][0], "path": "other.tsx"}])), "planned files"),
        (plan(patch(edits=[{**patch()["edits"][0], "before": "x" * 501}])), "at most 500"),
        (plan(patch(route="pricing")), "site path"),
        (plan(patch(), outcome="maybe"), "patch, report_only or insufficient_data"),
        ("# No outcome\n", "Outcome section"),
    ],
)
def test_an_unusable_plan_is_refused_and_says_why(text, match):
    with pytest.raises(ValueError, match=match):
        feedback_fix_plan.parse(text)


def test_a_reworded_fix_from_the_same_people_is_the_same_change():
    first = patch()
    reworded = patch(
        summary="Another wording.",
        edits=[{**patch()["edits"][0], "after": "Type the books"}],
    )
    assert feedback_fix_plan.quotes_digest(first) == feedback_fix_plan.quotes_digest(reworded)
    joined = patch(quotes=[*patch()["quotes"], "https://www.reddit.com/r/x/comments/d/y/"])
    assert feedback_fix_plan.quotes_digest(joined) != feedback_fix_plan.quotes_digest(first)
