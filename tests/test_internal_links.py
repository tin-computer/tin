"""Offline checks of the checked-in anchor selection; no network, repository or model calls."""

import re
from pathlib import Path

import pytest

from tin_lite.procedures import validate_codex_procedure_definition
from tin_lite.workflow_packages import decode_workflow_source

PACKAGE = Path(__file__).parents[1] / "workflow_packages/organic.internal_links"
RESOURCE = PACKAGE / "skills/internal-link-repair/SELECTION.md"
HREF = "https://example.test/guides/fragile-shipping"


@pytest.fixture
def selection():
    # This exact reviewed repository resource is executable inside the procedure sandbox.
    blocks = re.findall(r"```python\n(.*?)\n```", RESOURCE.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 1
    namespace = {}
    exec(compile(blocks[0], str(RESOURCE), "exec"), namespace)  # noqa: S102 - fixed, reviewed fixture
    return namespace


def page(text, path="docs/packing.md", kind="markdown"):
    return {"path": path, "text": text, "kind": kind}


def target(phrases=("shipping insurance",), path=None):
    return {"href": HREF, "phrases": list(phrases), "path": path}


def test_an_anchor_is_found_in_ordinary_prose(selection):
    text = "We sell boxes.\n\nMost orders travel without shipping insurance, which we regret.\n"
    anchor = selection["find_anchor"](text, "shipping insurance")
    assert text[anchor["start"] : anchor["end"]] == "shipping insurance"


@pytest.mark.parametrize(
    "text",
    [
        "---\ntitle: shipping insurance\n---\n\nBoxes are sold here.\n",
        "## Shipping insurance\n\nBoxes are sold here.\n",
        "```\npip install shipping insurance\n```\n",
        "Ask about `shipping insurance` at checkout.\n",
        "Read [shipping insurance](/old) first.\n",
        "See <a href='/old'>shipping insurance</a> today.\n",
        "import shipping insurance from './x'\n",
        "Visit https://other.test/shipping insurance now.\n",
    ],
)
def test_masked_regions_never_yield_an_anchor(selection, text):
    assert selection["find_anchor"](text, "shipping insurance") is None


def test_a_phrase_inside_a_longer_word_is_not_an_anchor(selection):
    assert selection["find_anchor"]("We discuss reshipping insurances here.\n", "shipping") is None


def test_matching_is_case_and_spacing_tolerant_but_never_spans_lines(selection):
    found = selection["find_anchor"]("We offer Shipping   Insurance today.\n", "shipping insurance")
    assert found["text"] == "Shipping   Insurance"
    assert selection["find_anchor"]("We offer shipping\ninsurance.\n", "shipping insurance") is None


def test_applying_a_link_preserves_every_other_byte(selection):
    text = "Most orders travel without shipping insurance, which we regret.\n"
    updated = selection["apply_link"](
        text, selection["find_anchor"](text, "shipping insurance"), HREF
    )
    assert updated == f"Most orders travel without [shipping insurance]({HREF}), which we regret.\n"
    assert updated.replace(f"[shipping insurance]({HREF})", "shipping insurance") == text


def test_applying_a_link_to_html_escapes_the_target(selection):
    text = "<p>We now offer shipping insurance on every order.</p>\n"
    anchor = selection["find_anchor"](text, "shipping insurance", "html")
    updated = selection["apply_link"](text, anchor, "https://x.test/a?b=1&c=2", "html")
    assert '<a href="https://x.test/a?b=1&amp;c=2">shipping insurance</a>' in updated
    assert updated.startswith("<p>We now offer <a") and updated.endswith("every order.</p>\n")


def test_an_html_attribute_is_not_prose(selection):
    text = '<img alt="shipping insurance" src="/a.png">\n'
    assert selection["find_anchor"](text, "shipping insurance", "html") is None


def test_a_stale_anchor_is_refused_rather_than_applied(selection):
    text = "We offer shipping insurance today.\n"
    anchor = selection["find_anchor"](text, "shipping insurance")
    with pytest.raises(ValueError):
        selection["apply_link"]("Different copy entirely.\n", anchor, HREF)


def test_ranking_skips_the_target_and_pages_that_already_link_to_it(selection):
    pages = [
        page("We sell shipping insurance here.\n", path="content/target.md"),
        page(f"Our [shipping insurance]({HREF}) explains it.\n", path="content/done.md"),
        page("Ask about shipping insurance at checkout.\n", path="content/faq.md"),
    ]
    ranked = selection["rank_candidates"](target(path="content/target.md"), pages)
    assert [item["path"] for item in ranked] == ["content/faq.md"]


def test_ranking_prefers_specific_phrases_and_breaks_ties_on_path(selection):
    pages = [
        page("We discuss shipping insurance for fragile items at length.\n", path="b.md"),
        page("We discuss shipping insurance for fragile items at length.\n", path="a.md"),
        page("We mention shipping insurance briefly.\n", path="c.md"),
    ]
    ranked = selection["rank_candidates"](
        target(phrases=["shipping insurance", "shipping insurance for fragile items"]), pages
    )
    assert [item["path"] for item in ranked] == ["a.md", "b.md", "c.md"]
    assert ranked[0]["phrase"] == "shipping insurance for fragile items"
    assert ranked[2]["phrase"] == "shipping insurance"


def test_ranking_respects_the_link_budget_and_takes_one_anchor_per_page(selection):
    body = "shipping insurance matters. shipping insurance again.\n"
    pages = [page(body, path=f"{n}.md") for n in "abcd"]
    ranked = selection["rank_candidates"](target(), pages, link_budget=2)
    assert len(ranked) == 2
    assert all(item["anchor"]["start"] < 20 for item in ranked)


def test_a_site_with_no_honest_anchor_returns_nothing_to_change(selection):
    pages = [page("## Shipping insurance\n\nNothing else here.\n", path="a.md")]
    assert selection["rank_candidates"](target(), pages) == []


def test_saturated_pages_rank_below_quiet_ones(selection):
    quiet = page("We offer shipping insurance here.\n", path="quiet.md")
    busy = page(
        "We offer shipping insurance here.\n" + "".join(f"[x](/p{n})\n" for n in range(14)),
        path="busy.md",
    )
    ranked = selection["rank_candidates"](target(), [busy, quiet])
    assert [item["path"] for item in ranked] == ["quiet.md", "busy.md"]
    assert ranked[1]["outbound_links"] >= 12


def test_the_package_contract_is_a_bounded_no_change_capable_pull_request():
    manifest = (PACKAGE / "workflow.json").read_bytes()
    source = decode_workflow_source(
        manifest, definition_path="workflow_packages/organic.internal_links/workflow.json"
    )
    spec = validate_codex_procedure_definition(source.definition)
    assert spec.result_kind == "github.pull_request"
    assert spec.workspace_kind == "github.repository"
    assert spec.allow_no_change is True
    assert spec.output_max_files == 5
    assert spec.verification_commands == ("git diff --check",)
    assert spec.sandbox.profile == "isolated" and spec.sandbox.egress == "fenced"
    assert source.definition["schedule_modes"] == ["on_demand"]
