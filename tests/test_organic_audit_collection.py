"""How organic-audit-v10 collects site evidence around the crawl; offline, in memory."""

from __future__ import annotations

import json
from types import SimpleNamespace

import organic_tin_fixture as tin
import pytest
from organic_site_stub import minimal_site
from test_organic_audit import activities_fixture
from test_organic_audit_acceptance import run_audit

from tin_lite import organic_audit_activities
from tin_lite.organic_audit import AUDIT_POLICY, audit_paths


async def poll_until_done(activities, run_id, limit=10):
    for _ in range(limit):
        if await activities.organic_poll_crawl(run_id):
            return
    raise AssertionError("The crawl evidence never finished.")


@pytest.mark.parametrize(
    ("configured", "expected"), [(150, 150), (300, 300), (500, 100), (5, 100), (None, 100)]
)
def test_page_cap_comes_from_settings_within_the_pinned_ceiling(configured, expected):
    settings = SimpleNamespace(organic_audit_max_pages=configured, pagespeed_api_key=None)
    activities = organic_audit_activities.OrganicAuditActivities(
        database=None, storage=None, settings=settings, provider=object()
    )
    assert activities._site_scope(AUDIT_POLICY) == {
        "page_cap": expected,
        "pagespeed": "not_configured",
    }


@pytest.mark.asyncio
async def test_scope_pins_the_cap_so_later_configuration_changes_nothing():
    activities, db, _, provider = await activities_fixture()
    run_id = str(db.run.id)
    scope = await activities._result(run_id, "scope")
    assert scope["policy_version"] == AUDIT_POLICY["version"]
    assert scope["page_cap"] == 100 and scope["pagespeed"] == "not_configured"
    activities.settings.organic_audit_max_pages = 250
    await activities.organic_start_crawl(run_id)
    assert provider.submit.await_args.args[0]["max_crawl_pages"] == 100


@pytest.mark.asyncio
async def test_a_site_read_defect_is_recorded_and_the_crawl_still_runs():
    activities, db, storage, provider = await activities_fixture()
    run_id = str(db.run.id)

    class Broken:
        def __init__(self, hosts):
            pass

        async def __aenter__(self):
            raise RuntimeError("injected defect")

        async def __aexit__(self, *exc):
            return None

    activities.site_reader = Broken
    await activities.organic_start_crawl(run_id)
    assert await activities._result(run_id, "site_files") == {
        "status": "unavailable",
        "reason": "site_read_failed",
    }
    assert provider.submit.await_count == 1
    await poll_until_done(activities, run_id)
    await activities.organic_prepare_panel(run_id)
    await activities.organic_brand_checks(run_id)
    await activities.organic_publish(run_id)
    report = storage.repo.trees[storage.repo.head][audit_paths(run_id)["AUDIT.md"]][1].decode()
    assert "Result: partial: Tin could not read the site; 1 page crawled." in report
    assert "robots.txt could not be read." in report
    assert "- site evidence: Tin could not read the site's robots.txt, sitemaps or pages." in (
        report
    )


@pytest.mark.asyncio
async def test_page_reads_resume_after_the_time_limit_without_rereading(monkeypatch):
    site = tin.site()
    original = organic_audit_activities.read_pages
    budgets = []

    async def limited(reader, urls, *, robots, policy, seconds):
        budgets.append(seconds)
        # The first batch runs out of time after ten pages.
        return await original(
            reader,
            urls[:10] if len(budgets) == 1 else urls,
            robots=robots,
            policy=policy,
            seconds=seconds,
        )

    monkeypatch.setattr(organic_audit_activities, "read_pages", limited)
    result = await run_audit(site=site)
    assert budgets[:2] == [120, 20]
    page_reads = [url for url in site.requests if not url.endswith((".txt", ".xml"))]
    # The plain-HTTP homepage and a made-up missing page are probes, not page reads.
    page_reads = [u for u in page_reads if u.startswith("https://") and "missing-page" not in u]
    assert len(page_reads) == len(set(page_reads)) == 100
    assert result.evidence["site"]["pages_status"] == "complete"


@pytest.mark.asyncio
async def test_a_reader_that_reads_nothing_records_unknown_pages_instead_of_stalling(monkeypatch):
    activities, db, storage, provider = await activities_fixture()
    run_id = str(db.run.id)

    async def nothing(reader, urls, *, robots, policy, seconds):
        return {}

    monkeypatch.setattr(organic_audit_activities, "read_pages", nothing)
    await activities.organic_start_crawl(run_id)
    assert await activities.organic_poll_crawl(run_id)
    facts = (await activities._result(run_id, "page_facts"))["pages"]
    assert facts == {
        "https://example.com/": {
            "url": "https://example.com/",
            "fetch": "unavailable",
            "reason": "site_read_failed",
        }
    }


