"""Audit policy v16 adds four site checks: the other address (www or bare), missing structured
data, complete link previews whose image loads, and links to a privacy policy and terms.

v15 is on main and may deploy at any time, so its policy, instructions and schemas stay
exactly as main shipped them. Every check runs on synthetic sites; nothing touches the network.
"""

from __future__ import annotations

import json

import organic_tin_fixture as tin
import pytest
from organic_site_stub import SyntheticSite, html_page
from test_organic_audit_acceptance import run_audit
from test_organic_audit_angles import BASE, HOST, facts, found, ids

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_audit import (
    AUDIT_POLICY,
    SITE_EVIDENCE_POLICY_KEYS,
    V15_AUDIT_POLICY,
    audit_policy,
    digest,
)
from tin_lite.organic_audit_ai import ai_contract, ai_schemas
from tin_lite.organic_audit_checks import SiteView, site_check_coverage, site_findings
from tin_lite.organic_audit_completion import NEUTRAL_KEYS
from tin_lite.organic_audit_fetch import read_pages, read_share_images, read_site_files
from tin_lite.organic_audit_site import V16_PAGE_FACTS, parse_robots, profile_link
from tin_lite.technical_repair_plan import classify, next_action

V15 = "organic-audit-v15"
V16 = "organic-audit-v16"
# What main shipped for v15 at affaa38, before v16 existed.
V15_POLICY_DIGEST = "bc488c6d069810ffa73862c194c78476e0734734a382e25581cee80619c48dc7"
V15_CONTRACT_DIGEST = "5cb54dc5c922fc65c396978744a3ddf0242828381e19957240a56b6d349e6148"
V15_SCHEMAS_DIGEST = "9697781502162fb2f38dfa57224b8dd1b521a1a7b93633c68aa5b68fdaa5a878"
NEW_CHECKS = {
    "redirects.host_variant_served",
    "schema.missing",
    "schema.entity_profiles_missing",
    "redirects.host_variant_chain",
    "onpage.link_preview_incomplete",
    "onpage.link_preview_duplicate",
    "onpage.share_image_broken",
    "trust.legal_pages_unlinked",
    "trust.legal_pages_missing",
}
ORGANIZATION = json.dumps(
    {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "X",
        "sameAs": ["https://www.linkedin.com/company/x"],
    }
)
FOOTER = '<footer><a href="/privacy">Privacy</a> <a href="/legal/terms">Terms</a></footer>'
PREVIEW = (
    '<meta property="og:title" content="Example"><meta property="og:image" '
    'content="https://cdn.example.net/og.png">'
)


def page(path: str, body: str = "", *, json_ld: str | None = None, head: str = "") -> dict:
    html = html_page(json_ld=json_ld, body="<p>" + "Plain words. " * 60 + "</p>" + body)
    return facts(path, html.replace(b"</head>", head.encode() + b"</head>"))


def view(pages, *, files=None, basics=True, sitemap=(), **site) -> SiteView:
    return SiteView(
        host=HOST,
        hosts=(HOST,),
        site={
            "files": {
                "status": "observed",
                "robots": {"status": "observed", **parse_robots("User-agent: *\nAllow: /\n")},
                "sitemaps": {
                    "files": [{"url": f"{BASE}/sitemap.xml", "status": "observed"}],
                    "urls": [{"loc": f"{BASE}{path}"} for path in sitemap],
                },
                **(files or {}),
            },
            "pages": pages,
            **site,
        },
        crawl_pages=[],
        search_pages=[],
        search_queries=[],
        basics=basics,
    )


def findings(site_view: SiteView) -> list[dict]:
    return site_findings(site_view, home=f"{BASE}/", pagespeed={"status": "not_configured"})


def complete_home() -> dict:
    return page("/", FOOTER, json_ld=ORGANIZATION)


def test_v15_is_exactly_what_main_shipped():
    v15 = audit_policy(V15)
    assert v15 is V15_AUDIT_POLICY
    assert digest(v15) == V15_POLICY_DIGEST
    assert digest(ai_contract(V15)) == V15_CONTRACT_DIGEST
    assert digest(ai_schemas(V15)) == V15_SCHEMAS_DIGEST


