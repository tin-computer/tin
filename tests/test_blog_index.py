"""content.blog_index: the route contract and the founder question, checked offline."""

import json
import re
from pathlib import Path

from tin_lite.page_routes import ask_the_founder
from tin_lite.workflow_qualification import Qualification

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/content.blog_index"


def test_it_opens_one_small_pull_request_or_explains_why_not():
    procedure = json.loads((PACKAGE / "workflow.json").read_text())["definition"]["procedure"]
    output = procedure["output"]
    assert output["kind"] == "github.pull_request" and output["allow_no_change"] is True
    assert output["max_files"] == 5
    assert output["receipt_path_template"] == "reports/blog-index/{run_id}/RESULT.md"


def test_the_founder_question_matches_the_page_route_question():
    text = (PACKAGE / "skills/blog-index/RESULT.md").read_text()
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


def test_qualification_cases_include_the_missing_route_and_a_truncated_read():
    raw = (ROOT / "workflow_evals/content.blog_index/qualification.json").read_text()
    cases = {case.id: case for case in Qualification.model_validate_json(raw).cases}
    assert "Where on your site should articles go?" in cases["no_article_route"].expect.contains
    assert "unknown" in cases["under_100_with_truncated_search"].expect.contains
