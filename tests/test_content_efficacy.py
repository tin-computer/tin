"""organic.content_efficacy offline: a synthetic snapshot, audit and judgment, frozen clock."""

import json
import re

from loop_workflow_fakes import context, definition, gsc, load

from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result
from tin_lite.workflow_qualification import Qualification, assess_output

KEY = "organic.content_efficacy"
SITE = "tallyfox.example"
CHANNELS = [
    "Paid",
    "Email",
    "Social",
    "Direct",
    "Internal",
    "AI assistants",
    "Search",
    "Referral",
    "Unknown",
    "Ambiguous",
]


def row(path, now, before, position=1.0, queries=(), paid=0, first_seen="2026-01-01"):
    visits = [0] * len(CHANNELS)
    visits[0] = paid
    return {
        "page": f"{SITE}{path}",
        "first_seen": first_seen,
        "first_seen_exact": True,
        "search": {
            "current": [now[0], now[1], None, position],
            "prior": [before[0], before[1], None, position],
            "queries": [[q, 0, 500, 1, False] for q in queries],
        },
        "visits": {"current": {"sessions": sum(visits), "by_channel": visits}},
        "signups": {"first_touch": [0, 0], "activated": [0, 0]},
        "audit": [],
    }


def snapshot(**extra):
    pages = [
        row("/sign-in", (48, 1100), (50, 1000), queries=["tallyfox login"]),
        row("/", (100, 900), (95, 850), queries=["tallyfox", "tallyfox app"]),
        row("/compare/quickbooks-alternatives", (4, 380), (5, 410)),
        row("/alternatives/quickbooks", (31, 2100), (34, 2250)),
        row("/blog/invoice-late-fee-template", (22, 4950), (34, 5100)),
        row("/blog/mileage-log-template", (18, 5000), (19, 4800), position=4.1),
        row("/blog/new-post", (0, 20), (0, 0), first_seen="2026-09-01"),
        row("/offer/spring-sale", (0, 40), (0, 30)),
        row("/blog/earning-post", (60, 900), (58, 880), position=3.0, queries=["studio cash flow"]),
    ]
    value = {
        "schema": "tin.traffic_snapshot/1",
        "generated_at": "2026-09-28T13:00:00Z",
        "status": "complete",
        "definitions": {"website_hosts": [SITE], "channels": CHANNELS, "short_columns": []},
        "windows": {"current": ["2026-08-30", "2026-09-26"], "prior": ["2026-08-02", "2026-08-29"]},
        "totals": {"visits": {"current": {"sessions": 300, "by_channel": [0] * 10}}},
        "pages": pages,
        "rest": {"visits": {"paths": 0}},
        "trimmed": {"short_rows_dropped": 0},
    }
    value.update(extra)
    return value


JUDGMENT = {
    "pairs": [],
    "queries": [{"url": "/blog/earning-post", "verdict": "answers"}],
    "sections": [],
}
FILES = {
    "analytics/traffic-snapshot.json": snapshot(),
    "brand/BRAND.md": "# Tallyfox\n\nInvoicing for small studios.",
}


async def run(monkeypatch, files=FILES, model=JUDGMENT, services=None, inputs=None):
    module = load(KEY, monkeypatch)
    ctx = context(files=files, services=services or {}, models={"judgment": model})
    result = await module.run(ctx, dict(inputs or {}))
    validate_code_result(json.dumps(result).encode(), validate_code_definition(definition(KEY)))
    return result["content"], ctx


def block(content):
    return json.loads(
        re.search(r"## Decisions block\s*```json\s*(\{.*?\})\s*```", content, re.S)[1]
    )


def decisions(content):
    return {r["url"]: r for r in block(content)["decisions"]}


def test_manifest_fits_the_code_contract():
    spec = validate_code_definition(definition(KEY))
    assert spec.output_path == "content/efficacy.md"
    assert [route.name for route in spec.model_routes] == ["judgment"]