def test_v16_is_the_default_and_only_adds_site_evidence_settings():
    assert AUDIT_POLICY["version"] == V16 and audit_policy() is AUDIT_POLICY
    assert {k: v for k, v in AUDIT_POLICY.items() if k != "version"} == {
        **{k: v for k, v in V15_AUDIT_POLICY.items() if k != "version"},
        "site_basics": True,
        "max_share_images": 3,
    }
    # Site evidence never changes how an answer is asked or graded, so a completion may cross.
    assert {"site_basics", "max_share_images"} <= SITE_EVIDENCE_POLICY_KEYS
    assert {k: v for k, v in AUDIT_POLICY.items() if k not in NEUTRAL_KEYS} == {
        k: v for k, v in V15_AUDIT_POLICY.items() if k not in NEUTRAL_KEYS
    }
    assert ai_contract(V16) == ai_contract(V15) and ai_schemas(V16) == ai_schemas(V15)
    workflow = next(w for w in BUILTIN_WORKFLOWS if w.key == "organic.audit")
    assert workflow.version_label == "0.12.0"
    assert workflow.definition["audit_policy"] == AUDIT_POLICY


def test_page_facts_name_the_preview_image_x_card_and_legal_links():
    head = PREVIEW + '<meta name="twitter:card" content="summary_large_image">'
    home = page(
        "/", '<a href="/datenschutz">Datenschutz</a><a href="/x">Terms of use</a>', head=head
    )
    assert home["og_image"] == "https://cdn.example.net/og.png" and home["og_title"] == "Example"
    assert home["twitter_card"] == "summary_large_image"
    assert home["legal_links"] == {"privacy": f"{BASE}/datenschutz", "terms": f"{BASE}/x"}
    # One /legal page usually holds both; a blog post about terms is not a terms page link.
    other = page("/about", '<a href="https://legal.example.net/legal">Legal</a>')
    assert other["legal_links"] == {"privacy": None, "terms": "https://legal.example.net/legal"}
    assert page("/b")["legal_links"] == {"privacy": None, "terms": None}
    assert page("/b")["og_image"] is None and page("/b")["twitter_card"] is None


@pytest.mark.asyncio
async def test_v15_runs_save_the_page_facts_they_saved_before():
    site = SyntheticSite()
    site.page(f"{BASE}/", html_page(body=FOOTER))
    async with site.reader((HOST,)) as reader:
        v15 = await read_pages(
            reader, [f"{BASE}/"], robots=None, policy=V15_AUDIT_POLICY, seconds=9
        )
        v16 = await read_pages(reader, [f"{BASE}/"], robots=None, policy=AUDIT_POLICY, seconds=9)
    assert not V16_PAGE_FACTS & set(v15[f"{BASE}/"])
    assert V16_PAGE_FACTS <= set(v16[f"{BASE}/"])
    assert {k: v for k, v in v16[f"{BASE}/"].items() if k not in V16_PAGE_FACTS} == v15[f"{BASE}/"]


@pytest.mark.asyncio
async def test_the_other_address_is_read_once_and_only_its_homepage():
    site = SyntheticSite()
    site.text(f"{BASE}/robots.txt", "User-agent: *\nAllow: /\n")
    site.page(f"https://www.{HOST}/", html_page(canonical=f"{BASE}/"))
    async with site.reader((HOST,)) as reader:
        files = await read_site_files(reader, f"{BASE}/", AUDIT_POLICY)
        assert not reader.in_scope(f"https://www.{HOST}/pricing", peer_root=True)
        assert not reader.in_scope(f"https://other.{HOST}/", peer_root=True)
        assert not reader.in_scope(f"http://www.{HOST}/pricing", peer_root=True)
        assert not reader.in_scope(f"http://www.{HOST}:8080/", peer_root=True)
    assert files["host_variant"] == {
        "url": f"https://www.{HOST}/",
        "status": "observed",
        "status_code": 200,
        "canonical": f"{BASE}/",
        "http_chain": {
            "status": "observed",
            "hops": [f"http://www.{HOST}/"],
            "final_status": 404,
        },
    }
    site.redirect(f"https://www.{HOST}/", f"{BASE}/")
    async with site.reader((HOST,)) as reader:
        files = await read_site_files(reader, f"{BASE}/", AUDIT_POLICY)
        old = await read_site_files(reader, f"{BASE}/", V15_AUDIT_POLICY)
    assert files["host_variant"]["to_site"] is True
    assert "host_variant" not in old
    # The run's address check already saw www redirect here: nothing to read.
    async with site.reader((f"www.{HOST}", HOST)) as reader:
        files = await read_site_files(reader, f"https://www.{HOST}/", AUDIT_POLICY)
    assert files["host_variant"]["status"] == "redirect_verified"


