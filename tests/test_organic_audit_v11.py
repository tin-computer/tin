"""Audit policy v10 is deployed, so v11's additions never reach a run pinned to it.

v11 also keeps a buyer-question panel when the reviewer rejects a few of its questions,
instead of discarding every question over one ambiguous one.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from organic_site_stub import SyntheticSite, html_page, sitemap
from test_organic_audit import activities_fixture, panel_fixture
from test_organic_audit_angles import BASE, HOST, facts, ids, view
from test_organic_audit_panel import draft, interpretations, research, review

from tin_lite.organic_audit import (
    AUDIT_POLICY,
    V10_AUDIT_POLICY,
    ai_report_details,
    audit_policy,
    digest,
)
from tin_lite.organic_audit_ai import (
    PanelReview,
    RejectedQuestion,
    ai_contract,
    ai_schemas,
    apply_review,
)
from tin_lite.organic_audit_checks import SiteView, site_check_coverage, site_findings
from tin_lite.organic_audit_fetch import read_pages, read_site_files
from tin_lite.organic_audit_site import V11_PAGE_FACTS

V10 = "organic-audit-v10"
# What release 1e34c86 pinned for v10: the policy, the AI instructions and the AI schemas.
V10_POLICY_DIGEST = "e7b25cd5e9896b5cb8cf8a4c31759a1edfe2a19e1beb011386805c3e15345000"
V10_CONTRACT_DIGEST = "2bb1ad3f07c38bc5fa2a06c11f7c04a7a735f6ef2eee205ca0780c1767d55c73"
V10_SCHEMAS_DIGEST = "0ce54739bee37b30e1ea3b2ba55939dc6ebe7b3c9ecadcaf53dc5deca03ca859"

# A page that trips several v11 checks: no viewport, no description, an image without alt.
BASICS = (
    b"<!doctype html><html lang=en><head><title>Plan work</title></head><body>"
    b"<h1>Plan work</h1><img src=/a.png><p>" + b"Plain words about planning. " * 60 + b"</p>"
    b"</body></html>"
)


def test_v10_is_exactly_the_deployed_policy_and_contract():
    assert audit_policy(V10) is V10_AUDIT_POLICY
    assert digest(V10_AUDIT_POLICY) == V10_POLICY_DIGEST
    assert digest(ai_contract(V10)) == V10_CONTRACT_DIGEST
    assert digest(ai_schemas(V10)) == V10_SCHEMAS_DIGEST
    assert AUDIT_POLICY["version"] == "organic-audit-v11"
    # v11 is v10 plus its own keys; it changes none of v10's values.
    assert {k: v for k, v in AUDIT_POLICY.items() if k in V10_AUDIT_POLICY and k != "version"} == {
        k: v for k, v in V10_AUDIT_POLICY.items() if k != "version"
    }
    assert {
        "site_angles",
        "min_panel_questions",
        "answer_ladder",
        "unsearched_answers",
        "url_inspection_max_urls",
        "access_check_pages",
        "content_review_pages",
        "decay_min_previous_clicks",
        "max_redirect_hops",
        "cannibalization_min_impressions",
    } <= set(AUDIT_POLICY) - set(V10_AUDIT_POLICY)
    assert "PanelReview" in ai_schemas(AUDIT_POLICY["version"])
    assert "PanelReview" not in ai_schemas(V10)


def v10_view(pages):
    current = view(pages)
    pinned = SiteView(
        host=HOST,
        hosts=(HOST,),
        site={"files": current.files, "pages": pages},
        crawl_pages=[],
        search_pages=[],
        search_queries=[],
        angles=False,
    )
    return current, pinned


def test_a_v10_run_keeps_v10_findings_and_coverage():
    pages = [facts("/", BASICS)]
    current, pinned = v10_view(pages)
    home = f"{BASE}/"
    v11 = ids(site_findings(current, home=home, pagespeed={}))
    v10 = ids(site_findings(pinned, home=home, pagespeed={}))
    assert {"onpage.viewport_missing", "onpage.image_alt_missing"} <= v11
    assert not v10 & {
        "onpage.viewport_missing",
        "onpage.image_alt_missing",
        "onpage.description_length",
        "aeo.llms_txt_missing",
    }
    checks = {row["check"] for row in site_check_coverage(pinned, pagespeed={}, search={})}
    assert not checks & {
        "javascript_rendering",
        "crawler_access",
        "url_inspection",
        "content_review",
    }


def test_a_v10_run_keeps_noindex_with_traffic_critical_and_skips_accessibility():
    from test_organic_audit_findings import checks, files, row
    from test_organic_audit_findings import facts as page_facts
    from test_organic_audit_findings import view as findings_view

    pages = [
        page_facts("/guide", robots=["noindex"]),
        page_facts("/signup", unnamed_controls=2, unlabeled_fields=1),
    ]
    search = [row("/guide", 3, 90, 6)]
    current = checks(findings_view(pages=pages, search=search))
    pinned = checks(
        SiteView(
            host=HOST,
            hosts=(HOST,),
            site={"files": files(), "pages": pages},
            crawl_pages=[],
            search_pages=search,
            search_queries=[],
            angles=False,
        )
    )
    # v11 asks whether the noindex is deliberate; v10 still calls it critical.
    assert current["indexation.noindex_with_search_traffic"]["priority"] == "quick_win"
    hidden = pinned["indexation.noindex_with_search_traffic"]
    assert (hidden["priority"], hidden["status"]) == ("critical", "fail")
    assert "onpage.accessible_name_missing" in current
    assert not {"onpage.accessible_name_missing", "onpage.form_label_missing"} & set(pinned)
    assert {"unnamed_controls", "unlabeled_fields"} <= V11_PAGE_FACTS


@pytest.mark.asyncio
async def test_a_v10_run_reads_no_new_site_files_and_saves_v10_page_facts():
    site = SyntheticSite()
    site.text(f"{BASE}/robots.txt", "User-agent: *\nAllow: /\n")
    site.text(f"{BASE}/sitemap.xml", sitemap([f"{BASE}/"]), content_type="application/xml")
    site.page(f"{BASE}/", html_page())
    async with site.reader((HOST,)) as reader:
        files = await read_site_files(reader, f"{BASE}/", V10_AUDIT_POLICY)
        pages = await read_pages(
            reader, [f"{BASE}/"], robots=None, policy=V10_AUDIT_POLICY, seconds=10
        )
    assert not {"llms_txt", "http_home", "missing_page"} & set(files)
    assert not any("llms.txt" in url or "missing-page" in url for url in site.requests)
    assert not any(url.startswith("http://") for url in site.requests)
    assert pages[f"{BASE}/"]["fetch"] == "observed"
    assert not V11_PAGE_FACTS & set(pages[f"{BASE}/"])


def candidate(count=4):
    panel = panel_fixture()
    questions = panel["questions"][:count]
    value = {**panel, "questions": questions, "repetitions": 3, "unsearched": True}
    return {**value, "sha256": digest(value), "planned_observations": count * 4}


def verdict(*numbers, accepted=True):
    return PanelReview(
        accepted=accepted,
        rejected_questions=[
            RejectedQuestion(number=n, reason="It could mean reviewing code before release.")
            for n in numbers
        ],
        explanation="Question 4 is ambiguous about what is being reviewed.",
    )


def test_one_rejected_question_leaves_a_panel_of_three():
    panel, reason = apply_review(candidate(), verdict(4), min_questions=3)
    assert reason is None and len(panel["questions"]) == 3
    assert panel["planned_observations"] == 12  # three searched answers and one without
    assert panel["dropped_questions"] == [
        {
            "question": candidate()["questions"][3]["question"],
            "reason": "It could mean reviewing code before release.",
        }
    ]
    assert panel["sha256"] != candidate()["sha256"]
    report = "\n".join(ai_report_details({"panel": panel, "observations": []}))
    assert "### Questions dropped in review" in report


@pytest.mark.parametrize(
    ("review_value", "reason"),
    [
        (verdict(3, 4), "panel_questions_too_few"),
        (verdict(accepted=False), "panel_review_rejected"),
        # Plausible but unusable: a question the panel does not have, or one named twice.
        (verdict(9), "panel_review_invalid"),
        (verdict(2, 2), "panel_review_invalid"),
    ],
)
def test_a_review_that_leaves_too_little_or_names_nonsense_redrafts(review_value, reason):
    assert apply_review(candidate(), review_value, min_questions=3) == (None, reason)


def test_a_review_that_rejects_nothing_keeps_the_panel_unchanged():
    assert apply_review(candidate(), verdict(), min_questions=3) == (candidate(), None)


@pytest.mark.asyncio
async def test_v11_preparation_asks_the_three_questions_that_passed():
    activities, db, _, _ = await activities_fixture()
    activities.responses = SimpleNamespace(
        create=AsyncMock(
            side_effect=[research(), draft(), *interpretations(), review(rejected=(4,))]
        )
    )
    run_id = str(db.run.id)
    # Four drafted, one rejected: three questions, three searched answers and one without each.
    assert await activities.organic_prepare_panel(run_id) == 12
    panel = await activities._result(run_id, "panel")
    assert len(panel["questions"]) == 3 and len(panel["dropped_questions"]) == 1
    requests = [call.args[0] for call in activities.responses.create.await_args_list]
    assert requests[-1]["text"]["format"]["name"] == "PanelReview"
    assert "rejected_questions" in json.dumps(requests[-1]["text"]["format"]["schema"])


@pytest.mark.asyncio
async def test_v11_redrafts_when_the_review_rejects_two_of_four():
    activities, db, _, _ = await activities_fixture()
    activities.responses = SimpleNamespace(
        create=AsyncMock(
            side_effect=[
                research(),
                draft(),
                *interpretations(),
                review(rejected=(3, 4)),
                draft(),
                *interpretations(),
                review(),
            ]
        )
    )
    run_id = str(db.run.id)
    assert await activities.organic_prepare_panel(run_id) == 16
    preparation = await activities._result(run_id, "panel_preparation")
    assert [row["reason"] for row in preparation["attempts"][1:]] == [
        "panel_questions_too_few",
        None,
    ]
    requests = [call.args[0] for call in activities.responses.create.await_args_list]
    correction = json.loads(requests[7]["input"])["correction"]
    assert correction["reason"] == "panel_questions_too_few"
    assert "rejected_questions" in correction["review"]


def _site_with_site_wide_speed() -> dict:
    from test_organic_audit_angles import pagespeed_payload
    from test_organic_audit_findings import facts as page_facts
    from test_organic_audit_findings import files

    from tin_lite.organic_audit_fetch import lighthouse_summary, pagespeed_summary

    payload = pagespeed_payload(origin_fallback=True)
    result = {
        "status": "observed",
        **pagespeed_summary(payload),
        "lighthouse": lighthouse_summary(payload),
    }
    return {
        "files": files(urls=["/"]),
        "plan": None,
        "pages": [page_facts("/", h1_count=1)],
        "pages_status": "complete",
        "pagespeed": {"status": "completed", "results": [{"url": f"{BASE}/", "result": result}]},
    }


def test_a_v10_report_keeps_the_wording_it_was_released_with():
    from test_organic_audit_findings import v10_documents

    from tin_lite.organic_audit import V10_AUDIT_POLICY

    _, v10, _, _ = v10_documents(policy=V10_AUDIT_POLICY, site=_site_with_site_wide_speed())
    _, v11, _, _ = v10_documents(policy=AUDIT_POLICY, site=_site_with_site_wide_speed())
    released = (
        "Not measured in this version: backlinks and brand mentions on other sites are out of "
        "scope."
    )
    assert released in v10 and released not in v11
    assert "The sites AI answers cite" not in v10 and "The sites AI answers cite" in v11
    # v10 called page and site-wide field data both "field"; v11 tells them apart.
    assert "| field |" in v10 and "site-wide field data" not in v10
    assert "site-wide field data" in v11


def test_a_malformed_link_is_one_link_not_the_rest_of_the_page():
    from tin_lite.organic_audit_site import html_facts

    body = (
        b'<html lang="en"><body><a href="https://[YOUR-DOMAIN]/pricing">Pricing</a>'
        b"<h1>Plans for small teams</h1><p>One plan for every team that ships weekly.</p>"
        b'<a href="https://other.example/">Partner</a></body></html>'
    )
    facts = html_facts(body, url=f"{BASE}/", charset="utf-8", truncated=False)
    assert facts["h1_count"] == 1
    assert facts["external_links"] == 1


def test_unclosed_tags_parse_in_linear_time():
    import time as clock

    from tin_lite.organic_audit_site import html_facts

    # Unclosed paragraphs and stray end tags: scanning a stack of every open tag on each end
    # tag made this quadratic, seconds per page inside the worker's event loop.
    body = (
        "<html><body><div>" + "<p>word " * 40_000 + "</b>" * 40_000 + "</div></body></html>"
    ).encode()
    started = clock.monotonic()
    facts = html_facts(body, url=f"{BASE}/", charset="utf-8", truncated=False)
    assert clock.monotonic() - started < 2
    assert facts["h1_count"] == 0
