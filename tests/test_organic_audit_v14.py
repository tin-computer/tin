"""Audit policy v14 follows links when the site has no sitemap; v13 is unchanged.

v13 is on main and may deploy at any time, so v14 reads one v14-only policy key. The crawl
request is decided from the run's saved site files, never from a fresh read, so a retry or a
recovery rebuilds exactly the request it submitted. Offline only.
"""

from __future__ import annotations

import hashlib

import pytest
from organic_site_stub import SyntheticSite, html_page, minimal_site
from temporalio.exceptions import ApplicationError
from test_organic_audit import activities_fixture, page_fixture
from test_organic_audit_v12 import RUN_ID, documents, site_evidence

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.dataforseo import DataForSEO
from tin_lite.organic_audit import (
    AUDIT_POLICY,
    SITE_EVIDENCE_POLICY_KEYS,
    V13_AUDIT_POLICY,
    audit_paths,
    audit_policy,
    check_outcome,
    crawl_respects_sitemap,
    digest,
    summary_paths,
)
from tin_lite.organic_audit_ai import ai_contract, ai_schemas
from tin_lite.organic_audit_completion import NEUTRAL_KEYS
from tin_lite.service_pricing import service_terms

V13 = "organic-audit-v13"
V14 = "organic-audit-v14"
# What main shipped for v13 at 0d9c853, before v14 existed: the policy, the AI instructions
# and schemas, and the page facts and files the synthetic v12 site produces under v13.
V13_POLICY_DIGEST = "a88c02c140f4161da204fab8844cb5bb69390e76d7aafdd4ecb61871e99f9203"
V13_CONTRACT_DIGEST = "8e4247192536b81a59d29fd43a50678ed50a809d53633b4699cc41f6ee4a7a3f"
V13_SCHEMAS_DIGEST = "7c0ac931e8e612cd09b2a9a55b1425f145fb71c5af613eb1b15ac02c70a1ada8"
V13_PAGE_FACTS_DIGEST = "5338ba3c3c18abf7b7f8cf9055ead3af61e5d73ef170d1c3274d0c914ceb25dc"
V13_FILES = {
    "AUDIT.md": "723c53988a626831637923ef2b6def15adc363e83637d455538834f36438a08d",
    "findings.json": "d386a64ae628d0ba8957190bf67e28feaaec0aa5a0e0faf840caeb3b821b5fb2",
    "evidence.json": "8bada13a4e0db6ca430697782dbc1f64bb06994f6ccf73852772652f7c62f0e3",
    "SUMMARY.json": "c70f570bec83cb88162377d435a77d7d076d683fdd9fdad01adfacb6c7e1a802",
    "LATEST.json": "c70f570bec83cb88162377d435a77d7d076d683fdd9fdad01adfacb6c7e1a802",
}
SCOPE = {"url": "https://example.com/", "host": "example.com", "policy_version": V14}


def no_sitemap_site() -> SyntheticSite:
    """A homepage with links and a robots.txt that names no sitemap; /sitemap.xml is a 404."""
    site = SyntheticSite()
    site.text("https://example.com/robots.txt", "User-agent: *\nAllow: /\n")
    site.page(
        "https://example.com/",
        html_page(
            title="Example product",
            canonical="https://example.com/",
            body='<a href="/pricing">Pricing</a><a href="/blog">Blog</a>',
        ),
    )
    return site


async def started(policy=None, site=None):
    activities, db, storage, provider = await activities_fixture(policy=policy)
    if site is not None:
        activities.site_reader = site.reader
    run_id = str(db.run.id)
    await activities.organic_start_crawl(run_id)
    return activities, db, storage, provider, run_id


def test_v13_is_exactly_what_main_shipped():
    v13 = audit_policy(V13)
    assert v13 is V13_AUDIT_POLICY
    assert digest(v13) == V13_POLICY_DIGEST
    assert digest(ai_contract(V13)) == V13_CONTRACT_DIGEST
    assert digest(ai_schemas(V13)) == V13_SCHEMAS_DIGEST


@pytest.mark.asyncio
async def test_a_v13_run_reads_and_writes_exactly_what_it_did_before_v14():
    files, pages = await site_evidence(V13_AUDIT_POLICY)
    assert digest(pages) == V13_PAGE_FACTS_DIGEST
    docs = documents(V13_AUDIT_POLICY, files, pages)
    paths = {**audit_paths(RUN_ID), **summary_paths(RUN_ID)}
    assert {name: hashlib.sha256(docs[path]).hexdigest() for name, path in paths.items()} == (
        V13_FILES
    )


def test_v14_is_the_default_and_adds_only_link_following_to_v13():
    assert AUDIT_POLICY["version"] == V14 and audit_policy() is AUDIT_POLICY
    assert audit_policy(V14) is AUDIT_POLICY
    assert {k: v for k, v in AUDIT_POLICY.items() if k != "version"} == {
        **{k: v for k, v in V13_AUDIT_POLICY.items() if k != "version"},
        "follow_links_without_sitemap": True,
    }
    # A crawl setting: the same answers and grading, so an answer completion may cross them.
    assert "follow_links_without_sitemap" in SITE_EVIDENCE_POLICY_KEYS
    assert ai_contract(V14) == ai_contract(V13) and ai_schemas(V14) == ai_schemas(V13)
    assert {k: v for k, v in AUDIT_POLICY.items() if k not in NEUTRAL_KEYS} == {
        k: v for k, v in V13_AUDIT_POLICY.items() if k not in NEUTRAL_KEYS
    }
    workflow = next(w for w in BUILTIN_WORKFLOWS if w.key == "organic.audit")
    assert workflow.version_label == "0.10.0"
    assert workflow.definition["audit_policy"] == AUDIT_POLICY
    # The same billing maximum as v13.
    definition = {"executor": "organic.audit"}
    v13 = service_terms({**definition, "audit_policy": V13_AUDIT_POLICY})
    v14 = service_terms({**definition, "audit_policy": AUDIT_POLICY})
    assert v14["maximum_nanos"] == v13["maximum_nanos"]


