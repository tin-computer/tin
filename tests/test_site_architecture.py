"""organic.site_architecture offline: live-data inputs, one model call, the redirects contract."""

import datetime as dt
import hashlib
import json
import re

import pytest
from loop_workflow_fakes import context, definition, gsc, load

from tin_lite import planned_url_changes as planned
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result
from tin_lite.workflow_qualification import Qualification, assess_output

KEY = "organic.site_architecture"
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


def findings(*checks):
    return {
        "schema_version": 3,
        "target_host": SITE,
        "coverage": {"inspected_pages": 40, "sitemap_pages": 60},
        "findings": [
            {"id": f"oa_{i:020d}", "check_id": check, "urls": [f"https://{SITE}{path}"]}
            for i, (check, path) in enumerate(checks)
        ],
    }


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
    f"reports/organic-audit/{AUDIT}/findings.json": findings(
        ("discovery.possible_orphan", "/pricing"), ("search.cannibalization", "/blog/late-fees")
    ),
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
    assert spec.output_path == planned.ARCHITECTURE_PATH
    assert {r["provider_key"] for r in definition(KEY)["integration_requirements"]} == {
        "analytics.gsc"
    }
    assert [(r.name, r.max_calls) for r in spec.model_routes] == [("sections", 1)]
    assert definition(KEY)["code"]["services"]["gsc"]["max_calls"] <= 8


async def test_a_url_change_plan_hands_its_redirects_to_the_technical_fix(monkeypatch):
    content, ctx = await plan(monkeypatch, URL_CHANGE)
    assert "Status: stopped" not in content
    changes = planned.read_changes({planned.ARCHITECTURE_PATH: content}, dt.date(2026, 9, 29))
    assert {(c["from"], c["to"]) for c in changes} == {
        ("/features", "/product"),
        ("/features/reports", "/product/reports"),
        ("/old-reports", "/product/reports"),  # the chain goes straight to its final target
    }
    assert all(c["source"] == "organic.site_architecture" for c in changes)
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


async def test_click_depth_is_labelled_as_not_measured_and_orphans_come_from_the_crawl_sample(
    monkeypatch,
):
    content, _ = await plan(monkeypatch, {"mode": "plan"})
    assert "Click depth is not measured" in content
    assert "crawl sample (at most 100 pages)" in content
    assert (
        "- /pricing: URL depth 1; 120 clicks in 12 months; a possible orphan in the crawl sample."
        in content
    )
    # (b) fires on the orphaned key page, (c) on three integration pages without an index.
    assert "(b) a key or clicked page is a possible orphan" in content
    assert "integration under /integrations (3 pages)" in content
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
    assert planned.read_changes({planned.ARCHITECTURE_PATH: content}, dt.date(2026, 9, 29)) == []


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
    files = {**FILES, planned.ARCHITECTURE_PATH: first}
    content, ctx = await plan(monkeypatch, {"mode": "follow_up"}, files=files)
    assert "Status: stopped" in content and "applied_on" in content
    assert "Search Console before and after" not in content


async def test_follow_up_compares_old_plus_new_with_the_lowest_baseline_week(monkeypatch):
    first, _ = await plan(monkeypatch, URL_CHANGE)
    files = {**FILES, planned.ARCHITECTURE_PATH: first}
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
