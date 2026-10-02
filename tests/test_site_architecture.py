"""organic.site_architecture offline: live-data inputs, one model call, the redirects contract."""

import datetime as dt
import hashlib
import json
import re

import pytest
from loop_workflow_fakes import context, definition, gsc, load

from tin_lite.community import REPOSITORY_ROOT
from tin_lite.organic_audit_summary import PAGE_COLUMNS
from tin_lite.workflow_code import validate_code_definition, validate_code_result
from tin_lite.workflow_qualification import Qualification, assess_output

KEY = "organic.site_architecture"
# website.change (PR #266, source `planned`) reads the redirects block at this path; its shape
# is the contract, checked by redirects() below.
ARCHITECTURE_PATH = "reports/organic/site-architecture/SITE_ARCHITECTURE.md"
SITE = "northpine.example"
LAST = dt.date(2026, 9, 26)
AUDIT = "11111111-1111-4111-8111-111111111111"


def dates(last=LAST, days=370, **extra):
    return gsc(
        [{"keys": [str(last - dt.timedelta(days=i))], "clicks": 5} for i in range(days)], **extra
    )


def page(path, clicks, impressions):
    return {"keys": [f"https://{SITE}{path}"], "clicks": clicks, "impressions": impressions}


PAGES = [
    page("/", 900, 9000),
    page("/pricing", 120, 1500),
    page("/features/invoicing", 80, 2000),
    page("/features/reports", 40, 900),
    page("/blog/late-fees", 60, 3000),
    page("/blog/invoice-template", 30, 1200),
    page("/integrations/stripe", 5, 300),
    page("/integrations/xero", 3, 200),
    page("/integrations/quickbooks", 2, 150),
    page(f"/invite/{'a' * 32}", 1, 5),
]


def redirects(content):
    """The plan's redirects.json block, as website.change reads it: permanent moves only."""
    found = re.search(
        r"<!-- redirects\.json:start -->\s*```json\s*(\{.*?\})\s*```\s*"
        r"<!-- redirects\.json:end -->",
        content,
        re.S,
    )
    if not found:
        return []
    block = json.loads(found.group(1))
    assert block["schema"] == "site_architecture.redirects/1"
    assert dt.date.fromisoformat(block["generated"]) and block["plan_id"]
    for row in block["redirects"]:
        assert set(row) == {"old", "new", "status", "reason"} and row["status"] == 301
        assert row["old"].startswith("/") and row["new"].startswith("/")
    return block["redirects"]


def baseline_rows():
    rows = []
    for i in range(56):
        day = str(LAST - dt.timedelta(days=i))
        rows.append({"keys": [day, f"https://{SITE}/features/reports"], "clicks": 1})
    return gsc(rows)


def snapshot():
    return {
        "schema": "tin.traffic_snapshot/1",
        "generated_at": "2026-09-28T13:00:00Z",
        "definitions": {"website_hosts": [SITE]},
        "totals": {"visits": {"current": {"sessions": 500}}},
        "audit": {"run_id": AUDIT},
        "pages": [
            {"page": f"{SITE}/", "visits": {"current": {"sessions": 300}}},
            {"page": f"{SITE}/features/reports", "visits": {"current": {"sessions": 25}}},
        ],
        "more_pages": [],
        "entry_only_pages": [[f"{SITE}/changelog", 0, 0, 0, 0, None, None, 12]],
    }


def summary(rows, *, by_check=(), depth="exact", host=SITE, columns=PAGE_COLUMNS, **extra):
    """An organic audit summary (LATEST.json) as policy v12 and later write it."""
    checks = [
        {"check": check, "findings": found, "pages": pages, "priority": "high_impact", "listed": n}
        for check, found, pages, n in by_check
    ]
    return {
        "schema_version": 1,
        "kind": "organic_audit_summary",
        "run_id": AUDIT,
        "host": host,
        "hosts": [host],
        "site_url": f"https://{host}/",
        "market": "US",
        "audited_at": "2026-09-27T00:00:00+00:00",
        "policy_version": "organic-audit-v13",
        "coverage": {
            "sitemap_pages": 60,
            "inspected_pages": 40,
            "crawled_pages": 40,
            "read_pages": 38,
        },
        "findings": {"total": sum(c["findings"] for c in checks), "by_check": checks},
        "links": {"status": "observed", "start": "/", "depth": depth, "unreached": 1},
        "pages": {
            "columns": list(columns),
            "rows": [[row.get(column) for column in columns] for row in rows],
            "total": len(rows),
        },
        "truncated": False,
        **extra,
    }


