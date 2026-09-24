import json
import runpy
from pathlib import Path

import pytest

from tin_lite.workflow_code import validate_code_definition, validate_code_result


ROOT = Path(__file__).parents[1]


def package():
    package_root = ROOT / "workflow_packages" / "reddit_pain_point_interceptor"
    definition = json.loads((package_root / "workflow.json").read_text())["definition"]
    return runpy.run_path(str(package_root / "main.py")), definition


def response(*posts):
    return json.dumps({"data": {"children": [{"data": post} for post in posts]}})


def post(number, title, body, score, comments):
    return {
        "title": title,
        "selftext": body,
        "permalink": f"/r/startups/comments/{number}/thread_{number}/",
        "subreddit": "startups",
        "score": score,
        "num_comments": comments,
    }


def test_ranks_three_unique_threads_and_renders_non_salesy_drafts():
    module, definition = package()
    result = module["run"](
        None,
        {
            "keywords": ["competitor research", "customer interviews"],
            "reddit_search_json": [
                response(
                    post(1, "Competitor research is taking forever", "Our process is painful", 20, 8),
                    post(2, "A useful competitor research workflow", "We compare tools weekly", 5, 2),
                ),
                response(
                    post(3, "How do you run customer interviews?", "I struggle to get useful answers", 40, 12),
                    post(1, "Duplicate result", "Our process is painful", 999, 999),
                ),
            ],
        },
    )
    assert result["path"] == "reports/REDDIT_PAIN_POINT_INTERCEPTOR.md"
    assert result["content"].count("### Draft reply") == 3
    assert result["content"].count("](" + "https://www.reddit.com/r/startups/comments/") == 3
    assert "buy" not in result["content"].lower()
    validate_code_result(json.dumps(result).encode(), validate_code_definition(definition))


def test_rejects_invalid_json_and_insufficient_relevant_threads():
    module, _ = package()
    base = {"keywords": ["analytics"], "reddit_search_json": ["not json"]}
    with pytest.raises(ValueError, match="valid JSON"):
        module["run"](None, base)

    with pytest.raises(ValueError, match="at least three"):
        module["run"](
            None,
            {
                "keywords": ["analytics"],
                "reddit_search_json": [
                    response(post(1, "Analytics dashboard", "The numbers are confusing", 2, 1))
                ],
            },
        )