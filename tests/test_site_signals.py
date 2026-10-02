"""Page decisions and the traffic snapshot shape content.plan 0.8.0.

Both files come from #239's packages (organic.content_efficacy writes content/efficacy.md,
organic.traffic_snapshot writes analytics/traffic-snapshot.json); these fixtures follow their
shapes. A missing, stale, oversized or other-schema file changes nothing and the plan says so.
"""

import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from test_content_plan_editorial import context, portfolio
from test_content_programs import setup
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_plan as legacy
from tin_lite import content_plan_editorial as editorial
from tin_lite import content_plan_sources as sources
from tin_lite.keyword_plan import markdown_text
from tin_lite.model_providers import ModelUsage

HOST = "https://example.com"
TODAY = date(2026, 10, 1)
SHORT = [
    "page",
    "clicks",
    "clicks_prior",
    "impressions",
    "impressions_prior",
    "position",
    "position_prior",
    "sessions",
    "sessions_prior",
    "by_channel",
    "signups",
    "activated",
    "top_query",
    "top_query_impressions",
    "top_query_position",
    "first_seen",
]
DECISIONS = [
    {
        "url": "/pricing",
        "decision": "refresh",
        "action": "title_description",
        "rule": "low_ctr",
        "reason": "Clicks are below 35% of the planning CTR estimate.",
    },
    {
        "url": "/guides/setup",
        "decision": "refresh",
        "action": "content",
        "rule": "near_page_one",
        "reason": "A leading search ranks between positions 4 and 15.",
    },
    {
        "url": "/blog/old-ai-seo-tips",
        "decision": "retire",
        "action": "301",
        "target": "/blog/ai-seo",
        "rule": "no_job",
        "reason": "No measured job in 56 days; keep the 301 at least one year.",
    },
    {
        "url": "/login",
        "decision": "retire",
        "action": "noindex",
        "target": "/",
        "rule": "utility_in_search",
        "reason": "Utility page appeared in search.",
    },
    {
        "url": "/compare/moz-vs-ahrefs",
        "decision": "merge",
        "action": "301",
        "target": "/alternatives/moz",
        "rule": "duplicate",
        "reason": "The survivor covers the same intent.",
    },
    {"url": "/docs", "decision": "keep", "rule": "earning", "clicks": [10, 9]},
    {
        "url": "/about",
        "decision": "rewrite",
        "action": "brief",
        "rule": "off_positioning",
        "reason": "Page earns clicks but its angle conflicts with current positioning.",
    },
]


def efficacy(generated=TODAY, rows=DECISIONS, schema="content.efficacy/1"):
    block = {"schema": schema, "generated": str(generated), "site": HOST, "decisions": rows}
    return (
        "# Page decisions\n\nFresh snapshot available.\n\n## Decisions block\n\n```json\n"
        + json.dumps(block, separators=(",", ":"))
        + "\n```\n"
    ).encode()


def page(path, sessions, signups, activated=None, clicks=0, impressions=0):
    return {
        "page": "example.com" + path,
        "search": {"current": [clicks, impressions, None, None]},
        "visits": {"current": {"sessions": sessions, "pageviews": sessions}},
        "signups": {"first_touch": [signups, None], "activated": [activated, None]},
    }


def short(path, sessions, signups):
    row = dict.fromkeys(SHORT)
    row.update(page="example.com" + path, sessions=sessions, signups=signups, activated=0)
    return [row[name] for name in SHORT]


def snapshot(generated=TODAY, pages=None, more=None, schema="tin.traffic_snapshot/1"):
    return json.dumps(
        {
            "schema": schema,
            "generated_at": f"{generated}T09:00:00Z",
            "definitions": {"short_columns": SHORT},
            "pages": pages
            if pages is not None
            else [
                page("/integrations/slack", 200, 12, 5, 40, 900),
                page("/pricing", 400, 20, 8, 30, 1200),
                page("/", 300, 10, 4),
                page("/blog/guide", 500, 2, 0, 80, 4000),
                # Three sessions and three signups: below every minimum count, so it drives nothing.
                page("/blog/tiny-tricks", 3, 3, 3),
                page("/blog/other", 40, None),
            ],
            "more_pages": more if more is not None else [short("/blog/short-row", 150, 0)],
        },
        separators=(",", ":"),
    ).encode()


