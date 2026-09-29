"""robots.txt, sitemap, static HTML and page selection for organic-audit-v10; offline."""

from __future__ import annotations

import gzip

import httpx
import pytest
from organic_site_stub import SyntheticSite, html_page, sitemap

from tin_lite.organic_audit import AUDIT_POLICY
from tin_lite.organic_audit_fetch import (
    SiteReader,
    pagespeed_summary,
    read_pages,
    read_site_files,
)
from tin_lite.organic_audit_site import (
    crawler_stances,
    html_facts,
    is_ad_landing_url,
    is_noindex,
    is_utility_url,
    language_prefix,
    named_groups_missing_wildcard_rules,
    parse_robots,
    parse_sitemap,
    path_allowed,
    robots_allows,
    robots_group,
    select_pages,
    url_key,
    x_robots_directives,
)

ROBOTS = """# comment
User-agent: *
Disallow: /api/
Disallow: /*.pdf$
Allow: /api/public

User-agent: Googlebot
User-agent: Bingbot
Disallow: /private

User-agent: GPTBot
Disallow: /

User-agent: OAI-SearchBot
Allow: /

Sitemap: https://example.com/sitemap.xml
"""


def observed(text: str) -> dict:
    return {"status": "observed", **parse_robots(text)}


def test_robots_groups_follow_rfc_9309_without_inheritance():
    robots = observed(ROBOTS)
    assert robots["sitemaps"] == ["https://example.com/sitemap.xml"]
    source, rules = robots_group(robots, "googlebot")
    assert source == "named" and rules == [["disallow", "/private"]]
    # Googlebot's own group replaces the * rules entirely.
    assert robots_allows(robots, "googlebot", "https://example.com/api/x") is True
    assert robots_allows(robots, "bingbot", "https://example.com/private/x") is False
    assert robots_group(robots, "unknown-bot")[0] == "wildcard"
    assert robots_allows(robots, "unknown-bot", "https://example.com/api/x") is False
    # Longest match wins; an equal-length allow beats disallow.
    assert robots_allows(robots, "unknown-bot", "https://example.com/api/public/x") is True
    assert robots_allows(robots, "unknown-bot", "https://example.com/files/a.pdf") is False
    assert robots_allows(robots, "unknown-bot", "https://example.com/files/a.pdf?x=1") is True
    assert path_allowed([["disallow", "/a"], ["allow", "/a"]], "/a") is True


def test_robots_status_decides_unknown_versus_allowed():
    assert robots_allows(None, "googlebot", "https://example.com/") is None
    assert robots_allows({"status": "missing"}, "googlebot", "https://example.com/") is True
    assert robots_allows({"status": "server_error"}, "googlebot", "https://example.com/") is None


def test_ai_crawler_stances_and_named_groups_that_reopen_wildcard_paths():
    robots = observed(ROBOTS)
    stances = {row["agent"]: row for row in crawler_stances(robots)}
    assert stances["GPTBot"]["stance"] == "blocked"
    assert stances["OAI-SearchBot"]["stance"] == "allowed"
    assert stances["OAI-SearchBot"]["group"] == "named"
    assert stances["PerplexityBot"]["stance"] == "partly_blocked"
    assert stances["PerplexityBot"]["closed_paths"] == ["/*.pdf$", "/api/"]
    rows = named_groups_missing_wildcard_rules(robots)
    reopened = {row["agent"]: row["reopened"] for row in rows}
    # GPTBot is fully blocked, so it cannot reopen anything; the others can.
    assert set(reopened) == {"Googlebot", "Bingbot", "OAI-SearchBot"}
    assert reopened["OAI-SearchBot"] == ["/*.pdf$", "/api/"]
    assert named_groups_missing_wildcard_rules(observed("User-agent: *\nAllow: /\n")) == []


