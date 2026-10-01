"""The refresh picks one page from the audit and changes exactly the approved text."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tin_lite import content_refresh as refresh

HOST = "https://example.com"


def audit(*findings):
    return {"schema_version": 3, "findings": list(findings)}


def finding(check, *urls):
    return {"check_id": check, "urls": [f"{HOST}{url}" for url in urls]}


EVIDENCE = {
    "scope": {"host": "example.com"},
    "search_console": {
        "value": {
            "pages": [
                {"url": f"{HOST}/pricing", "clicks": 4, "impressions": 900, "position": 3.2},
                {"url": f"{HOST}/guides/setup/", "clicks": 30, "impressions": 2400, "position": 9},
                {"url": f"{HOST}/blog/old", "clicks": 1, "impressions": 40, "position": 12},
            ]
        }
    },
    "search_console_queries": {
        "value": {
            "queries": [
                ["how to set up tin", f"{HOST}/guides/setup/", 20, 1500, 8.7],
                ["tin setup guide", f"{HOST}/guides/setup", 10, 600, 9.4],
                ["tin pricing", f"{HOST}/pricing", 4, 800, 3.1],
            ]
        }
    },
}

PAGE = """<!doctype html><html lang="en"><head>
<title>Setup &amp; configuration | Example</title>
<meta name="description" content="Everything about configuring Example.">
</head><body><nav><p>Menu text that is long enough to be a paragraph here.</p></nav>
<main><h1>Setup   guide</h1>
<p>Short.</p>
<p>Example connects to your coding agent in two minutes, then drafts your first plan.</p>
<p>A second paragraph that explains the options in plain words for new users.</p>
</main><footer><p>Footer paragraph that is long enough to count as one.</p></footer>
</body></html>"""


def test_the_page_with_most_impressions_at_stake_is_chosen_and_blocked_pages_wait():
    findings = audit(
        finding("search.low_ctr", "/pricing"),
        finding("search.near_page_one", "/guides/setup/"),
        finding("search.cannibalization", "/blog/old"),
    )
    chosen = refresh.choose(findings, EVIDENCE, blocked=set())
    assert chosen["path"] == "/guides/setup"
    assert chosen["checks"] == ["search.near_page_one"]
    assert chosen["metrics"]["impressions"] == 2400 and chosen["metrics"]["ctr"] == 0.0125
    # Both URL spellings of the page count as its searches.
    assert [s["query"] for s in chosen["searches"]] == ["how to set up tin", "tin setup guide"]
    assert chosen["body_allowed"] is False
    # A page refreshed recently waits; the next page takes its turn.
    assert refresh.choose(findings, EVIDENCE, blocked={"/guides/setup"})["path"] == "/pricing"
    assert refresh.choose(findings, EVIDENCE, blocked={"/guides/setup", "/pricing"}) is None
    decayed = audit(finding("search.decay", "/blog/old"))
    assert refresh.choose(decayed, EVIDENCE, set())["body_allowed"] is True


def test_the_page_text_is_what_a_reader_sees():
    text = refresh.page_text(PAGE)
    assert text["title"] == "Setup & configuration | Example"
    assert text["description"] == "Everything about configuring Example."
    assert text["h1"] == "Setup guide"
    # The opening answer is the first real paragraph after the H1; nav and footer are skipped.
    assert text["lead"].startswith("Example connects to your coding agent")
    assert all("Menu" not in p and "Footer" not in p for p in text["paragraphs"])


def context(**page):
    current = refresh.page_text(PAGE)
    return {
        "page": {
            "url": f"{HOST}/guides/setup/",
            "path": "/guides/setup",
            "checks": ["search.near_page_one"],
            "body_allowed": False,
            **page,
        },
        "current": current,
        "results_markdown": refresh.results_markdown([]),
    }


def document(items, ctx=None, *, table=True, results=True):
    ctx = ctx or context()
    rows = "\n".join(
        f"| {item['field']} | {item['old']} | {item['new']} | {item['reason']} |" for item in items
    )
    block = json.dumps(
        {"schema": refresh.DOCUMENT_SCHEMA, "page": ctx["page"]["url"], "replacements": items},
        indent=2,
    )
    parts = [
        "# Refresh: /guides/setup",
        "",
        "Leads with how to set up Tin, the page's main search.",
        "",
        "## Changes",
        "",
        "| Field | Now | Proposed | Why |",
        "| --- | --- | --- | --- |",
        rows if table else "",
        "",
        "## Replacements",
        "",
        f"```json\n{block}\n```",
        "",
        "## Results of earlier refreshes",
        "",
        ctx["results_markdown"] if results else "",
        "",
    ]
    return "\n".join(parts).encode()


GOOD = [
    {
        "field": "title",
        "old": "Setup & configuration | Example",
        "new": "How to set up Example in two minutes",
        "reason": "Matches the main search.",
    },
    {
        "field": "h1",
        "old": "Setup guide",
        "new": "How to set up Example",
        "reason": "The H1 should answer the search.",
    },
]


def test_a_valid_refresh_starts_from_the_exact_current_text():
    items = refresh.validate_document(document(GOOD), context())
    assert [item["field"] for item in items] == ["title", "h1"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        # A plausible but unusable result: the model paraphrased the current title.
        ({"old": "Setup and configuration | Example"}, "current title, exactly"),
        ({"new": "Setup & configuration | Example"}, "must differ"),
        ({"new": "x" * 71}, "under 70"),
        ({"new": "How to <b>set up</b> Example"}, "plain text"),
        ({"field": "footer"}, "not a field"),
    ],
)
def test_unusable_replacements_are_refused(change, message):
    items = [{**GOOD[0], **change}]
    with pytest.raises(ValueError, match=message):
        refresh.validate_document(document(items), context())


def test_body_paragraphs_change_only_when_the_audit_flags_the_body():
    paragraph = {
        "field": "paragraph",
        "old": "A second paragraph that explains the options in plain words for new users.",
        "new": "Pick a plan, connect GitHub, and Tin drafts the first article.",
        "reason": "Answer directly.",
    }
    with pytest.raises(ValueError, match="decay or weak"):
        refresh.validate_document(document([paragraph]), context())
    allowed = context(body_allowed=True)
    assert refresh.validate_document(document([paragraph], allowed), allowed)


def test_the_reviewer_sees_every_change_and_tins_results_table():
    with pytest.raises(ValueError, match="review table"):
        refresh.validate_document(document(GOOD, table=False), context())
    with pytest.raises(ValueError, match="results unchanged"):
        refresh.validate_document(document(GOOD, results=False), context())
    other = context()
    other["page"]["url"] = f"{HOST}/pricing"
    with pytest.raises(ValueError, match="different page"):
        refresh.validate_document(document(GOOD), other)


PAGE_TSX = """export const metadata = {
  title: "Setup & configuration | Example",
  description: "Everything about configuring Example.",
};