def signals(efficacy_raw=None, snapshot_raw=None, today=TODAY):
    return {
        "page_decisions": sources.page_decisions(efficacy_raw, today),
        "traffic": sources.traffic_snapshot(snapshot_raw, today),
    }


FINDINGS = {
    "findings": [
        {"check_id": "search.low_ctr", "urls": [f"{HOST}/pricing"]},
        {"check_id": "search.decay", "urls": [f"{HOST}/blog/old"]},
        # Page decisions keeps /docs, so the audit's flag does not make it a refresh candidate.
        {"check_id": "search.near_page_one", "urls": [f"{HOST}/docs"]},
    ]
}
AUDIT = {
    "scope": {"host": "example.com", "market": "US"},
    "search_console": {
        "value": {
            "pages": [
                {"url": f"{HOST}/pricing", "clicks": 4, "impressions": 900, "position": 3.2},
                {"url": f"{HOST}/docs", "clicks": 9, "impressions": 400, "position": 7},
                {"url": f"{HOST}/blog/guide", "clicks": 80, "impressions": 4000, "position": 26},
            ]
        }
    },
}
KEYWORDS = {"scope": {"host": "example.com"}, "keywords": []}


def research(found):
    planned = {
        path: set(row["checks"])
        for path, row in (found["page_decisions"].get("refresh") or {}).items()
    }
    return sources.typed_research_rows(FINDINGS, AUDIT, KEYWORDS, planned=planned, signals=found)


def shaped_context(found, base=None):
    data = deepcopy(base) if base is not None else context()
    rows, extra = research(found)
    data["research"]["rows"] += rows
    data["research"].update(extra)
    return data


def pages():
    inspected = ["/pricing", "/docs", "/compare/moz-vs-ahrefs", "/guides/setup"]
    return {
        "pages": [
            {"page_id": f"p{i:03d}", "url": f"{HOST}{path}", "status": "inspected", "text": path}
            for i, path in enumerate(inspected, 1)
        ],
        "omitted_candidates": 0,
    }


def opportunity(id, title, kind="article", **fields):
    return {
        "id": id,
        "title": title,
        "intent": f"Buyer task: {title}",
        "brief": f"Explain {title}.",
        "action": "new_page",
        "page_id": "",
        "source_ids": ["s001"],
        "verification": ["Inspect the current site first."],
        "rationale": "Distinct task.",
        "kind": kind,
        **fields,
    }


def proposal(*opportunities):
    result = portfolio(0)
    result["opportunities"] = list(opportunities)
    return result


ALIASES = {"s001": "keyword:k1"}


def test_page_decisions_are_read_with_their_freshness_and_schema():
    found = sources.page_decisions(efficacy(), TODAY)
    assert found["status"] == "used" and found["generated"] == "2026-10-01"
    assert found["refresh"]["/pricing"] == {
        "checks": ["search.low_ctr"],
        "rule": "low_ctr",
        "reason": "Clicks are below 35% of the planning CTR estimate.",
    }
    assert {path: row["decision"] for path, row in found["cut"].items()} == {
        "/blog/old-ai-seo-tips": "retire",
        "/login": "noindex",
        "/compare/moz-vs-ahrefs": "merge",
    }
    assert found["keep"] == ["/docs"] and list(found["rewrite"]) == ["/about"]
    old = TODAY - timedelta(days=15)
    assert sources.page_decisions(efficacy(old), TODAY) == {
        "status": "older than 14 days (2026-09-16)"
    }
    assert sources.page_decisions(efficacy(TODAY - timedelta(days=14)), TODAY)["status"] == "used"
    assert sources.page_decisions(None, TODAY) == {"status": "none saved yet"}
    assert sources.page_decisions(efficacy(schema="content.efficacy/2"), TODAY) == {
        "status": "not content.efficacy/1"
    }
    assert sources.page_decisions(b"# Page decisions\n\nNo block.", TODAY)["status"] == (
        "no readable decisions block"
    )


