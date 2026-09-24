"""Dry-run the Reddit pain-point workflow with synthetic Reddit responses."""

import json
import py_compile
import runpy
from pathlib import Path


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages" / "reddit_pain_point_interceptor"
ENTRYPOINT = PACKAGE / "main.py"


def reddit_response(*posts):
    return json.dumps({"data": {"children": [{"data": post} for post in posts]}})


def reddit_post(number, title, body, score, comments):
    return {
        "title": title,
        "selftext": body,
        "permalink": f"/r/startups/comments/{number}/thread_{number}/",
        "subreddit": "startups",
        "score": score,
        "num_comments": comments,
    }


def main():
    py_compile.compile(str(ENTRYPOINT), doraise=True)
    module = runpy.run_path(str(ENTRYPOINT))
    run = module["run"]
    inputs = {
        "keywords": ["competitor research", "customer interviews"],
        "reddit_search_json": [
            reddit_response(
                reddit_post(
                    101,
                    "Competitor research is taking forever",
                    "Our process is painful and hard to repeat.",
                    24,
                    9,
                ),
                reddit_post(
                    102,
                    "How do you compare competitors?",
                    "I need a practical research workflow.",
                    18,
                    5,
                ),
            ),
            reddit_response(
                reddit_post(
                    103,
                    "How do you run customer interviews?",
                    "I struggle to get useful answers.",
                    40,
                    12,
                )
            ),
        ],
    }

    result = run(None, inputs)
    output = result["content"]
    assert result["path"] == "reports/REDDIT_PAIN_POINT_INTERCEPTOR.md"
    assert output.count("### Draft reply") == 3
    assert output.count("](" + "https://www.reddit.com/") == 3
    assert "Traceback" not in output

    print(output)
    print("Dry run passed: syntax, execution, three links, and three reply drafts verified.")


if __name__ == "__main__":
    main()