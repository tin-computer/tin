"""Served-file checks shared by site-fix-v5: exact page and canonical fixes; offline."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime

import pytest
from test_organic_audit_findings import facts, files
from test_procedure_publication import publication_db as publication_db
from test_technical_fix_sources import source_fixture

from tin_lite import technical_site_rules as rules
from tin_lite.technical_fix_live import present

HOST = "example.com"
BASE = f"https://{HOST}"
ROBOTS = "User-agent: *\nDisallow: /admin\n"
SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    f"  <url><loc>{BASE}/</loc><lastmod>2026-09-01</lastmod></url>\n"
    f"  <url><loc>{BASE}/login</loc></url>\n"
    "</urlset>\n"
)
PAGE = (
    "<!doctype html>\n<html>\n<head>\n<title>Sign in</title>\n</head>\n"
    "<body>\n<form></form>\n</body>\n</html>\n"
)
NEXT_PACKAGE = json.dumps({"dependencies": {"next": "15.0.0", "react": "19.0.0"}})
NEXT_LAYOUT = (
    "export const metadata = { title: 'Example' };\n\n"
    "export default function RootLayout({ children }) {\n"
    "  return (\n    <html>\n      <body>{children}</body>\n    </html>\n  );\n}\n"
)


# --- Pure rules ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "after"),
    [
        ("html_lang", PAGE.replace("<html>", '<html lang="en">')),
        (
            "html_noindex",
            PAGE.replace("<head>\n", '<head>\n<meta name="robots" content="noindex">'),
        ),
        ("html_h1", PAGE.replace("<form>", "<h1>Sign in to Example</h1>\n<form>")),
        (
            "html_description",
            PAGE.replace("<head>", '<head><meta name="description" content="Sign in.">'),
        ),
    ],
)
def test_each_page_fix_passes_alone_and_fails_with_an_extra_edit(kind, after):
    url = f"{BASE}/login"
    assert rules.page_needs(kind, PAGE, url) and not rules.page_needs(kind, after, url)
    rules.verify_html_change(PAGE, after, kind, url)
    with pytest.raises(ValueError):
        rules.verify_html_change(PAGE, after.replace("<form></form>", "<form>x</form>"), kind, url)


def test_canonical_fixes_point_home_or_keep_one_existing_tag():
    url = f"{BASE}/pricing"
    elsewhere = PAGE.replace("<head>\n", f'<head>\n<link rel="canonical" href="{BASE}/">\n')
    assert rules.page_needs("html_self_canonical", elsewhere, url)
    rules.verify_html_change(
        elsewhere,
        elsewhere.replace(f'href="{BASE}/"', 'href="/pricing"'),
        "html_self_canonical",
        url,
    )
    with pytest.raises(ValueError, match="page itself"):
        rules.verify_html_change(
            elsewhere,
            elsewhere.replace(f'href="{BASE}/"', 'href="https://other.example/pricing"'),
            "html_self_canonical",
            url,
        )
    two = elsewhere.replace("</head>", f'<link rel="canonical" href="{url}">\n</head>')
    one = two.replace(f'<link rel="canonical" href="{BASE}/">\n', "")
    rules.verify_html_change(two, one, "html_one_canonical", url)


# --- Sources ---------------------------------------------------------------------------------


def site_source(*, policy="organic-audit-v10", **changes):
    site = {
        "files": files(robots=ROBOTS, urls=["/", "/login"]),
        "plan": None,
        "pages": [facts("/"), facts("/login", lang=None)],
        "pages_status": "complete",
        "pagespeed": {"status": "not_configured", "results": []},
        **changes,
    }
    return source_fixture(policy=policy, site=site, checks={})


# --- Preparation, delivery checks and the report ---------------------------------------------


def archive(files_):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, text in files_.items():
            raw = text.encode()
            item = tarfile.TarInfo(path)
            item.size = len(raw)
            tar.addfile(item, io.BytesIO(raw))
    return buffer.getvalue()


def served(url, text, status=200):
    return {
        "url": url,
        "redirects": [],
        "status_code": status,
        "observed_at": "2026-09-29T00:00:00+00:00",
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "text": text,
    }


def served_page(url, html):
    return {**served(url, html), "html": html}


def patch(prepared, files_, body="Adds the sitemap line."):
    binding = prepared["repository_binding"]
    return {
        "repository": binding["repository"],
        "default_branch": binding["default_branch"],
        "head_sha": binding["head_sha"],
        "title": "Fix the audit finding",
        "body": body,
        "files": files_,
        "outcome": "patch",
        "reason": "",
        "verification": [],
    }


# --- The live check after merge --------------------------------------------------------------


def test_a_closed_unmerged_pr_is_said_plainly():
    assert present({"closed": True}, datetime.now(UTC))["state"] == "closed_unmerged"


# --- The traffic system's technical step ------------------------------------------------------


# --- Disposable Postgres: preparation receipts and PR delivery ---------------------------------