def test_sitemap_parser_reads_urlsets_indexes_cdata_and_entities_without_xml_entities():
    index = parse_sitemap(
        '<?xml version="1.0"?><sitemapindex><sitemap><loc>https://example.com/a.xml</loc>'
        "</sitemap></sitemapindex>",
        max_urls=10,
    )
    assert index["kind"] == "index" and index["entries"][0]["loc"] == "https://example.com/a.xml"
    urlset = parse_sitemap(
        "<urlset><url><loc><![CDATA[https://example.com/a?x=1&amp;y=2]]></loc>"
        "<lastmod>2026-09-01</lastmod></url><url><loc>https://example.com/b</loc></url>"
        "<url><loc>https://example.com/c</loc></url></urlset>",
        max_urls=2,
    )
    assert urlset["total"] == 3 and len(urlset["entries"]) == 2
    assert urlset["entries"][0] == {"loc": "https://example.com/a?x=1&y=2", "lastmod": "2026-09-01"}
    assert parse_sitemap("<html>not a sitemap</html>", max_urls=5)["kind"] == "invalid"
    # Entity expansion is never performed: the text is matched, not parsed as XML.
    bomb = '<!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>'
    assert parse_sitemap(bomb, max_urls=5)["entries"][0]["loc"] == "&a;"


def test_static_html_facts_resolve_relative_links_and_read_robots_directives():
    body = (
        b'<html lang="nl-NL"><head><title> Prijzen  | Tin </title>'
        b'<meta name="robots" content="noindex, follow">'
        b'<link rel="canonical" href="/nl/pricing">'
        b'<link rel="alternate" hreflang="en" href="/pricing">'
        b'<script type="application/ld+json">{"@type":"Product","offers":{"@type":"Offer"}}'
        b"</script></head><body><h1>A</h1><div itemscope></div><h1>B</h1></body></html>"
    )
    facts = html_facts(body, url="https://example.com/nl/pricing/", charset=None, truncated=False)
    assert facts["lang"] == "nl-NL" and facts["title"] == "Prijzen | Tin"
    assert facts["canonical"] == "https://example.com/nl/pricing"
    assert facts["hreflang"] == [{"lang": "en", "href": "https://example.com/pricing"}]
    assert facts["h1_count"] == 2 and facts["robots"] == ["follow", "noindex"]
    assert facts["json_ld_types"] == ["Product", "Offer"] and facts["microdata"]
    assert is_noindex(facts)
    assert x_robots_directives(["googlebot: noindex", "otherbot: noindex", "nofollow"]) == [
        "noindex",
        "nofollow",
    ]
    assert x_robots_directives(["unavailable_after: 25 Jun 2030"]) == [
        "unavailable_after: 25 jun 2030"
    ]


def test_url_classification_for_utility_ad_and_language_pages():
    assert url_key("https://example.com/a/") == url_key("https://www.example.com/a") == "/a"
    assert url_key("https://example.com") == "/"
    assert is_utility_url("https://example.com/sign-in")
    assert is_utility_url("https://example.com/account/settings")
    assert not is_utility_url("https://example.com/blog/how-to-sign-in-faster")
    assert is_ad_landing_url("https://example.com/offer/launch")
    assert is_ad_landing_url("https://example.com/pricing?utm_source=ads")
    assert not is_ad_landing_url("https://example.com/offers-and-pricing-explained/x")
    assert language_prefix("https://example.com/nl/pricing") == "nl"
    assert language_prefix("https://example.com/pt-br/") == "pt-br"
    assert language_prefix("https://example.com/en/pricing") is None
    assert language_prefix("https://example.com/blog/x") is None


def test_selection_keeps_top_search_pages_and_every_section_under_the_cap():
    sitemap_urls = (
        [f"https://example.com/blog/post-{i}" for i in range(40)]
        + [f"https://example.com/compare/x-{i}" for i in range(10)]
        + ["https://example.com/nl/", "https://example.com/offer/a", "https://example.com/about"]
    )
    search = [
        {"url": "https://example.com/compare/x-7", "clicks": 0, "impressions": 900},
        {"url": "https://example.com/sign-in", "clicks": 3, "impressions": 500},
        {"url": "https://example.com/blog/post-39", "clicks": 1, "impressions": 50},
    ]
    plan = select_pages(
        home="https://example.com/", sitemap_urls=sitemap_urls, search_pages=search, cap=10
    )
    selected = [row["url"] for row in plan["selected"]]
    assert len(selected) == 10 and selected[0] == "https://example.com/"
    assert selected[1:4] == [
        "https://example.com/compare/x-7",
        "https://example.com/sign-in",
        "https://example.com/blog/post-39",
    ]
    sections = {row["section"] for row in plan["selected"]}
    assert {"/nl/", "/offer/", "/about/", "/blog/", "/compare/"} <= sections
    assert plan["candidates"] == 55
    again = select_pages(
        home="https://example.com/", sitemap_urls=sitemap_urls, search_pages=search, cap=10
    )
    assert again == plan


