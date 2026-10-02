"""Audit policy v12 writes SUMMARY.json for code workflows; a run pinned to v11 is unchanged.

Code workflows read project files of at most 64,000 bytes, and tin.computer's evidence.json was
188 KB. v11 can deploy any time, so it must keep writing exactly v11's files. Offline only.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from organic_site_stub import SyntheticSite, html_page, sitemap
from test_organic_audit import activities_fixture
from test_procedure_publication import HistoryStorage

from tin_lite.code_project_files import MAX_FILE_BYTES
from tin_lite.organic_audit import (
    ARTIFACT_LIMITS,
    LATEST_SUMMARY_PATH,
    SUMMARY_READ_LIMIT,
    V11_AUDIT_POLICY,
    V12_AUDIT_POLICY,
    audit_paths,
    audit_policy,
    canonical_json,
    digest,
    normalize_pages,
    publication_contract,
    site_check_documents,
    summary_paths,
)
from tin_lite.organic_audit_ai import ai_contract, ai_schemas
from tin_lite.organic_audit_checks import SiteView
from tin_lite.organic_audit_completion import NEUTRAL_KEYS
from tin_lite.organic_audit_fetch import read_pages, read_site_files
from tin_lite.organic_audit_publication import publish_audit
from tin_lite.organic_audit_site import (
    MAX_LINK_KEY_CHARS,
    V12_PAGE_FACTS,
    html_facts,
    parse_robots,
)
from tin_lite.organic_audit_summary import (
    DROP_ORDER,
    MAX_CHECKS,
    PAGE_COLUMNS,
    ai_headline,
    link_graph,
    summary_document,
)
from tin_lite.publication import OutputConflictError, PublicationPendingError

V11 = "organic-audit-v11"
V12 = "organic-audit-v12"
# What main pinned for v11 at 4a48249, before v12 existed: the policy, the AI instructions and
# schemas, and the page facts and files the synthetic run below produced.
V11_POLICY_DIGEST = "110197be94310c48ab841bf325018c742d256f9acde5183dd4c73eee93b317a9"
V11_CONTRACT_DIGEST = "8e4247192536b81a59d29fd43a50678ed50a809d53633b4699cc41f6ee4a7a3f"
V11_SCHEMAS_DIGEST = "7c0ac931e8e612cd09b2a9a55b1425f145fb71c5af613eb1b15ac02c70a1ada8"
V11_PAGE_FACTS_DIGEST = "8f145cba1a4480f4b9c37ab3573b5fed7cac10699f3c5de167b8836fc1e3eba2"
V11_FILES = {
    "AUDIT.md": "44c5279dc153dc62d97ea3bf8c4e659f207bee95304a9ce3dbebf2bbdfcb79ca",
    "findings.json": "33099007ff38fcf3302e4e5d64af430d23c7979f41ed0049f58abf1204e88756",
    "evidence.json": "02c20df40ce076ee283519d3c10597e5ad8d9afb572e1291bd3d804fbbec356f",
}

HOST = "example.com"
BASE = f"https://{HOST}"
RUN_ID = "00000000-0000-4000-8000-000000000012"
STARTED_AT = "2026-09-30T00:00:00+00:00"
PROJECT_ID = "00000000-0000-4000-8000-000000000001"
# The homepage links to pricing and the blog; the blog to two posts (once through www and a
# trailing slash); post A to post C through a query string. Nothing links to /orphan.
LINKS = {
    "/": ["/pricing", "/blog", "https://elsewhere.example/x", "#top", "mailto:a@example.com"],
    "/pricing": ["/", "/blog/post-a", "/pricing"],
    "/blog": ["/blog/post-a", "/blog/post-b", "https://www.example.com/blog/post-b/"],
    "/blog/post-a": ["/blog/post-c?ref=a"],
    "/blog/post-b": [],
    "/blog/post-c": [],
    "/orphan": [],
}


async def site_evidence(policy):
    site = SyntheticSite()
    site.text(f"{BASE}/robots.txt", f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n")
    site.text(
        f"{BASE}/sitemap.xml", sitemap([BASE + p for p in LINKS]), content_type="application/xml"
    )
    for path, links in LINKS.items():
        anchors = "".join(f'<a href="{href}">Link {i}</a>' for i, href in enumerate(links))
        body = "<p>" + "Words about planning work. " * 20 + "</p>" + anchors
        site.page(BASE + path, html_page(title=f"Page {path}", canonical=BASE + path, body=body))
    async with site.reader((HOST,)) as reader:
        files = await read_site_files(reader, f"{BASE}/", policy)
        pages = await read_pages(
            reader, [BASE + p for p in LINKS], robots=files.get("robots"), policy=policy, seconds=30
        )
    files["observed_at"] = "2026-09-30T00:00:00+00:00"  # the one clock reading
    return files, [pages[url] for url in sorted(pages)]


def documents(
    policy, files, pages, *, run_id=RUN_ID, ai=None, started_at=STARTED_AT, completion=None
):
    crawl = normalize_pages(
        [
            {
                "url": BASE + path,
                "resource_type": "html",
                "status_code": 200,
                "meta": {"title": f"Page {path}"},
                "checks": {"canonical": True, "is_orphan_page": path == "/orphan"},
            }
            for path in LINKS
        ],
        HOST,
        policy_version=policy["version"],
    )
    return site_check_documents(
        run_id=run_id,
        project_id=PROJECT_ID,
        definition_sha="d" * 40,
        scope={
            "url": f"{BASE}/",
            "host": HOST,
            "market": "US",
            "language": "en",
            "started_at": started_at,
            "page_cap": 100,
            "policy_version": policy["version"],
            **({"completion": completion} if completion else {}),
        },
        crawl={"status": "completed", "pages": crawl},
        ai=ai or {"status": "partial", "summary": "Not measured.", "planned": 0, "completed": 0},
        spending={},
        policy=policy,
        search_console=None,
        search_queries=None,
        site={
            "files": files,
            "plan": None,
            "pages": pages,
            "pages_status": "complete",
            "pagespeed": {"status": "not_configured", "results": []},
        },
    )


def rows_of(summary):
    columns = summary["pages"]["columns"]
    return {row[0]: dict(zip(columns, row, strict=True)) for row in summary["pages"]["rows"]}


# --- pinning ----------------------------------------------------------------------------------


def test_v11_is_exactly_what_main_shipped_and_v12_adds_only_the_summary():
    v11 = audit_policy(V11)
    assert v11 is V11_AUDIT_POLICY
    assert digest(v11) == V11_POLICY_DIGEST
    assert digest(ai_contract(V11)) == V11_CONTRACT_DIGEST
    assert digest(ai_schemas(V11)) == V11_SCHEMAS_DIGEST
    assert V12_AUDIT_POLICY["version"] == V12 and audit_policy(V12) is V12_AUDIT_POLICY
    assert {k: v for k, v in V12_AUDIT_POLICY.items() if k != "version"} == {
        **{k: v for k, v in v11.items() if k != "version"},
        "summary_max_bytes": 60_000,
        "max_internal_links": 250,
    }
    # The same questions, answers and grading as v11, so an answer completion may cross them.
    assert ai_contract(V12) == ai_contract(V11) and ai_schemas(V12) == ai_schemas(V11)
    assert {k: v for k, v in V12_AUDIT_POLICY.items() if k not in NEUTRAL_KEYS} == {
        k: v for k, v in v11.items() if k not in NEUTRAL_KEYS
    }
    # The budget sits under what a code workflow may read, which the publication enforces.
    assert V12_AUDIT_POLICY["summary_max_bytes"] < SUMMARY_READ_LIMIT == MAX_FILE_BYTES


@pytest.mark.asyncio
async def test_a_v11_run_reads_and_writes_exactly_what_it_did_before_v12():
    files, pages = await site_evidence(V11_AUDIT_POLICY)
    assert digest(pages) == V11_PAGE_FACTS_DIGEST
    assert not any(V12_PAGE_FACTS & set(page) for page in pages)
    docs = documents(V11_AUDIT_POLICY, files, pages)
    paths = audit_paths(RUN_ID)
    assert set(docs) == set(paths.values())
    assert {name: hashlib.sha256(docs[path]).hexdigest() for name, path in paths.items()} == (
        V11_FILES
    )


# --- the summary ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v12_writes_the_summary_and_an_identical_latest_copy():
    files, pages = await site_evidence(V12_AUDIT_POLICY)
    docs = documents(V12_AUDIT_POLICY, files, pages)
    paths = audit_paths(RUN_ID) | summary_paths(RUN_ID)
    assert set(docs) == set(paths.values())
    assert LATEST_SUMMARY_PATH == paths["LATEST.json"] == "reports/organic-audit/LATEST.json"
    raw = docs[paths["SUMMARY.json"]]
    assert docs[LATEST_SUMMARY_PATH] == raw and len(raw) <= V12_AUDIT_POLICY["summary_max_bytes"]
    summary = json.loads(raw)
    findings = json.loads(docs[paths["findings.json"]])
    evidence = json.loads(docs[paths["evidence.json"]])
    assert set(summary) == {
        "schema_version",
        "kind",
        "run_id",
        "host",
        "hosts",
        "site_url",
        "market",
        "audited_at",
        "policy_version",
        "files",
        "findings_sha256",
        "evidence_sha256",
        "coverage",
        "findings",
        "ai_visibility",
        "links",
        "pages",
        "truncated",
    }
    assert (summary["schema_version"], summary["kind"]) == (1, "organic_audit_summary")
    assert (summary["run_id"], summary["host"], summary["hosts"]) == (RUN_ID, HOST, [HOST])
    assert (summary["site_url"], summary["market"], summary["policy_version"]) == (
        f"{BASE}/",
        "US",
        V12,
    )
    assert summary["audited_at"] == "2026-09-30T00:00:00+00:00" and summary["truncated"] is False
    assert summary["files"] == {
        "report": paths["AUDIT.md"],
        "findings": paths["findings.json"],
        "evidence": paths["evidence.json"],
        "summary": paths["SUMMARY.json"],
    }
    assert summary["findings_sha256"] == digest(findings)
    assert summary["evidence_sha256"] == findings["evidence_sha256"] == digest(evidence)
    assert summary["coverage"] == {
        "status": "complete",
        "sitemap_read": True,
        "sitemap_pages": 7,
        "inspected_sitemap_pages": 7,
        "inspected_pages": 7,
        "page_cap": 100,
        "skipped_sitemap_pages": 0,
        "crawled_pages": 7,
        "read_pages": 7,
    }
    counts = summary["findings"]
    assert (
        counts["total"]
        == len(findings["findings"])
        == sum(entry["findings"] for entry in counts["by_check"])
    )
    assert counts["by_priority"] == findings["summary"]["by_priority"]
    assert [item["id"] for item in counts["top"]] == findings["summary"]["top_issue_ids"]
    assert counts["checks_omitted"] == 0
    orphan = next(e for e in counts["by_check"] if e["check"] == "discovery.possible_orphan")
    assert orphan == {
        "check": "discovery.possible_orphan",
        "findings": 1,
        "pages": 1,
        "listed": 1,
        "priority": "high_impact",
    }
    assert summary["pages"]["columns"] == list(PAGE_COLUMNS)
    assert summary["pages"]["total"] == len(summary["pages"]["rows"]) == 7
    assert summary["pages"]["rows"][0][0] == "/"  # the homepage comes first
    rows = rows_of(summary)
    assert rows["/"] == {
        "path": "/",
        "status": 200,
        "read": "observed",
        "indexable": True,
        "noindex": False,
        "canonical": "self",
        "title": True,
        "description": False,
        "words": 92,
        "inbound": 1,
        "depth": 0,
        "checks": [],
    }
    named = [counts["by_check"][index]["check"] for index in rows["/orphan"]["checks"]]
    assert named == ["discovery.possible_orphan"]
    # The evidence keeps the links Tin saw; the report points code workflows at the summary.
    home = next(page for page in evidence["site"]["pages"] if page["url"] == f"{BASE}/")
    assert home["internal_links"] == ["/pricing", "/blog"]
    assert "`reports/organic-audit/LATEST.json`" in docs[paths["AUDIT.md"]].decode()


@pytest.mark.asyncio
async def test_click_depth_and_inbound_links_on_a_small_link_graph():
    files, pages = await site_evidence(V12_AUDIT_POLICY)
    summary = json.loads(
        documents(V12_AUDIT_POLICY, files, pages)[summary_paths(RUN_ID)["LATEST.json"]]
    )
    rows = rows_of(summary)
    assert {path: row["depth"] for path, row in rows.items()} == {
        "/": 0,
        "/pricing": 1,
        "/blog": 1,
        "/blog/post-a": 2,
        "/blog/post-b": 2,
        "/blog/post-c": 3,  # through /blog/post-c?ref=a
        "/orphan": None,
    }
    # Distinct read pages that link in; the www, trailing-slash link to post B is the same page.
    assert {path: row["inbound"] for path, row in rows.items()} == {
        "/": 1,
        "/pricing": 1,
        "/blog": 1,
        "/blog/post-a": 2,
        "/blog/post-b": 1,
        "/blog/post-c": 1,
        "/orphan": 0,
    }
    assert summary["links"] | {"note": None} == {
        "status": "observed",
        "start": "/",
        "pages_with_links": 7,
        "links": 7,
        "capped_pages": 0,
        "depth": "exact",
        "unreached": 1,
        "note": None,
    }


def test_a_link_list_missing_a_link_is_capped_so_depth_is_not_exact():
    def facts(body, *, truncated=False):
        return html_facts(
            f"<html><body>{body}</body></html>".encode(),
            url=f"{BASE}/",
            charset="utf-8",
            truncated=truncated,
            max_links=V12_AUDIT_POLICY["max_internal_links"],
        )

    plain = facts('<a href="/a">A</a>')
    assert plain["internal_links"] == ["/a"] and plain["internal_links_capped"] is False
    # A link too long to keep is missing from the list, and so is anything past a cut body.
    long = facts(f'<a href="/a">A</a><a href="/{"x" * MAX_LINK_KEY_CHARS}">B</a>')
    assert long["internal_links"] == ["/a"] and long["internal_links_capped"] is True
    cut = facts('<a href="/a">A</a>', truncated=True)
    assert cut["internal_links"] == ["/a"] and cut["internal_links_capped"] is True
    # v11 pages carry neither field.
    assert not V12_PAGE_FACTS & set(
        html_facts(b"<a href='/a'>A</a>", url=f"{BASE}/", charset="utf-8", truncated=True)
    )


def test_a_redirect_costs_no_click_and_a_run_without_links_reports_none():
    def page(path, links):
        return {
            "url": BASE + path,
            "fetch": "observed",
            "status_code": 200,
            "internal_links": links,
        }

    def redirect(path, target):
        return {"url": BASE + path, "fetch": "redirect", "location": BASE + target}

    def view(pages):
        return SiteView(
            host=HOST,
            hosts=(HOST,),
            site={"files": {}, "pages": pages},
            crawl_pages=[],
            search_pages=[],
            search_queries=[],
        )

    graph = link_graph(
        view(
            [
                redirect("/", "/en"),
                page("/en", ["/old", "/guide"]),
                redirect("/old", "/new"),
                page("/new", ["/en"]),
                page("/guide", ["/guide/deep"]),
            ]
        ),
        "/",
    )
    assert graph["depth"] == {
        "/": 0,
        "/en": 0,
        "/old": 1,
        "/new": 1,
        "/guide": 1,
        "/guide/deep": 2,
    }
    assert graph["inbound"] == {"/old": 1, "/guide": 1, "/guide/deep": 1, "/en": 1}
    # Pages read before v12 carry no links: no graph, so the rows say null instead of zero.
    plain = {"url": f"{BASE}/", "fetch": "observed", "status_code": 200}
    assert link_graph(view([plain]), "/") is None


def test_the_ai_headline_carries_counts_not_answers():
    ai = {
        "status": "completed",
        "planned": 24,
        "completed": 24,
        "panel": {"questions": [{"question": f"Q{i}"} for i in range(8)]},
        "metrics": {"mentioned": 5, "owned_domain_cited": 2, "shortlisted": 3, "selected_first": 1},
        "observed_metrics": {
            "mentioned": 5,
            "owned_domain_cited": 2,
            "shortlisted": 3,
            "selected_first": 1,
        },
        "ladder": {
            "scored": 24,
            "counts": {
                "found": 9,
                "mentioned": 5,
                "evaluated": 4,
                "shortlisted": 3,
                "selected_first": 1,
            },
            "bottleneck": {"stage": "found", "label": "Discovery", "why": "Not retrieved."},
        },
        "memory": {
            "planned": 8,
            "completed": 8,
            "mentioned": 1,
            "shortlisted": 0,
            "observations": [{"answer": "long text"}],
        },
        "question_set": {"sha256": "f" * 64, "source_run_id": "r1", "questions": 8},
        "cited_domains": [{"domain": f"site{i}.example", "answers": 9 - i} for i in range(8)],
        "observations": [{"answer": "never copied"}],
        "summary": "Long prose that stays in the report.",
    }
    assert ai_headline(ai) == {
        "status": "completed",
        "planned": 24,
        "completed": 24,
        "questions": 8,
        "metrics": ai["metrics"],
        "observed": ai["observed_metrics"],
        "ladder": {"scored": 24, "counts": ai["ladder"]["counts"], "main_break": "found"},
        "without_search": {"planned": 8, "completed": 8, "mentioned": 1, "shortlisted": 0},
        "question_set": {"sha256": "f" * 64, "reused_from": "r1"},
        "cited_instead": [[f"site{i}.example", 9 - i] for i in range(5)],
    }
    # Not measured: every count is there, as zero or null, never guessed.
    empty = ai_headline({"status": "partial", "summary": "Not measured."})
    assert empty["planned"] == 0 and empty["metrics"] is None and empty["ladder"] is None


# --- the 64 KB cap ----------------------------------------------------------------------------


def large_site(count=500, links=60):
    """A crawl of `count` pages under long paths, every one linking to `links` others."""
    paths = ["/"] + [
        "/resources/guides/how-small-product-teams-plan-review-and-ship-work-each-week/"
        f"a-long-descriptive-slug-about-planning-work-{i:03}"
        for i in range(count - 1)
    ]
    robots = parse_robots(f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n")
    files = {
        "status": "observed",
        "robots": {"status": "observed", **robots},
        "sitemaps": {
            "referenced_in_robots": True,
            "files": [{"url": f"{BASE}/sitemap.xml", "status": "observed", "kind": "urlset"}],
            "unread": 0,
            "total_urls": count,
            "urls": [{"loc": BASE + path, "lastmod": None} for path in paths],
            "urls_capped": False,
        },
    }
    facts = [
        {
            "url": BASE + path,
            "fetch": "observed",
            "status_code": 200,
            "x_robots_tag": [],
            "lang": "en",
            "robots": [],
            "canonical": BASE + path,
            "canonical_count": 1,
            "hreflang": [],
            "h1_count": 0,
            "title": "",
            "json_ld_blocks": 0,
            "json_ld_types": [],
            "microdata": False,
            "html_bytes": 40_000,
            "truncated": False,
            "description_length": None,
            "text_words": 1200 + i,
            "internal_links": [paths[(i * 7 + k) % count] for k in range(1, links + 1)],
            "internal_links_capped": False,
        }
        for i, path in enumerate(paths)
    ]
    crawl = normalize_pages(
        [
            {
                "url": BASE + path,
                "resource_type": "html",
                "status_code": 200,
                "meta": {"title": ""},
                "checks": {"canonical": True, "no_title": True, "no_description": True},
            }
            for path in paths[:300]
        ],
        HOST,
        policy_version=V12,
    )
    search = {
        "status": "completed",
        "value": {
            "property": "sc-domain:example.com",
            "start_date": "2026-09-01",
            "end_date": "2026-09-28",
            "pages": [
                {"url": BASE + path, "clicks": 1, "impressions": 1000 - i, "position": 9.0}
                for i, path in enumerate(paths[:200])
            ],
        },
    }
    return paths, files, facts, crawl, search


def test_a_500_page_crawl_keeps_the_summary_under_64_kb():
    paths, files, facts, crawl, search = large_site()
    # A low evidence bound also shows the link lists leave evidence first, after the summary
    # has counted them.
    policy = {**V12_AUDIT_POLICY, "max_evidence_bytes": 1_500_000}
    run_id = str(uuid4())
    docs = site_check_documents(
        run_id=run_id,
        project_id=PROJECT_ID,
        definition_sha="d" * 40,
        scope={
            "url": f"{BASE}/",
            "host": HOST,
            "market": "US",
            "language": "en",
            "started_at": "2026-09-30T00:00:00+00:00",
            "page_cap": 300,
            "policy_version": V12,
        },
        crawl={"status": "completed", "pages": crawl},
        ai={"status": "partial", "summary": "Not measured.", "planned": 0, "completed": 0},
        spending={},
        policy=policy,
        search_console=search,
        search_queries=None,
        site={
            "files": files,
            "plan": None,
            "pages": facts,
            "pages_status": "complete",
            "pagespeed": {"status": "not_configured", "results": []},
        },
    )
    named = audit_paths(run_id) | summary_paths(run_id)
    raw = docs[named["SUMMARY.json"]]
    assert len(docs[named["evidence.json"]]) > MAX_FILE_BYTES  # what code could not read
    assert len(raw) <= V12_AUDIT_POLICY["summary_max_bytes"] < MAX_FILE_BYTES
    assert docs[named["LATEST.json"]] == raw
    summary = json.loads(raw)
    truncated = summary["truncated"]
    # Columns of least use went first, in order; the rest of the cut came from whole pages.
    assert truncated["columns"] == list(DROP_ORDER[: len(truncated["columns"])])
    assert truncated["pages"] > 0
    assert summary["pages"]["total"] == 500
    assert len(summary["pages"]["rows"]) + truncated["pages"] == 500
    assert {"path", "status", "indexable", "noindex", "inbound", "depth"} <= set(
        summary["pages"]["columns"]
    )
    kept = [row[0] for row in summary["pages"]["rows"]]
    # The homepage, then the pages with the most search impressions.
    assert len(kept) > 200 and kept[:200] == paths[:200]
    rows = rows_of(summary)
    assert rows["/"]["depth"] == 0 and rows["/"]["inbound"] > 0
    assert all(row["depth"] is not None for row in rows.values())
    edges = sum(len(set(f["internal_links"]) - {f["url"].removeprefix(BASE)}) for f in facts)
    assert summary["links"]["status"] == "observed" and summary["links"]["links"] == edges
    evidence = json.loads(docs[named["evidence.json"]])
    assert evidence["trimmed_for_size"]["internal_links"] == 500
    assert not any("internal_links" in page for page in evidence["site"]["pages"])


def test_long_findings_over_many_checks_still_fit():
    paths, files, facts, crawl, _ = large_site(count=60, links=10)
    view = SiteView(
        host=HOST,
        hosts=(HOST,),
        site={"files": files, "pages": facts},
        crawl_pages=crawl,
        search_pages=[],
        search_queries=[],
    )
    findings = [
        {
            "id": f"oa_{i:020}",
            "check_id": f"synthetic.check_with_a_long_descriptive_name_{i % 150:03}",
            "priority": "quick_win",
            "issue": "A long issue statement. " * 40,
            "evidence": ["A long evidence line that names many pages. " * 9] * 12,
            "urls": [BASE + path for path in paths[i % 50 : i % 50 + 10]],
            "affected_count": 40,
        }
        for i in range(300)
    ]
    assert len(canonical_json(findings)) > MAX_FILE_BYTES
    run_id = str(uuid4())
    raw = summary_document(
        run_id=run_id,
        scope={"url": f"{BASE}/", "host": HOST, "market": "US", "started_at": "2026-09-30"},
        hosts=(HOST,),
        policy=V12_AUDIT_POLICY,
        view=view,
        crawl_pages=crawl,
        cover={"status": "complete"},
        findings=findings,
        top_issue_ids=[findings[0]["id"]],
        ai={},
        paths=audit_paths(run_id) | summary_paths(run_id),
        findings_sha256="a" * 64,
        evidence_sha256="b" * 64,
    )
    assert len(raw) <= V12_AUDIT_POLICY["summary_max_bytes"]
    summary = json.loads(raw)
    assert summary["findings"]["total"] == 300
    assert len(summary["findings"]["by_check"]) == MAX_CHECKS
    assert summary["findings"]["checks_omitted"] == 150 - MAX_CHECKS
    assert all(entry["findings"] == 2 for entry in summary["findings"]["by_check"])
    if "checks" in summary["pages"]["columns"]:
        for row in rows_of(summary).values():
            assert all(0 <= index < MAX_CHECKS for index in row["checks"])


# --- publication ------------------------------------------------------------------------------


async def publish(
    storage, run_id, docs, *, policy_version=V12, intent=None, saved=None, completion=False
):
    saved = {} if saved is None else saved

    async def save(value):
        saved.update(value)

    return await publish_audit(
        storage=storage,
        repo_id=storage.repo.id,
        branch="main",
        run_id=run_id,
        documents=docs,
        intent=intent,
        save_intent=save,
        validate_active=AsyncMock(),
        policy_version=policy_version,
        completion=completion,
    )


@pytest.mark.asyncio
async def test_each_v12_publication_replaces_latest_and_never_a_run_file():
    storage = HistoryStorage()
    files, pages = await site_evidence(V12_AUDIT_POLICY)
    first, second, third = RUN_ID, str(uuid4()), str(uuid4())
    one = documents(V12_AUDIT_POLICY, files, pages, run_id=first)
    two = documents(V12_AUDIT_POLICY, files, pages, run_id=second)
    await publish(storage, first, one)
    await publish(storage, second, two)
    tree = storage.repo.trees[storage.repo.head]
    assert tree[LATEST_SUMMARY_PATH][1] == two[summary_paths(second)["SUMMARY.json"]]
    assert json.loads(tree[LATEST_SUMMARY_PATH][1])["run_id"] == second
    first_summary = summary_paths(first)["SUMMARY.json"]
    assert tree[first_summary][1] == one[first_summary]
    # A run's own files stay create-only, its summary included.
    three = documents(V12_AUDIT_POLICY, files, pages, run_id=third)
    storage.repo.edit({summary_paths(third)["SUMMARY.json"]: b"{}"})
    writes = storage.repo.writes
    with pytest.raises(OutputConflictError):
        await publish(storage, third, three)
    assert storage.repo.writes == writes
    # A v11 run publishes its three files and leaves LATEST.json alone; v11 files declared as
    # v12 fail closed instead of publishing without a summary.
    old_files, old_pages = await site_evidence(V11_AUDIT_POLICY)
    fourth = str(uuid4())
    v11_docs = documents(V11_AUDIT_POLICY, old_files, old_pages, run_id=fourth)
    with pytest.raises(ValueError, match="declared run-scoped artifacts"):
        await publish(storage, fourth, v11_docs)
    latest = storage.repo.trees[storage.repo.head][LATEST_SUMMARY_PATH]
    await publish(storage, fourth, v11_docs, policy_version=V11)
    tree = storage.repo.trees[storage.repo.head]
    assert tree[LATEST_SUMMARY_PATH] == latest
    assert summary_paths(fourth)["SUMMARY.json"] not in tree


@pytest.mark.asyncio
async def test_a_lost_response_recovers_even_after_a_later_audit_replaced_latest():
    storage = HistoryStorage()
    files, pages = await site_evidence(V12_AUDIT_POLICY)
    first, second = RUN_ID, str(uuid4())
    one = documents(V12_AUDIT_POLICY, files, pages, run_id=first)
    saved = {}
    storage.repo.lose_response = True
    with pytest.raises(PublicationPendingError):
        await publish(storage, first, one, saved=saved)
    original = storage.repo.head
    await publish(storage, second, documents(V12_AUDIT_POLICY, files, pages, run_id=second))
    later = storage.repo.head
    assert await publish(storage, first, one, intent=saved) == original
    assert storage.repo.head == later and storage.repo.writes == 2
    assert json.loads(storage.repo.trees[later][LATEST_SUMMARY_PATH][1])["run_id"] == second


EARLIER = "2026-09-29T00:00:00+00:00"


def provenance(source):
    """An answer completion's scope note, as `completion_seed` writes it."""
    return {
        "source_run_id": source,
        "source_revision": "a" * 40,
        "source_definition_commit_sha": "d" * 40,
        "source_policy_version": V12,
        "source_evidence_sha256": "e" * 64,
        "requested_at": "2026-09-30T01:00:00+00:00",
        "retried_index": 0,
        "retained_observations": 7,
        "original_missing_observation": {"index": 0, "status": "unavailable"},
        "note": "One explicitly authorized replacement request.",
    }