@pytest.mark.asyncio
async def test_plain_http_on_the_other_address_reaching_the_site_in_two_hops():
    # A www site: http://bare → https://bare → https://www, which a crawl of www never sees.
    www = f"https://www.{HOST}/"
    site = SyntheticSite()
    site.page(www, html_page())
    site.redirect(f"https://{HOST}/", www, 308)
    site.redirect(f"http://{HOST}/", f"https://{HOST}/")
    site.redirect(f"http://www.{HOST}/", www)
    async with site.reader((f"www.{HOST}",)) as reader:
        files = await read_site_files(reader, www, AUDIT_POLICY)
    variant = files["host_variant"]
    assert variant["to_site"] is True
    assert variant["http_chain"] == {
        "status": "observed",
        "hops": [f"http://{HOST}/", f"https://{HOST}/", www],
        "final_status": 200,
    }
    site_view = SiteView(
        host=f"www.{HOST}",
        hosts=(f"www.{HOST}",),
        site={"files": {"status": "observed", "host_variant": variant}, "pages": []},
        crawl_pages=[],
        search_pages=[],
        search_queries=[],
        basics=True,
    )
    chain = found(findings(site_view), "redirects.host_variant_chain")
    assert chain["issue"] == f"http://{HOST}/ reaches the site in 2 redirects"
    assert chain["evidence"][0] == f"http://{HOST}/ → https://{HOST}/ → {www}."
    assert "redirects.host_variant_served" not in ids(findings(site_view))
    # One hop is fine.
    site.redirect(f"http://{HOST}/", www)
    async with site.reader((f"www.{HOST}",)) as reader:
        files = await read_site_files(reader, www, AUDIT_POLICY)
    assert len(files["host_variant"]["http_chain"]["hops"]) == 2


def test_a_second_copy_at_the_other_address_is_a_finding():
    def variant(**row):
        return {"host_variant": {"url": f"https://www.{HOST}/", "status": "observed", **row}}

    served = found(
        findings(view([complete_home()], files=variant(status_code=200))),
        "redirects.host_variant_served",
    )
    assert served["issue"] == f"Both {HOST} and www.{HOST} serve the site"
    assert served["impact"] == "medium" and served["urls"] == [f"{BASE}/"]
    # The published audit maps a fix to website_change from the repair plan.
    assert next_action("redirects.host_variant_served") == "technical_fix"
    canonical = variant(status_code=200, canonical=f"{BASE}/")
    assert (
        found(findings(view([complete_home()], files=canonical)), "redirects.host_variant_served")[
            "impact"
        ]
        == "low"
    )
    error = found(
        findings(view([complete_home()], files=variant(status_code=404))),
        "redirects.host_variant_served",
    )
    assert "answers HTTP 404" in error["evidence"][0]
    away = variant(status_code=301, location="https://elsewhere.example/", to_site=False)
    assert "redirects.host_variant_served" in ids(findings(view([complete_home()], files=away)))
    for quiet in (
        variant(status_code=301, location=f"{BASE}/", to_site=True),
        {"host_variant": {"url": f"https://www.{HOST}/", "status": "unreachable"}},
        {"host_variant": {"url": f"https://www.{HOST}/", "status": "redirect_verified"}},
        {"host_variant": {"url": f"https://www.{HOST}/", "status": "refused"}},
    ):
        assert "redirects.host_variant_served" not in ids(
            findings(view([complete_home()], files=quiet))
        )