def test_the_snapshot_counts_only_pages_with_enough_sessions_and_signups():
    found = sources.traffic_snapshot(snapshot(), TODAY)
    assert found["status"] == "used"
    traffic = sources.traffic_signals(found, "www.example.com")
    # /blog/tiny-tricks (3 sessions) and /blog/other (signups unmeasured) do not count.
    assert traffic["pages_counted"] == 6 and traffic["signups_counted"] == 44
    assert traffic["site_rate"] == round(44 / 1550, 4)
    assert [p["path"] for p in traffic["converting"]] == ["/integrations/slack", "/pricing", "/"]
    # Real traffic, few signups: the short-row page counts as well.
    assert [p["path"] for p in traffic["weak"]] == ["/blog/guide", "/blog/short-row"]
    # Too few signups overall: conversion says nothing.
    sparse = sources.traffic_snapshot(
        snapshot(pages=[page("/pricing", 400, 2), page("/blog/guide", 500, 1)], more=[]), TODAY
    )
    quiet = sources.traffic_signals(sparse, "example.com")
    assert quiet["site_rate"] is None and quiet["converting"] == quiet["weak"] == []
    assert quiet["note"].startswith("fewer than 5 signups")
    assert sources.traffic_snapshot(snapshot(schema="tin.traffic_snapshot/2"), TODAY) == {
        "status": "not tin.traffic_snapshot/1"
    }


def test_refresh_merge_and_retire_rows_shape_the_items():
    found = signals(efficacy(), snapshot())
    data = shaped_context(found)
    rows = {row["source_id"]: row for row in data["research"]["rows"]}
    refresh_paths = [
        r["data"]["path"] for r in rows.values() if r["source_id"].startswith("refresh:")
    ]
    # Kept and cut pages are no refresh candidates; Page decisions' refreshes and the weakly
    # converting pages are, after the near-the-top page.
    assert "/docs" not in refresh_paths
    assert refresh_paths[0] == "/pricing"
    assert {"/guides/setup", "/blog/guide", "/blog/short-row"} <= set(refresh_paths)
    tiers = {
        r["data"]["path"]: r["data"]["upside"]["tier"]
        for r in rows.values()
        if r["source_id"].startswith("refresh:")
    }
    assert tiers["/blog/guide"] == "weak_conversion"
    plan, quality = editorial.allocate(
        data,
        proposal(
            opportunity("keep-update", "Docs overhaul", action="update_page", page_id="p002"),
            opportunity(
                "merge-refresh",
                "Refresh the Moz vs Ahrefs page",
                "refresh",
                action="update_page",
                page_id="p003",
            ),
            opportunity("retired-topic", "Old AI SEO tips for 2026"),
            opportunity("login-answer", "Login", "answer"),
            opportunity("fine", "Distinct buyer task"),
        ),
        pages(),
        ALIASES,
        typed=True,
    )
    assert [i["id"] for b in plan["batches"] for i in b["items"]] == ["fine"]
    left_out = {entry["item_id"]: entry["reason"] for entry in quality["site_signals"]["left_out"]}
    assert left_out == {
        "keep-update": "Page decisions keeps /docs as it is",
        "merge-refresh": "Page decisions will merge /compare/moz-vs-ahrefs",
        "retired-topic": "its topic is /blog/old-ai-seo-tips, which Page decisions will retire",
        "login-answer": "its topic is /login, which Page decisions will noindex",
    }
    # Each page Page decisions marks for a refresh becomes a refresh item, once.
    plan, added = editorial.page_decision_items(data, plan)
    items = {i["destination"]: i for b in plan["batches"] for i in b["items"] if i.get("source")}
    assert set(items) == {f"{HOST}/pricing", f"{HOST}/guides/setup"} and len(added) == 2
    pricing = items[f"{HOST}/pricing"]
    assert pricing["kind"] == "refresh" and pricing["action"] == "update_page"
    assert (
        pricing["source"] == "organic.content_efficacy" and pricing["evidence"] == f"{HOST}/pricing"
    )
    assert "Clicks are below 35% of the planning CTR estimate." in pricing["brief"]
    assert pricing["source_ids"][0].startswith("efficacy:")
    assert any(s.startswith("refresh:") for s in pricing["source_ids"])
    assert legacy.parse_plan(legacy.canonical_json(plan)) == plan
    again, none = editorial.page_decision_items(data, plan)
    assert none == [] and again == plan
    quality["site_signals"]["added"] = added
    report = legacy.render_plan(plan, label="Roadmap", editorial=quality, pages=pages())
    assert (
        "Shaped by Page decisions of 2026-10-01 (refresh items added: 2; proposals left out: 4)"
        in report
    )
    assert "### Left out by Page decisions" in report
    assert "From: organic.content_efficacy" in report