def row(path, depth, inbound, *checks):
    return {
        "path": path,
        "status": 200,
        "indexable": True,
        "depth": depth,
        "inbound": inbound,
        "checks": list(checks),
    }


# Checks are positions in by_check: 0 is the possible orphan, 1 the competing pages.
LATEST = summary(
    [
        row("/", 0, 0),
        row("/pricing", None, 0, 0),
        row("/features/invoicing", 1, 3),
        row("/features/reports", 2, 2),
        row("/blog/late-fees", 4, 1, 1),
        row("/blog/invoice-template", 2, 1, 1),
    ],
    by_check=[("discovery.possible_orphan", 1, 1, 1), ("search.cannibalization", 1, 2, 2)],
)


def efficacy():
    block = {
        "schema": "content.efficacy/1",
        "generated": "2026-09-28",
        "url_changes": [
            {
                "kind": "301",
                "from": "/blog/invoice-template",
                "to": "/blog/late-fees",
                "reason": "same intent",
            }
        ],
    }
    return "# Page decisions\n\n## Decisions block\n\n```json\n" + json.dumps(block) + "\n```\n"


FILES = {
    "analytics/traffic-snapshot.json": snapshot(),
    "reports/organic-audit/LATEST.json": LATEST,
    "content/efficacy.md": efficacy(),
}
LABELS = {
    "sections": [
        {"id": "S1", "label": "Home", "group": "product", "nav": "none", "page_type": "home"},
        {
            "id": "S2",
            "label": "Pricing",
            "group": "product",
            "nav": "header",
            "page_type": "pricing",
        },
        {
            "id": "S3",
            "label": "Features",
            "group": "product",
            "nav": "header",
            "page_type": "feature",
        },
        {"id": "S4", "label": "Blog", "group": "resources", "nav": "footer", "page_type": "blog"},
    ]
}


def services(**overrides):
    return {
        "G1_dates": dates(),
        "G2_pages_12m": gsc(PAGES),
        "G3_baseline": baseline_rows(),
        **overrides,
    }


async def plan(monkeypatch, inputs=None, files=FILES, models=None, **overrides):
    module = load(KEY, monkeypatch)
    ctx = context(
        files=files, services=services(**overrides), models=models or {"name_sections": LABELS}
    )
    result = await module.run(ctx, dict(inputs or {}))
    validate_code_result(json.dumps(result).encode(), validate_code_definition(definition(KEY)))
    return result["content"], ctx


URL_CHANGE = {
    "mode": "plan",
    "planned_change": "url_change",
    "new_paths": "/features -> /product\n/features/reports -> /product/reports\n"
    "/old-reports -> /features/reports\n/pricing",
}


def test_it_reads_live_data_only_and_writes_the_path_the_technical_fix_reads():
    spec = validate_code_definition(definition(KEY))
    assert spec.output_path == ARCHITECTURE_PATH
    assert {r["provider_key"] for r in definition(KEY)["integration_requirements"]} == {
        "analytics.gsc"
    }
    assert [(r.name, r.max_calls) for r in spec.model_routes] == [("sections", 1)]
    assert definition(KEY)["code"]["services"]["gsc"]["max_calls"] <= 8


async def test_a_url_change_plan_writes_its_redirects_for_website_change(monkeypatch):
    content, ctx = await plan(monkeypatch, URL_CHANGE)
    assert "Status: stopped" not in content
    assert {(r["old"], r["new"]) for r in redirects(content)} == {
        ("/features", "/product"),
        ("/features/reports", "/product/reports"),
        ("/old-reports", "/product/reports"),  # the chain goes straight to its final target
    }
    # Clicked pages the new routes do not cover become questions, never invented redirects.
    assert "- /features/invoicing: 80 clicks in 12 months; redirect, 404 or 410?" in content
    assert "/blog/late-fees" in content and "/invite/" not in content
    # Page decisions' change is listed, not repeated as a redirect row.
    assert "- 301 /blog/invoice-template -> /blog/late-fees: same intent" in content
    assert [c["step"] for c in ctx.services.calls] == ["G1_dates", "G2_pages_12m", "G3_baseline"]
    assert [c["step"] for c in ctx.models.calls] == ["name_sections"]
    baseline = json.loads(
        re.search(r"baseline\.json:start -->\n```json\n(.*?)\n```", content, re.S).group(1)
    )
    assert "/blog/late-fees: 60 clicks" not in content  # outside the moved paths
    reports = next(g for g in baseline["groups"] if g["old"] == "/features/reports")
    assert [w["clicks"] for w in reports["weeks"]] == [7] * 8
    assert reports["weeks"][-1]["end"] == str(LAST)


