"""Blog posts get the answer-page search structure as guidance, without its code checks."""

import pytest

from tin_lite.article_review import search_listing, validate_article
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.content_delivery import document_body, page_frontmatter

BODY = "# Which runner keeps AI work reliable?\n\n" + "A durable runner saves run state. " * 12


def workflow(key):
    return next(w for w in BUILTIN_WORKFLOWS if w.key == key)


def test_public_article_may_start_with_its_search_listing():
    listing = (
        '---\nmeta_title: "Which runner keeps AI work reliable?"\n'
        'meta_description: "A durable runner keeps recurring AI work reliable: it saves run '
        'state and retries safely."\n---\n\n'
    )
    validate_article((listing + BODY).encode())
    validate_article(BODY.encode())  # The listing stays optional.
    # Delivery merges the same listing into one site header and keeps the title.
    article, title = document_body((listing + BODY).encode())
    metadata, copy = page_frontmatter(article)
    assert title == "Which runner keeps AI work reliable?"
    assert set(metadata) == {"meta_title", "meta_description"}
    assert copy.startswith("# Which runner")
    assert search_listing(listing + BODY)[0] == metadata


@pytest.mark.parametrize(
    "listing",
    [
        "---\nmeta_title: Runners: a guide\n---\n\n",  # An unquoted colon is not YAML.
        '---\nmeta_title: "Title"\nauthor: "Someone"\n---\n\n',
        "---\nmeta_title:\n  - one\n---\n\n",
        '---\nmeta_title: ""\n---\n\n',
    ],
)
def test_a_listing_delivery_could_not_merge_is_refused(listing):
    with pytest.raises(ValueError, match="search listing"):
        validate_article((listing + BODY).encode())


def test_listing_does_not_excuse_a_missing_title():
    with pytest.raises(ValueError, match="title"):
        validate_article(('---\nmeta_title: "Title"\n---\n\n' + BODY[2:]).encode())


@pytest.mark.parametrize(
    ("key", "version", "skill"),
    [
        ("content.public_article", "1.5.0", "search-and-answer-engines"),
        ("content.generate", "1.7.0", "search-and-answer-engines"),
    ],
)
def test_blog_workflows_pin_the_search_structure_guidance(key, version, skill):
    spec = workflow(key)
    assert spec.version_label == version
    _, files = spec.definition_and_resource_files()
    text = next(
        content.decode()
        for path, content in files.items()
        if path.endswith(f"/skills/{skill}/SKILL.md")
    )
    for rule in ("40 to 60 words", "## FAQ", "## Sources", "question mark", "Readability"):
        assert rule in text
    # The answer-page code checks and their markers stay with answer pages.
    assert "ANSWER_SEO_V1" not in text and "Last updated" not in text
    prompt = (spec.procedure.root / "PROMPT.md").read_text()
    assert "search-and-answer-engines" in prompt


def test_public_articles_carry_the_listing_and_planned_drafts_do_not():
    article = (
        workflow("content.public_article").procedure.root
        / "skills/search-and-answer-engines/SKILL.md"
    ).read_text()
    planned = (
        workflow("content.generate").procedure.root / "skills/search-and-answer-engines/SKILL.md"
    ).read_text()
    assert "meta_title" in article and "meta_description" in article
    # Planned drafts keep a title-first file: their delivery writes the site header.
    assert "meta_title" not in planned and "no frontmatter" in planned.replace("\n   ", " ")
    assert "keep the destination's structure" in planned