def test_selection_takes_everything_when_the_site_fits():
    urls = [f"https://example.com/p{i}" for i in range(5)]
    plan = select_pages(home="https://example.com/", sitemap_urls=urls, search_pages=[], cap=100)
    assert len(plan["selected"]) == 6


@pytest.mark.asyncio
async def test_site_files_follow_robots_sitemaps_indexes_and_gzip_within_scope():
    site = SyntheticSite()
    site.text(
        "https://example.com/robots.txt",
        "User-agent: *\nDisallow: /private\nSitemap: https://example.com/index.xml\n"
        "Sitemap: https://other.example/sitemap.xml\n",
    )
    site.text(
        "https://example.com/index.xml",
        "<sitemapindex><sitemap><loc>https://example.com/a.xml.gz</loc></sitemap>"
        "<sitemap><loc>https://other.example/b.xml</loc></sitemap>"
        "<sitemap><loc>https://example.com/missing.xml</loc></sitemap></sitemapindex>",
        content_type="application/xml",
    )
    site.text(
        "https://example.com/a.xml.gz",
        gzip.compress(sitemap(["https://example.com/", "https://example.com/a"])),
        content_type="application/x-gzip",
    )
    async with site.reader(("example.com",)) as reader:
        files = await read_site_files(reader, "https://example.com/", AUDIT_POLICY)
    assert files["robots"]["status"] == "observed"
    sitemaps = files["sitemaps"]
    assert sitemaps["referenced_in_robots"] and sitemaps["total_urls"] == 2
    assert [row["loc"] for row in sitemaps["urls"]] == [
        "https://example.com/",
        "https://example.com/a",
    ]
    statuses = {row["url"]: row["status"] for row in sitemaps["files"]}
    assert statuses["https://example.com/missing.xml"] == "http_error"
    assert "https://other.example/sitemap.xml" not in site.requests
    assert "https://other.example/b.xml" not in site.requests


@pytest.mark.asyncio
async def test_default_sitemap_and_robots_errors_are_statuses_not_exceptions():
    site = SyntheticSite()
    site.routes["https://example.com/robots.txt"] = (503, {}, b"down")
    async with site.reader(("example.com",)) as reader:
        files = await read_site_files(reader, "https://example.com/", AUDIT_POLICY)
    assert files["robots"] == {
        "url": "https://example.com/robots.txt",
        "status": "server_error",
        "status_code": 503,
    }
    assert files["sitemaps"]["files"] == [
        {"url": "https://example.com/sitemap.xml", "status": "http_error", "status_code": 404}
    ]