async def test_click_depth_and_orphans_come_from_the_audit_summary(monkeypatch):
    content, ctx = await plan(monkeypatch, {"mode": "plan"})
    assert "reports/organic-audit/LATEST.json" in ctx.files.reads
    assert not any(path.endswith("findings.json") for path in ctx.files.reads)
    assert "Click depth is not measured" not in content
    assert "They are exact: every sitemap page was read" in content
    assert (
        "- /pricing: not reached from home through the pages the audit read; URL depth 1; "
        "120 clicks in 12 months; a possible orphan in the audit's crawl." in content
    )
    assert "- /features/invoicing: 1 click from home; URL depth 2; 80 clicks" in content
    assert "| /blog/late-fees | /blog | 4 | 1 | 2 | 60 |" in content
    assert "| /pricing | /pricing | not reached | 0 | 1 | 120 |" in content
    # (b) fires on the orphaned key page and the key page four clicks deep, (c) on three
    # integration pages without an index, (f) on the audit's competing pages.
    assert "(b) a key or clicked page is a possible orphan" in content
    assert "more than 3 clicks from home: /blog/late-fees" in content
    assert "integration under /integrations (3 pages)" in content
    assert "(f) pages compete for one search (1 audit findings" in content
    assert (
        "Header, by 12-month search clicks, signup last: Features (/features), Pricing (/pricing)"
        in content
    )
    assert '"pattern": "/features/{slug}"' in content


async def test_the_report_hash_verifies(monkeypatch):
    content, _ = await plan(monkeypatch, URL_CHANGE)
    digest = re.search(r"Content hash: ([0-9a-f]{64})", content).group(1)
    assert f'"plan_hash": "{digest}"' in content
    pending = content.replace(digest, "PENDING")
    assert hashlib.sha256(pending.encode()).hexdigest() == digest


async def test_an_unusable_model_answer_keeps_the_paths_as_labels(monkeypatch):
    """A plausible answer that names sections Tin never sent and invents a group."""
    bad = {
        "sections": [
            {"id": "S99", "label": "Shop", "group": "store", "nav": "header", "page_type": "other"}
        ]
    }
    content, _ = await plan(monkeypatch, {"mode": "plan"}, models={"name_sections": bad})
    assert "The model named 0 of" in content
    assert "Features (/features)" not in content


async def test_redirects_never_point_unrelated_pages_home_and_loops_are_refused(monkeypatch):
    inputs = {**URL_CHANGE, "new_paths": "/features/reports -> /\n/a -> /b\n/b -> /a"}
    content, _ = await plan(monkeypatch, inputs)
    assert "Not a redirect: /features/reports would redirect to the home page" in content
    assert "loops back to itself" in content
    assert redirects(content) == []


async def test_a_truncated_page_read_stays_unknown(monkeypatch):
    truncated = gsc(PAGES[:3], truncated=True, next_start_row=3)
    content, _ = await plan(monkeypatch, URL_CHANGE, G2_pages_12m=truncated)
    assert "G2_pages_12m was truncated" in content
    assert "| unknown |" in content


async def test_without_a_trigger_it_stops_with_the_unknowns(monkeypatch):
    few = gsc([page("/", 90, 900), page("/about", 3, 40)])
    content, ctx = await plan(monkeypatch, {"mode": "plan"}, files={}, G2_pages_12m=few)
    assert "Status: stopped" in content and "Run again when" in content
    assert "(e) breadcrumbs" in content and "not assessed" in content
    assert ctx.models.calls == []
    assert "redirects.json:start" not in content


async def test_follow_up_needs_the_change_date(monkeypatch):
    first, _ = await plan(monkeypatch, URL_CHANGE)
    files = {**FILES, ARCHITECTURE_PATH: first}
    content, ctx = await plan(monkeypatch, {"mode": "follow_up"}, files=files)
    assert "Status: stopped" in content and "applied_on" in content
    assert "Search Console before and after" not in content


async def test_follow_up_compares_old_plus_new_with_the_lowest_baseline_week(monkeypatch):
    first, _ = await plan(monkeypatch, URL_CHANGE)
    files = {**FILES, ARCHITECTURE_PATH: first}
    after = gsc(
        [
            {
                "keys": [str(LAST - dt.timedelta(days=i)), f"https://{SITE}/product/reports"],
                "clicks": 0,
            }
            for i in range(14)
        ]
    )
    inputs = {"mode": "follow_up", "applied_on": "2026-09-13"}
    content, _ = await plan(monkeypatch, inputs, files=files, G2_weekly_old_new=after)
    assert "## Search Console before and after" in content
    assert (
        "/features/reports -> /product/reports | 7 | 0 | below the lowest baseline week" in content
    )
    assert "a redirect is confirmed only by a live fetch" in content