def test_snapshot_conversion_moves_new_topics_up_with_minimum_counts():
    found = signals(None, snapshot())
    data = shaped_context(found)
    plan, quality = editorial.allocate(
        data,
        proposal(
            opportunity("seo", "AI SEO checklist for founders"),
            opportunity("tiny", "Tiny tricks for launch week"),
            opportunity("slack", "Slack alerts for deploy failures"),
        ),
        pages(),
        ALIASES,
        typed=True,
    )
    # Next to /integrations/slack, which converts at twice the site rate: first. The page with
    # three sessions converts every visitor but drives nothing.
    order = [i["id"] for b in plan["batches"] for i in b["items"]]
    assert order == ["slack", "seo", "tiny"]
    assert quality["site_signals"]["moved_up"] == [
        {"item_id": "slack", "near": "/integrations/slack"}
    ]
    assert quality["site_signals"]["refresh_candidates"] == ["/blog/guide", "/blog/short-row"]
    assert quality["site_signals"]["page_decisions"] is None
    assert quality["site_signals"]["note"] == (
        "Not used: page decisions (content/efficacy.md): none saved yet."
    )


def test_missing_or_stale_files_leave_the_plan_unchanged_with_a_note():
    stale = TODAY - timedelta(days=20)
    found = signals(None, snapshot(generated=stale))
    base = context()
    data = shaped_context(found, base)
    plain = deepcopy(base)
    plain["research"]["rows"] += sources.refresh_rows(FINDINGS, AUDIT)
    assert [r for r in data["research"]["rows"] if r["source_id"].startswith("refresh:")] == [
        r for r in plain["research"]["rows"] if r["source_id"].startswith("refresh:")
    ]
    offered = proposal(
        opportunity("slack", "Slack alerts for deploy failures"),
        opportunity("docs", "Docs overhaul", action="update_page", page_id="p002"),
    )
    shaped_plan, quality = editorial.allocate(data, offered, pages(), ALIASES, typed=True)
    plain_plan, _ = editorial.allocate(plain, offered, pages(), ALIASES, typed=True)
    assert shaped_plan == plain_plan
    assert editorial.page_decision_items(data, shaped_plan) == (shaped_plan, [])
    note = quality["site_signals"]["note"]
    assert note == (
        "Not used: page decisions (content/efficacy.md): none saved yet; traffic snapshot "
        "(analytics/traffic-snapshot.json): older than 14 days (2026-09-11)."
    )
    assert not quality["site_signals"]["left_out"] and not quality["site_signals"]["moved_up"]
    report = legacy.render_plan(shaped_plan, label="Roadmap", editorial=quality, pages=pages())
    assert markdown_text(note) in report and "Shaped by" not in report


