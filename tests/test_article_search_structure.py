"""Planned blog posts get the answer-page search structure as guidance, without its code checks."""

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.content_delivery import document_body, page_frontmatter

BODY = "# Which runner keeps AI work reliable?\n\n" + "A durable runner saves run state. " * 12


def workflow(key):
    return next(w for w in BUILTIN_WORKFLOWS if w.key == key)


def test_delivery_merges_a_search_listing_into_one_site_header():
    listing = (
        '---\nmeta_title: "Which runner keeps AI work reliable?"\n'
        'meta_description: "A durable runner keeps recurring AI work reliable: it saves run '
        'state and retries safely."\n---\n\n'
    )
    # The Markdown publisher splits the listing off a document and keeps its title.
    article, title = document_body((listing + BODY).encode())
    metadata, copy = page_frontmatter(article)
    assert title == "Which runner keeps AI work reliable?"
    assert set(metadata) == {"meta_title", "meta_description"}
    assert copy.startswith("# Which runner")
    assert page_frontmatter(BODY) == ({}, BODY)  # The listing stays optional.


def test_planned_drafts_pin_the_search_structure_guidance():
    skill = "search-and-answer-engines"
    spec = workflow("content.generate")
    assert spec.version_label == "1.17.0"
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


def test_planned_drafts_do_not_carry_the_listing():
    planned = (
        workflow("content.generate").procedure.root / "skills/search-and-answer-engines/SKILL.md"
    ).read_text()
    # Planned drafts keep a title-first file: their delivery writes the site header.
    assert "meta_title" not in planned and "no frontmatter" in planned.replace("\n   ", " ")
    assert "keep the destination's structure" in planned