def latest_run(storage):
    return json.loads(storage.repo.trees[storage.repo.head][LATEST_SUMMARY_PATH][1])["run_id"]


@pytest.mark.asyncio
async def test_an_audit_that_started_earlier_but_publishes_later_leaves_latest_alone():
    storage = HistoryStorage()
    files, pages = await site_evidence(V12_AUDIT_POLICY)
    newer, older = str(uuid4()), str(uuid4())
    await publish(storage, newer, documents(V12_AUDIT_POLICY, files, pages, run_id=newer))
    slow = documents(V12_AUDIT_POLICY, files, pages, run_id=older, started_at=EARLIER)
    await publish(storage, older, slow)
    tree = storage.repo.trees[storage.repo.head]
    assert latest_run(storage) == newer
    # The rest of the slow audit still publishes, its own summary included.
    for path in [*audit_paths(older).values(), summary_paths(older)["SUMMARY.json"]]:
        assert tree[path][1] == slow[path]
    # A later start replaces it, and so does a pointer Tin can't read as a summary.
    latest = str(uuid4())
    later = "2026-10-01T00:00:00+00:00"
    await publish(
        storage, latest, documents(V12_AUDIT_POLICY, files, pages, run_id=latest, started_at=later)
    )
    assert latest_run(storage) == latest
    storage.repo.edit({LATEST_SUMMARY_PATH: b"edited by hand"})
    again = str(uuid4())
    await publish(
        storage, again, documents(V12_AUDIT_POLICY, files, pages, run_id=again, started_at=EARLIER)
    )
    assert latest_run(storage) == again


