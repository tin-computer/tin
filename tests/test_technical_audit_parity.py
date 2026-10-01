"""The audit and the technical fix agree on whether a problem is there; offline.

Each test runs the same pages through the audit's check and through the fix's checks (the
preparation check that decides a finding is still open, and the live check after merge), so
the two can't drift apart again.
"""

from __future__ import annotations

import pytest
from organic_site_stub import SyntheticSite, sitemap
from test_organic_audit_angles import facts, ids, view
from test_technical_site_fixes import served

from tin_lite import technical_batch as rules
from tin_lite import technical_site_rules as site_rules
from tin_lite.organic_audit import AUDIT_POLICY
from tin_lite.organic_audit_checks import site_findings
from tin_lite.organic_audit_fetch import read_site_files
from tin_lite.organic_audit_site import url_key
from tin_lite.technical_fix_live import LiveRecheck

HOST = "example.com"
BASE = f"https://{HOST}"
URL = f"{BASE}/guide"


def page(head: str = "", body: str = "<h1>Guide</h1><p>Plain words.</p>") -> str:
    return (
        f'<!doctype html><html lang="en"><head><title>Guide to planning</title>{head}</head>'
        f"<body>{body}</body></html>"
    )


def audit_flags(html: str, check_id: str) -> bool:
    findings = site_findings(view([facts("/guide", html.encode())]), home=f"{BASE}/", pagespeed={})
    return check_id in ids(findings)


OG = {
    "none": "",
    "title only": '<meta property="og:title" content="Guide">',
    "image only": '<meta property="og:image" content="https://example.com/a.png">',
    "both": '<meta property="og:title" content="Guide">'
    '<meta property="og:image" content="https://example.com/a.png">',
}


@pytest.mark.parametrize("case", OG)
def test_link_previews(case):
    html = page(OG[case])
    flagged = audit_flags(html, "onpage.open_graph_missing")
    assert flagged == (case != "both")
    assert flagged == (not rules.page_fixed("open_graph", html, URL, {}, hosts=[HOST]))


ARTICLE = '<script type="application/ld+json">{body}</script>'
SCHEMA = {
    "valid": ARTICLE.format(
        body='{"@context":"https://schema.org","@type":"Article","headline":"Guide",'
        '"author":{"@type":"Person","name":"Ada"},"datePublished":"2026-09-01"}'
    ),
    "recommended fields missing": ARTICLE.format(
        body='{"@context":"https://schema.org","@type":"Article","headline":"Guide"}'
    ),
    "required field missing": ARTICLE.format(
        body='{"@context":"https://schema.org","@type":"Article","author":"Ada"}'
    ),
    "not json": ARTICLE.format(body='{"@type": "Article",'),
    "no structured data": "",
}


@pytest.mark.parametrize("case", SCHEMA)
def test_structured_data(case):
    html = page(SCHEMA[case])
    flagged = audit_flags(html, "schema.invalid")
    assert flagged == (case in {"required field missing", "not json"})
    assert flagged == (not rules.page_fixed("schema", html, URL, {}, hosts=[HOST]))


CANONICAL = {
    "none": None,
    "itself": URL,
    "itself, trailing slash": URL + "/",
    "itself over http": f"http://{HOST}/guide",
    "relative to itself": "/guide",
    "another page": f"{BASE}/other",
    "another host, same path": "https://elsewhere.example.org/guide",
}


@pytest.mark.parametrize("case", CANONICAL)
def test_canonicals(case):
    href = CANONICAL[case]
    html = page(f'<link rel="canonical" href="{href}">' if href else "")
    flagged = audit_flags(html, "indexation.canonical_elsewhere")
    assert flagged == (case in {"another page", "another host, same path"})
    assert flagged == (not rules.page_fixed("self_canonical", html, URL, {}, hosts=[HOST]))
    assert flagged == site_rules.page_needs("html_self_canonical", html, URL)


DESCRIPTION = {
    "missing": "",
    "empty": '<meta name="description" content="">',
    "blank": '<meta name="description" content="   ">',
    "present": '<meta name="description" content="How to plan a week of work.">',
}


@pytest.mark.parametrize("case", DESCRIPTION)
def test_meta_descriptions(case):
    # The audit's missing-description finding comes from the crawl provider, so the fix's
    # two checks are held to each other: an empty description is a missing one.
    html = page(DESCRIPTION[case])
    needed = site_rules.page_needs("html_description", html, URL)
    assert needed == (case != "present")
    assert needed == (not rules.page_fixed("description", html, URL, {}, hosts=[HOST]))


def index(urls: list[str]) -> str:
    rows = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</sitemapindex>'
    )


SITEMAPS = {
    "robots.txt names an index": {
        f"{BASE}/robots.txt": f"User-agent: *\nAllow: /\nSitemap: {BASE}/maps/index.xml\n",
        f"{BASE}/maps/index.xml": index([f"{BASE}/maps/pages.xml", f"{BASE}/maps/posts.xml"]),
        f"{BASE}/maps/pages.xml": sitemap([f"{BASE}/", f"{BASE}/pricing"]).decode(),
        f"{BASE}/maps/posts.xml": sitemap([URL]).decode(),
    },
    "the default location": {
        f"{BASE}/robots.txt": "User-agent: *\nAllow: /\n",
        f"{BASE}/sitemap.xml": sitemap([f"{BASE}/", URL]).decode(),
    },
    "no sitemap": {f"{BASE}/robots.txt": "User-agent: *\nAllow: /\n"},
}


@pytest.mark.parametrize("case", SITEMAPS)
async def test_sitemaps(case):
    files = SITEMAPS[case]
    site = SyntheticSite()
    for url, text in files.items():
        site.text(url, text, content_type="application/xml")
    async with site.reader((HOST,)) as reader:
        audited = await read_site_files(reader, f"{BASE}/", AUDIT_POLICY)
    listed = {url_key(row["loc"]) for row in audited["sitemaps"]["urls"]}

    async def fetch_file(url, *, host, kind):
        if url not in files:
            return served(url, "", 404)
        return served(url, files[url])

    repairs = [
        {"finding_id": "oa_" + "1" * 20, "check_id": "c", "live": "sitemap_exists", "urls": []},
        {"finding_id": "oa_" + "2" * 20, "check_id": "c", "live": "sitemap_add", "urls": [URL]},
        {"finding_id": "oa_" + "3" * 20, "check_id": "c", "live": "sitemap_remove", "urls": [URL]},
    ]
    prepared = {
        "target": {"url": f"{BASE}/", "host": HOST, "site_hosts": [HOST]},
        "batch": {"repairs": repairs},
    }
    live = LiveRecheck(database=None, integrations=None, fetch_file=fetch_file)
    exists, added, removed = [row["live"] for row in await live._live_batch(prepared)]
    # The fix sees a sitemap exactly when the audit read one, and the same pages in it.
    assert (exists == "fixed") == bool(listed)
    if listed:
        assert (added == "fixed") == (url_key(URL) in listed)
        assert (removed == "fixed") == (url_key(URL) not in listed)
    else:
        assert added == removed == "unknown"
