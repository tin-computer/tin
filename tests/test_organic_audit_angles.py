"""The audit's extra angles: rendering, access, status, page basics, answer engines, search.

Every check runs on saved evidence from synthetic sites; nothing here touches the network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from organic_site_stub import SyntheticSite, html_page, sitemap
from test_organic_audit import response
from test_organic_audit_results import frozen_panel, negative_judgment

from tin_lite.organic_audit import AUDIT_POLICY, ladder_report_lines
from tin_lite.organic_audit_ai import (
    AuditValidationError,
    classify,
    classify_absent_target,
    read_response,
    summarize,
)
from tin_lite.organic_audit_checks import SiteView, site_findings, speed_rows
from tin_lite.organic_audit_content import review_pages, validate_review
from tin_lite.organic_audit_fetch import (
    follow_redirects,
    lighthouse_summary,
    pagespeed_summary,
    read_crawler_access,
    read_pages,
    read_site_files,
)
from tin_lite.organic_audit_search import (
    inspection_row,
    inspection_urls,
    search_findings,
)
from tin_lite.organic_audit_site import (
    BROWSER_AGENT,
    client_rendered,
    html_facts,
    parse_robots,
)

HOST = "example.com"
BASE = f"https://{HOST}"


def facts(path: str, body: bytes, *, status: int = 200) -> dict:
    url = f"{BASE}{path}"
    return {
        "url": url,
        "status_code": status,
        "x_robots_tag": [],
        "fetch": "observed",
        **html_facts(body, url=url, charset=None, truncated=False),
    }


def view(pages: list[dict], *, files: dict | None = None, **site) -> SiteView:
    robots = parse_robots("User-agent: *\nAllow: /\n")
    return SiteView(
        host=HOST,
        hosts=(HOST,),
        site={
            "files": {
                "status": "observed",
                "robots": {"status": "observed", **robots},
                "sitemaps": {"files": [], "urls": []},
                **(files or {}),
            },
            "pages": pages,
            **site,
        },
        crawl_pages=[],
        search_pages=[],
        search_queries=[],
    )


def found(findings: list[dict], check_id: str) -> dict:
    matches = [item for item in findings if item["check_id"] == check_id]
    assert matches, f"{check_id} not in {[item['check_id'] for item in findings]}"
    return matches[0]


def ids(findings: list[dict]) -> set[str]:
    return {item["check_id"] for item in findings}


CONTENT = (
    "<p>The short answer: pick the tool that fits your stack and budget today.</p>"
    + "<p>"
    + "Plain words about planning work for small teams. " * 40
    + "</p>"
)


def test_static_html_facts_cover_page_basics_and_answer_engine_signals():
    body = (
        b"<!doctype html><html lang=en><head><title>How to pick a planning tool</title>"
        b'<meta name="description" content="Short.">'
        b'<meta name="viewport" content="width=device-width">'
        b'<meta property="og:title" content="Pick a tool">'
        b'<script async src="https://www.googletagmanager.com/gtag/js?id=G-1"></script>'
        b'<script type="application/ld+json">{"@type":"FAQPage","mainEntity":'
        b'[{"@type":"Question","name":"Which tool?"}]}</script>'
        b'<script type="application/ld+json">{not json</script>'
        b"</head><body><h1>Pick a tool</h1>"
        b"<p>The short answer is to pick the one that fits your stack today.</p>"
        b"<h2>How do I compare tools?</h2><h2>Pricing</h2>"
        b'<img src="a.png"><img src="b.png" alt="">'
        b'<a href="https://research.example.org/study">study</a>'
        b'<time datetime="2026-09-01">Sep 1</time></body></html>'
    )
    page = html_facts(body, url=f"{BASE}/guides/pick", charset=None, truncated=False)
    assert page["description_length"] == len("Short.")
    assert page["viewport"] is True and page["open_graph"] == ["title"]
    assert (page["images"], page["images_without_alt"]) == (2, 1)
    assert page["analytics"] == ["Google Analytics"]
    assert page["lead"] == "The short answer is to pick the one that fits your stack today."
    assert page["headings"] == ["How do I compare tools?", "Pricing"]
    assert page["question_headings"] == 1 and page["external_links"] == 1
    assert page["dated"] is True and page["author"] is False
    assert page["schema_invalid_blocks"] == 1
    assert page["schema_problems"] == [
        {
            "type": "FAQPage",
            "missing": ["mainEntity[].acceptedAnswer.text"],
            "recommended_missing": [],
        }
    ]
    assert page["not_found_text"] is False
    assert not client_rendered({**page, "fetch": "observed"})
    shell = html_facts(
        b'<html><head><script src="/app.js"></script></head><body><div id="root"></div></body>'
        b"</html>",
        url=f"{BASE}/",
        charset=None,
        truncated=False,
    )
    assert client_rendered({**shell, "fetch": "observed"})


def test_a_javascript_rendered_site_gets_one_finding_not_sitewide_missing_h1():
    shell = b'<html lang="en"><head><title>App</title><script src="/a.js"></script></head>'
    shell += b'<body><div id="__next"></div></body></html>'
    pages = [facts(path, shell) for path in ("/", "/pricing", "/blog/post-1")]
    findings = site_findings(view(pages), home=f"{BASE}/", pagespeed={})
    rendering = found(findings, "rendering.content_not_in_html")
    assert rendering["impact"] == "high" and rendering["affected_count"] == 3
    assert "onpage.h1_missing" not in ids(findings)
    assert "onpage.image_alt_missing" not in ids(findings)
    assert "aeo.dates_missing" not in ids(findings)


def test_bot_protection_and_ai_crawler_blocks_are_findings_not_unknowns():
    refused = {"url": f"{BASE}/pricing", "fetch": "refused", "status_code": 403}
    home = facts("/", html_page(title="Example product home", body=CONTENT))
    rows = [
        {"url": f"{BASE}/", "agent": "browser", "status": "observed", "status_code": 200},
        {"url": f"{BASE}/", "agent": "GPTBot", "status": "refused", "status_code": 403},
        {"url": f"{BASE}/", "agent": "OAI-SearchBot", "status": "refused", "status_code": 403},
        {"url": f"{BASE}/", "agent": "ClaudeBot", "status": "observed", "status_code": 200},
    ]
    findings = site_findings(
        view([home, refused], access={"status": "observed", "pages": [f"{BASE}/"], "rows": rows}),
        home=f"{BASE}/",
        pagespeed={},
    )
    readers = found(findings, "access.readers_refused")
    assert readers["urls"] == [f"{BASE}/pricing"] and readers["impact"] == "medium"
    crawlers = found(findings, "access.ai_crawlers_refused")
    assert crawlers["confidence"] == "likely" and crawlers["impact"] == "high"
    assert crawlers["evidence"][0].startswith("GPTBot: refused with HTTP 403")
    assert any("likely, not certain" in line for line in crawlers["evidence"])
    assert not any(line.startswith("ClaudeBot") for line in crawlers["evidence"])


def test_robots_that_close_the_wildcard_group_block_every_unnamed_crawler():
    robots = parse_robots("User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n")
    site = view(
        [{"url": f"{BASE}/a", "fetch": "blocked_by_robots"}],
        files={"robots": {"status": "observed", **robots}},
    )
    findings = site_findings(site, home=f"{BASE}/", pagespeed={})
    item = found(findings, "robots.wildcard_blocks_other_crawlers")
    assert item["priority"] == "critical"
    assert any("Googlebot" in line for line in item["evidence"])
    assert any("blocked on 1 page" in line for line in item["evidence"])


def test_soft_404s_redirect_loops_and_plain_http_are_reported():
    missing = facts("/gone", html_page(title="Page not found", body="<p>Sorry.</p>"))
    loop = {
        "url": f"{BASE}/a",
        "fetch": "redirect",
        "status_code": 301,
        "location": f"{BASE}/b",
        "redirect": {"chain": [f"{BASE}/a", f"{BASE}/b", f"{BASE}/a"], "loop": True},
    }
    files = {
        "missing_page": {
            "status": "observed",
            "url": f"{BASE}/tin-audit-check-missing-page",
            "status_code": 200,
        },
        "http_home": {
            "status": "observed",
            "url": f"http://{HOST}/",
            "status_code": 200,
            "to_https": False,
        },
    }
    findings = site_findings(view([missing, loop], files=files), home=f"{BASE}/", pagespeed={})
    soft = found(findings, "indexation.soft_404")
    assert "returned HTTP 200 instead of 404" in soft["evidence"][0]
    assert f"{BASE}/gone" in soft["urls"]
    assert found(findings, "redirects.loop")["evidence"][1] == "/a → /b → /a"
    assert found(findings, "https.http_not_redirected")["impact"] == "high"


def test_page_basics_structured_data_and_article_signals():
    long_title = "A very long title that keeps going well past what results can show in full"
    article = facts(
        "/blog/how-to-plan",
        html_page(
            title=long_title,
            json_ld='{"@type":"Product","name":"Planner"}',
            body='<img src="x.png">' + CONTENT,
        ),
    )
    home = facts("/", html_page(title="Example planning tool", body=CONTENT))
    files = {
        "llms_txt": {"status": "missing", "status_code": 404},
        "sitemaps": {
            "files": [{"status": "observed", "kind": "urlset"}],
            "urls": [{"loc": f"{BASE}/"}, {"loc": f"{BASE}/blog/how-to-plan"}],
        },
    }
    findings = site_findings(view([home, article], files=files), home=f"{BASE}/", pagespeed={})
    assert found(findings, "onpage.title_length")["urls"] == [article["url"]]
    assert found(findings, "onpage.viewport_missing")["affected_count"] == 2
    assert found(findings, "onpage.image_alt_missing")["urls"] == [article["url"]]
    assert found(findings, "onpage.open_graph_missing")["affected_count"] == 2
    schema = found(findings, "schema.invalid")
    assert "Product missing offers or review or aggregateRating" in schema["evidence"][1]
    assert found(findings, "aeo.llms_txt_missing")["impact"] == "low"
    assert found(findings, "aeo.dates_missing")["urls"] == [article["url"]]
    assert found(findings, "trust.author_missing")["urls"] == [article["url"]]
    assert found(findings, "trust.about_contact_missing")["area"] == "authority"


def test_recommended_schema_fields_alone_are_not_an_error():
    page = facts("/", html_page(json_ld='{"@type":"Organization","name":"Example"}'))
    assert "schema.invalid" not in ids(site_findings(view([page]), home=f"{BASE}/", pagespeed={}))


def test_analytics_on_some_pages_only_is_a_hypothesis():
    tagged = facts(
        "/",
        html_page(body='<script src="https://plausible.io/js/script.js"></script>' + CONTENT),
    )
    untagged = facts("/pricing", html_page(body="<script>void 0</script>" + CONTENT))
    item = found(
        site_findings(view([tagged, untagged]), home=f"{BASE}/", pagespeed={}),
        "measurement.analytics_inconsistent",
    )
    assert item["confidence"] == "hypothesis" and item["urls"] == [untagged["url"]]


def pagespeed_payload(*, origin_fallback: bool) -> dict:
    return {
        "loadingExperience": {
            "origin_fallback": origin_fallback,
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4200},
                "INTERACTION_TO_NEXT_PAINT": {"percentile": 150},
                "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 2},
            },
        },
        "lighthouseResult": {
            "categories": {
                "performance": {"score": 0.5},
                "seo": {"score": 0.75, "auditRefs": [{"id": "meta-description"}]},
                "accessibility": {"score": 0.95, "auditRefs": [{"id": "image-alt"}]},
                "best-practices": {"score": 1, "auditRefs": []},
            },
            "audits": {
                "meta-description": {
                    "title": "Document does not have a meta description",
                    "score": 0,
                    "scoreDisplayMode": "binary",
                },
                "image-alt": {"title": "Images lack alt", "score": 1, "scoreDisplayMode": "binary"},
            },
        },
    }


def test_site_wide_field_data_is_labelled_and_lighthouse_categories_are_reported():
    payload = pagespeed_payload(origin_fallback=True)
    summary = {**pagespeed_summary(payload), "lighthouse": lighthouse_summary(payload)}
    assert summary["lighthouse"]["scores"] == {
        "seo": 0.75,
        "accessibility": 0.95,
        "best-practices": 1.0,
    }
    assert summary["lighthouse"]["failed"]["seo"][0]["id"] == "meta-description"
    pagespeed = {"results": [{"url": f"{BASE}/", "result": {"status": "observed", **summary}}]}
    assert speed_rows(pagespeed)[0]["source_label"] == "site-wide field data"
    findings = site_findings(view([]), home=f"{BASE}/", pagespeed=pagespeed)
    speed = found(findings, "speed.core_web_vitals")
    assert any("describes the whole site" in line for line in speed["evidence"])
    lighthouse = found(findings, "lighthouse.failed_audits")
    assert lighthouse["impact"] == "medium"
    assert "SEO 75" in lighthouse["evidence"][1]
    assert "Document does not have a meta description" in lighthouse["evidence"][1]


def test_url_inspection_rows_and_findings():
    payload = {
        "inspectionResult": {
            "indexStatusResult": {
                "verdict": "NEUTRAL",
                "coverageState": "Crawled - currently not indexed",
                "robotsTxtState": "ALLOWED",
                "lastCrawlTime": "2026-09-20T10:00:00Z",
                "googleCanonical": f"{BASE}/pricing/",
                "userCanonical": f"{BASE}/plans",
            }
        }
    }
    row = inspection_row(f"{BASE}/pricing", payload)
    assert row["indexed"] is False and row["coverage_state"] == "Crawled - currently not indexed"
    with pytest.raises(ValueError):
        inspection_row(f"{BASE}/x", {"inspectionResult": {"indexStatusResult": "PASS"}})
    findings = site_findings(
        view([], url_inspection={"status": "observed", "results": [row]}),
        home=f"{BASE}/",
        pagespeed={},
    )
    assert found(findings, "indexation.not_indexed_by_google")["priority"] == "critical"
    assert "Google uses" in found(findings, "indexation.google_canonical_differs")["evidence"][0]
    urls = inspection_urls(
        home=f"{BASE}/",
        host=HOST,
        search_pages=[
            {"url": f"{BASE}/a", "impressions": 5},
            {"url": f"{BASE}/b", "impressions": 9},
        ],
        facts={
            "n": {"url": f"{BASE}/hidden", "fetch": "observed", "robots": ["noindex"]},
        },
        cap=10,
    )
    assert urls == [f"{BASE}/", f"{BASE}/b", f"{BASE}/a", f"{BASE}/hidden"]


def review_input():
    record = facts(
        "/guides/pick",
        html_page(title="How to pick a planning tool", body="<h2>Pricing</h2>" + CONTENT),
    )
    return review_pages(
        facts={"p": record},
        search_pages=[{"url": record["url"], "impressions": 40, "clicks": 1}],
        queries=[{"url": record["url"], "query": "pick a planning tool", "impressions": 30}],
        host=HOST,
        cap=5,
    )


def test_content_review_rejects_a_plausible_but_unusable_model_result():
    pages = review_input()
    assert pages[0]["queries"] == ["pick a planning tool"]
    assert pages[0]["measured_gaps"] == ["question_headings", "sources", "date", "author"]
    good = {"path": "/guides/pick", "gaps": ["specific_facts"], "answer_quote": ""}
    # Claims a direct answer but quotes a sentence the page does not contain.
    invented = {**good, "answer_quote": "Pick the cheapest planning tool on the market."}
    with pytest.raises(ValueError):
        validate_review(json.dumps({"pages": [invented]}), pages)
    # Names a page that was never supplied.
    with pytest.raises(ValueError):
        validate_review(
            json.dumps({"pages": [{**good, "path": "/other", "gaps": ["direct_answer"]}]}), pages
        )
    quote = "The short answer: pick the tool that fits your stack and budget today."
    rows = validate_review(json.dumps({"pages": [{**good, "answer_quote": quote}]}), pages)
    assert rows[0]["gaps"] == ["specific_facts", "question_headings", "sources", "date", "author"]
    findings = site_findings(
        view([], content_review={"status": "completed", "pages": rows}),
        home=f"{BASE}/",
        pagespeed={},
    )
    item = found(findings, "aeo.answer_structure")
    assert item["confidence"] == "hypothesis"
    assert '("pick a planning tool")' in item["evidence"][1]


def test_translations_do_not_compete_and_small_searches_are_ignored():
    policy = AUDIT_POLICY
    queries = [
        {
            "query": "planner",
            "url": f"{BASE}/pricing",
            "clicks": 1,
            "impressions": 30,
            "position": 5,
        },
        {
            "query": "planner",
            "url": f"{BASE}/de/pricing",
            "clicks": 0,
            "impressions": 20,
            "position": 9,
        },
        {"query": "rare", "url": f"{BASE}/a", "clicks": 0, "impressions": 3, "position": 8},
        {"query": "rare", "url": f"{BASE}/b", "clicks": 0, "impressions": 2, "position": 9},
    ]
    findings = search_findings(
        host=HOST, pages=[], queries=queries, window="w", titles={}, policy=policy, brand=[]
    )
    assert "search.cannibalization" not in ids(findings)
    queries[1] = {**queries[1], "url": f"{BASE}/plans"}
    findings = search_findings(
        host=HOST, pages=[], queries=queries, window="w", titles={}, policy=policy, brand=[]
    )
    assert found(findings, "search.cannibalization")["affected_count"] == 2


def test_pages_losing_clicks_against_the_previous_window():
    pages = [{"url": f"{BASE}/guide", "clicks": 4, "impressions": 300, "position": 9.5}]
    previous = {
        "start_date": "2026-08-02",
        "end_date": "2026-08-29",
        "pages": [
            {"url": f"{BASE}/guide", "clicks": 40, "impressions": 900, "position": 4.2},
            {"url": f"{BASE}/steady", "clicks": 20, "impressions": 200, "position": 3.0},
        ],
    }
    pages.append({"url": f"{BASE}/steady", "clicks": 18, "impressions": 210, "position": 3.1})
    findings = search_findings(
        host=HOST,
        pages=pages,
        queries=[],
        window="w",
        titles={},
        policy=AUDIT_POLICY,
        brand=[],
        previous=previous,
    )
    decay = found(findings, "search.decay")
    assert decay["urls"] == [f"{BASE}/guide"]
    assert "/guide: 40 → 4 clicks" in decay["evidence"][1]


@pytest.mark.asyncio
async def test_site_file_probes_crawler_comparison_and_redirect_paths():
    site = SyntheticSite()
    site.text(f"{BASE}/robots.txt", "User-agent: *\nAllow: /\n")
    site.text(f"{BASE}/sitemap.xml", sitemap([f"{BASE}/"]), content_type="application/xml")
    site.page(f"{BASE}/", html_page())
    site.page(f"{BASE}/tin-audit-check-missing-page", html_page(title="Home"))
    site.routes[f"http://{HOST}/"] = (200, {"content-type": "text/html"}, b"<html></html>")
    handle = site._handle
    async with site.reader((HOST,)) as reader:
        files = await read_site_files(reader, f"{BASE}/", AUDIT_POLICY)
    assert files["llms_txt"]["status"] == "missing"
    assert files["http_home"] == {
        "url": f"http://{HOST}/",
        "status": "observed",
        "status_code": 200,
        "location": None,
        "to_https": False,
    }
    assert files["missing_page"]["status_code"] == 200

    def handler(request):
        agent = request.headers.get("user-agent", "")
        if "GPTBot" in agent:
            return httpx.Response(403, content=b"blocked")
        return handle(request)

    site._handle = handler
    async with site.reader((HOST,)) as reader:
        access = await read_crawler_access(reader, [f"{BASE}/"], seconds=10)
    by_agent = {row["agent"]: row for row in access["rows"]}
    assert by_agent["browser"]["status_code"] == 200
    assert by_agent["GPTBot"] == {
        "url": f"{BASE}/",
        "agent": "GPTBot",
        "status": "refused",
        "status_code": 403,
    }
    assert (f"{BASE}/", BROWSER_AGENT) in site.agent_requests
    site._handle = handle
    site.redirect(f"{BASE}/a", f"{BASE}/b")
    site.redirect(f"{BASE}/b", f"{BASE}/a")
    async with site.reader((HOST,)) as reader:
        pages = await read_pages(
            reader, [f"{BASE}/a"], robots=None, policy=AUDIT_POLICY, seconds=10
        )
        assert pages[f"{BASE}/a"]["redirect"]["loop"] is True
        chain = await follow_redirects(reader, f"{BASE}/x", {"location": f"{BASE}/"}, hops=5)
    assert chain == {"chain": [f"{BASE}/x", f"{BASE}/"], "loop": False, "final_status": 200}


def ladder_panel():
    return {**frozen_panel(), "unsearched": True}


def test_the_ladder_grades_evaluation_and_finds_retrieval_without_a_mention():
    panel = ladder_panel()
    answer = {"text": "Acme is strong on scheduling but costs more.", "citations": []}
    grade = {
        **negative_judgment(),
        "mentioned": True,
        "mention_quote": answer["text"],
        "evaluated": True,
        "evaluation_quote": answer["text"],
    }
    result = classify(answer, {"text": json.dumps(grade)}, panel, ladder=True)
    assert result["found"] and result["evaluated"] and not result["shortlisted"]
    # A plausible grade whose evaluation quote does not name the target is not used.
    bad = {**grade, "evaluation_quote": "It costs more."}
    with pytest.raises(AuditValidationError, match="evaluation"):
        classify(answer, {"text": json.dumps(bad)}, panel, ladder=True)
    # A recommendation counts as an evaluation.
    shortlist = {
        **grade,
        "evaluated": False,
        "evaluation_quote": "",
        "shortlisted": True,
        "shortlist_quote": answer["text"],
    }
    result = classify(answer, {"text": json.dumps(shortlist)}, panel, ladder=True)
    assert result["evaluated"] and result["evaluation_quote"] == answer["text"]
    retrieved = classify_absent_target(
        {"text": "Try a planner.", "citations": [], "sources": ["https://example.com/pricing"]},
        panel,
        ladder=True,
    )
    assert retrieved["found"] and not retrieved["mentioned"]


def graded(index, *, question, found=True, mentioned=False, cited=(), memory=False):
    value = read_response(response(), search=True)
    value["citations"] = list(cited)
    row = {
        "status": "completed",
        "index": index,
        "question_index": question,
        "answer": {"status": "completed", "value": value},
        "classification": {
            **negative_judgment(),
            "found": found,
            "mentioned": mentioned,
            "evaluated": mentioned,
            "evaluation_quote": "",
            "owned_domain_cited": False,
        },
    }
    return {**row, "mode": "memory"} if memory else row


def test_summary_separates_unsearched_answers_and_names_the_sites_answers_cite():
    panel = {**ladder_panel(), "repetitions": 1}
    panel["planned_observations"] = len(panel["questions"]) * 2
    searched = [
        graded(index, question=index, cited=["https://www.g2.com/x", "https://reddit.com/r"])
        for index in range(len(panel["questions"]))
    ]
    memory = [
        graded(len(searched) + index, question=index, mentioned=index == 0, memory=True)
        for index in range(len(panel["questions"]))
    ]
    ai = summarize(panel, [*searched, *memory], policy_version=AUDIT_POLICY["version"])
    assert ai["status"] == "completed" and ai["planned"] == len(searched)
    assert ai["ladder"]["counts"]["found"] == len(searched)
    assert ai["ladder"]["bottleneck"]["label"] == "Answer inclusion"
    assert ai["memory"]["mentioned"] == 1
    assert ai["cited_domains"][0] == {"domain": "g2.com", "answers": len(searched)}
    lines = ladder_report_lines(ai)
    assert "### Recommendation ladder" in lines and "### Sites AI answers cite" in lines
    from tin_lite.organic_audit import content_review_findings

    cited = [f for f in content_review_findings(ai, HOST) if f["check_id"] == "ai.cited_instead"]
    assert cited and cited[0]["area"] == "authority"


@pytest.mark.asyncio
async def test_answer_pages_take_questions_from_the_newest_audit_of_either_kind():
    from tin_lite.activities import TinActivities
    from tin_lite.domain import Project

    project = Project(uuid4(), "Tin", "projects/test", "main")
    runs = [
        SimpleNamespace(
            executor=executor,
            canonical_commit_sha=sha * 40,
            artifact_path=path,
            artifact_ref=f"code.storage://projects/test@{sha * 40}/{path}",
        )
        for executor, sha, path in (
            ("visibility.audit", "a", "reports/AI_VISIBILITY.md"),
            ("organic.audit", "b", "reports/organic/AUDIT.md"),
        )
    ]
    activities = object.__new__(TinActivities)
    activities._db = SimpleNamespace(list_memory_source_runs=AsyncMock(return_value=runs))
    activities._storage = SimpleNamespace(read_canonical_artifact=AsyncMock(return_value=b"# Q"))
    activities._integrations = None

    async def evidence(project):
        return "none"

    activities._integration_evidence = evidence
    sources = await activities._answer_page_sources(run_id=uuid4(), project=project)
    assert [source.label for source in sources] == [
        "current integration availability",
        "latest organic audit, with its AI buyer questions",
    ]
