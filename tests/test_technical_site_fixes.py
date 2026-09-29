"""site-fix-v4: audit site findings become checked PRs, and merged PRs get a live check; offline."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_organic_audit_findings import facts, files
from test_organic_system_db import technical_fixture
from test_procedure_publication import publication_db as publication_db
from test_technical_fix_sources import source_fixture
from test_technical_title_repair import spec

from tin_lite import technical_fix
from tin_lite import technical_site_rules as rules
from tin_lite.integrations import GitHubRepositoryBinding
from tin_lite.organic_audit import digest
from tin_lite.technical_fix_execution import TechnicalFixExecution
from tin_lite.technical_fix_live import LiveRecheck, present
from tin_lite.technical_fix_sources import finding_rank

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


def test_every_mapped_check_has_one_kind_and_a_plain_change_sentence():
    assert set(rules.SITE_FIXES.values()) == rules.KINDS == set(rules.CHANGES)
    assert technical_fix.supported_checks(technical_fix.SITE_POLICY) >= set(rules.SITE_FIXES)
    # The historical metadata policy keeps its two checks.
    assert set(rules.SITE_FIXES) - technical_fix.supported_checks(technical_fix.POLICY)


def test_robots_sitemap_line_may_add_only_the_sitemap():
    expected = {"sitemaps": [f"{BASE}/sitemap.xml"]}
    rules.verify_robots_change(
        ROBOTS, ROBOTS + f"\nSitemap: {BASE}/sitemap.xml\n", "robots_sitemap_line", expected
    )
    # A plausible but unusable result: the sitemap line plus an unrelated rule change.
    with pytest.raises(ValueError, match="Only Sitemap lines"):
        rules.verify_robots_change(
            ROBOTS,
            ROBOTS.replace("/admin", "/") + f"Sitemap: {BASE}/sitemap.xml\n",
            "robots_sitemap_line",
            expected,
        )
    with pytest.raises(ValueError, match="exactly the site's Sitemap"):
        rules.verify_robots_change(
            ROBOTS, ROBOTS + f"Sitemap: {BASE}/other.xml\n", "robots_sitemap_line", expected
        )
    new = f"User-agent: *\nAllow: /\n\nSitemap: {BASE}/sitemap.xml\n"
    rules.verify_robots_change(None, new, "robots_sitemap_line", expected)
    with pytest.raises(ValueError, match="may not disallow"):
        rules.verify_robots_change(None, new + "Disallow: /x\n", "robots_sitemap_line", expected)


def test_allowing_ai_search_crawlers_leaves_every_other_crawler_alone():
    before = "User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n"
    need = rules.robots_needs("robots_allow_ai_search", {"status": "observed", "text": before})
    assert need == {"needed": True, "agents": ["ChatGPT-User", "OAI-SearchBot", "PerplexityBot"]}
    group = "\nUser-agent: OAI-SearchBot\nUser-agent: ChatGPT-User\nUser-agent: PerplexityBot\n"
    rules.verify_robots_change(
        before, before + group + "Allow: /\n", "robots_allow_ai_search", need
    )
    for wrong in (
        "User-agent: *\nAllow: /\n",  # opens the site to training crawlers too
        before + group + "Disallow: /\n",  # still blocked
    ):
        with pytest.raises(ValueError):
            rules.verify_robots_change(before, wrong, "robots_allow_ai_search", need)


def test_sitemap_entries_leave_or_join_whole_and_nothing_else_moves():
    removed = SITEMAP.replace(f"  <url><loc>{BASE}/login</loc></url>\n", "")
    rules.verify_sitemap_change(
        SITEMAP, removed, "sitemap_remove_urls", {"remove": [f"{BASE}/login"]}
    )
    with pytest.raises(ValueError):
        rules.verify_sitemap_change(
            SITEMAP,
            removed.replace("2026-09-01", "2026-09-29"),  # also rewrites another entry
            "sitemap_remove_urls",
            {"remove": [f"{BASE}/login"]},
        )
    added = SITEMAP.replace("</urlset>", f"  <url><loc>{BASE}/pricing</loc></url>\n</urlset>")
    rules.verify_sitemap_change(SITEMAP, added, "sitemap_add_urls", {"add": [f"{BASE}/pricing"]})
    with pytest.raises(ValueError):
        rules.verify_sitemap_change(
            SITEMAP,
            added.replace(
                "</loc></url>\n</urlset>", "</loc><priority>1</priority></url>\n</urlset>"
            ),
            "sitemap_add_urls",
            {"add": [f"{BASE}/pricing"]},
        )
    assert rules.still_needed(
        "sitemap_remove_urls", SITEMAP, "", {"remove": [f"{BASE}/login/"]}
    ) and not rules.still_needed("sitemap_remove_urls", removed, "", {"remove": [f"{BASE}/login"]})


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


def test_nextjs_candidates_follow_the_app_router_layout():
    names = [
        "package.json",
        "tsconfig.json",
        "app/layout.tsx",
        "app/(site)/pricing/page.tsx",
        "app/blog/[slug]/page.tsx",
        "app/login/page.tsx",
        "app/login/layout.tsx",
        "public/favicon.ico",
    ]
    assert rules.framework_candidates("html_noindex", names, [f"{BASE}/login"]) == {
        "existing": ["app/login/page.tsx", "app/login/layout.tsx", "app/layout.tsx"],
        "new": [],
    }
    assert rules.framework_candidates("html_self_canonical", names, [f"{BASE}/blog/post"])[
        "existing"
    ] == ["app/blog/[slug]/page.tsx", "app/layout.tsx"]
    assert rules.framework_candidates("robots_sitemap_line", names, []) == {
        "existing": [],
        "new": ["app/robots.ts"],
    }
    assert rules.framework_candidates("html_lang", names, [])["existing"] == ["app/layout.tsx"]
    assert rules.is_nextjs(NEXT_PACKAGE) and not rules.is_nextjs('{"dependencies": {}}')


def test_framework_changes_stay_inside_the_named_files_and_small():
    originals = {"app/layout.tsx": NEXT_LAYOUT}
    changed = NEXT_LAYOUT.replace("<html>", '<html lang="en">')
    rules.verify_framework_change([{"path": "app/layout.tsx", "content": changed}], originals, [])
    for files_, message in (
        ([{"path": "next.config.js", "content": "x"}], "not one of the files"),
        ([{"path": "app/layout.tsx", "content": NEXT_LAYOUT}], "did not change"),
        (
            [{"path": "app/layout.tsx", "content": changed + "// x\n" * 80}],
            "larger than this repair",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            rules.verify_framework_change(files_, originals, [])


# --- Sources ---------------------------------------------------------------------------------


def site_source(**changes):
    site = {
        "files": files(robots=ROBOTS, urls=["/", "/login"]),
        "plan": None,
        "pages": [facts("/"), facts("/login", lang=None)],
        "pages_status": "complete",
        "pagespeed": {"status": "not_configured", "results": []},
        **changes,
    }
    return source_fixture(policy="organic-audit-v10", site=site, checks={})


async def test_site_findings_are_repair_candidates_only_under_site_fix_v4():
    source = site_source()
    old = await source.service.inspect(project_id=source.project.id, audit_run_id=source.run.id)
    assert not old["findings"] and {
        row["finding"]["check_id"] for row in old["excluded_findings"]
    } >= {"robots.sitemap_reference_missing", "onpage.lang_missing"}
    source.service.supported_checks = technical_fix.supported_checks(technical_fix.SITE_POLICY)
    new = await source.service.inspect(project_id=source.project.id, audit_run_id=source.run.id)
    eligible = {row["finding"]["check_id"]: row for row in new["findings"]}
    assert eligible["onpage.lang_missing"]["affected_urls"] == [f"{BASE}/login"]
    assert all(row["source_eligible"] for row in eligible.values())
    assert new["repair_availability"]["available"] is True


def test_the_most_urgent_finding_goes_first():
    rows = [
        {"check_id": "onpage.lang_missing", "priority": "quick_win", "impact": "low"},
        {"check_id": "canonical.broken"},  # crawl finding: the report's tier is critical
        {"check_id": "robots.ai_search_crawlers_blocked", "priority": "high_impact"},
    ]
    assert [row["check_id"] for row in sorted(rows, key=finding_rank)] == [
        "canonical.broken",
        "robots.ai_search_crawlers_blocked",
        "onpage.lang_missing",
    ]


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


def execution_for(*, finding, urls=(), repo, site_files=None, pages=None, overlap=()):
    binding = GitHubRepositoryBinding(uuid4(), 123, 456, "owner/site", "main", "b" * 40)
    site_files = site_files or {}
    pages = pages or {}

    async def fetch_file(url, *, host, kind):
        assert host == HOST
        if url not in site_files:
            raise ValueError("not served")
        text, status = site_files[url]
        return served(url, text, status)

    async def fetch(url, *, host):
        return served_page(url, pages[url])

    integrations = SimpleNamespace(
        github_repository_bundle=AsyncMock(
            return_value=SimpleNamespace(archive=archive(repo), complete=True)
        ),
        github_open_pull_requests=AsyncMock(
            return_value=SimpleNamespace(
                truncated=False, changed_paths=tuple(overlap), document=b"{}"
            )
        ),
    )
    execution = TechnicalFixExecution(
        database=None,
        storage=None,
        integrations=integrations,
        fetch=AsyncMock(side_effect=fetch),
        fetch_file=AsyncMock(side_effect=fetch_file),
    )

    async def once(run_id, key, operation):
        return json.loads(json.dumps(await operation(), default=str))

    execution.once = once
    selection = {
        "source": {"audit_run_id": str(uuid4()), "audit_revision": "a" * 40},
        "target": {"url": f"{BASE}/", "host": HOST, "site_hosts": [HOST]},
        "selection": {
            "finding": {"id": "oa_" + "1" * 20, "check_id": finding},
            "affected_urls": list(urls),
            "affected_count": len(urls),
        },
        "repository_binding": asdict(binding),
        "input_sha256": digest({}),
    }
    return execution, selection, SimpleNamespace(id=uuid4(), project_id=uuid4())


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


async def test_a_served_robots_file_becomes_a_static_fix_checked_from_the_diff():
    execution, selection, run = execution_for(
        finding="robots.sitemap_reference_missing",
        repo={"public/robots.txt": ROBOTS, "package.json": NEXT_PACKAGE},
        site_files={
            f"{BASE}/robots.txt": (ROBOTS, 200),
            f"{BASE}/sitemap.xml": (SITEMAP, 200),
        },
    )
    prepared = await execution._resolve_site(run, selection)
    fix = prepared["site_fix"]
    assert prepared["reason"] is None
    assert fix["mode"] == "static" and fix["expected"] == {"sitemaps": [f"{BASE}/sitemap.xml"]}
    assert prepared["originals"] == {"public/robots.txt": ROBOTS}
    good = patch(
        prepared,
        [{"path": "public/robots.txt", "content": ROBOTS + f"Sitemap: {BASE}/sitemap.xml\n"}],
    )
    technical_fix.validate_manifest(good, prepared)
    await execution._validate_site_delivery(prepared)
    # Plausible but unusable: the right line, plus a quiet change to another rule.
    bad = patch(
        prepared,
        [
            {
                "path": "public/robots.txt",
                "content": "User-agent: *\nDisallow:\n" + f"Sitemap: {BASE}/sitemap.xml\n",
            }
        ],
    )
    with pytest.raises(ValueError):
        technical_fix.validate_manifest(bad, prepared)
    report = technical_fix.report(
        prepared, pull_request=SimpleNamespace(url="https://github.com/owner/site/pull/7")
    ).decode()
    assert "adds a Sitemap line to robots.txt" in report
    assert "checks the live site for this one finding" in report


async def test_a_static_patch_is_refused_when_the_live_file_changed_since_preparation():
    execution, selection, run = execution_for(
        finding="robots.sitemap_reference_missing",
        repo={"robots.txt": ROBOTS},
        site_files={f"{BASE}/robots.txt": (ROBOTS, 200), f"{BASE}/sitemap.xml": (SITEMAP, 200)},
    )
    prepared = await execution._resolve_site(run, selection)
    execution.fetch_file.side_effect = lambda url, **_: served(url, ROBOTS + "# edited\n")
    with pytest.raises(ValueError, match="changed after preparation"):
        await execution._validate_site_delivery(prepared)


async def test_without_a_sitemap_robots_txt_is_left_alone():
    execution, selection, run = execution_for(
        finding="robots.sitemap_reference_missing",
        repo={"robots.txt": ROBOTS},
        site_files={f"{BASE}/robots.txt": (ROBOTS, 200)},
    )
    prepared = await execution._resolve_site(run, selection)
    assert prepared["reason"] == "no_sitemap_to_reference"
    assert (
        "no readable sitemap" in technical_fix.report(prepared, reason=prepared["reason"]).decode()
    )


async def test_a_generated_page_in_nextjs_becomes_a_bounded_framework_fix():
    url = f"{BASE}/login"
    execution, selection, run = execution_for(
        finding="onpage.lang_missing",
        urls=[url],
        repo={
            "package.json": NEXT_PACKAGE,
            "tsconfig.json": "{}",
            "app/layout.tsx": NEXT_LAYOUT,
            "app/login/page.tsx": "export default function Page() { return <form />; }\n",
        },
        pages={url: PAGE},
    )
    prepared = await execution._resolve_site(run, selection)
    fix = prepared["site_fix"]
    assert prepared["reason"] is None and fix["mode"] == "framework"
    assert set(prepared["originals"]) == {"app/layout.tsx"}
    changed = [
        {"path": "app/layout.tsx", "content": NEXT_LAYOUT.replace("<html>", '<html lang="en">')}
    ]
    with pytest.raises(ValueError, match="could not build"):
        technical_fix.validate_manifest(patch(prepared, changed, body="Sets lang."), prepared)
    body = f"Sets lang on the root layout.\n\n{rules.FRAMEWORK_NOTE}"
    technical_fix.validate_manifest(patch(prepared, changed, body=body), prepared)
    await execution._validate_site_delivery(prepared)
    report = technical_fix.report(
        prepared, pull_request=SimpleNamespace(url="https://github.com/owner/site/pull/8")
    ).decode()
    assert rules.FRAMEWORK_NOTE in report


async def test_nothing_to_fix_and_unknown_code_end_without_a_pr():
    url = f"{BASE}/login"
    fixed_page = PAGE.replace("<html>", '<html lang="en">')
    execution, selection, run = execution_for(
        finding="onpage.lang_missing",
        urls=[url],
        repo={"index.html": PAGE},
        pages={url: fixed_page},
    )
    assert (await execution._resolve_site(run, selection))["reason"] == "already_resolved"
    execution, selection, run = execution_for(
        finding="onpage.lang_missing",
        urls=[url],
        repo={"package.json": '{"dependencies": {"astro": "4"}}', "src/pages/login.astro": "x"},
        pages={url: PAGE},
    )
    assert (await execution._resolve_site(run, selection))["reason"] == "unsupported_source"


async def test_an_open_pr_on_the_same_file_stops_a_duplicate():
    url = f"{BASE}/login"
    execution, selection, run = execution_for(
        finding="indexation.utility_pages_indexable",
        urls=[url],
        repo={"login/index.html": PAGE},
        pages={url: PAGE},
        overlap=["login/index.html"],
    )
    prepared = await execution._resolve_site(run, selection)
    assert prepared["reason"] == "open_pr_overlap"
    assert prepared["site_fix"]["pages"] == {"login/index.html": url}


async def test_sitemap_fix_names_the_file_and_the_urls_it_serves():
    execution, selection, run = execution_for(
        finding="sitemap.non_indexable_urls",
        urls=[f"{BASE}/login", f"{BASE}/gone"],
        repo={"public/sitemap.xml": SITEMAP},
        site_files={
            f"{BASE}/robots.txt": (ROBOTS + f"Sitemap: {BASE}/sitemap.xml\n", 200),
            f"{BASE}/sitemap.xml": (SITEMAP, 200),
        },
    )
    prepared = await execution._resolve_site(run, selection)
    assert prepared["site_fix"]["expected"] == {"remove": [f"{BASE}/login"]}
    assert prepared["originals"] == {"public/sitemap.xml": SITEMAP}
    removed = SITEMAP.replace(f"  <url><loc>{BASE}/login</loc></url>\n", "")
    technical_fix.validate_manifest(
        patch(prepared, [{"path": "public/sitemap.xml", "content": removed}]), prepared
    )


# --- The live check after merge --------------------------------------------------------------


def live_fixture(*, merged, served_text):
    prepared = {
        "site_fix": {
            "kind": "robots_sitemap_line",
            "target": "robots",
            "mode": "static",
            "expected": {"sitemaps": [f"{BASE}/sitemap.xml"]},
            "observations": [{"url": f"{BASE}/robots.txt", "needed": True, "page_url": None}],
        },
        "target": {"host": HOST, "site_hosts": [HOST]},
        "reason": None,
    }
    saved = {}

    async def get_effect(key):
        return SimpleNamespace(status="completed", result=prepared)

    async def fetchrow(query, execution_key, operation):
        value = saved.get(execution_key)
        return {"result": value} if value is not None else None

    async def execute(query, execution_key, operation, value):
        saved[execution_key] = value

    database = SimpleNamespace(
        get_effect=AsyncMock(side_effect=get_effect),
        get_integration_call_receipt=AsyncMock(
            return_value=SimpleNamespace(
                status="completed",
                response_summary={
                    "repository": "owner/site",
                    "number": 7,
                    "url": "https://github.com/owner/site/pull/7",
                },
            )
        ),
        pool=SimpleNamespace(fetchrow=fetchrow, execute=execute),
    )
    integrations = SimpleNamespace(
        github_pull_request_state=AsyncMock(
            return_value={
                "state": "closed" if merged else "open",
                "merged": merged,
                "merged_at": "2026-09-29T10:00:00+00:00" if merged else None,
            }
        )
    )
    live = LiveRecheck(
        database=database,
        integrations=integrations,
        fetch_file=AsyncMock(side_effect=lambda url, **_: served(url, served_text)),
    )
    run = SimpleNamespace(id=uuid4(), project_id=uuid4(), executor="codex.procedure")
    return live, run, integrations, saved


async def test_an_open_pr_waits_and_a_merged_fix_is_confirmed_on_the_live_site():
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    live, run, integrations, _ = live_fixture(merged=False, served_text=ROBOTS)
    view = await live.view(run, check=True, now=now)
    assert view["state"] == "waiting_for_merge"
    # Within ten minutes nothing is asked again.
    await live.view(run, check=True, now=now + timedelta(minutes=5))
    assert integrations.github_pull_request_state.await_count == 1

    fixed = ROBOTS + f"Sitemap: {BASE}/sitemap.xml\n"
    live, run, _, _ = live_fixture(merged=True, served_text=fixed)
    assert (await live.view(run, check=True, now=now))["state"] == "fixed"

    live, run, _, _ = live_fixture(merged=True, served_text=ROBOTS)
    assert (await live.view(run, check=True, now=now))["state"] == "waiting_for_deploy"
    later = await live.view(run, check=True, now=now + timedelta(days=2))
    assert later["state"] == "still_broken"


def test_a_closed_unmerged_pr_is_said_plainly():
    assert present({"closed": True}, datetime.now(UTC))["state"] == "closed_unmerged"


async def test_older_policies_and_other_runs_have_no_live_check():
    live, run, _, _ = live_fixture(merged=True, served_text=ROBOTS)
    assert await live.view(SimpleNamespace(**{**vars(run), "executor": "organic.audit"})) is None
    live.db.get_effect = AsyncMock(
        return_value=SimpleNamespace(status="completed", result={"policy": "html-metadata-v3"})
    )
    assert await live.view(run, check=True) is None


# --- The traffic system's technical step ------------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "chosen"),
    [(technical_fix.SITE_POLICY, "oa_urgent"), (technical_fix.POLICY, "oa_first")],
)
async def test_the_system_takes_the_most_urgent_finding_only_under_v4(monkeypatch, policy, chosen):
    from tin_lite.organic_system_activities import OrganicSystemActivities

    facts_ = {"steps": [{"step": "audit", "status": "succeeded", "run_id": str(uuid4())}]}
    monkeypatch.setattr(
        "tin_lite.organic_system_activities.system_facts", AsyncMock(return_value=facts_)
    )

    async def inspect(service, **kwargs):
        return {
            "source": {"audit_revision": "a" * 40},
            "findings": [
                {
                    "finding": {
                        "id": "oa_first",
                        "check_id": "onpage.lang_missing",
                        "priority": "quick_win",
                    },
                    "source_eligible": True,
                },
                {
                    "finding": {
                        "id": "oa_urgent",
                        "check_id": "robots.blocks_important_pages",
                        "priority": "critical",
                    },
                    "source_eligible": True,
                },
            ],
        }

    monkeypatch.setattr("tin_lite.organic_system_activities.TechnicalFixSources.inspect", inspect)
    activities = OrganicSystemActivities(
        database=None, storage=None, settings=None, integrations=None
    )
    definition = {"procedure": {"output": {"repair_policy": policy}}}
    monkeypatch.setattr(
        activities, "saved", AsyncMock(return_value={"definitions": {"technical": definition}})
    )
    run = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        input={
            "technical_fix": True,
            "expected_repository": "owner/site",
            "repository_serves_site": True,
        },
    )
    inputs, reason = await activities._child_inputs(run, "technical")
    assert reason is None and inputs["finding_id"] == chosen


# --- Disposable Postgres: preparation receipts and PR delivery ---------------------------------


async def test_a_v4_robots_fix_prepares_once_and_delivers_one_checked_pr(
    publication_db, monkeypatch
):
    source = site_source()
    source.service.supported_checks = technical_fix.supported_checks(technical_fix.SITE_POLICY)
    source.selection["finding_id"] = next(
        row["id"]
        for row in source.inventory["findings"]
        if row["check_id"] == "robots.sitemap_reference_missing"
    )
    f = await technical_fixture(publication_db, monkeypatch, source=source)
    f.integrations.github_repository_bundle.return_value = SimpleNamespace(
        archive=archive({"public/robots.txt": ROBOTS}), complete=True
    )
    f.integrations.github_open_pull_requests.return_value = SimpleNamespace(
        truncated=False, changed_paths=(), document=b"{}"
    )
    site = {f"{BASE}/robots.txt": ROBOTS, f"{BASE}/sitemap.xml": SITEMAP}
    f.execution.fetch_file = AsyncMock(side_effect=lambda url, **_: served(url, site[url]))

    assert await f.execution.prepare(f.run, policy=technical_fix.SITE_POLICY) is False
    assert await f.execution.prepare(f.run, policy=technical_fix.SITE_POLICY) is False
    prepared = (await f.db.get_effect(f"technical:{f.run.id}:prepare")).result
    assert prepared["site_fix"]["mode"] == "static"
    assert prepared["originals"] == {"public/robots.txt": ROBOTS}
    reads = f.execution.fetch_file.await_count
    assert reads == 2  # robots.txt and the sitemap it will name, each read once.

    await f.db.pool.execute("UPDATE workflow_runs SET lease_active=true WHERE id=$1", f.run.id)
    await f.db.pool.execute(
        "UPDATE effect_receipts SET result=$2::jsonb WHERE execution_key=$1",
        f"{f.run.id}:procedure_artifact_persist",
        json.dumps({"ephemeral_commit_sha": "e" * 40, "summary": "untrusted model summary"}),
    )
    contract = replace(
        spec(technical_fix.SITE_POLICY),
        receipt_path_template="reports/technical-fix/{run_id}/RESULT.md",
    )
    monkeypatch.setattr(
        f.activities,
        "_pinned_codex_procedure",
        AsyncMock(
            return_value=(
                SimpleNamespace(key="organic.technical_fix", title="Technical fix"),
                contract,
            )
        ),
    )
    proposed = patch(
        prepared,
        [{"path": "public/robots.txt", "content": ROBOTS + f"Sitemap: {BASE}/sitemap.xml\n"}],
    )
    monkeypatch.setattr(
        f.storage,
        "read_procedure_checkpoint",
        AsyncMock(return_value=json.dumps(proposed).encode()),
    )
    monkeypatch.setattr(
        f.storage, "publish_state_document", AsyncMock(return_value=("f" * 40, True))
    )
    monkeypatch.setattr(
        "tin_lite.technical_fix_execution.TechnicalFixExecution", lambda **kwargs: f.execution
    )
    create_pr = AsyncMock(
        return_value=SimpleNamespace(
            url="https://github.com/owner/site/pull/3",
            number=3,
            repository="owner/site",
            branch="tin/fixture",
        )
    )
    f.integrations.github_create_pull_request = create_pr
    f.activities._integrations = f.integrations
    await f.activities.commit_codex_procedure_artifact(str(f.run.id))
    assert create_pr.await_count == 1
    assert create_pr.await_args.kwargs["files"][0].path == "public/robots.txt"
    # The delivery recheck read robots.txt again and found the same bytes.
    assert f.execution.fetch_file.await_count == reads + 1
    saved = await f.db.get_effect(f"{f.run.id}:procedure_canonical_commit")
    assert saved.result["outcome"] == "pull_request"
