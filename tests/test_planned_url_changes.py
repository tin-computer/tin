"""Planned page changes, read as a pure list; site-fix-v5 and content.refresh 1.0.0 ignore it.

The founder dropped the technical fix workflow, and site-fix-v5 and content.refresh 1.0.0 are
on main, so neither reads these files. website.change phase 3 will turn the changes into
website_changes rows.
"""

import hashlib
import json
from datetime import date
from unittest.mock import AsyncMock

from test_content_efficacy import run as run_efficacy
from test_technical_batch import batch_source

from tin_lite import planned_url_changes as planned
from tin_lite import technical_repair_plan as plan
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_audit import digest

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


# What main shipped at d69d337: site-fix-v5 (organic.technical_fix 0.6.0) and content.refresh
# 1.0.0, as the definition and resource files the catalog publishes, and v5's repair checks.
MAIN = {
    "organic.technical_fix": (
        "0.6.0",
        "0c35530bd7b45290b6cba55c8083707d422ab954e47306047d1f406de46e8ef5",
        "b597f8ae3b90102147c1ee9ab130854bfb76b66b89834c45428355908d7f9288",
    ),
    "content.refresh": (
        "1.0.0",
        "a40c2c90db815298e9cf58d7912eb7a2696abe26ac086c2d0b98c6b405631d3f",
        "682c3af7e9a484aa84ce54a32fd29b242da53a911179628b162f2dceaffecae8",
    ),
}
V5_REPAIRS_DIGEST = "cf4ddd79137c12d38c39d971bd423aee5626a701d7f26772d2b3138a764f51b5"


def test_site_fix_v5_and_content_refresh_are_exactly_what_main_shipped():
    for key, (version, definition_digest, files_digest) in MAIN.items():
        workflow = next(w for w in BUILTIN_WORKFLOWS if w.key == key)
        definition, resources = workflow.definition_and_resource_files()
        assert workflow.version_label == version
        assert digest(definition) == definition_digest, key
        hashed = {path: hashlib.sha256(raw).hexdigest() for path, raw in resources.items()}
        assert digest(hashed) == files_digest, key
    assert plan.POLICY == "site-fix-v5"
    assert digest(sorted(plan.REPAIRS)) == V5_REPAIRS_DIGEST
    assert not any(check.startswith("planned.") for check in plan.REPAIRS)


async def test_the_technical_fix_preview_never_reads_planned_changes():
    source = batch_source()
    texts = {planned.EFFICACY_PATH: efficacy().encode(), planned.ARCHITECTURE_PATH: b""}
    source.storage.read_canonical_artifact_if_exists = AsyncMock(
        side_effect=lambda **kw: texts.get(kw["path"])
    )
    preview = await source.service.batch(
        project_id=source.project.id,
        audit_run_id=source.run.id,
        audit_revision=source.run.canonical_commit_sha,
        expected_repository="owner/site",
        repository_serves_site=True,
        bind=False,
    )
    assert "planned_changes" not in preview
    assert not any(
        d["finding"]["check_id"].startswith("planned.") for d in preview["decisions_needed"]
    )
    assert not any(
        call.kwargs.get("path") in texts
        for call in source.storage.read_canonical_artifact_if_exists.await_args_list
    )


def test_refresh_rows_become_refresh_candidates():
    found = planned.refresh_candidates(efficacy(), TODAY)
    assert found == {"/blog/mileage-log": {"search.low_ctr"}, "/blog/late-fees": {"search.decay"}}
    assert planned.refresh_candidates(efficacy(generated="2026-09-01"), TODAY) == {}


async def test_the_efficacy_package_output_is_what_the_reader_reads(monkeypatch):
    content, _ = await run_efficacy(monkeypatch)
    changes = planned.read_changes(files(content), TODAY)
    assert {(c["kind"], c["from"]) for c in changes} == {
        ("redirect", "/compare/quickbooks-alternatives"),
        ("noindex", "/sign-in"),
        ("noindex", "/offer/spring-sale"),
    }
    assert set(planned.refresh_candidates(content, TODAY)) >= {"/blog/mileage-log-template"}
