"""Acceptance: the v10 audit reports what an earlier run on tin.computer's own site missed.

Offline: a synthetic site, Search Console rows and provider crawl modeled on tin.computer.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import organic_tin_fixture as tin
import pytest
from test_organic_audit import MemoryDB
from test_procedure_publication import HistoryStorage

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.dataforseo import DataForSEO
from tin_lite.organic_audit import audit_paths, canonical_json
from tin_lite.organic_audit_activities import OrganicAuditActivities


async def run_audit(*, page_cap=100, site=None, pagespeed_key=None, pagespeed_reader=None):
    db, storage = MemoryDB(), HistoryStorage()
    db.run = replace(
        db.run,
        input={"site_url": f"{tin.BASE}/", "market": "US"},
        created_at=datetime(2026, 9, 29, tzinfo=UTC),
    )
    definition = next(w.definition for w in BUILTIN_WORKFLOWS if w.key == "organic.audit")
    storage.read_canonical_artifact = AsyncMock(return_value=canonical_json(definition))
    submitted: dict = {}

    async def submit(request):
        submitted.update(request)
        return {"task_id": str(uuid4()), "reported_cost_usd": "0.015"}

    async def pages(task_id, include_broken=False, limit=100):
        return tin.provider_pages(submitted.get("priority_urls", []), limit)

    provider = SimpleNamespace(
        validate_target=AsyncMock(return_value=(f"{tin.BASE}/", tin.HOST)),
        crawl_request=DataForSEO.crawl_request,
        submit=AsyncMock(side_effect=submit),
        recover=AsyncMock(),
        summary=AsyncMock(return_value={"crawl_progress": "finished", "domain": tin.HOST}),
        pages=AsyncMock(side_effect=pages),
        stop=AsyncMock(),
    )

    async def analytics(**kwargs):
        return tin.gsc_response(tuple(kwargs["dimensions"]))

    integrations = SimpleNamespace(search_console_analytics=AsyncMock(side_effect=analytics))
    db.get_integration_connection = AsyncMock(
        return_value=SimpleNamespace(
            status="connected", configuration={"selected_site_url": f"sc-domain:{tin.HOST}"}
        )
    )
    site = site or tin.site()
    settings = SimpleNamespace(
        organic_audit_max_cost_usd=8,
        organic_audit_max_pages=page_cap,
        pagespeed_api_key=pagespeed_key,
    )
    activities = OrganicAuditActivities(
        database=db,
        storage=storage,
        settings=settings,
        provider=provider,
        integrations=integrations,
        site_resolver=AsyncMock(return_value={"status": "observed", "redirects": []}),
        site_reader=site.reader,
        **({"pagespeed_reader": pagespeed_reader} if pagespeed_reader else {}),
    )
    run_id = str(db.run.id)
    polls = 0
    for _ in range(2):  # Duplicate delivery of every activity changes nothing.
        await activities.organic_prepare(run_id)
        await activities.organic_start_crawl(run_id)
        while not await activities.organic_poll_crawl(run_id):
            polls += 1
            assert polls < 20
        await activities.organic_end_crawl(run_id)
        assert await activities.organic_prepare_panel(run_id) == 0
        await activities.organic_brand_checks(run_id)
        await activities.organic_publish(run_id)
    artifacts = await activities._result(run_id, "artifacts")
    paths = audit_paths(run_id)
    return SimpleNamespace(
        activities=activities,
        run_id=run_id,
        report=artifacts[paths["AUDIT.md"]],
        inventory=json.loads(artifacts[paths["findings.json"]]),
        evidence=json.loads(artifacts[paths["evidence.json"]]),
        submitted=submitted,
        provider=provider,
        integrations=integrations,
        site=site,
        db=db,
    )


def finding(result, check_id):
    return next(item for item in result.inventory["findings"] if item["check_id"] == check_id)


@pytest.fixture(scope="module")
def audit():
    import asyncio

    return asyncio.run(run_audit())


def test_crawl_is_chosen_by_search_impressions_and_section_and_reported_as_partial(audit):
    report = audit.report
    assert "Result: partial: 98 of 137 sitemap pages inspected (page cap 100)." in report
    assert "completed within the stated scope" not in report
    assert audit.inventory["coverage_status"] == "partial"
    coverage = audit.evidence["coverage"]
    assert coverage["skipped_sitemap_pages"] == 39
    skipped = {row["url"] for row in coverage["skipped"]}
    # Every page with search impressions and every URL section was read.
    impression_pages = {tin.BASE + path for path in tin.PAGE_ROWS}
    assert not skipped & impression_pages and not coverage["skipped_search_pages"]
    read = {row["url"] for row in audit.evidence["site"]["pages"] if row["fetch"] == "observed"}
    for section in ("/nl/", "/offer/", "/alternatives/", "/compare/", "/workflows"):
        assert any(section in url for url in read), section
    assert "### Sitemap pages not inspected" in report and "- /blog/post-077" in report
    # The provider crawl is steered toward the same pages first, inside the pinned cap.
    submitted = audit.submitted
    assert submitted["max_crawl_pages"] == 100 and len(submitted["priority_urls"]) == 20
    assert submitted["priority_urls"][:3] == [
        f"{tin.BASE}/sign-in",
        f"{tin.BASE}/pricing",  # Ties on impressions go to the page with more clicks.
        f"{tin.BASE}/compare/semrush-alternatives",
    ]
    assert audit.provider.submit.await_count == 1


def test_search_console_queries_show_cannibalization_across_duplicate_url_patterns(audit):
    item = finding(audit, "search.cannibalization")
    text = "\n".join(item["evidence"])
    assert (
        '"semrush alternative": /compare/semrush-alternatives (position 9.2, 200 impressions, '
        "0 clicks); /alternatives/semrush (position 11.4, 150 impressions, 0 clicks)"
    ) in text
    assert (
        "URL patterns /alternatives/{x} and /compare/{x}-alternatives both cover 16 topics"
    ) in text
    assert (
        "The 33 pages following these patterns got 53% of all impressions (5,300 of 10,000) "
        "and 0 clicks."
    ) in text
    assert item["impact"] == "high" and item["area"] == "content"
    patterns = item["verification"]["url_patterns"][0]
    assert patterns["pages"] == 33 and patterns["share_percent"] == 53 and patterns["clicks"] == 0


def test_near_page_one_and_low_click_through(audit):
    near = "\n".join(finding(audit, "search.near_page_one")["evidence"])
    assert '"moz alternative" → /alternatives/moz: position 8.5, 300 impressions, 0 clicks' in near
    assert (
        '"ai tools for startups" → /compare/ai-tools-for-startups: position 5.8, 280 '
        "impressions, 0 clicks"
    ) in near
    low = "\n".join(finding(audit, "search.low_ctr")["evidence"])
    assert "/compare/ai-tools-for-startups: position 5.8, 310 impressions, 0 clicks" in low
    assert "/alternatives/moz: position 8.5, 350 impressions, 0 clicks" in low


def test_indexable_sign_in_page_ranking_for_the_brand(audit):
    item = finding(audit, "indexation.utility_pages_indexable")
    assert item["impact"] == "high" and item["urls"] == [f"{tin.BASE}/sign-in"]
    line = item["evidence"][0]
    for fact in (
        "/sign-in — open to indexing (no noindex tag or header)",
        'ranks position 1.5 for "tin computer"',
        'title "Sign in – Iteration Machine"',
        "no H1",
        f"canonical points to {tin.BASE}/",
    ):
        assert fact in line
    brand = "\n".join(finding(audit, "search.brand_landing_page")["evidence"])
    assert '"tin computer" mostly shows /sign-in (position 1.5' in brand
    assert "which does not name the brand" in brand
    assert f"/sign-in → {tin.BASE}/" in finding(audit, "indexation.canonical_elsewhere")["evidence"]
    assert f"{tin.BASE}/sign-in" in finding(audit, "onpage.h1_missing")["urls"]


def test_dutch_pages_declare_english_and_no_hreflang(audit):
    lang = finding(audit, "onpage.lang_mismatch")
    assert lang["evidence"][0] == '/nl/: lang="en" on 12 pages; expected nl.'
    hreflang = finding(audit, "onpage.hreflang_missing")
    assert hreflang["evidence"][0] == "/nl/: no hreflang alternates on 12 of 12 pages."
    assert lang["area"] == hreflang["area"] == "technical_foundations"


def test_ad_landing_pages_are_indexable_and_in_the_sitemap(audit):
    indexable = finding(audit, "indexation.ad_landing_pages_indexable")
    assert indexable["evidence"][0] == "Ad landing pages without noindex: 6; in the sitemap: 6."
    listed = finding(audit, "sitemap.ad_landing_urls")
    assert listed["affected_count"] == 6 and listed["urls"][0] == f"{tin.BASE}/offer/launch"


def test_search_pages_missing_from_the_sitemap(audit):
    item = finding(audit, "sitemap.missing_search_pages")
    assert item["urls"] == [f"{tin.BASE}/about"]
    assert "/about: 250 impressions, 5 clicks" in item["evidence"]
    # Sign-in also has impressions but belongs out of the sitemap, so it is not listed here.
    assert f"{tin.BASE}/sign-in" not in item["urls"]


def test_robots_sitemap_and_limits_are_reported_without_inventing_passes(audit):
    robots = finding(audit, "robots.named_group_rules")
    assert "GPTBot may crawl /api/, /app/, which the * group disallows." in robots["evidence"]
    assert finding(audit, "sitemap.uniform_lastmod")["impact"] == "low"
    report = audit.report
    assert "| OAI-SearchBot | search | partly blocked: /api/, /app/ |" in report
    assert "structured data: Structured data found in the static HTML of 1 page." in report
    assert "speed: PageSpeed Insights is not configured on this deployment." in report
    assert not any("schema" in item["check_id"] for item in audit.inventory["findings"])


def test_one_finding_format_ordered_by_area_with_summary_and_action_plan(audit):
    findings = audit.inventory["findings"]
    areas = [
        "crawlability_indexation",
        "technical_foundations",
        "on_page",
        "content",
        "authority",
    ]
    assert [areas.index(item["area"]) for item in findings] == sorted(
        areas.index(item["area"]) for item in findings
    )
    for item in findings:
        assert item["issue"] and item["evidence"] and item["fix"]
        assert item["impact"] in {"high", "medium", "low"}
        assert item["priority"] in {"critical", "high_impact", "quick_win", "long_term"}
    report = audit.report
    order = [
        "## Summary",
        "### Top issues",
        "### Quick wins",
        "## Findings",
        "### 1. Crawlability and indexation",
        "### 2. Technical foundations",
        "### 3. On-page",
        "### 4. Content",
        "### 5. Authority",
        "## Action plan",
        "## Pages inspected",
        "## Search Console",
        "## robots.txt and AI crawlers",
        "## Technical SEO (provider crawl)",
        "## AI visibility",
        "## Evidence and limits",
    ]
    assert [report.index(header) for header in order] == sorted(
        report.index(header) for header in order
    )
    assert "- Impact: high\n- Priority: Quick win\n- Evidence:" in report
    summary = audit.inventory["summary"]
    assert summary["by_priority"]["quick_win"] >= 5 and len(summary["top_issue_ids"]) == 5


def test_search_console_reads_are_receipted_once_and_scoped(audit):
    calls = audit.integrations.search_console_analytics.await_args_list
    assert [tuple(call.kwargs["dimensions"]) for call in calls] == [("page",), ("query", "page")]
    assert {call.kwargs["execution_key"].rsplit(":", 2)[-2] for call in calls} == {
        "search_console",
        "search_console_queries",
    }
    assert calls[0].kwargs["start_date"] == "2026-08-30"
    assert calls[0].kwargs["end_date"] == "2026-09-26"
    queries = audit.evidence["search_console_queries"]["value"]
    assert queries["fields"] == ["query", "page", "clicks", "impressions", "position"]
    assert len(queries["queries"]) == len(tin.QUERY_ROWS)


def test_the_site_is_read_once_per_page_across_duplicate_delivery(audit):
    requests = audit.site.requests
    assert requests.count(f"{tin.BASE}/robots.txt") == 1
    assert requests.count(f"{tin.BASE}/sitemap.xml") == 1
    assert requests.count(f"{tin.BASE}/sign-in") == 1
    page_reads = [url for url in requests if not url.endswith((".txt", ".xml"))]
    assert len(page_reads) == len(set(page_reads)) == 100


@pytest.mark.asyncio
async def test_a_larger_configured_cap_inspects_the_whole_sitemap():
    result = await run_audit(page_cap=200)
    # Every page was read; AI visibility still needs a model, so the evidence stays partial.
    assert (
        "Result: partial evidence; complete: all 137 sitemap pages inspected (page cap 200)."
        in result.report
    )
    assert result.inventory["coverage_status"] == "complete"
    assert not result.evidence["coverage"]["skipped"]
    assert result.submitted["max_crawl_pages"] == 200


@pytest.mark.asyncio
async def test_pagespeed_runs_one_page_per_poll_and_reports_slow_pages():
    reads = []

    async def reader(url, key):
        reads.append(url)
        assert key == "psi-test-key"  # noqa: S105
        slow = url == f"{tin.BASE}/"
        return {
            "status": "observed",
            "field": {
                "lcp_ms": 4200.0 if slow else 1800.0,
                "inp_ms": 150.0,
                "cls": 0.02,
                "origin_fallback": False,
            },
            "lab": {"lcp_ms": 3000.0, "cls": 0.02, "tbt_ms": 100.0, "performance_score": 0.6},
        }

    from pydantic import SecretStr

    result = await run_audit(pagespeed_key=SecretStr("psi-test-key"), pagespeed_reader=reader)
    assert len(reads) == 3 and reads[0] == f"{tin.BASE}/"
    item = finding(result, "speed.core_web_vitals")
    assert item["impact"] == "high" and item["urls"] == [f"{tin.BASE}/"]
    assert (
        "/ (field data): LCP 4.2 s (poor), INP 150 ms (good), CLS 0.02 (good)" in item["evidence"]
    )
    assert "psi-test-key" not in json.dumps(result.evidence)
    assert "psi-test-key" not in json.dumps(
        {key: value.result for key, value in result.db.effects.items()}
    )