QUALIFICATION = {
    "url_change": ({}, FILES),
    "orphaned_pricing": ({}, FILES),
    "no_audit_summary": (
        {},
        {k: v for k, v in FILES.items() if k != "reports/organic-audit/LATEST.json"}
        | {f"reports/organic-audit/{AUDIT}/findings.json": {"schema_version": 3}},
    ),
    "thin_data_stop": ({"G2_pages_12m": gsc([page("/", 90, 900)])}, {}),
    "follow_up_without_date": ({}, FILES),
    "truncated_gsc": (
        {"G2_pages_12m": gsc(PAGES[:3], truncated=True, next_start_row=3)},
        FILES,
    ),
}


async def test_qualification_cases_pass_on_their_fixtures(monkeypatch):
    raw = (REPOSITORY_ROOT / "workflow_evals" / KEY / "qualification.json").read_text()
    qualification = Qualification.model_validate_json(raw)
    assert {case.id for case in qualification.cases} == set(QUALIFICATION)
    for case in qualification.cases:
        overrides, files = QUALIFICATION[case.id]
        module = load(KEY, monkeypatch)
        ctx = context(
            files=files,
            services=services(**overrides),
            models={"name_sections": LABELS},
        )
        result = await module.run(ctx, dict(case.inputs))
        verdict = assess_output(case, status="succeeded", content=result["content"].encode())
        assert verdict["status"] == "passed", (case.id, verdict["checks"])


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("/a -> /a", "points at itself"),
        ("/a -> /b\n/a -> /c", "moved twice"),
        ("https://other.example/a -> /b", "is not a path on this site"),
    ],
)
def test_new_paths_are_validated_line_by_line(monkeypatch, text, error):
    _, _, errors = load(KEY, monkeypatch).parse_moves(text)
    assert any(error in e for e in errors), errors


async def test_an_audit_of_another_host_is_set_aside(monkeypatch):
    files = {**FILES, "reports/organic-audit/LATEST.json": summary([], host="elsewhere.example")}
    content, _ = await plan(monkeypatch, URL_CHANGE, files=files)
    assert (
        "The organic audit is for elsewhere.example, not northpine.example; set aside." in content
    )
    assert "Click depth is not measured: there is no organic audit summary" in content
    assert "| not measured | not measured |" in content


async def test_an_upper_bound_depth_says_so_and_never_fires_the_depth_trigger(monkeypatch):
    latest = {
        **LATEST,
        "links": {**LATEST["links"], "depth": "at_most"},
        "truncated": {"columns": ["description", "title"], "pages": 12},
        "pages": {**LATEST["pages"], "total": 18},
        "findings": {
            **LATEST["findings"],
            "by_check": [
                {**LATEST["findings"]["by_check"][0], "pages": 5},
                LATEST["findings"]["by_check"][1],
            ],
        },
    }
    files = {**FILES, "reports/organic-audit/LATEST.json": latest}
    content, _ = await plan(monkeypatch, {"mode": "plan"}, files=files)
    assert "Depths are upper bounds" in content
    assert "- /features/invoicing: at most 1 click from home;" in content
    assert "more than 3 clicks from home: /blog/late-fees" not in content
    assert "The summary left out 12 of 18 pages and these columns to fit 64 KB" in content
    # The check names five pages; one is a row of the summary.
    assert "possible-orphan check names 5 pages; 1 are rows of its summary" in content


async def test_it_reads_the_summary_the_audit_writes(monkeypatch):
    from test_organic_audit_v12 import documents, site_evidence

    from tin_lite.organic_audit import AUDIT_POLICY, LATEST_SUMMARY_PATH

    files, pages = await site_evidence(AUDIT_POLICY)
    latest = documents(AUDIT_POLICY, files, pages)[LATEST_SUMMARY_PATH].decode()
    ctx = context(files={LATEST_SUMMARY_PATH: latest})
    notes = []
    audit = load(KEY, monkeypatch).latest_audit(ctx, notes)
    assert notes == [] and audit["host"] == "example.com" and audit["depth"] == "exact"
    depth = {path: row["depth"] for path, row in audit["rows"].items()}
    assert depth == {
        "/": 0,
        "/pricing": 1,
        "/blog": 1,
        "/blog/post-a": 2,
        "/blog/post-b": 2,
        "/blog/post-c": 3,
        "/orphan": None,
    }
    assert "/orphan" in audit["orphans"] and "/" not in audit["orphans"]
