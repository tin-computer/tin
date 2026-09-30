"""organic-audit-v10 checks, one finding format, and compatibility with pinned runs; offline."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from tin_lite.organic_audit import (
    AUDIT_POLICY,
    V9_AUDIT_POLICY,
    audit_paths,
    build_documents,
    normalize_pages,
    site_check_documents,
    technical_findings,
)
from tin_lite.organic_audit_checks import SiteView, coverage, site_check_coverage, site_findings
from tin_lite.organic_audit_search import (
    brand_terms,
    page_topic,
    search_console_rows,
    search_findings,
)
from tin_lite.organic_audit_site import parse_robots

HOST = "example.com"
BASE = f"https://{HOST}"
WINDOW = "2026-09-01 to 2026-09-28"


def facts(path, **changes):
    return {
        "url": BASE + path,
        "fetch": "observed",
        "status_code": 200,
        "x_robots_tag": [],
        "lang": "en",
        "robots": [],
        "canonical": BASE + path,
        "canonical_count": 1,
        "hreflang": [],
        "h1_count": 1,
        "title": f"{path} | Example",
        "json_ld_blocks": 0,
        "json_ld_types": [],
        "microdata": False,
        "html_bytes": 1000,
        "truncated": False,
        **changes,
    }


def files(*, robots="User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n", urls=()):
    return {
        "status": "observed",
        "robots": {"status": "observed", **parse_robots(robots)},
        "sitemaps": {
            "referenced_in_robots": True,
            "files": [{"url": f"{BASE}/sitemap.xml", "status": "observed", "kind": "urlset"}],
            "unread": 0,
            "total_urls": len(urls),
            "urls": [{"loc": BASE + path, "lastmod": None} for path in urls],
            "urls_capped": False,
        },
    }


def view(*, site_files=None, pages=(), search=(), queries=(), crawl=()):
    return SiteView(
        host=HOST,
        hosts=(HOST,),
        site={"files": site_files or files(), "pages": list(pages)},
        crawl_pages=list(crawl),
        search_pages=list(search),
        search_queries=list(queries),
    )


def checks(site_view, pagespeed=None):
    found = site_findings(site_view, home=f"{BASE}/", pagespeed=pagespeed or {"status": "x"})
    return {item["check_id"]: item for item in found}


def row(path, clicks, impressions, position, query=None):
    value = {"url": BASE + path, "clicks": clicks, "impressions": impressions, "position": position}
    return {**value, "query": query} if query else value


# --- Search Console ---------------------------------------------------------------------------


def test_search_console_rows_are_validated_and_scoped():
    def in_scope(url):
        return url.startswith(BASE)

    raw = {
        "rows": [
            {"keys": ["q", f"{BASE}/a"], "clicks": 1, "impressions": 9, "position": 4.26},
            {"keys": ["q", "https://other.example/a"], "clicks": 1, "impressions": 9},
        ]
    }
    rows, returned = search_console_rows(raw, ("query", "page"), in_scope=in_scope, max_rows=10)
    assert returned == 2
    assert rows == [
        {"url": f"{BASE}/a", "clicks": 1, "impressions": 9, "position": 4.26, "query": "q"}
    ]
    for bad in (
        {"rows": [{"keys": ["q", f"{BASE}/a"], "clicks": 5, "impressions": 1}]},
        {"rows": [{"keys": ["q", f"{BASE}/a"], "clicks": True, "impressions": 1}]},
        {"rows": [{"keys": ["q", f"{BASE}/a"], "clicks": 1, "impressions": float("nan")}]},
        {"rows": [{"keys": [f"{BASE}/a"], "clicks": 0, "impressions": 1}]},
        {"rows": [{"keys": ["q", f"{BASE}/a"], "clicks": 0, "impressions": 1}] * 11},
    ):
        with pytest.raises(ValueError):
            search_console_rows(bad, ("query", "page"), in_scope=in_scope, max_rows=10)


def test_url_topics_group_near_duplicate_patterns_but_not_translations():
    assert page_topic(f"{BASE}/compare/semrush-alternatives") == (
        "/compare/{x}-alternatives",
        "semrush",
    )
    assert page_topic(f"{BASE}/alternatives/semrush") == ("/alternatives/{x}", "semrush")
    assert page_topic(f"{BASE}/alternatives-to-semrush") == ("/alternatives-to-{x}", "semrush")
    assert page_topic(f"{BASE}/") is None
    pages = [
        row("/compare/a-alternatives", 0, 100, 9),
        row("/alternatives/a", 0, 100, 9),
        row("/nl/pricing", 0, 50, 9),
        row("/pricing", 5, 50, 3),
        row("/nl/about", 0, 50, 9),
        row("/about", 1, 50, 3),
    ]
    queries = [
        row("/compare/a-alternatives", 0, 60, 9, "a alternative"),
        row("/alternatives/a", 0, 40, 11, "a alternative"),
    ]
    found = search_findings(
        host=HOST,
        pages=pages,
        queries=queries,
        window=WINDOW,
        titles={},
        policy=AUDIT_POLICY,
        brand=brand_terms(HOST, None),
    )
    cannibal = next(f for f in found if f["check_id"] == "search.cannibalization")
    patterns = cannibal["verification"]["url_patterns"]
    assert [p["templates"] for p in patterns] == [
        ["/alternatives/{x}", "/compare/{x}-alternatives"]
    ]
    assert patterns[0]["share_percent"] == 50 and cannibal["affected_count"] == 2


@pytest.mark.parametrize(
    ("position", "impressions", "expected"),
    [(3.9, 100, False), (4.0, 100, True), (15.0, 100, True), (15.1, 100, False), (8, 19, False)],
)
def test_near_page_one_uses_the_pinned_position_window_and_minimum(position, impressions, expected):
    found = search_findings(
        host=HOST,
        pages=[row("/a", 0, impressions, position)],
        queries=[row("/a", 0, impressions, position, "a query")],
        window=WINDOW,
        titles={},
        policy=AUDIT_POLICY,
        brand=[],
    )
    assert any(f["check_id"] == "search.near_page_one" for f in found) is expected


@pytest.mark.parametrize(
    ("clicks", "impressions", "position", "expected"),
    [
        (0, 310, 5.8, True),
        (8, 310, 5.8, False),  # About 14 expected; 8 is more than a third.
        (0, 49, 2.0, False),  # Under the pinned impression minimum.
        (0, 500, 10.5, False),  # Not near the top.
        (0, 60, 9.0, False),  # Too few expected clicks to call it low.
    ],
)
def test_low_click_through_needs_position_impressions_and_a_real_shortfall(
    clicks, impressions, position, expected
):
    found = search_findings(
        host=HOST,
        pages=[row("/a", clicks, impressions, position)],
        queries=[],
        window=WINDOW,
        titles={},
        policy=AUDIT_POLICY,
        brand=[],
    )
    assert any(f["check_id"] == "search.low_ctr" for f in found) is expected


def test_brand_searches_only_flag_non_home_pages_that_do_not_present_the_brand():
    panel = {"name": "Acme", "aliases": ["Acme Tools"]}
    brand = brand_terms(HOST, panel)
    assert brand == ["acme", "acme tools", "example", "example com", "example.com"]

    def run(queries, titles):
        return {
            f["check_id"]: f
            for f in search_findings(
                host=HOST,
                pages=[],
                queries=queries,
                window=WINDOW,
                titles=titles,
                policy=AUDIT_POLICY,
                brand=brand,
            )
        }

    home = run([row("/", 5, 100, 1.2, "acme")], {"/": "Old name"})
    assert "search.brand_landing_page" not in home
    named = run([row("/features", 5, 100, 1.2, "acme")], {"/features": "Acme features"})
    assert "search.brand_landing_page" not in named
    stale = run([row("/login", 1, 100, 1.4, "acme tools")], {"/login": "Log in – Oldco"})
    item = stale["search.brand_landing_page"]
    assert item["impact"] == "high"
    assert "does not name the brand and is a sign-in or account page" in item["evidence"][0]


# --- site checks ---------------------------------------------------------------------------------


def test_schema_is_never_reported_missing_from_unrendered_html():
    site_view = view(pages=[facts("/"), facts("/a")], site_files=files(urls=["/", "/a"]))
    assert not any("schema" in check for check in checks(site_view))
    rows = {r["check"]: r for r in site_check_coverage(site_view, pagespeed={}, search={})}
    assert rows["structured_data"]["status"] == "unknown"
    assert "not reported as missing schema" in rows["structured_data"]["note"]
    marked = view(pages=[facts("/", json_ld_blocks=1, json_ld_types=["Organization"])])
    rows = {r["check"]: r for r in site_check_coverage(marked, pagespeed={}, search={})}
    assert rows["structured_data"]["status"] == "partial"


def test_robots_failures_block_and_ai_search_crawler_findings():
    unreachable = view(
        site_files={**files(), "robots": {"status": "server_error", "status_code": 500}}
    )
    item = checks(unreachable)["robots.unavailable"]
    assert item["priority"] == "critical" and "HTTP 500" in item["evidence"][0]
    blocked = view(
        site_files=files(
            robots="User-agent: *\nDisallow: /docs\n\nUser-agent: OAI-SearchBot\n"
            "User-agent: PerplexityBot\nDisallow: /\n",
            urls=["/", "/docs/a"],
        ),
        search=[row("/docs/a", 1, 30, 7)],
    )
    found = checks(blocked)
    assert found["robots.blocks_important_pages"]["priority"] == "critical"
    assert found["robots.blocks_important_pages"]["urls"] == [f"{BASE}/docs/a"]
    ai = found["robots.ai_search_crawlers_blocked"]
    assert ai["evidence"][0] == (
        "OAI-SearchBot, PerplexityBot cannot fetch the homepage under your robots.txt."
    )
    assert "robots.sitemap_reference_missing" in found
    missing = view(site_files={**files(), "robots": {"status": "missing", "status_code": 404}})
    found = checks(missing)
    assert "robots.unavailable" not in found
    assert found["robots.sitemap_reference_missing"]["evidence"] == [
        "robots.txt returned HTTP 404."
    ]


def test_sitemap_lists_only_indexable_canonical_urls():
    site_view = view(
        site_files=files(urls=["/", "/hidden", "/moved", "/copy", "/gone"]),
        pages=[
            facts("/"),
            facts("/hidden", robots=["noindex"]),
            {"url": f"{BASE}/moved", "fetch": "redirect", "status_code": 301, "x_robots_tag": []},
            facts("/copy", canonical=f"{BASE}/"),
            {"url": f"{BASE}/gone", "fetch": "http_error", "status_code": 410, "x_robots_tag": []},
        ],
    )
    item = checks(site_view)["sitemap.non_indexable_urls"]
    assert item["affected_count"] == 4 and item["impact"] == "high"
    assert item["evidence"][0] == (
        "Of 5 sitemap URLs checked, 4 cannot or should not be indexed: 1 canonicalized to "
        "another URL; 1 with an error status; 1 marked noindex; 1 redirecting."
    )


def test_unchecked_sitemap_urls_are_not_judged_and_plain_http_urls_are_flagged():
    site_view = view(
        site_files=files(urls=["/", "/unread"]),
        pages=[facts("/"), {"url": f"{BASE}/unread", "fetch": "unavailable"}],
    )
    assert "sitemap.non_indexable_urls" not in checks(site_view)
    listed = files(urls=["/"])
    listed["sitemaps"]["urls"].append({"loc": "http://example.com/old", "lastmod": None})
    item = checks(view(site_files=listed, pages=[facts("/")]))["sitemap.non_indexable_urls"]
    assert item["urls"] == ["http://example.com/old"]
    assert item["evidence"][1] == "On plain HTTP: /old"


def test_sitemap_urls_past_the_stored_list_count_as_not_inspected():
    listed = files(urls=["/", "/a"])
    listed["sitemaps"].update(total_urls=5, urls_capped=True)
    summary = coverage(view(site_files=listed, pages=[facts("/"), facts("/a")]), None, page_cap=9)
    assert summary["status"] == "partial"
    assert (summary["sitemap_pages"], summary["inspected_sitemap_pages"]) == (5, 2)
    assert summary["skipped_sitemap_pages"] == 3 and summary["skipped"] == []


def test_refused_reads_raise_no_findings_and_are_named_as_unknown():
    refused = {
        **files(urls=["/", "/a"]),
        "robots": {"status": "refused", "status_code": 403},
    }
    refused["sitemaps"]["files"] = [
        {"url": f"{BASE}/sitemap.xml", "status": "refused", "status_code": 403}
    ]
    refused["sitemaps"]["urls"] = []
    site_view = view(
        site_files=refused,
        pages=[facts("/"), {"url": f"{BASE}/a", "fetch": "refused", "status_code": 403}],
    )
    found = checks(site_view)
    assert not {"sitemap.missing", "robots.unavailable", "sitemap.non_indexable_urls"} & set(found)
    rows = {r["check"]: r for r in site_check_coverage(site_view, pagespeed={}, search={})}
    assert rows["robots_txt"]["status"] == "unknown"
    assert "refused Tin's reader" in rows["robots_txt"]["note"]
    assert rows["page_html"]["status"] == "partial"
    assert "refused Tin's reader for 1 page" in rows["page_html"]["note"]
    assert coverage(site_view, None, page_cap=10)["inspected_pages"] == 1


def test_sitemap_missing_uniform_lastmod_and_unreadable_files():
    none = view(
        site_files={
            **files(),
            "sitemaps": {
                "files": [
                    {"url": f"{BASE}/sitemap.xml", "status": "http_error", "status_code": 404}
                ],
                "urls": [],
                "total_urls": 0,
            },
        }
    )
    assert checks(none)["sitemap.missing"]["evidence"] == [
        "https://example.com/sitemap.xml: http_error (HTTP 404)"
    ]
    stamped = files(urls=[f"/p{i}" for i in range(5)])
    for entry in stamped["sitemaps"]["urls"]:
        entry["lastmod"] = "2026-09-01"
    assert "sitemap.uniform_lastmod" in checks(view(site_files=stamped))
    stamped["sitemaps"]["urls"][0]["lastmod"] = "2026-08-01"
    assert "sitemap.uniform_lastmod" not in checks(view(site_files=stamped))


def test_noindex_pages_with_search_traffic_and_multiple_canonicals():
    site_view = view(
        pages=[
            facts("/guide", robots=["noindex"]),
            facts("/login", robots=["noindex"]),
            facts("/twice", canonical_count=2),
        ],
        search=[row("/guide", 3, 90, 6), row("/login", 1, 40, 2)],
    )
    found = checks(site_view)
    hidden = found["indexation.noindex_with_search_traffic"]
    # Often deliberate, so a question for the founder rather than a critical failure.
    assert hidden["priority"] == "quick_win" and hidden["status"] == "review"
    assert hidden["urls"] == [f"{BASE}/guide"]
    assert found["indexation.multiple_canonicals"]["urls"] == [f"{BASE}/twice"]
    assert "indexation.utility_pages_indexable" not in found


def test_h1_and_language_checks_ignore_unread_and_noindexed_pages():
    site_view = view(
        pages=[
            facts("/a", h1_count=0),
            facts("/b", h1_count=3),
            facts("/c", lang=None),
            facts("/noindexed", h1_count=0, robots=["noindex"]),
            facts("/de/preise", lang="de-DE", hreflang=[{"lang": "en", "href": f"{BASE}/"}]),
            {"url": f"{BASE}/unread", "fetch": "unavailable", "reason": "unreachable"},
        ]
    )
    found = checks(site_view)
    assert found["onpage.h1_missing"]["urls"] == [f"{BASE}/a"]
    assert found["onpage.h1_multiple"]["urls"] == [f"{BASE}/b"]
    assert found["onpage.lang_missing"]["urls"] == [f"{BASE}/c"]
    # A German page that declares German and links its alternates is correct.
    assert "onpage.lang_mismatch" not in found and "onpage.hreflang_missing" not in found


def test_speed_is_unknown_without_measurements_and_graded_when_measured():
    site_view = view(pages=[facts("/")])
    rows = {
        r["check"]: r
        for r in site_check_coverage(
            site_view, pagespeed={"status": "not_configured", "results": []}, search={}
        )
    }
    assert rows["speed"]["status"] == "unknown"
    measured = {
        "status": "observed",
        "results": [
            {
                "url": f"{BASE}/",
                "result": {
                    "status": "observed",
                    "field": {"lcp_ms": None, "inp_ms": None, "cls": None},
                    "lab": {"lcp_ms": 2600.0, "cls": 0.3, "tbt_ms": 10.0},
                },
            }
        ],
    }
    item = checks(site_view, pagespeed=measured)["speed.core_web_vitals"]
    assert item["impact"] == "high"
    assert item["evidence"][1] == (
        "/ (lab data): LCP 2.6 s (needs improvement), INP unknown, CLS 0.30 (poor)"
    )


def test_coverage_counts_only_pages_tin_read():
    site_view = view(
        site_files=files(urls=["/", "/a", "/b"]),
        pages=[
            facts("/"),
            {"url": f"{BASE}/a", "fetch": "blocked_by_robots"},
            {"url": f"{BASE}/x", "fetch": "redirect", "status_code": 301, "x_robots_tag": []},
        ],
        search=[row("/b", 0, 10, 20)],
    )
    summary = coverage(site_view, None, page_cap=50)
    assert summary["status"] == "partial"
    assert summary["inspected_sitemap_pages"] == 1 and summary["inspected_pages"] == 2
    assert [r["url"] for r in summary["skipped"]] == [f"{BASE}/b", f"{BASE}/a"]
    assert summary["skipped_search_pages"] == [{"url": f"{BASE}/b", "impressions": 10}]


# --- documents, format and compatibility -------------------------------------------------------


def v10_documents(*, pages=None, site=None, policy=AUDIT_POLICY, crawl_status="completed"):
    run_id = str(uuid4())
    crawl_pages = normalize_pages(
        pages
        or [
            {
                "url": f"{BASE}/",
                "resource_type": "html",
                "status_code": 200,
                "meta": {"title": ""},
                "checks": {"canonical": True, "no_title": True},
            }
        ],
        HOST,
        policy_version=policy["version"],
    )
    docs = site_check_documents(
        run_id=run_id,
        project_id=str(uuid4()),
        definition_sha="d" * 40,
        scope={
            "url": f"{BASE}/",
            "host": HOST,
            "market": "US",
            "language": "en",
            "started_at": "2026-09-29T00:00:00Z",
            "page_cap": 100,
        },
        crawl={"status": crawl_status, "pages": crawl_pages},
        ai={"status": "partial", "summary": "Not measured."},
        spending={},
        policy=policy,
        search_console=None,
        search_queries=None,
        site=site
        if site is not None
        else {
            "files": files(urls=["/"]),
            "plan": None,
            "pages": [facts("/", h1_count=0)],
            "pages_status": "complete",
            "pagespeed": {"status": "not_configured", "results": []},
        },
    )
    paths = audit_paths(run_id)
    return (
        run_id,
        docs[paths["AUDIT.md"]].decode(),
        json.loads(docs[paths["findings.json"]]),
        json.loads(docs[paths["evidence.json"]]),
    )


def test_every_v10_finding_uses_the_five_field_format_including_crawl_findings():
    _, report, inventory, _ = v10_documents()
    assert inventory["schema_version"] == 3 and inventory["coverage_status"] == "complete"
    by_check = {item["check_id"]: item for item in inventory["findings"]}
    title = by_check["metadata.title_missing"]
    assert title["area"] == "on_page" and title["priority"] == "quick_win"
    assert title["evidence"][0] == "The provider crawl flagged 1 of 1 pages it could check."
    assert by_check["onpage.h1_missing"]["category"] == "site"
    for item in inventory["findings"]:
        assert {"issue", "impact", "evidence", "fix", "priority", "area"} <= set(item)
    assert "Result: partial evidence; complete: all 1 sitemap pages inspected" in report


def test_technical_findings_keep_their_recomputable_order_for_technical_fix():
    raw = [
        {
            "url": f"{BASE}/{index}",
            "resource_type": "html",
            "status_code": code,
            "checks": {"canonical": True, "no_title": True, "broken_links": True},
        }
        for index, code in enumerate([200, 404, 503])
    ]
    pages = normalize_pages(raw, HOST, policy_version=AUDIT_POLICY["version"])
    findings, _ = technical_findings(pages, HOST, policy_version=AUDIT_POLICY["version"])
    assert [item["check_id"] for item in findings] == [
        "http.server_error",
        "http.client_error",
        "links.broken",
        "metadata.title_missing",
    ]
    # v9 keeps its severity ordering and has no format fields.
    old, _ = technical_findings(
        normalize_pages(raw, HOST, policy_version=V9_AUDIT_POLICY["version"]),
        HOST,
        policy_version=V9_AUDIT_POLICY["version"],
    )
    assert "priority" not in old[0]


def test_pinned_v9_runs_keep_their_report_and_inventory_shape():
    run_id = str(uuid4())
    docs = build_documents(
        run_id=run_id,
        project_id=str(uuid4()),
        definition_sha="d" * 40,
        scope={"url": f"{BASE}/", "host": HOST, "market": "US", "started_at": "2026-09-08"},
        crawl={"status": "completed", "pages": []},
        ai={"status": "partial", "summary": "Not measured."},
        spending={},
        policy_version=V9_AUDIT_POLICY["version"],
    )
    paths = audit_paths(run_id)
    report = docs[paths["AUDIT.md"]].decode()
    assert "## Search appearance" in report and "## Summary" not in report
    assert "(limit 100)" in report
    assert json.loads(docs[paths["findings.json"]])["schema_version"] == 2


def test_evidence_is_trimmed_to_its_bound_and_says_so():
    site = {
        "files": files(urls=[f"/p{i}" for i in range(3000)]),
        "plan": None,
        "pages": [facts(f"/p{i}") for i in range(50)],
        "pages_status": "complete",
        "pagespeed": {"status": "not_configured", "results": []},
    }
    policy = {**AUDIT_POLICY, "max_evidence_bytes": 150_000}
    _, _, inventory, evidence = v10_documents(site=site, policy=policy)
    assert evidence["trimmed_for_size"]["sitemap_urls"] == 2000
    assert len(json.dumps(evidence)) < 150_000
    # Findings came from the full evidence before trimming.
    assert inventory["coverage"]["sitemap_pages"] == 3000


def test_documents_without_site_evidence_say_so_instead_of_guessing():
    _, report, inventory, evidence = v10_documents(site={})
    assert "Result: partial: site files were not collected; 1 page crawled." in report
    assert evidence["site"]["status"] == "not_collected"
    assert inventory["site_check_coverage"][0]["status"] == "unknown"
    assert not [f for f in inventory["findings"] if f["category"] == "site"]


@pytest.mark.asyncio
async def test_technical_fix_accepts_v10_audits_and_explains_site_findings():
    from test_technical_fix_sources import source_fixture

    site = {
        "files": files(urls=["/page-000"]),
        "plan": None,
        "pages": [facts("/page-000", h1_count=0)],
        "pages_status": "complete",
        "pagespeed": {"status": "not_configured", "results": []},
    }
    fixture = source_fixture(policy=AUDIT_POLICY["version"], site=site)
    source = await fixture.service.inspect(
        project_id=fixture.project.id, audit_run_id=fixture.run.id
    )
    assert [row["finding"]["check_id"] for row in source["findings"]] == ["metadata.title_missing"]
    assert source["findings"][0]["source_eligible"]
    assert fixture.inventory["schema_version"] == 3
    excluded = {row["finding"]["check_id"]: row for row in source["excluded_findings"]}
    assert excluded["onpage.h1_missing"]["ineligible_reason"] == "site_finding"
    assert "does not cover it yet" in excluded["onpage.h1_missing"]["message"]
