"""Planned page changes reach the workflows that make them: the technical fix and the refresh."""

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_content_efficacy import run as run_efficacy
from test_technical_batch import batch_source

from tin_lite import content_refresh as refresh
from tin_lite import planned_url_changes as planned
from tin_lite import technical_repair_plan as plan

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


def test_each_change_is_a_judgment_call_then_a_repair():
    selections = planned.as_selections(planned.read_changes(files(efficacy()), TODAY), "t.example")
    redirect, noindex = (row["finding"] for row in selections)
    assert redirect["id"] == planned.finding_id(
        {
            "source": "organic.content_efficacy",
            "kind": "redirect",
            "from": "/compare/x-alternatives",
            "to": "/alternatives/x",
        }
    )
    waiting = plan.build_plan(selections, {})
    assert [d["id"] for d in waiting["decisions_needed"]] == [redirect["id"], noindex["id"]]
    assert waiting["decisions_needed"][0]["suggestion"] == "apply"
    answered = plan.build_plan(selections, {redirect["id"]: "apply", noindex["id"]: "keep"})
    [repair] = answered["repairs"]
    assert repair["kind"] == "merge_redirect" and repair["planned_by"] == "organic.content_efficacy"
    assert repair["redirects"] == [
        {
            "from": "https://t.example/compare/x-alternatives",
            "to": "https://t.example/alternatives/x",
        }
    ]
    assert [r["id"] for r in answered["left_out"]["decided_keep"]] == [noindex["id"]]


async def test_the_technical_fix_preview_asks_about_planned_changes():
    source = batch_source()
    host = source.run.input["site_url"].split("/")[2]
    source.project.canonical_branch = "main"
    texts = {planned.EFFICACY_PATH: efficacy().encode()}
    source.storage.get_repo = AsyncMock(return_value=object())
    source.storage.head_sha = AsyncMock(return_value="c" * 40)
    source.storage.read_canonical_artifact_if_exists = AsyncMock(
        side_effect=lambda **kw: texts.get(kw["path"])
    )
    args = {
        "project_id": source.project.id,
        "audit_run_id": source.run.id,
        "audit_revision": source.run.canonical_commit_sha,
        "expected_repository": "owner/site",
        "repository_serves_site": True,
        "bind": False,
    }
    preview = await source.service.batch(**args)
    asked = {d["finding"]["check_id"]: d for d in preview["decisions_needed"]}
    assert {"planned.redirect", "planned.noindex"} <= set(asked)
    assert asked["planned.redirect"]["finding"]["urls"] == [
        f"https://{host}/compare/x-alternatives"
    ]
    assert preview["planned_changes"] == {
        "revision": "c" * 40,
        "count": 2,
        "sources": ["organic.content_efficacy"],
    }
    answer = f"{asked['planned.redirect']['id']}=apply"
    answered = await source.service.batch(**args, decisions=[answer])
    moved = [r for r in answered["plan"]["repairs"] if r["check_id"] == "planned.redirect"]
    assert moved[0]["redirects"][0]["to"] == f"https://{host}/alternatives/x"


async def test_without_planning_files_the_preview_is_unchanged():
    source = batch_source()
    preview = await source.service.batch(
        project_id=source.project.id,
        audit_run_id=source.run.id,
        audit_revision=source.run.canonical_commit_sha,
        expected_repository="owner/site",
        repository_serves_site=True,
        bind=False,
    )
    assert preview["planned_changes"]["count"] == 0
    assert not any(
        d["finding"]["check_id"].startswith("planned.") for d in preview["decisions_needed"]
    )


def test_refresh_rows_become_refresh_candidates():
    found = planned.refresh_candidates(efficacy(), TODAY)
    assert found == {"/blog/mileage-log": {"search.low_ctr"}, "/blog/late-fees": {"search.decay"}}
    evidence = {
        "scope": {"host": "t.example"},
        "search_console": {
            "value": {
                "pages": [
                    {"url": "https://t.example/blog/mileage-log", "clicks": 3, "impressions": 900},
                    {"url": "https://t.example/blog/late-fees", "clicks": 9, "impressions": 400},
                ]
            }
        },
    }
    chosen = refresh.choose({"findings": []}, evidence, set(), found)
    assert chosen["url"] == "https://t.example/blog/mileage-log"
    assert chosen["planned_by"] == "organic.content_efficacy"
    assert chosen["body_allowed"] is False
    second = refresh.choose({"findings": []}, evidence, {"/blog/mileage-log"}, found)
    assert second["path"] == "/blog/late-fees" and second["body_allowed"] is True
    assert refresh.choose({"findings": []}, evidence, set()) is None


async def test_content_refresh_preparation_reads_the_current_decisions():
    from tin_lite.content_refresh_sources import ContentRefreshSources

    storage = SimpleNamespace(
        read_canonical_artifact_if_exists=AsyncMock(return_value=efficacy().encode())
    )
    sources = ContentRefreshSources(database=None, storage=storage)
    project = SimpleNamespace(state_repo_id="project/test")
    from datetime import UTC, datetime

    found = await sources.planned_refreshes(project, "c" * 40, datetime(2026, 9, 29, tzinfo=UTC))
    assert set(found) == {"/blog/mileage-log", "/blog/late-fees"}


async def test_the_efficacy_package_output_is_what_the_hook_reads(monkeypatch):
    content, _ = await run_efficacy(monkeypatch)
    changes = planned.read_changes(files(content), TODAY)
    assert {(c["kind"], c["from"]) for c in changes} == {
        ("redirect", "/compare/quickbooks-alternatives"),
        ("noindex", "/sign-in"),
        ("noindex", "/offer/spring-sale"),
    }
    assert set(planned.refresh_candidates(content, TODAY)) >= {"/blog/mileage-log-template"}
