"""Planned page changes, read as a pure list that website.change (source planned) owns.

Moved here from #239, which drops its copy, so the two PRs merge in either order. The package
tests for organic.content_efficacy stay with #239.
"""

import json
from datetime import date

from tin_lite import planned_url_changes as planned

TODAY = date(2026, 9, 29)


def efficacy(generated="2026-09-28", changes=None, decisions=None):
    block = {
        "schema": "content.efficacy/1",
        "generated": generated,
        "decisions": decisions
        or [
            {"url": "/blog/mileage-log", "decision": "refresh", "rule": "low_ctr"},
            {"url": "/blog/late-fees", "decision": "refresh", "rule": "decline"},
            {"url": "/blog/old", "decision": "rewrite", "rule": "intent_mismatch"},
        ],
        "url_changes": changes
        if changes is not None
        else [
            {
                "from": "/compare/x-alternatives",
                "to": "/alternatives/x",
                "kind": "301",
                "reason": "merge",
                "confirmed": True,
            },
            {"from": "/sign-in", "to": "/", "kind": "noindex", "reason": "utility"},
            {"from": "/blog/dead", "to": None, "kind": "gone", "reason": "retire"},
        ],
    }
    return "# Page decisions\n\n## Decisions block\n\n```json\n" + json.dumps(block) + "\n```\n"


def architecture(generated="2026-09-20", redirects=None):
    block = {
        "schema": "site_architecture.redirects/1",
        "plan_id": "11111111-1111-4111-8111-111111111111",
        "generated": generated,
        "redirects": redirects
        or [
            {"old": "/features", "new": "/product", "status": 308, "reason": "url_change"},
            {"old": "/compare/x-alternatives", "new": "/alternatives/x", "status": 301},
            {"old": "/spring-promo", "new": "/", "status": 302},
        ],
    }
    return (
        "# Site architecture\n\n<!-- redirects.json:start -->\n```json\n"
        + json.dumps(block)
        + "\n```\n<!-- redirects.json:end -->\n"
    )


def files(efficacy_text=None, architecture_text=None):
    return {planned.EFFICACY_PATH: efficacy_text, planned.ARCHITECTURE_PATH: architecture_text}


def test_current_decisions_become_url_changes_and_stale_ones_do_not():
    changes = planned.read_changes(files(efficacy()), TODAY)
    assert [(c["kind"], c["from"], c["to"]) for c in changes] == [
        ("redirect", "/compare/x-alternatives", "/alternatives/x"),
        ("noindex", "/sign-in", None),
    ]
    assert changes[0]["confirmed"] is True
    assert planned.read_changes(files(efficacy(generated="2026-09-01")), TODAY) == []
    assert planned.read_changes(files("## Decisions block\n```json\n{bad\n```"), TODAY) == []


def test_a_plan_redirect_wins_over_the_same_weekly_proposal():
    changes = planned.read_changes(files(efficacy(), architecture()), TODAY)
    assert [(c["source"], c["from"]) for c in changes] == [
        ("organic.site_architecture", "/features"),
        ("organic.site_architecture", "/compare/x-alternatives"),
        ("organic.content_efficacy", "/sign-in"),
    ]  # the 302 is not a permanent move and is left out


def test_off_site_or_traversing_paths_are_refused():
    bad = [
        {"from": "/a/../../etc", "to": "/b", "kind": "301"},
        {"from": "a", "to": "/b", "kind": "301"},
    ]
    assert planned.read_changes(files(efficacy(changes=bad)), TODAY) == []


def test_each_change_has_a_stable_id_and_tin_s_suggestion():
    redirect, noindex = planned.read_changes(files(efficacy()), TODAY)
    assert redirect["id"] == planned.finding_id(
        {
            "source": "organic.content_efficacy",
            "kind": "redirect",
            "from": "/compare/x-alternatives",
            "to": "/alternatives/x",
        }
    )
    assert redirect["id"].startswith("oa_") and len(redirect["id"]) == 23
    assert planned.read_changes(files(efficacy()), TODAY)[0]["id"] == redirect["id"]
    assert redirect["suggestion"] == "apply"
    assert noindex["suggestion"] == "ask"  # /sign-in is protected


def test_protected_pages_are_asked_about_never_suggested():
    """An approved noindex on /sign-in once touched sign-in pages another app shares."""
    changes = [
        {"from": "/sign-in", "to": None, "kind": "noindex", "reason": "utility"},
        {"from": "/sign-up/team", "to": None, "kind": "noindex", "reason": "utility"},
        {"from": "/partners/acme", "to": "/partners", "kind": "301", "reason": "merge"},
        {"from": "/login-help", "to": "/auth-complete", "kind": "301", "reason": "merge"},
        {"from": "/blog/a", "to": "/blog/b", "kind": "301", "reason": "merge"},
    ]
    found = planned.read_changes(
        files(efficacy(changes=changes)), TODAY, protected_paths=["/partners"]
    )
    assert [c["protected"] for c in found] == [True, True, True, True, False]
    assert [c["suggestion"] for c in found] == ["ask", "ask", "ask", "ask", "apply"]
    # Without the extra path, /partners is an ordinary page again; the auth defaults stay.
    plain = planned.read_changes(files(efficacy(changes=changes)), TODAY)
    assert [c["protected"] for c in plain] == [True, True, False, True, False]
    assert planned.protected("/sign-inside", ()) is False


def test_refresh_rows_become_refresh_candidates():
    found = planned.refresh_candidates(efficacy(), TODAY)
    assert found == {"/blog/mileage-log": {"search.low_ctr"}, "/blog/late-fees": {"search.decay"}}
    assert planned.refresh_candidates(efficacy(generated="2026-09-01"), TODAY) == {}


def test_deleting_a_page_is_listed_for_the_founder_never_planned():
    changes = planned.read_changes(files(efficacy()), TODAY)
    assert "/blog/dead" not in {c["from"] for c in changes}
    assert planned.read_deletions(files(efficacy()), TODAY) == [
        {"from": "/blog/dead", "reason": "retire"}
    ]
    assert planned.read_deletions(files(efficacy(generated="2026-09-01")), TODAY) == []


def test_missing_files_contribute_nothing():
    assert planned.read_changes(files(), TODAY) == []
    assert planned.read_deletions(files(), TODAY) == []
    assert planned.refresh_candidates(None, TODAY) == {}