def test_only_an_in_scope_https_sitemap_url_keeps_the_sitemap():
    def files(*locs, status="observed"):
        return {"status": status, "sitemaps": {"urls": [{"loc": loc} for loc in locs]}}

    assert crawl_respects_sitemap(SCOPE, files("https://example.com/pricing"))
    for saved in (
        None,
        {"status": "unavailable", "reason": "site_read_failed"},
        {"status": "observed", "robots": {}},
        files(),
        files("https://elsewhere.example/pricing"),
        files("http://example.com/pricing"),
    ):
        assert crawl_respects_sitemap(SCOPE, saved) is False
    # Before v14 the policy decides, whatever the files hold.
    assert crawl_respects_sitemap({**SCOPE, "policy_version": V13}, None) is True
    assert crawl_respects_sitemap({**SCOPE, "policy_version": "organic-audit-v8"}, None) is False
    # An answer completion reuses its source crawl and reads no site files.
    assert crawl_respects_sitemap({**SCOPE, "completion": {"source_run_id": "x"}}, None) is True


@pytest.mark.asyncio
async def test_v14_without_a_sitemap_asks_the_provider_to_follow_links():
    activities, _, storage, provider, run_id = await started(site=no_sitemap_site())
    files = await activities._result(run_id, "site_files")
    assert not (files.get("sitemaps") or {}).get("urls")
    request = provider.submit.await_args.args[0]
    assert request["respect_sitemap"] is False
    assert request == DataForSEO.crawl_request(
        host="example.com",
        tag=f"tin-organic-{run_id}",
        respect_sitemap=False,
        max_pages=100,
        priority_urls=[],
    )
    provider.pages.return_value = [page_fixture(checks={"canonical": True, "is_orphan_page": True})]
    assert await activities.organic_poll_crawl(run_id)
    crawl = await activities._result(run_id, "crawl")
    assert crawl["crawl_mode"] == "links"
    assert "followed links from the homepage" in crawl["note"]
    # The page context says what the request sent, so orphan flags are not trusted.
    page = crawl["pages"][0]
    assert page["provider_context"]["respect_sitemap"] is False
    assert check_outcome(page, "is_orphan_page") == "unknown"
    assert await activities.organic_prepare_panel(run_id) == 0  # No model configured.
    await activities.organic_publish(run_id)
    report = storage.repo.trees[storage.repo.head][audit_paths(run_id)["AUDIT.md"]][1]
    assert b"followed links from the homepage up to the page cap" in report


@pytest.mark.asyncio
async def test_v14_with_a_sitemap_keeps_respecting_it():
    activities, _, _, provider, run_id = await started(site=minimal_site())
    assert provider.submit.await_args.args[0]["respect_sitemap"] is True
    assert await activities.organic_poll_crawl(run_id)
    crawl = await activities._result(run_id, "crawl")
    assert crawl["crawl_mode"] == "sitemap"
    assert crawl["pages"][0]["provider_context"]["respect_sitemap"] is True


@pytest.mark.asyncio
async def test_v13_still_respects_the_sitemap_when_there_is_none():
    activities, _, _, provider, run_id = await started(
        policy=V13_AUDIT_POLICY, site=no_sitemap_site()
    )
    assert (await activities._result(run_id, "scope"))["policy_version"] == V13
    request = provider.submit.await_args.args[0]
    assert request["respect_sitemap"] is True
    assert request == DataForSEO.crawl_request(
        host="example.com",
        tag=f"tin-organic-{run_id}",
        respect_sitemap=True,
        max_pages=100,
        priority_urls=[],
    )
    assert await activities.organic_poll_crawl(run_id)
    crawl = await activities._result(run_id, "crawl")
    assert "crawl_mode" not in crawl
    assert crawl["note"] == "Static HTML only; no JavaScript or resource rendering."
    assert crawl["pages"][0]["provider_context"]["respect_sitemap"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("site", "expected"), [(no_sitemap_site, False), (minimal_site, True)], ids=["links", "map"]
)
async def test_recovery_rebuilds_the_request_it_submitted(site, expected):
    activities, db, _, provider = await activities_fixture()
    run_id = str(db.run.id)
    activities.site_reader = site().reader
    provider.submit.side_effect = TimeoutError
    with pytest.raises(ApplicationError):
        await activities.organic_start_crawl(run_id)
    submitted = provider.submit.await_args.args[0]
    assert submitted["respect_sitemap"] is expected
    # The site changes before the retry; the saved site files still decide.
    activities.site_reader = (minimal_site if site is no_sitemap_site else no_sitemap_site)().reader
    provider.recover.return_value = {"task_id": run_id, "reported_cost_usd": "0.015"}
    await activities.organic_start_crawl(run_id)
    assert provider.submit.await_count == 1 and provider.recover.await_count == 1
    assert provider.recover.await_args.kwargs["request"] == submitted


@pytest.mark.asyncio
async def test_a_stopped_v14_crawl_still_says_how_it_crawled():
    activities, _, _, _, run_id = await started(site=no_sitemap_site())
    await activities.organic_end_crawl(run_id)
    crawl = await activities._result(run_id, "crawl")
    assert crawl["status"] == "partial" and crawl["crawl_mode"] == "links"
    assert crawl["note"].startswith("Crawl collection reached its time or retry limit. ")
    assert crawl["pages"][0]["provider_context"]["respect_sitemap"] is False
