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


def flat(text: str) -> str:
    """The text with its line wrapping undone, so a test doesn't pin where lines break."""
    return " ".join(text.split())


def test_the_analyst_is_on_its_current_version():
    definition, _files = client_judge_files()
    assert definition["version"] == "1.2.0"


def test_the_brief_carries_no_verify_notes():
    """Founders read the brief; notes on what to check belong in the report's Source notes."""
    prompt, skill = prompt_text(), skill_text()
    assert '"verify": true' not in prompt + skill
    assert "No meta comments" in prompt and "No meta comments" in skill


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


def test_the_report_is_written_once_one_section_per_turn_and_joined():
    """Prod 2026-11-26/27 traces: no cache miss in 174 requests after a turn under 60 s, and
    most turns over 90 s missed. The four-part report still had 65 to 130 s parts."""
    prompt = prompt_text()
    assert "one section per turn" in prompt
    for part in ("report-1.md", "report-8.md", "report-2a.md", "report-2b.md", "report-5a.md"):
        assert f"`{part}`" in prompt
    assert "cat <scratch folder>/report-*.md > <output path>" in prompt
    # The glob joins the parts in section order.
    parts = ["report-1.md", "report-2a.md", "report-2b.md", "report-3.md", "report-4.md"]
    parts += ["report-5a.md", "report-5b.md", "report-6.md", "report-7.md", "report-8.md"]
    assert sorted(reversed(parts)) == parts
    assert "more than about a minute usually loses the model's cache" in prompt


def test_fixes_are_edits_and_length_is_never_a_reason_to_rewrite():
    prompt = prompt_text()
    assert "Never write a part or the whole report again to fix a few lines." in prompt
    assert "Don't rewrite or trim a complete report to hit the word counts." in prompt
    assert "Don't print or reread the" in prompt
    # The controller's generic "reread the deliverable" finishing note is answered here.
    assert "the count check after the join is that reread" in prompt


def test_reading_and_stories_stay_small_per_turn():
    prompt, skill = flat(prompt_text()), flat(skill_text())
    assert "Read each item once" in prompt
    assert "at most three per turn" in prompt
    assert "Read an item once" in skill
    assert "at most three per turn" in skill
    assert "on its own `intel read` call" in prompt
    assert "read a primary source or any long item alone" in skill
    assert "link a source the first time a section uses it" in skill


def test_the_rules_that_shape_the_report_and_brief_are_unchanged():
    """The cost fix touches how the report is written, not what it holds."""
    prompt, skill = prompt_text(), skill_text()
    assert "Use these eight sections" in prompt
    assert "Section 7 is binding on everything you write" in prompt
    assert "The brief reports; it never advises." in prompt
    assert "One to five items, most important first" in skill


def test_stories_go_through_a_json_file_so_dollar_amounts_survive():
    """Prod 2026-11-26: 26 story summaries lost `$` figures to the shell ("0 billion")."""
    prompt, skill = flat(prompt_text()), flat(skill_text())
    assert "intel story put --json" in prompt
    assert "intel story put --json" in skill
    assert '--title "..."' not in skill and '--summary "..."' not in skill
    assert "the shell erases `$` amounts" in skill


def test_the_brief_bar_follows_the_content_review():
    """Review 2026-11-27: stale news, 1-point Reddit posts and "validates our position" lines
    filled fixed five-item briefs, and a skip rule in section 7 only shaped wording."""
    prompt, skill = flat(prompt_text()), flat(skill_text())
    assert "One to five developments" in prompt and "fewer is right" in prompt
    assert "Nothing is news unless today adds a fact." in prompt
    assert "A low-engagement post never makes the brief on its own" in prompt
    assert "one concrete consequence" in prompt and "one concrete consequence" in skill
    assert '"validates"' in prompt and '"validates"' in skill
    assert "decide which developments you pick, not only how you word them" in prompt
    assert "at most six" in prompt and "at most six" in skill


def test_the_report_reads_briefs_not_prior_reports_and_doesnt_repitch():
    prompt = flat(prompt_text())
    assert "last three days' `intel/reports/<day>/brief.md`" in prompt
    assert "Never print a whole earlier report." in prompt
    assert '"Still open from <day>: <idea>"' in prompt
    assert "Open the section with the section 7 rules that apply to today's ideas, once" in prompt
    assert "don't need the project's brand guide (`BRAND.md`) or design spec" in prompt


def test_web_search_is_allowed_sparingly_for_checking_claims():
    prompt = flat(prompt_text())
    assert "Web search and reading a page on the web are allowed, sparingly" in prompt
    assert "primary source" in prompt and "headline only" in prompt
