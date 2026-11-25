"""The client analyst writes its report once, in short parts, and never pays for it twice.

Production 2026-11-25, Databasin run 96e9f7c2 (analyst dc4ef038): $6.73 for the analyst alone.
Its first report ran 11,070 words against "about 5,000 to 6,000", so it measured the file,
reread parts of it and wrote the whole report twice more (27.6k, 23.4k and 21.8k output tokens).
Each of those writes was one turn of three to four and a half minutes, and Gemini's implicit
cache lapsed after four of them: five requests above 175k tokens paid full input price, about
$3.50 together. These tests keep the instructions that prevent it in the pinned package.
"""

from __future__ import annotations

import re

from tin_lite.catalog import BUILTIN_WORKFLOWS


def client_judge_files() -> tuple[dict, dict[str, str]]:
    found = next(b for b in BUILTIN_WORKFLOWS if b.key == "intel.client_judge")
    definition, files = found.definition_and_resource_files()
    texts = {
        path: content.decode() if isinstance(content, bytes) else content
        for path, content in files.items()
    }
    return definition, texts


def prompt_text() -> str:
    _definition, files = client_judge_files()
    return files["procedures/intel.client_judge/PROMPT.md"]


def skill_text() -> str:
    _definition, files = client_judge_files()
    return files["procedures/intel.client_judge/skills/client-intel/SKILL.md"]


def test_the_cost_fix_is_a_new_version_of_the_analyst():
    definition, _files = client_judge_files()
    assert definition["version"] == "1.1.0"


def test_every_report_section_has_a_word_plan_that_adds_up_to_the_report():
    """A plan per section, so the first draft lands near the length instead of at double it."""
    prompt = prompt_text()
    plans = re.findall(r"^\d\. \*\*([^*]+)\*\* \(about ([\d,]+) words", prompt, re.M)
    assert [name for name, _words in plans] == [
        "Summary",
        "Developments that matter",
        "Competitor and platform moves",
        "Buyer voice",
        "Content opportunities",
        "World hooks",
        "Watch",
        "Source notes",
    ]
    total = sum(int(words.replace(",", "")) for _name, words in plans)
    assert 5_000 <= total <= 6_000
    assert "About 5,500 words in all" in prompt


def test_the_report_is_written_once_in_parts_and_joined():
    prompt = prompt_text()
    for part in ("report-1.md", "report-2.md", "report-3.md", "report-4.md"):
        assert f"`{part}`" in prompt
    assert "one part per turn" in prompt
    assert "report-[1-4].md > <output path>" in prompt


def test_fixes_are_edits_and_length_is_never_a_reason_to_rewrite():
    prompt = prompt_text()
    assert "Never write a part or the whole report again to fix a few lines." in prompt
    assert "Don't rewrite or trim a complete report to hit the word counts." in prompt
    assert "Don't print or reread the" in prompt
    # The controller's generic "reread the deliverable" finishing note is answered here.
    assert "the count check after the join is that reread" in prompt


def test_reading_and_stories_stay_small_per_turn():
    prompt, skill = prompt_text(), skill_text()
    assert "Read each item once" in prompt
    assert "one or two per turn" in prompt
    assert "Read an item once" in skill
    assert "one or two per turn" in skill
    assert "link a source\n  the first time a section uses it" in skill


def test_the_rules_that_shape_the_report_and_brief_are_unchanged():
    """The cost fix touches how the report is written, not what it holds."""
    prompt, skill = prompt_text(), skill_text()
    assert "Use these eight sections" in prompt
    assert "Section 7 is binding on everything you write" in prompt
    assert "The brief reports; it never advises." in prompt
    assert "Three to five items, most important first." in skill