@pytest.mark.asyncio
async def test_page_reads_refuse_other_hosts_private_addresses_and_robots_blocks():
    site = SyntheticSite()
    site.page("https://example.com/a", html_page(title="A"), headers={"x-robots-tag": "noindex"})
    site.redirect("https://example.com/old", "https://example.com/a")
    site.redirect("https://example.com/away", "https://elsewhere.example/")
    site.text("https://example.com/file.txt", "plain")
    robots = observed("User-agent: *\nDisallow: /secret\n")
    urls = [
        "https://example.com/a",
        "https://example.com/old",
        "https://example.com/away",
        "https://example.com/secret/x",
        "https://example.com/file.txt",
        "https://example.com/missing",
        "https://elsewhere.example/a",
        "http://example.com/a",
    ]
    async with site.reader(("example.com",)) as reader:
        result = await read_pages(reader, urls, robots=robots, policy=AUDIT_POLICY, seconds=30)
    assert result["https://example.com/a"]["fetch"] == "observed"
    assert result["https://example.com/a"]["x_robots_tag"] == ["noindex"]
    assert is_noindex(result["https://example.com/a"])
    assert result["https://example.com/old"] == {
        "url": "https://example.com/old",
        "status_code": 301,
        "x_robots_tag": [],
        "fetch": "redirect",
        "location": "https://example.com/a",
    }
    assert result["https://example.com/away"]["location"] == "(outside the audited site)"
    assert result["https://example.com/secret/x"]["fetch"] == "blocked_by_robots"
    assert result["https://example.com/file.txt"]["fetch"] == "not_html"
    assert result["https://example.com/missing"]["fetch"] == "http_error"
    assert result["https://elsewhere.example/a"]["reason"] == "out_of_scope"
    assert result["http://example.com/a"]["reason"] == "out_of_scope"
    # Redirects were recorded, not followed, and nothing left the audited host.
    assert "https://elsewhere.example/" not in site.requests
    assert "https://example.com/secret/x" not in site.requests

    async def private(host, port, type=None):
        return [(2, 1, 6, "", ("10.0.0.5", port))]

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    async with SiteReader(("example.com",), client=client, resolver=private) as reader:
        assert (await reader.get("https://example.com/", max_bytes=10))["status"] == (
            "non_public_address"
        )


@pytest.mark.asyncio
async def test_page_reads_stop_at_the_byte_limit_and_mark_truncation():
    site = SyntheticSite()
    site.page("https://example.com/big", b"<html lang='en'><body><h1>x</h1>" + b"a" * 5000)
    policy = {**AUDIT_POLICY, "max_page_bytes": 1000}
    async with site.reader(("example.com",)) as reader:
        result = await read_pages(
            reader, ["https://example.com/big"], robots=None, policy=policy, seconds=30
        )
    facts = result["https://example.com/big"]
    assert facts["truncated"] and facts["html_bytes"] == 1000 and facts["h1_count"] == 1


def test_pagespeed_summary_prefers_field_data_and_never_invents_inp():
    payload = {
        "loadingExperience": {
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 3100},
                "INTERACTION_TO_NEXT_PAINT": {"percentile": 180},
                "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 12},
            }
        },
        "lighthouseResult": {
            "audits": {"largest-contentful-paint": {"numericValue": 2000.5}},
            "categories": {"performance": {"score": 0.71}},
        },
    }
    summary = pagespeed_summary(payload)
    assert summary["field"] == {
        "lcp_ms": 3100.0,
        "inp_ms": 180.0,
        "cls": 0.12,
        "origin_fallback": False,
    }
    assert summary["lab"]["lcp_ms"] == 2000.5 and summary["lab"]["cls"] is None
    assert pagespeed_summary({})["field"]["inp_ms"] is None


@pytest.mark.asyncio
async def test_bot_protection_refusals_are_unknown_not_errors():
    site = SyntheticSite()
    site.routes["https://example.com/robots.txt"] = (403, {}, b"Forbidden")
    site.routes["https://example.com/sitemap.xml"] = (429, {}, b"Too many requests")
    site.routes["https://example.com/a"] = (403, {"content-type": "text/html"}, b"Access denied")
    site.routes["https://example.com/b"] = (
        503,
        {"content-type": "text/html"},
        b"<html><title>Just a moment...</title></html>",
    )
    site.routes["https://example.com/c"] = (503, {"content-type": "text/html"}, b"Down")
    async with site.reader(("example.com",)) as reader:
        files = await read_site_files(reader, "https://example.com/", AUDIT_POLICY)
        pages = await read_pages(
            reader,
            ["https://example.com/a", "https://example.com/b", "https://example.com/c"],
            robots=files["robots"],
            policy=AUDIT_POLICY,
            seconds=30,
        )
    assert files["robots"] == {
        "url": "https://example.com/robots.txt",
        "status": "refused",
        "status_code": 403,
    }
    assert files["sitemaps"]["files"][0]["status"] == "refused"
    # Unknown robots rules do not stop Tin from reading the pages the owner asked about.
    assert robots_allows(files["robots"], "googlebot", "https://example.com/a") is None
    assert [pages[f"https://example.com/{p}"]["fetch"] for p in "abc"] == [
        "refused",
        "refused",
        "http_error",
    ]