@pytest.mark.asyncio
async def test_a_latest_path_that_is_not_a_file_is_left_alone_and_the_audit_still_publishes():
    storage = HistoryStorage()
    storage.repo.edit({f"{LATEST_SUMMARY_PATH}/notes.md": b"a folder"})
    files, pages = await site_evidence(V12_AUDIT_POLICY)
    docs = documents(V12_AUDIT_POLICY, files, pages)
    await publish(storage, RUN_ID, docs)
    tree = storage.repo.trees[storage.repo.head]
    assert LATEST_SUMMARY_PATH not in tree
    assert (
        tree[summary_paths(RUN_ID)["SUMMARY.json"]][1]
        == docs[summary_paths(RUN_ID)["SUMMARY.json"]]
    )


@pytest.mark.asyncio
async def test_an_answer_completion_writes_its_own_summary_and_never_latest():
    storage = HistoryStorage()
    files, pages = await site_evidence(V12_AUDIT_POLICY)
    source, completion = str(uuid4()), str(uuid4())
    await publish(storage, source, documents(V12_AUDIT_POLICY, files, pages, run_id=source))
    # A completion copies its source's scope, start included, and reads no pages.
    docs = documents(V12_AUDIT_POLICY, files, [], run_id=completion, completion=provenance(source))
    paths = audit_paths(completion) | {"SUMMARY.json": summary_paths(completion)["SUMMARY.json"]}
    assert set(docs) == set(paths.values())
    assert publication_contract(completion, V12_AUDIT_POLICY, completion=True) == (
        paths,
        {**ARTIFACT_LIMITS, "SUMMARY.json": SUMMARY_READ_LIMIT},
        frozenset(),
    )
    with pytest.raises(ValueError, match="declared run-scoped artifacts"):
        await publish(storage, completion, docs)
    await publish(storage, completion, docs, completion=True)
    tree = storage.repo.trees[storage.repo.head]
    assert latest_run(storage) == source
    assert tree[paths["SUMMARY.json"]][1] == docs[paths["SUMMARY.json"]]