async def test_ordinary_week_decides_every_page(monkeypatch):
    content, ctx = await run(monkeypatch)
    rows = decisions(content)
    assert (rows["/sign-in"]["decision"], rows["/sign-in"]["action"]) == ("retire", "noindex")
    merge = rows["/compare/quickbooks-alternatives"]
    assert (merge["decision"], merge["action"], merge["target"]) == (
        "merge",
        "301",
        "/alternatives/quickbooks",
    )
    assert rows["/alternatives/quickbooks"]["rule"] == "merge_survivor"
    assert rows["/blog/invoice-late-fee-template"]["rule"] == "decline"
    mileage = rows["/blog/mileage-log-template"]
    assert (mileage["action"], mileage["owner"]) == ("title_description", "content.refresh")
    assert rows["/blog/new-post"]["action"] == "wait"
    assert (rows["/offer/spring-sale"]["decision"], rows["/offer/spring-sale"]["action"]) == (
        "retire",
        "noindex",
    )
    assert rows["/blog/earning-post"]["decision"] == "keep"
    changes = block(content)["url_changes"]
    assert {(c["from"], c["kind"], c["owner"]) for c in changes} == {
        ("/sign-in", "noindex", "website.change"),
        ("/compare/quickbooks-alternatives", "301", "website.change"),
        ("/offer/spring-sale", "noindex", "website.change"),
    }
    assert "asks you before it makes one" in content
    # website.change (PR #266, source `planned`) reads this block; its shape is the contract.
    assert block(content)["schema"] == "content.efficacy/1" and block(content)["generated"]
    assert all({"from", "to", "kind", "reason", "confirmed", "owner"} <= set(c) for c in changes)
    assert "approved" not in content.lower()  # no approval to hand-edit in this file
    assert [c["step"] for c in ctx.services.calls] == []  # a fresh snapshot needs no GSC reads
    assert "Tallyfox" in json.dumps(ctx.models.calls[0]["data"]["sources"])


async def test_a_repeat_proposal_is_marked_confirmed(monkeypatch):
    first, _ = await run(monkeypatch)
    files = {**FILES, "content/efficacy.md": first}
    second, _ = await run(monkeypatch, files=files)
    assert decisions(second)["/sign-in"]["confirmed"] is True
    assert "Proposed two weeks running." in second


async def test_paid_visits_block_a_merge(monkeypatch):
    pages = snapshot()["pages"]
    pages[2] = row("/compare/quickbooks-alternatives", (4, 380), (5, 410), paid=3)
    files = {**FILES, "analytics/traffic-snapshot.json": snapshot(pages=pages)}
    content, _ = await run(monkeypatch, files=files)
    row_ = decisions(content)["/compare/quickbooks-alternatives"]
    assert row_["decision"] == "keep" and "paid visits recorded" in row_["reason"]


async def test_unusable_judgment_changes_no_decision(monkeypatch):
    """Plausible but unusable: verdicts on pages we never asked about and a made-up quote."""
    unusable = {
        "pairs": [{"a": "/blog/earning-post", "b": "/", "verdict": "same_intent"}],
        "queries": [{"url": "/blog/mileage-log-template", "verdict": "does_not_answer"}],
        "sections": [{"section": "/blog", "verdict": "off_positioning", "quote": "we sell boats"}],
    }
    content, _ = await run(monkeypatch, model=unusable)
    rows = decisions(content)
    assert rows["/blog/earning-post"]["decision"] == "keep"
    assert not any(r["decision"] == "rewrite" for r in rows.values())


async def test_a_malformed_judgment_is_retried_then_skipped(monkeypatch):
    content, ctx = await run(monkeypatch, model={"pairs": "none"})
    assert [c["step"] for c in ctx.models.calls] == ["judgment", "judgment_retry"]
    assert "Model: skipped." in content
    assert decisions(content)["/sign-in"]["action"] == "noindex"  # code rules still apply