def test_key_pages_without_the_structured_data_that_states_their_facts():
    pages = [
        page("/", FOOTER),
        page("/pricing"),
        page("/faq"),
        page("/blog/launch"),
        page("/docs/setup"),
        page("/blog/typed", json_ld=json.dumps({"@type": "BlogPosting", "headline": "Typed"})),
    ]
    item = found(findings(view(pages)), "schema.missing")
    assert item["confidence"] == "likely" and item["impact"] == "low"
    assert item["evidence"][:4] == [
        "No Organization data on the homepage (name, logo, social profiles): /",
        "No Product, SoftwareApplication or Service data with offers: /pricing",
        "Visible questions and answers but no FAQPage data: /faq",
        "Articles without Article or BlogPosting data: /blog/launch",
    ]
    assert f"{BASE}/docs/setup" not in item["urls"] and f"{BASE}/blog/typed" not in item["urls"]
    # Microdata, a JavaScript app shell or a v15 run: no verdict.
    microdata = page("/", '<div itemscope itemtype="https://schema.org/Organization"></div>')
    shell = facts(
        "/", b'<html><body><div id="root"></div><script src="/a.js"></script></body></html>'
    )
    for quiet in (view([microdata]), view([shell]), view(pages, basics=False)):
        assert "schema.missing" not in ids(findings(quiet))


def test_previews_need_a_description_an_x_card_and_an_image_that_loads():
    head = PREVIEW
    pages = [page("/", FOOTER, json_ld=ORGANIZATION, head=head), page("/about", head=head)]
    item = found(findings(view(pages)), "onpage.link_preview_incomplete")
    assert item["urls"] == [f"{BASE}/", f"{BASE}/about"]
    assert any("no og:description" in line for line in item["evidence"])
    assert any("no twitter:card" in line for line in item["evidence"])
    images = {
        "status": "observed",
        "images": [
            {
                "url": "https://cdn.example.net/og.png",
                "status": "observed",
                "status_code": 404,
                "content_type": "text/html",
            }
        ],
    }
    broken = found(findings(view(pages, share_images=images)), "onpage.share_image_broken")
    assert broken["evidence"][0] == "https://cdn.example.net/og.png: HTTP 404."
    assert broken["urls"] == [f"{BASE}/", f"{BASE}/about"]
    images["images"][0].update(status_code=200, content_type="text/html")
    assert (
        "not an image"
        in found(findings(view(pages, share_images=images)), "onpage.share_image_broken")[
            "evidence"
        ][0]
    )
    images["images"][0].update(content_type="image/png")
    assert "onpage.share_image_broken" not in ids(findings(view(pages, share_images=images)))
    images["images"][0] = {"url": "https://cdn.example.net/og.png", "status": "refused"}
    assert "onpage.share_image_broken" not in ids(findings(view(pages, share_images=images)))
    assert "onpage.link_preview_duplicate" not in ids(findings(view(pages)))


def test_a_page_with_the_homepages_preview_is_a_finding():
    card = (
        PREVIEW + '<meta property="og:description" content="d"><meta name="twitter:card" '
        'content="summary">'
    )

    def titled(path, title, body=""):
        return facts(
            path,
            html_page(title=title, body=body).replace(b"</head>", card.encode() + b"</head>"),
        )

    pages = [
        titled("/", "Home", FOOTER),
        titled("/platform", "Platform"),
        titled("/privacy-policy", "Privacy"),
        titled("/cookie-policy", "Cookies"),
        facts("/own", html_page(title="Own")),
        facts(
            "/moved",
            html_page(title="Moved", canonical=f"{BASE}/").replace(
                b"</head>", card.encode() + b"</head>"
            ),
        ),
        facts(
            "/app",
            b"<html><head><title>App</title>" + card.encode() + b'</head><body><div id="root">'
            b'</div><script src="/a.js"></script></body></html>',
        ),
    ]
    item = found(findings(view(pages)), "onpage.link_preview_duplicate")
    assert item["urls"] == [f"{BASE}/platform"]
    assert '("Example")' in item["evidence"][0]
    assert classify("onpage.link_preview_duplicate") == "fix"


