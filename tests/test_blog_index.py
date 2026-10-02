"""content.blog_index: a read-only planner, its PLAN.md contract and the founder question."""

import json
import re
from pathlib import Path

import pytest

from tin_lite import blog_index_plan as plan
from tin_lite.page_routes import ask_the_founder
from tin_lite.procedures import (
    PinnedCodexProcedure,
    validate_codex_procedure_definition,
    validate_procedure_artifact,
)
from tin_lite.workflow_packages import decode_workflow_source
from tin_lite.workflow_qualification import Qualification

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/content.blog_index"
PATH = "workflow_packages/content.blog_index/workflow.json"
# The definition as Tin stores it: decoded from the package, its procedure checked.
DEFINITION = decode_workflow_source((ROOT / PATH).read_bytes(), definition_path=PATH).definition
SHA = "a" * 40


def patch(**changes):
    value = {
        "schema": "blog-index-patch/1",
        "repository": "northpine/site",
        "base_ref": "main",
        "base_sha": SHA,
        "route": "/blog",
        "summary": "An index at /blog from content/blog, with an RSS feed and post dates.",
        "files": [
            {
                "path": "src/pages/blog/index.astro",
                "action": "create",
                "content": "---\nconst posts = await getCollection('blog');\n---\n",
            },
            {"path": "src/pages/rss.xml.ts", "action": "create", "content": "export const GET"},
        ],
        "caps": {"max_files": 5},
    }
    return {**value, **changes}


def document(value=None, *, outcome="plan", reason=None, fence=True, extra=""):
    body = json.dumps(patch() if value is None else value)
    if fence:
        body = f"```json\n{body}\n```"
    summary = {
        "version": 1,
        "outcome": outcome,
        "reason": reason,
        "route": "/blog/{slug}",
        "posts": 12,
        "within_two_clicks": 12,
    }
    return (
        "# Blog index plan\n\n## Outcome\n\nplan, auto, northpine/site at aaaa.\n\n"
        "## Patch\n\n<!-- blog-index-patch.json:start -->\n"
        f"{body}\n<!-- blog-index-patch.json:end -->\n\n{extra}"
        f"```json blog-index\n{json.dumps(summary)}\n```\n"
    )


def no_change(reason, **changes):
    return document(patch(files=[], **changes), outcome="no_change", reason=reason)


def ask_block():
    text = (PACKAGE / "skills/blog-index/PLAN.md").read_text()
    return re.search(r"```json ask-the-founder\n.*?\n```", text, re.S).group(0)


def pinned():
    spec = validate_codex_procedure_definition(DEFINITION)
    return PinnedCodexProcedure(
        workflow_key=DEFINITION["key"],
        prompt="Plan",
        entry_skill="blog-index",
        skill_files={},
        workspace_kind=spec.workspace_kind,
        output_validator=spec.output_validator,
        output_max_bytes=spec.output_max_bytes,
    )


def test_it_plans_from_a_read_only_repository_and_opens_no_pull_request():
    spec = validate_codex_procedure_definition(DEFINITION)
    assert spec.result_kind == "project.artifact"
    assert spec.output_path_template == "reports/blog-index/{run_id}/PLAN.md"
    assert spec.output_validator == plan.VALIDATOR and spec.output_media_type == "text/markdown"
    assert spec.workspace_kind == "github.repository"
    assert spec.workspace_capabilities == ("contents.read",)
    github = next(
        r for r in DEFINITION["integration_requirements"] if r["provider_key"] == "infra.github"
    )
    assert github["capabilities"] == ["contents.read"]  # no write, no pull requests
    procedure = DEFINITION["procedure"]
    assert procedure["output"]["kind"] != "github.pull_request"
    assert "verification" not in procedure and "receipt_path_template" not in json.dumps(procedure)
    assert "Opens no pull request" in DEFINITION["description"]


def test_only_content_blog_index_may_write_a_blog_index_plan():
    other = json.loads(
        json.dumps(DEFINITION).replace("procedures/content.blog_index/", "procedures/custom.x/")
    )
    other["key"] = "custom.x"
    with pytest.raises(ValueError, match="content.blog_index"):
        validate_codex_procedure_definition(other)
    moved = json.loads(json.dumps(DEFINITION))
    moved["procedure"]["output"]["path_template"] = "reports/blog-index/{run_id}.md"
    with pytest.raises(ValueError, match="PLAN.md"):
        validate_codex_procedure_definition(moved)


def test_a_plan_with_files_passes_the_pinned_validator():
    found = plan.parse(document())
    assert found["patch"]["files"][0]["path"] == "src/pages/blog/index.astro"
    assert found["summary"]["outcome"] == "plan"
    validate_procedure_artifact(document().encode(), spec=pinned())
    # The block may also be bare JSON between the markers.
    assert plan.parse(document(fence=False))["patch"]["base_sha"] == SHA


def test_no_change_plans_name_their_reason_and_carry_no_files():
    assert plan.parse(no_change("check_only"))["patch"]["files"] == []
    with pytest.raises(ValueError, match="names its reason"):
        plan.parse(no_change("bored"))
    with pytest.raises(ValueError, match="no files"):
        plan.parse(document(outcome="no_change", reason="check_only"))
    with pytest.raises(ValueError, match="carries files"):
        plan.parse(document(patch(files=[])))