def search_console():
    pages = [
        {"keys": [f"https://{SITE}/"], "clicks": 100, "impressions": 900, "position": 1.0},
        {"keys": [f"https://{SITE}/sign-in"], "clicks": 48, "impressions": 1100, "position": 1.0},
        {"keys": [f"https://{SITE}/blog/old-launch-notes"], "clicks": 0, "impressions": 5},
    ]
    return {
        "gsc_pages_current": gsc(pages),
        "gsc_pages_prior": gsc(pages),
        "gsc_low_query": gsc([]),
    }


async def test_without_a_snapshot_no_url_changes_are_written(monkeypatch):
    content, ctx = await run(monkeypatch, files={}, services=search_console())
    assert "No fresh snapshot; 3 direct Search Console calls." in content
    assert block(content)["url_changes"] == []
    assert "no merge or retirement was written" in content


CASES = {
    "ordinary": (FILES, JUDGMENT, None),
    "no_snapshot": ({}, JUDGMENT, search_console()),
    "unusable_judgment": (FILES, {"pairs": "none"}, None),
    # Called when the case runs: brand_snapshot is defined further down.
    "brand_searches": (
        lambda: {**FILES, "analytics/traffic-snapshot.json": brand_snapshot()},
        JUDGMENT,
        None,
    ),
}


async def test_qualification_cases_pass_on_their_fixtures(monkeypatch):
    raw = (REPOSITORY_ROOT / "workflow_evals" / KEY / "qualification.json").read_text()
    qualification = Qualification.model_validate_json(raw)
    assert {case.id for case in qualification.cases} == set(CASES)
    for case in qualification.cases:
        files, model, services = CASES[case.id]
        files = files() if callable(files) else files
        content, _ = await run(
            monkeypatch, files=files, model=model, services=services, inputs=case.inputs
        )
        verdict = assess_output(case, status="succeeded", content=content.encode())
        assert verdict["status"] == "passed", (case.id, verdict["checks"])


def audit_summary(rows, by_check, *, host=SITE, run_id="22222222-2222-4222-8222-222222222222"):
    """An organic audit summary as policy v12 and later write it (LATEST.json)."""
    from tin_lite.organic_audit_summary import PAGE_COLUMNS

    return {
        "schema_version": 1,
        "kind": "organic_audit_summary",
        "run_id": run_id,
        "host": host,
        "policy_version": "organic-audit-v13",
        "findings": {
            "by_check": [
                {"check": check, "findings": found, "pages": pages, "listed": listed}
                for check, found, pages, listed in by_check
            ]
        },
        "pages": {
            "columns": list(PAGE_COLUMNS),
            "rows": [
                [{"path": path, "checks": checks}.get(column) for column in PAGE_COLUMNS]
                for path, checks in rows
            ],
            "total": len(rows),
        },
        "truncated": False,
    }


# Positions in by_check: 0 competing pages (one finding), 1 a utility page in search, 2 a
# site-level check that names no page row.
SUMMARY = audit_summary(
    [
        ("/blog/invoice-late-fee-template", [0]),
        ("/blog/mileage-log-template", [0]),
        ("/sign-in", [1]),
        ("/", []),
    ],
    [
        ("search.cannibalization", 1, 2, 2),
        ("indexation.utility_pages_indexable", 1, 1, 1),
        ("crawl.robots_blocks_sitemap", 1, 3, 0),
    ],
)


async def test_page_checks_come_from_the_audit_summary(monkeypatch):
    files = {**FILES, "reports/organic-audit/LATEST.json": SUMMARY}
    content, ctx = await run(monkeypatch, files=files)
    assert "reports/organic-audit/LATEST.json" in ctx.files.reads
    assert not any(path.endswith("findings.json") for path in ctx.files.reads)
    rows = decisions(content)
    assert rows["/sign-in"]["evidence"]["audit"] == ["indexation.utility_pages_indexable"]
    found = block(content)
    assert found["sources"]["audit"] == "22222222-2222-4222-8222-222222222222"
    pair = next(g for g in found["groups"] if g["summary"].startswith("The audit found"))
    assert pair["kind"] == "duplicate_pair" and pair["pages"] == 2
    # Site-level and example-only checks are named, never spread over pages.
    assert "crawl.robots_blocks_sitemap (0 of 3)" in content