@pytest.mark.asyncio
async def test_page_reads_unfinished_at_the_crawl_deadline_are_published_as_partial():
    activities, db, storage, provider = await activities_fixture()
    run_id = str(db.run.id)
    provider.summary.return_value = {"crawl_progress": "in_progress"}
    await activities.organic_start_crawl(run_id)
    # The poll loop ends at its deadline while the provider is still crawling.
    assert not await activities.organic_poll_crawl(run_id)
    await activities.organic_end_crawl(run_id)
    await activities.organic_prepare_panel(run_id)
    await activities.organic_brand_checks(run_id)
    await activities.organic_publish(run_id)
    artifacts = await activities._result(run_id, "artifacts")
    evidence = json.loads(artifacts[audit_paths(run_id)["evidence.json"]])
    assert evidence["site"]["pages_status"] == "partial"
    assert evidence["crawl"]["status"] == "partial"
    # Speed was never configured, so it is not reported as an empty measurement.
    assert evidence["site"]["pagespeed"] == {"status": "not_configured", "results": []}
    report = artifacts[audit_paths(run_id)["AUDIT.md"]]
    assert "Result: partial evidence; complete: all 1 sitemap pages inspected" in report
    summary = (await activities._result(run_id, "publish"))["summary"]
    assert summary.startswith("Organic visibility audit is ready with partial evidence.")
    assert "Inspected 1 of 1 sitemap pages;" in summary


@pytest.mark.asyncio
async def test_without_search_console_the_search_checks_say_they_did_not_run():
    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    await activities.organic_start_crawl(run_id)
    await poll_until_done(activities, run_id)
    await activities.organic_prepare_panel(run_id)
    await activities.organic_brand_checks(run_id)
    await activities.organic_publish(run_id)
    artifacts = await activities._result(run_id, "artifacts")
    report = artifacts[audit_paths(run_id)["AUDIT.md"]]
    assert "so the search checks (competing pages, near page one" in report
    inventory = json.loads(artifacts[audit_paths(run_id)["findings.json"]])
    assert not [f for f in inventory["findings"] if f["category"] == "search"]
    assert inventory["coverage_status"] == "complete"
    assert "complete: all 1 sitemap pages inspected (page cap 100)" in report


@pytest.mark.asyncio
async def test_answer_completion_runs_do_not_read_the_site_again(monkeypatch):
    from unittest.mock import AsyncMock

    from test_organic_audit import response
    from test_organic_audit_completion import source_fixture

    from tin_lite.organic_audit_completion import completion_seed

    seed = completion_seed(**source_fixture())
    site = minimal_site()
    activities, db, _, provider = await activities_fixture()
    activities.site_reader = site.reader
    run_id = str(db.run.id)
    db.effects.clear()
    for stage, result in seed.items():
        await activities._save(run_id, stage, result)
    activities.responses = type("Responses", (), {"create": AsyncMock(return_value=response())})()
    await activities.organic_start_crawl(run_id)
    assert await activities.organic_poll_crawl(run_id)
    assert await activities.organic_prepare_panel(run_id) == 8
    for index in range(8):
        await activities.organic_observe({"run_id": run_id, "index": index})
    await activities.organic_brand_checks(run_id)
    await activities.organic_publish(run_id)
    assert site.requests == [] and provider.submit.await_count == 0
    report = (await activities._result(run_id, "artifacts"))[audit_paths(run_id)["AUDIT.md"]]
    assert "Result: partial: site files were not collected;" in report
    assert "explicitly retried the one missing answer" in report
    summary = (await activities._result(run_id, "publish"))["summary"]
    assert "Crawled 1 page; scored 8/8 AI observations." in summary


@pytest.mark.asyncio
async def test_a_crash_between_search_console_reads_still_reads_queries_once():
    from unittest.mock import AsyncMock

    result = await run_audit()
    activities, run_id = result.activities, result.run_id
    calls = result.integrations.search_console_analytics
    assert calls.await_count == 3
    # Lose the query receipts as if the worker stopped between the reads.
    del result.db.effects[activities.key(run_id, "search_console_queries")]
    del result.db.effects[activities.key(run_id, "search_console_previous")]
    activities.integrations = SimpleNamespace(
        search_console_analytics=AsyncMock(side_effect=calls.side_effect)
    )
    scope = await activities._result(run_id, "scope")
    await activities._search_console_evidence(run_id, scope)
    retried = activities.integrations.search_console_analytics
    assert [tuple(c.kwargs["dimensions"]) for c in retried.await_args_list] == [
        ("query", "page"),
        ("page",),
    ]
    await activities._search_console_evidence(run_id, scope)
    assert retried.await_count == 2