def test_without_a_route_for_articles_the_plan_asks_the_founder():
    without = document(patch(files=[], route=""), outcome="no_change", reason="no_article_route")
    with pytest.raises(ValueError, match="asks the founder"):
        plan.parse(without)
    asked = document(
        patch(files=[], route=""),
        outcome="no_change",
        reason="no_article_route",
        extra=ask_block() + "\n\n",
    )
    assert plan.parse(asked)["summary"]["reason"] == "no_article_route"
    with pytest.raises(ValueError, match="names the index route"):
        plan.parse(document(patch(route="")))


@pytest.mark.parametrize(
    ("files", "error"),
    [
        (
            [{"path": f"src/f{i}.ts", "action": "create", "content": "x"} for i in range(6)],
            "at most 5",
        ),
        ([{"path": "package.json", "action": "update", "content": "{}"}], "package.json"),
        ([{"path": "web/pnpm-lock.yaml", "action": "update", "content": "x"}], "pnpm-lock.yaml"),
        ([{"path": ".github/workflows/ci.yml", "action": "update", "content": "x"}], "CI"),
        ([{"path": "vercel.json", "action": "update", "content": "{}"}], "deploy"),
        ([{"path": "../outside.ts", "action": "create", "content": "x"}], "leave the repository"),
        ([{"path": "/etc/passwd", "action": "update", "content": "x"}], "relative"),
        ([{"path": "a.ts", "action": "delete", "content": "x"}], "create or update"),
        ([{"path": "a.ts", "action": "create", "content": ""}], "full text"),
        (
            [{"path": "a.ts", "action": "create", "content": "x"}] * 2,
            "twice",
        ),
        ([{"path": "a.ts", "action": "create", "content": "x" * 120_001}], "120000 bytes"),
        ([{"path": "a.ts", "action": "create"}], "path, action and content"),
    ],
)
def test_the_patch_refuses_what_website_change_must_never_write(files, error):
    with pytest.raises(ValueError, match=error):
        plan.parse(document(patch(files=files)))


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"schema": "blog-index-patch/2"}, "schema"),
        ({"repository": "northpine"}, "owner/repo"),
        ({"base_sha": "HEAD"}, "base_sha"),
        ({"base_ref": "main branch"}, "base_ref"),
        ({"route": "/blog/{slug}"}, "route"),
        ({"caps": {"max_files": 9}}, "caps"),
        ({"extra": True}, "exactly the blog-index-patch/1 keys"),
    ],
)
def test_the_patch_keeps_the_shared_contract(changes, error):
    with pytest.raises(ValueError, match=error):
        plan.parse(document(patch(**changes)))


def test_a_plan_without_its_blocks_or_report_is_refused():
    with pytest.raises(ValueError, match="exactly one"):
        plan.parse(document().replace("<!-- blog-index-patch.json:end -->", ""))
    with pytest.raises(ValueError, match="summary block"):
        plan.parse(document().split("```json blog-index")[0])
    with pytest.raises(ValueError, match="Outcome"):
        plan.parse(document().replace("## Outcome", "## Result"))
    with pytest.raises(ValueError, match="not JSON"):
        plan.parse(document().replace('"schema"', "schema"))


def test_the_founder_question_matches_the_page_route_question():
    text = (PACKAGE / "skills/blog-index/PLAN.md").read_text()
    block = json.loads(re.search(r"```json ask-the-founder\n(.*?)\n```", text, re.S).group(1))
    expected = ask_the_founder("article", None)
    assert block["question"] == expected["question"]
    assert block["suggestion"] == expected["suggestion"]
    assert block["then"] == expected["then"]
    assert "answers" not in block["how_to_suggest"].replace("never a Tin term", "")


def test_the_skill_reads_the_saved_route_and_never_types_a_post_list():
    skill = (PACKAGE / "skills/blog-index/SKILL.md").read_text()
    assert "content/page-routes.json" in skill
    assert "never a typed" in skill.replace("\n   ", " ")
    assert "lifecycle." not in skill
    prompt = (PACKAGE / "PROMPT.md").read_text()
    assert "read-only" in prompt and "Open no pull request" in prompt
    assert "head_sha" in prompt


def test_the_template_names_every_patch_key():
    text = (PACKAGE / "skills/blog-index/PLAN.md").read_text()
    template = re.search(
        r"<!-- blog-index-patch.json:start -->\n```json\n(.*?)\n```", text, re.S
    ).group(1)
    for key in sorted(plan.KEYS):
        assert f'"{key}"' in template
    assert plan.START in text and plan.END in text


def test_qualification_cases_include_the_missing_route_and_a_truncated_read():
    raw = (ROOT / "workflow_evals/content.blog_index/qualification.json").read_text()
    cases = {case.id: case for case in Qualification.model_validate_json(raw).cases}
    assert "Where on your site should articles go?" in cases["no_article_route"].expect.contains
    assert "unknown" in cases["under_100_with_truncated_search"].expect.contains
    assert plan.START in cases["ordinary_build"].expect.contains
    assert all("RESULT.md" not in json.dumps(case.model_dump()) for case in cases.values())