async def test_an_audit_of_another_site_or_without_pairs_is_not_guessed(monkeypatch):
    other = audit_summary([("/sign-in", [0])], [("search.cannibalization", 3, 6, 1)])
    files = {**FILES, "reports/organic-audit/LATEST.json": {**other, "host": "elsewhere.example"}}
    content, _ = await run(monkeypatch, files=files)
    assert "the organic audit is for elsewhere.example, not tallyfox.example" in content
    assert block(content)["sources"]["audit"] == "none"
    files = {**FILES, "reports/organic-audit/LATEST.json": other}
    content, _ = await run(monkeypatch, files=files)
    assert "the audit found 3 sets of competing pages; its summary does not say" in content
    assert not any(g["summary"].startswith("The audit found") for g in block(content)["groups"])


async def test_a_named_audit_needs_its_summary(monkeypatch):
    run_id = "33333333-3333-4333-8333-333333333333"
    named = audit_summary([("/sign-in", [0])], [("indexation.utility_pages_indexable", 1, 1, 1)])
    files = {
        **FILES,
        "reports/organic-audit/LATEST.json": SUMMARY,
        f"reports/organic-audit/{run_id}/SUMMARY.json": {**named, "run_id": run_id},
    }
    content, ctx = await run(monkeypatch, files=files, inputs={"audit_run_id": run_id})
    assert block(content)["sources"]["audit"] == run_id
    assert "reports/organic-audit/LATEST.json" not in ctx.files.reads
    old = "44444444-4444-4444-8444-444444444444"
    files[f"reports/organic-audit/{old}/findings.json"] = {"schema_version": 3}
    content, _ = await run(monkeypatch, files=files, inputs={"audit_run_id": old})
    assert "that audit wrote no SUMMARY.json" in content
    assert block(content)["sources"]["audit"] == "none"


def brand_snapshot():
    """/about and /help rank only for the brand, however it is spaced."""
    pages = snapshot()["pages"] + [
        row("/about", (2, 700), (3, 650), position=3.2, queries=["tallyfox", "tally fox.com"]),
        row("/help", (1, 650), (2, 600), position=6.0, queries=["tallyfox", "tally-fox"]),
    ]
    return snapshot(pages=pages)


async def test_brand_searches_drive_no_refresh_pair_or_judgment(monkeypatch):
    files = {**FILES, "analytics/traffic-snapshot.json": brand_snapshot()}
    content, ctx = await run(monkeypatch, files=files)
    rows = decisions(content)
    about = rows["/about"]
    assert (about["decision"], about["rule"]) == ("keep", "brand_search")
    assert "brand searches" in about["reason"]
    # Ranking 4-15 for "tally-fox" is not a near-page-one opportunity either.
    assert (rows["/help"]["decision"], rows["/help"]["rule"]) == ("keep", "brand_search")
    found = block(content)["groups"]
    assert not any(g["kind"] == "duplicate_pair" and "tallyfox" in g["summary"] for g in found)
    asked = ctx.models.calls[0]["data"]["queries"]
    assert [item["url"] for item in asked] == ["/blog/earning-post"]
    assert "tally" not in json.dumps(asked)
    # The homepage still gets a title refresh when its own brand searches do not click.
    pages = brand_snapshot()["pages"]
    pages[1] = row("/", (2, 900), (3, 850), position=2.0, queries=["tallyfox"])
    files = {**FILES, "analytics/traffic-snapshot.json": snapshot(pages=pages)}
    content, _ = await run(monkeypatch, files=files)
    assert decisions(content)["/"]["rule"] == "low_ctr"


