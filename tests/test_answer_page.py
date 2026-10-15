"""The answer-format checks content.generate's answer drafts reuse.

content_draft.validate_answer checks the listing and the direct answer
(test_one_content_generate); these cover the remaining search-structure pieces and the page
header delivery writes with the search listing.
"""

from __future__ import annotations

import json

import pytest

from tin_lite.answer_page import answer_page_problems, search_structure_problems

FRONTMATTER = (
    '---\nmeta_title: "Which tools keep recurring AI work reliable?"\n'
    'meta_description: "Durable workflow runners keep recurring AI work reliable by saving '
    'run state outside the model and retrying safely."\n---\n\n'
)


def markdown_page() -> str:
    return (
        "# How to keep recurring AI work reliable\n\n"
        "Use a durable workflow runner that separates orchestration from product state.\n\n"
        "## What reliability requires\n\nPersist run state outside the model.\n\n"
        "## What to evaluate\n\nCheck retries, provenance, and readable outputs.\n\n"
        "## Sources\n\n- [Workflow guidance](https://example.com/workflows)\n"
    )


def structured_page(
    *,
    frontmatter: str = FRONTMATTER,
    updated: str = "Last updated: 2026-09-28\n\n",
    faq: str = (
        "## FAQ\n\n### Do I need a workflow runner?\n\nYes, once work repeats weekly.\n\n"
        "### Can a cron job do this?\n\nOnly for work without retries or review.\n\n"
    ),
) -> str:
    answer = (
        "Durable workflow runners keep recurring AI work reliable because they save each run's "
        "state outside the model, retry failed steps without repeating paid calls, and keep "
        "every output readable after the run ends. Compare runners by how they recover, how "
        "they record provenance and what a reviewer sees before anything ships."
    )
    return (
        f"{frontmatter}# Which tools keep recurring AI work reliable?\n\n{updated}{answer}\n\n"
        "## What does reliability require?\n\nRun state lives outside the model "
        "([Temporal docs](https://docs.temporal.io/workflows)).\n\n"
        "## How do the options compare?\n\n| Option | Recovery |\n| --- | --- |\n"
        "| Runner | Durable ([guide](https://example.com/runner)) |\n\n"
        f"{faq}"
        "## Sources\n\n- [Temporal docs](https://docs.temporal.io/workflows)\n"
        "- [Runner guide](https://example.com/runner)\n- [Review notes](https://example.org/review)\n"
    )


def test_a_search_structured_page_passes_every_check():
    assert answer_page_problems(structured_page().encode(), structured=True) == []
    body = structured_page().split("---\n\n", 1)[1].split("\n\n", 1)[1]
    assert search_structure_problems(body) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda page: page.replace("## FAQ", "## Questions"), "FAQ section"),
        (lambda page: page.replace("## How do the options compare?", "## Options"), "questions"),
        (
            lambda page: page.replace("- [Review notes](https://example.org/review)\n", ""),
            "3 sources",
        ),
        (
            lambda page: page.replace(
                "([Temporal docs](https://docs.temporal.io/workflows))", ""
            ).replace("Durable ([guide](https://example.com/runner))", "Durable"),
            "inline",
        ),
        (
            lambda page: (
                page.replace("Durable workflow runners keep", "Runners keep", 1).split(" because")[
                    0
                ]
                + ".\n\n## What does"
                + page.split("## What does", 1)[1]
            ),
            "direct answer",
        ),
        (
            lambda page: page.replace(
                "Run state lives outside the model", " ".join(["word"] * 160)
            ),
            "150 words",
        ),
        (lambda page: page.replace("## Sources", "## Sources and notes"), "Sources"),
        (lambda page: page.replace("Last updated: 2026-09-28\n\n", ""), "Last updated"),
        (lambda page: page.replace(FRONTMATTER, ""), "frontmatter"),
        (
            lambda page: page.replace(
                'Which tools keep recurring AI work reliable?"', "x" * 61 + '"'
            ),
            "meta_title",
        ),
    ],
)
def test_search_structure_checks_catch_missing_pieces(change, message):
    problems = answer_page_problems(change(structured_page()).encode(), structured=True)
    assert any(message in problem for problem in problems), problems


def test_pages_without_search_structure_keep_their_original_checks():
    assert answer_page_problems(markdown_page().encode()) == []
    assert answer_page_problems(b"") and answer_page_problems(b"\xff")
    assert answer_page_problems(b"No title\n") == [
        "answer page must begin with a Markdown title",
        "answer page must contain a Sources section",
        "answer page must contain at least three sections",
    ]


def test_delivery_writes_one_header_with_the_search_listing():
    from tin_lite.content_delivery import DeliverySettings, new_page_header, page_frontmatter

    metadata, article = page_frontmatter(structured_page())
    assert metadata["meta_title"] == "Which tools keep recurring AI work reliable?"
    assert json.dumps(metadata["meta_description"]).startswith('"Durable workflow runners')
    assert article.startswith("# Which tools keep recurring AI work reliable?")

    plain = DeliverySettings.model_construct(frontmatter={})
    configured = DeliverySettings.model_construct(frontmatter={"title": "{title}"})
    only_page = new_page_header(plain, "Title", "2026-09-28", "slug", metadata)
    merged = new_page_header(configured, "Title", "2026-09-28", "slug", metadata)
    assert only_page.count("---\n") == 2 and "meta_description:" in only_page
    assert merged.count("---\n") == 2 and "title: Title" in merged and "meta_title:" in merged
    assert new_page_header(plain, "Title", "2026-09-28", "slug") == ""
    assert page_frontmatter("# No frontmatter\n") == ({}, "# No frontmatter\n")