async def test_nothing_reads_over_64_kb():
    assert sources.SIGNAL_FILE_BYTES == 64_000
    padded = efficacy() + b" " * (64_001 - len(efficacy()))
    files = {sources.EFFICACY_PATH: padded, sources.SNAPSHOT_PATH: b"{" + b" " * 64_000 + b"}"}

    class Storage:
        async def read_canonical_artifact_if_exists(self, *, repo_id, commit_sha, path):
            return files.get(path)

    found = await sources.site_signals(
        storage=Storage(),
        project=SimpleNamespace(state_repo_id="r"),
        revision="a" * 40,
        today=TODAY,
    )
    assert found == {
        "page_decisions": {"status": "over 64 KB"},
        "traffic": {"status": "over 64 KB"},
    }
    # A file just under the bound is used.
    files[sources.EFFICACY_PATH] = efficacy() + b" " * (63_999 - len(efficacy()))
    found = await sources.site_signals(
        storage=Storage(),
        project=SimpleNamespace(state_repo_id="r"),
        revision="a" * 40,
        today=TODAY,
    )
    assert found["page_decisions"]["status"] == "used"


async def test_the_planner_reads_both_files_at_its_revision(publication_db, monkeypatch):
    db, storage, project, configured, activities, _model, create = await setup(
        publication_db, monkeypatch, editorial=True
    )
    today = datetime.now(UTC).date()
    storage.repo.edit(
        {
            sources.EFFICACY_PATH: efficacy(today),
            sources.SNAPSHOT_PATH: snapshot(today),
        }
    )
    seen = {}

    async def fake_research(**kwargs):
        seen.update(kwargs)
        rows, extra = sources.typed_research_rows(
            FINDINGS,
            AUDIT,
            KEYWORDS,
            planned=kwargs["planned"],
            published=kwargs["published"],
            signals=kwargs["signals"],
        )
        return {
            "scope": {"host": "example.com", "market": "US"},
            "sources": {"audit": {"revision": "c" * 40}},
            "rows": [{"source_id": "keyword:k1", "data": {"keyword": "deploy alerts"}}, *rows],
            **extra,
        }

    class Model:
        inputs = []

        async def generate(self, key, request, *, timeout_seconds=None):
            data = json.loads(request.messages[0].content)
            self.inputs.append(data)
            alias = data["sources"][0]["source_id"]
            result = proposal(
                opportunity("seo", "AI SEO checklist for founders", source_ids=[alias]),
                opportunity("retired", "Old AI SEO tips for 2026", source_ids=[alias]),
                opportunity("slack", "Slack alerts for deploy failures", source_ids=[alias]),
            )
            return SimpleNamespace(parsed=result, usage=ModelUsage(), request_id="model-test")

    monkeypatch.setattr("tin_lite.content_plan_activities.research_sources", fake_research)
    activities.router = Model()
    run = await create()
    await activities.content_plan_execute(str(run.id))
    saved = await db.get_run(run.id)
    assert saved.status.value == "succeeded"
    assert seen["planned"] == {
        "/pricing": {"search.low_ctr"},
        "/guides/setup": {"search.near_page_one"},
    }
    view = Model.inputs[0]["site_signals"]
    assert ["/blog/old-ai-seo-tips", "retire"] in view["page_decisions"]["do_not_plan"]
    assert view["traffic"]["converting"][0][0] == "/integrations/slack"

    async def read(path):
        return await storage.read_canonical_artifact(
            repo_id=project.state_repo_id, commit_sha=saved.canonical_commit_sha, path=path
        )

    plan = legacy.parse_plan(await read(legacy.plan_path(configured.id)))
    items = [i for b in plan["batches"] for i in b["items"]]
    assert [i["id"] for i in items if not i.get("source")] == ["slack", "seo"]
    assert sorted(i["destination"] for i in items if i.get("source")) == [
        f"{HOST}/guides/setup",
        f"{HOST}/pricing",
    ]
    evidence = json.loads(await read(legacy.paths(str(run.id))["evidence.json"]))
    assert evidence["editorial"]["site_signals"]["left_out"][0]["item_id"] == "retired"
    report = (await read(legacy.paths(str(run.id))["PLAN.md"])).decode()
    assert f"Shaped by Page decisions of {today}" in report
    assert "topics moved up next to converting pages: 1" in report