async def test_without_a_snapshot_the_site_comes_from_search_console(monkeypatch):
    content, ctx = await run(monkeypatch, files={}, services=search_console())
    assert block(content)["site"] == f"https://{SITE}"
    assert "site origin unavailable" not in content
    low = next(c for c in ctx.services.calls if c["step"] == "gsc_low_query")
    expression = low["arguments"]["dimension_filters"][0]["expression"]
    assert expression.startswith(r"^https?://(?:www\.)?tallyfox\.example(?:")
    assert re.search(expression, f"https://www.{SITE}/blog/old-launch-notes/?ref=x")
    assert not re.search(expression, f"https://{SITE}/blog/other")


def variant_console():
    """www. and bare homepages, a trailing-slash twin and a ?ref= variant of one page."""
    current = [
        {"keys": [f"https://{SITE}/"], "clicks": 2695, "impressions": 44100, "position": 9.0},
        {"keys": [f"https://www.{SITE}/"], "clicks": 1, "impressions": 1, "position": 2.0},
        {"keys": [f"https://{SITE}/guide/"], "clicks": 15, "impressions": 900, "position": 4.0},
        {"keys": [f"https://{SITE}/guide"], "clicks": 2, "impressions": 100, "position": 14.0},
        {"keys": [f"https://{SITE}/guide?ref=nav"], "clicks": 1, "impressions": 0},
        {"keys": [f"https://{SITE}/cdn-cgi/l/email-protection"], "clicks": 0, "impressions": 9},
    ]
    prior = [
        {"keys": [f"https://{SITE}/"], "clicks": 1518, "impressions": 46488, "position": 14.0},
        {"keys": [f"https://{SITE}/guide/"], "clicks": 30, "impressions": 800, "position": 5.0},
    ]
    query = [
        {"keys": [f"https://{SITE}/guide/", "ledger guide"], "clicks": 3, "impressions": 60},
        {"keys": [f"https://{SITE}/guide", "ledger guide"], "impressions": 40, "position": 6.0},
    ]
    return {
        "gsc_pages_current": gsc(current),
        "gsc_pages_prior": gsc(prior),
        "gsc_low_query": gsc(query),
    }


async def test_url_variants_fold_into_one_page(monkeypatch):
    previous = {
        "schema": "content.efficacy/1",
        "generated": "2026-09-22",
        "decisions": [
            {"url": "/guide?ref=nav", "decision": "keep", "rule": "earning", "clicks": [0, 0]},
            {"url": "/cdn-cgi/l/email-protection", "decision": "keep", "rule": "earning"},
        ],
    }
    summary = audit_summary(
        [("/guide", [0]), ("/cdn-cgi/l/email-protection", [0]), ("/guide/?ref=x", [0])],
        [("search.brand_landing_page", 1, 1, 1)],
    )
    files = {
        "content/efficacy.md": "## Decisions block\n```json\n" + json.dumps(previous) + "\n```",
        "reports/organic-audit/LATEST.json": summary,
    }
    content, _ = await run(monkeypatch, files=files, services=variant_console())
    rows = decisions(content)
    assert set(rows) == {"/", "/guide/"}  # no ?ref=, slash twin or /cdn-cgi/ row
    home = rows["/"]["evidence"]
    assert home["clicks"] == [2696, 1518] and home["impressions"] == [44101, 46488]
    guide = rows["/guide/"]["evidence"]
    assert guide["clicks"] == [18, 30] and guide["impressions"] == [1000, 800]
    assert guide["position"][0] == 5.0  # impression-weighted: (4 * 900 + 14 * 100) / 1000
    assert guide["audit"] == ["search.brand_landing_page"]
    # Clicks fell while impressions held, across both spellings; their searches are merged.
    assert (rows["/guide/"]["rule"], guide["top_query"]) == ("decline", "ledger guide")
    assert "Clicks and impressions fell together" not in json.dumps(rows["/"])
    # The variant's old row counts as the page's, so this is a comparison, not a first run.
    assert "First run" not in content