@pytest.mark.asyncio
async def test_share_images_keep_only_status_and_type_and_follow_redirects():
    site = SyntheticSite()
    site.redirect("https://cdn.example.net/og.png", "https://img.example.net/og.png")
    site.text("https://img.example.net/og.png", b"\x89PNG" * 2000, content_type="image/png")
    async with site.reader((HOST,)) as reader:
        assert not reader.in_scope("http://cdn.example.net/og.png", asset=True)
        result = await read_share_images(
            reader,
            ["https://cdn.example.net/og.png", "https://cdn.example.net/gone.png"],
            seconds=9,
        )
    assert result["images"] == [
        {
            "url": "https://cdn.example.net/og.png",
            "status": "observed",
            "status_code": 200,
            "content_type": "image/png",
        },
        {
            "url": "https://cdn.example.net/gone.png",
            "status": "observed",
            "status_code": 404,
            "content_type": "text/html",
        },
    ]


def test_privacy_policy_and_terms_linked_from_the_homepage():
    assert not NEW_CHECKS & ids(findings(view([complete_home()])))
    # The pages exist but the homepage doesn't link them: Tin adds the footer links.
    home = page("/", json_ld=ORGANIZATION)
    unlinked = found(
        findings(view([home], sitemap=["/privacy", "/terms"])), "trust.legal_pages_unlinked"
    )
    assert unlinked["issue"] == "The homepage doesn't link to a privacy policy or terms of service"
    assert classify("trust.legal_pages_unlinked") == "fix"
    # Nowhere to be found: the founder writes them.
    missing = found(
        findings(view([home], sitemap=["/blog/terms-explained"])), "trust.legal_pages_missing"
    )
    assert missing["issue"] == "No privacy policy or terms of service was found"
    assert missing["impact"] == "medium" and missing["next_action"] == "review"
    assert next_action("trust.legal_pages_missing") == "manual"
    # A link to a page that answers 404 is no privacy policy.
    gone = {"url": f"{BASE}/privacy", "fetch": "http_error", "status_code": 404}
    item = found(findings(view([complete_home(), gone])), "trust.legal_pages_missing")
    assert item["issue"] == "No privacy policy was found" and item["impact"] == "medium"
    assert "answers HTTP 404" in item["evidence"][0]


def test_coverage_names_the_new_reads_and_v15_keeps_its_rows():
    files = {"host_variant": {"url": f"https://www.{HOST}/", "status": "unreachable"}}
    rows = site_check_coverage(
        view([complete_home()], files=files), pagespeed={"status": "not_configured"}, search={}
    )
    by_check = {row["check"]: row for row in rows}
    assert by_check["host_variant"]["status"] == "observed"
    assert by_check["share_images"]["status"] == "unknown"
    assert "homepage, pricing, FAQ and article pages" in by_check["structured_data"]["note"]
    old = site_check_coverage(
        view([complete_home()], files=files, basics=False),
        pagespeed={"status": "not_configured"},
        search={},
    )
    assert {row["check"] for row in old} == set(by_check) - {"host_variant", "share_images"}


def test_every_new_check_has_a_place_in_the_repair_plan():
    assert {check: classify(check) for check in NEW_CHECKS} == {
        "redirects.host_variant_served": "fix",
        "redirects.host_variant_chain": "manual",
        "schema.missing": "fix",
        "schema.entity_profiles_missing": "manual",
        "onpage.link_preview_incomplete": "fix",
        "onpage.link_preview_duplicate": "fix",
        "onpage.share_image_broken": "fix",
        "trust.legal_pages_unlinked": "fix",
        "trust.legal_pages_missing": "manual",
    }


@pytest.mark.asyncio
async def test_a_published_audit_reads_the_preview_image_and_reports_it():
    site = tin.site()
    image = "https://cdn.example.net/og.png"
    site.page(
        f"{tin.BASE}/", html_page(body=FOOTER).replace(b"</head>", PREVIEW.encode() + b"</head>")
    )
    result = await run_audit(site=site)
    assert site.requests.count(image) == 1
    assert result.evidence["site"]["share_images"]["images"][0]["status_code"] == 404
    broken = next(
        f for f in result.inventory["findings"] if f["check_id"] == "onpage.share_image_broken"
    )
    assert broken["next_action"] == "website_change" and broken["urls"] == [f"{tin.BASE}/"]
    assert "The link-preview image does not load" in result.report