@pytest.mark.asyncio
async def test_a_retry_keeps_its_first_choice_until_that_attempt_is_proven_absent():
    storage = HistoryStorage()
    files, pages = await site_evidence(V12_AUDIT_POLICY)
    older, newer = str(uuid4()), str(uuid4())
    slow = documents(V12_AUDIT_POLICY, files, pages, run_id=older, started_at=EARLIER)
    fast = documents(V12_AUDIT_POLICY, files, pages, run_id=newer)
    # The slow audit decides to replace LATEST.json, then loses the race to a newer audit
    # before its commit lands: the retry finds no landed attempt and decides again.
    saved = {}
    storage.repo.before_send = lambda: storage.repo.edit(
        {path: content for path, content in fast.items()}, f"organic.audit {newer}"
    )
    with pytest.raises(PublicationPendingError):
        await publish(storage, older, slow, saved=saved)
    assert LATEST_SUMMARY_PATH in saved["manifest"]
    await publish(storage, older, slow, intent=saved)
    tree = storage.repo.trees[storage.repo.head]
    assert latest_run(storage) == newer
    assert (
        tree[summary_paths(older)["SUMMARY.json"]][1] == slow[summary_paths(older)["SUMMARY.json"]]
    )
    # A landed attempt that left LATEST.json alone reconciles to that same commit.
    oldest = str(uuid4())
    late = documents(V12_AUDIT_POLICY, files, pages, run_id=oldest, started_at=EARLIER)
    saved = {}
    storage.repo.lose_response = True
    with pytest.raises(PublicationPendingError):
        await publish(storage, oldest, late, saved=saved)
    landed, writes = storage.repo.head, storage.repo.writes
    assert LATEST_SUMMARY_PATH not in saved["manifest"]
    assert await publish(storage, oldest, late, intent=saved) == landed
    assert storage.repo.writes == writes and latest_run(storage) == newer