export default function Page() {
  return (
    <main>
      <h1>Setup guide</h1>
      <p>
        Example connects to your coding agent in two minutes, then drafts your
        first plan.
      </p>
    </main>
  );
}
"""
NAV_TSX = 'export const links = [{ href: "/guides/setup", label: "Setup guide" }];\n'


def test_the_patch_changes_exactly_the_approved_text_in_the_pages_own_file():
    items = [
        *GOOD,
        {
            "field": "lead",
            "old": "Example connects to your coding agent in two minutes, then drafts your "
            "first plan.",
            "new": "Set up Example in two minutes: connect your coding agent and it drafts "
            "your first plan.",
            "reason": "Answer first.",
        },
    ]
    files = {
        "app/guides/setup/page.tsx": PAGE_TSX.encode(),
        "components/nav.tsx": NAV_TSX.encode(),
        "node_modules/pkg/index.js": PAGE_TSX.encode(),
    }
    changed = refresh.plan_patch(files, items)
    # The H1 text also appears in the navigation, but the page's own file holds most of its
    # texts, so only that file changes.
    assert list(changed) == ["app/guides/setup/page.tsx"]
    text = changed["app/guides/setup/page.tsx"]
    assert 'title: "How to set up Example in two minutes"' in text
    assert "<h1>How to set up Example</h1>" in text
    # A wrapped JSX paragraph is matched across its line break.
    assert "Set up Example in two minutes: connect your coding agent" in text
    assert 'description: "Everything about configuring Example."' in text


def test_text_the_source_does_not_hold_stops_the_delivery():
    missing = [{**GOOD[0], "old": "A title the site builds from a template"}]
    with pytest.raises(ValueError, match="could not find the page's current title"):
        refresh.plan_patch({"app/page.tsx": PAGE_TSX.encode()}, missing)
    ambiguous = [GOOD[1]]
    files = {"a/page.tsx": b"<h1>Setup guide</h1>", "b/page.tsx": b"<h1>Setup guide</h1>"}
    with pytest.raises(ValueError, match="several files"):
        refresh.plan_patch(files, ambiguous)


def test_the_worker_check_refuses_any_other_edit():
    original = {"app/page.tsx": PAGE_TSX}
    good = PAGE_TSX.replace("Setup guide", "How to set up Example")
    refresh.verify_patch(original, {"app/page.tsx": good}, [GOOD[1]])
    sneaky = good.replace("export default", "export  default")
    with pytest.raises(ValueError, match="more than the approved text"):
        refresh.verify_patch(original, {"app/page.tsx": sneaky}, [GOOD[1]])
    with pytest.raises(ValueError, match="did not reach the source"):
        refresh.verify_patch(original, {"app/page.tsx": PAGE_TSX}, [GOOD[1]])
    with pytest.raises(ValueError, match="already exist"):
        refresh.verify_patch(original, {"app/new.tsx": good}, [GOOD[1]])


def test_json_and_html_escaped_copies_are_found_and_kept_in_their_encoding():
    source = '{"title": "Setup \\"fast\\" guide"}\n<p>Tom &amp; Jerry pages</p>\n'
    items = [
        {"field": "title", "old": 'Setup "fast" guide', "new": 'The "fast" setup', "reason": "x"},
        {"field": "h1", "old": "Tom & Jerry pages", "new": "Tom & Jerry, set up", "reason": "x"},
    ]
    changed = refresh.plan_patch({"content/page.json": source.encode()}, items[:1])
    assert changed["content/page.json"].startswith('{"title": "The \\"fast\\" setup"}')
    changed = refresh.plan_patch({"page.html": source.encode()}, items[1:])
    assert "<p>Tom &amp; Jerry, set up</p>" in changed["page.html"]


def test_results_compare_28_days_before_and_after_going_live():
    live = datetime(2026, 8, 1, 15, tzinfo=UTC)
    windows = refresh.result_windows(live)
    assert windows == {
        "before": {"start": "2026-07-04", "end": "2026-07-31"},
        "after": {"start": "2026-08-01", "end": "2026-08-28"},
    }
    assert not refresh.measurable(live, live + timedelta(days=30))
    assert refresh.measurable(live, live + timedelta(days=31))
    assert refresh.totals({"rows": [{"clicks": 10, "impressions": 400, "position": 5.0}]}) == {
        "clicks": 10.0,
        "impressions": 400.0,
        "ctr": 0.025,
        "position": 5.0,
    }
    table = refresh.results_markdown(
        [
            {
                "path": "/pricing",
                "live_at": live.isoformat(),
                "before": {"clicks": 10, "impressions": 400, "ctr": 0.025, "position": 5.0},
                "after": {"clicks": 25, "impressions": 500, "ctr": 0.05, "position": 4.1},
            }
        ]
    )
    assert "| /pricing | 2026-08-01 | 10 → 25 | 400 → 500 | 2.5% → 5.0% | 5 → 4.1 |" in table


def test_a_text_matches_whole_words_only():
    # A probe once turned PricingTable into teamsTable.
    source = 'import { PricingTable } from "./table";\nexport const title = "Pricing";\n'
    items = [{"field": "title", "old": "Pricing", "new": "Pricing for teams", "reason": "x"}]
    changed = refresh.plan_patch({"app/pricing/page.tsx": source.encode()}, items)
    text = changed["app/pricing/page.tsx"]
    assert "{ PricingTable }" in text and 'title = "Pricing for teams";' in text


def test_a_text_held_twice_in_one_file_stops_delivery():
    source = 'export const metadata = { title: "Setup guide" };\nconst nav = "Setup guide";\n'
    with pytest.raises(ValueError, match="appears 2 times in app/page.tsx"):
        refresh.plan_patch({"app/page.tsx": source.encode()}, [GOOD[1]])


@pytest.mark.parametrize(
    ("path", "source", "new", "expected"),
    [
        # A string in code keeps its quote type, with the quote escaped.
        ("app/page.tsx", "const t = 'Setup guide';\n", "Founder's guide", "'Founder\\'s guide'"),
        (
            "app/page.tsx",
            'const t = "Setup guide";\n',
            'The "fast" guide',
            '"The \\"fast\\" guide"',
        ),
        ("app/page.ts", "const t = `Setup guide`;\n", "Costs $5", "`Costs $5`"),
        ("site/seo.py", "TITLE = 'Setup guide'\n", "Founder's guide", "'Founder\\'s guide'"),
        # JSON keeps valid string escapes.
        (
            "content/page.json",
            '{"h1": "Setup guide"}\n',
            'The "fast" guide',
            '"The \\"fast\\" guide"',
        ),
        # YAML: double quotes escape like JSON; single quotes double up.
        ("content/page.yaml", "h1: 'Setup guide'\n", "Founder's guide", "'Founder''s guide'"),
        # An HTML attribute and HTML text are written as HTML.
        (
            "index.html",
            '<meta content="Setup guide">\n',
            'The "fast" & easy guide',
            '"The &quot;fast&quot; &amp; easy guide"',
        ),
        (
            "index.html",
            "<h1>Setup guide</h1>\n",
            "Tom & Jerry's guide",
            ">Tom &amp; Jerry's guide<",
        ),
        # JSX text uses an entity for a quote the source did not already write raw.
        ("app/page.tsx", "<h1>Setup guide</h1>\n", "Founder's guide", ">Founder&apos;s guide<"),
    ],
)
def test_the_new_text_is_escaped_for_where_it_sits(path, source, new, expected):
    items = [{"field": "h1", "old": "Setup guide", "new": new, "reason": "x"}]
    changed = refresh.plan_patch({path: source.encode()}, items)
    assert expected in changed[path]


def test_a_quote_tin_cannot_place_safely_stops_delivery():
    # A JSX attribute takes no backslash escape, and part of a longer string has no edges.
    for path, source, new in [
        ("app/page.tsx", '<Hero title="Setup guide" />\n', 'The "fast" guide'),
        ("app/page.ts", 'const t = "Setup guide for teams";\n', 'The "fast" guide'),
    ]:
        items = [{"field": "h1", "old": "Setup guide", "new": new, "reason": "x"}]
        with pytest.raises(ValueError, match="safely"):
            refresh.plan_patch({path: source.encode()}, items)


# Realistic upside: near the top results first, pages beyond position 30 last.

MOZ_EVIDENCE = {
    "scope": {"host": "example.com"},
    "search_console": {
        "value": {
            "pages": [
                # The organic audit of 1 October: /alternatives/moz sat at position 8.3 with 324
                # impressions, listed under near_page_one, yet the refresh picked a page at 53.5.
                {
                    "url": f"{HOST}/alternatives/moz",
                    "clicks": 1,
                    "impressions": 324,
                    "position": 8.3,
                },
                {"url": f"{HOST}/blog/deep", "clicks": 0, "impressions": 1500, "position": 53.5},
                {"url": f"{HOST}/guides/mid", "clicks": 3, "impressions": 600, "position": 24},
                {"url": f"{HOST}/docs/close", "clicks": 2, "impressions": 90, "position": 11},
            ]
        }
    },
}


def test_a_page_near_the_top_beats_a_far_page_with_more_impressions():
    findings = audit(
        finding("search.near_page_one", "/alternatives/moz"),
        finding("aeo.answer_structure", "/blog/deep", "/guides/mid", "/docs/close", "/new"),
    )
    ranked = refresh.plan_candidates(findings, MOZ_EVIDENCE)
    assert [page["path"] for page in ranked] == [
        "/alternatives/moz",  # near_page_one, position 8.3
        "/docs/close",  # position 11: inside 4 to 20, fewer impressions
        "/guides/mid",  # position 24
        "/new",  # no Search Console position
        "/blog/deep",  # position 53.5, most impressions, last
    ]
    assert [page["upside"]["tier"] for page in ranked] == [
        "near_top",
        "near_top",
        "possible",
        "possible",
        "far",
    ]
    assert ranked[0]["upside"]["reason"].startswith("Average position 8.3 with 324 impressions")
    assert "just below the top results" in ranked[0]["upside"]["reason"]
    # A far page drops out of a short list first, and is still offered when nothing else is.
    assert "/blog/deep" not in [
        p["path"] for p in refresh.plan_candidates(findings, MOZ_EVIDENCE, limit=4)
    ]
    alone = audit(finding("aeo.answer_structure", "/blog/deep"))
    assert [p["path"] for p in refresh.plan_candidates(alone, MOZ_EVIDENCE)] == ["/blog/deep"]
    # content.generate's refresh item carries the same verdict for its page.
    entry = refresh.page_entry(findings, MOZ_EVIDENCE, f"{HOST}/blog/deep")
    assert entry["upside"]["tier"] == "far"


def test_content_refresh_keeps_its_own_impressions_order():
    # content.refresh 1.0.0 (on main) and its retired 1.1.0 keep choosing by impressions; the
    # upside rule applies to content.plan's candidates and content.generate's refresh items.
    findings = audit(
        finding("search.near_page_one", "/alternatives/moz"),
        finding("aeo.answer_structure", "/blog/deep"),
    )
    chosen = refresh.choose(findings, MOZ_EVIDENCE, blocked=set())
    assert chosen["path"] == "/blog/deep" and "upside" not in chosen