def test_faq_questions_in_definition_lists_count_and_legal_pages_are_left_out():
    faq = "<dl>" + "".join(f"<dt>Is it fast {n}?</dt><dd>Yes.</dd>" for n in range(3)) + "</dl>"
    asks = "".join(f"<h2>What data do we collect {n}?</h2>" for n in range(3))
    pages = [page("/", FOOTER + faq, json_ld=ORGANIZATION), page("/privacy-policy", asks)]
    assert pages[0]["faq_questions"] == 3 and pages[0]["question_headings"] == 0
    item = found(findings(view(pages)), "schema.missing")
    assert item["evidence"][0] == "Visible questions and answers but no FAQPage data: /"
    assert item["urls"] == [f"{BASE}/"]


SOCIAL = (
    '<a href="https://www.linkedin.com/company/acme/">LinkedIn</a>'
    '<a href="https://x.com/acme?ref=site">X</a>'
    '<a href="https://twitter.com/intent/tweet?text=hi">Share</a>'
    '<a href="https://www.linkedin.com/shareArticle?url=x">Share</a>'
    '<a href="https://example.org/partner">Partner</a>'
)


def test_profile_links_are_candidates_and_share_links_are_not():
    assert profile_link("https://www.linkedin.com/company/acme/") == (
        "https://www.linkedin.com/company/acme"
    )
    assert profile_link("https://x.com/acme?ref=site") == "https://x.com/acme"
    assert (
        profile_link("https://en.wikipedia.org/wiki/Acme") == "https://en.wikipedia.org/wiki/Acme"
    )
    for not_profile in (
        "https://twitter.com/intent/tweet?text=hi",
        "https://www.facebook.com/sharer/sharer.php?u=x",
        "https://github.com/",
        "https://notgithub.com/acme",
    ):
        assert profile_link(not_profile) is None
    home = page("/", SOCIAL)
    assert home["profile_links"] == ["https://www.linkedin.com/company/acme", "https://x.com/acme"]
    assert complete_home()["organization_same_as"] == ["https://www.linkedin.com/company/x"]


def test_an_organization_without_official_profiles_is_one_finding_for_the_site():
    bare = json.dumps({"@type": "Organization", "name": "Acme", "url": BASE})
    home = page("/", FOOTER + SOCIAL, json_ld=bare)
    other = page("/about", '<a href="https://github.com/acme">GitHub</a>', json_ld=bare)
    item = found(findings(view([home, other])), "schema.entity_profiles_missing")
    assert item["urls"] == [f"{BASE}/"] and item["confidence"] == "likely"
    candidates = [
        "https://www.linkedin.com/company/acme",
        "https://x.com/acme",
        "https://github.com/acme",
    ]
    assert item["verification"]["candidates"] == candidates
    assert item["evidence"][1] == (
        "Profiles the site links to, as candidates to confirm: " + ", ".join(candidates) + "."
    )
    assert next_action("schema.entity_profiles_missing") == "manual"
    alone = found(
        findings(view([page("/", FOOTER, json_ld=bare)])), "schema.entity_profiles_missing"
    )
    assert alone["verification"]["candidates"] == []
    assert "None of the pages Tin read links to a profile" in alone["evidence"][1]
    # sameAs present, no Organization at all, or a v15 run: no entity finding.
    assert "schema.entity_profiles_missing" not in ids(findings(view([complete_home()])))
    no_org = findings(view([page("/", FOOTER + SOCIAL)]))
    assert "schema.entity_profiles_missing" not in ids(no_org)
    assert (
        "Profiles the site links to, for its sameAs: https://www.linkedin.com/company/acme, "
        "https://x.com/acme." in found(no_org, "schema.missing")["evidence"]
    )
    assert "schema.entity_profiles_missing" not in ids(findings(view([home], basics=False)))