@pytest.mark.asyncio
async def test_a_completion_run_publishes_without_moving_latest():
    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    await activities.organic_start_crawl(run_id)
    assert await activities.organic_poll_crawl(run_id)
    await activities.organic_end_crawl(run_id)
    assert await activities.organic_prepare_panel(run_id) == 0  # No model configured.
    # An older audit's pointer, which an ordinary run would replace.
    old = json.dumps({"run_id": "earlier", "audited_at": "2000-01-01T00:00:00+00:00"}).encode()
    storage.repo.edit({LATEST_SUMMARY_PATH: old})
    db.effects[f"organic:{run_id}:scope"].result["completion"] = provenance(str(uuid4()))
    await activities.organic_publish(run_id)
    receipt = db.effects[f"organic:{run_id}:publish"].result
    tree = storage.repo.trees[receipt["canonical_commit_sha"]]
    assert tree[LATEST_SUMMARY_PATH][1] == old
    assert receipt["summary_path"] in tree


@pytest.mark.asyncio
async def test_the_publish_receipt_still_verifies_only_the_three_audit_files():
    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    await activities.organic_start_crawl(run_id)
    assert await activities.organic_poll_crawl(run_id)
    await activities.organic_end_crawl(run_id)
    assert await activities.organic_prepare_panel(run_id) == 0  # No model configured.
    await activities.organic_publish(run_id)
    receipt = db.effects[f"organic:{run_id}:publish"].result
    tree = storage.repo.trees[receipt["canonical_commit_sha"]]
    # Technical fix, keyword and content plans read these three files back and compare.
    bundle = {path: tree[path][1].decode() for path in audit_paths(run_id).values()}
    assert receipt["documents_sha256"] == digest(bundle)
    summary = tree[summary_paths(run_id)["SUMMARY.json"]][1]
    assert receipt["summary_path"] == summary_paths(run_id)["SUMMARY.json"]
    assert receipt["summary_sha256"] == hashlib.sha256(summary).hexdigest()
    assert tree[LATEST_SUMMARY_PATH][1] == summary
    # The fixture pins the current policy, which keeps v12's summary.
    assert json.loads(summary)["policy_version"] == audit_policy()["version"]
