"""site-fix-v5: one technical fix repairs every fixable audit finding in one PR; offline."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_organic_audit_findings import files as audit_files
from test_organic_system_db import technical_fixture
from test_procedure_publication import publication_db as publication_db
from test_technical_site_fixes import (
    BASE,
    HOST,
    NEXT_PACKAGE,
    PAGE,
    ROBOTS,
    SITEMAP,
    archive,
    patch,
    served,
    served_page,
    site_source,
)
from test_technical_title_repair import spec

from tin_lite import technical_batch as rules
from tin_lite import technical_fix
from tin_lite import technical_repair_plan as plan
from tin_lite.integrations import GitHubRepositoryBinding
from tin_lite.organic_audit import CHECKS, digest
from tin_lite.technical_fix_execution import TechnicalFixExecution
from tin_lite.technical_fix_live import LiveRecheck, present

SRC = Path(__file__).resolve().parents[1] / "src" / "tin_lite"
FINDING = "oa_" + "1" * 20
NOTE = rules.FRAMEWORK_NOTE


def audit_check_ids():
    found = set()
    for path in SRC.glob("organic_audit*.py"):
        found |= set(re.findall(r'check_id="([a-z_]+\.[a-z_0-9]+)"', path.read_text()))
    return found | {check for _flag, check, *_ in CHECKS} | {"content.buyer_answer_coverage"}


def row(check_id, finding_id=None, *, priority="quick_win", urls=(), **extra):
    finding_id = finding_id or f"oa_{abs(hash(check_id)) % 10**20:020d}"
    return {
        "finding": {
            "id": finding_id,
            "check_id": check_id,
            "issue": check_id,
            "priority": priority,
            "urls": list(urls),
            **extra,
        },
        "affected_urls": list(urls),
        "affected_count": len(urls),
        "source_eligible": True,
    }


# --- The plan ------------------------------------------------------------------------------


def test_every_audit_check_has_a_place():
    ids = audit_check_ids()
    assert len(ids) > 60
    assert {check for check in ids if plan.classify(check) == "unknown"} == set()
    # Copy is never repaired, and every repair says what it changes in plain words.
    assert not set(plan.COPY) & set(plan.REPAIRS)
    assert all(
        repair.change and repair.group in plan.GROUP_TITLES for repair in plan.REPAIRS.values()
    )


def test_v11_audit_findings_name_the_plans_next_step_and_v10_keeps_its_own():
    from test_organic_audit_findings import facts as page_facts
    from test_organic_audit_findings import v10_documents

    from tin_lite.organic_audit import V10_AUDIT_POLICY, V11_AUDIT_POLICY

    crawl = [
        {
            "url": f"{BASE}{path}",
            "resource_type": "html",
            "status_code": 200,
            "meta": {"title": title},
            "checks": {"canonical": True, "no_title": not title, "duplicate_description": True},
        }
        for path, title in (("/", ""), ("/b", "B"))
    ]
    long_title = "A title much too long to show in full on any search results page at all"
    site = {
        "files": audit_files(urls=["/", "/b"]),
        "plan": None,
        "pages": [page_facts("/", h1_count=0, title="Hi"), page_facts("/b", title=long_title)],
        "pages_status": "complete",
        "pagespeed": {"status": "not_configured", "results": []},
    }
    _, _, v11, _ = v10_documents(pages=crawl, site=site, policy=V11_AUDIT_POLICY)
    actions = {item["check_id"]: item["next_action"] for item in v11["findings"]}
    assert actions == {check: plan.next_action(check) for check in actions}
    # Copy goes to the content workflows, never the technical fix.
    assert actions["onpage.title_length"] == actions["metadata.description_duplicate"]
    assert actions["metadata.description_duplicate"] == "content_plan"
    assert actions["metadata.title_missing"] == "technical_fix"
    # A run pinned to the deployed v10 keeps the next steps it was released with.
    _, _, v10, _ = v10_documents(pages=crawl, site=site, policy=V10_AUDIT_POLICY)
    v10_actions = {item["check_id"]: item["next_action"] for item in v10["findings"]}
    assert v10_actions["metadata.description_duplicate"] == "technical_fix"
    assert {plan.next_action(check) for check in plan.MANUAL} == {"manual"}
    assert {plan.next_action(check) for check in plan.NO_CHANGE} == {"review"}


def test_copy_manual_and_review_findings_are_listed_not_repaired():
    planned = plan.build_plan(
        [
            row("onpage.title_length"),
            row("access.ai_crawlers_refused"),
            row("http.redirect"),
            row("aeo.dates_missing"),
        ],
        {},
    )
    assert planned["repairs"] == [] and planned["decisions_needed"] == []
    left = planned["left_out"]
    assert [r["check_id"] for r in left["copy"]] == ["onpage.title_length", "aeo.dates_missing"]
    assert "not invented" in left["copy"][1]["reason"]
    assert "CDN" in left["manual"][0]["reason"]
    assert left["no_change"][0]["check_id"] == "http.redirect"


def test_a_judgment_call_waits_for_an_answer_then_follows_it():
    blocked = row("robots.ai_search_crawlers_blocked", FINDING, priority="high_impact")
    waiting = plan.build_plan([blocked], {})
    [decision] = waiting["decisions_needed"]
    assert decision["id"] == FINDING and decision["suggestion"] == "allow"
    assert [option["value"] for option in decision["options"]] == ["allow", "keep_blocked"]
    assert (
        waiting["repairs"] == [] and waiting["left_out"]["decision_unanswered"][0]["id"] == FINDING
    )
    allowed = plan.build_plan([blocked], {FINDING: "allow"})
    assert allowed["repairs"][0]["decision"] == "allow"
    assert allowed["repairs"][0]["kind"] == "robots_allow_ai_search"
    kept = plan.build_plan([blocked], {FINDING: "keep_blocked"})
    assert kept["repairs"] == [] and kept["left_out"]["decided_keep"][0]["id"] == FINDING
    # A plausible but unusable answer: not one of the options.
    with pytest.raises(ValueError, match="must be one of"):
        plan.build_plan([blocked], {FINDING: "yes please"})


def test_a_merge_decision_offers_the_competing_url_patterns():
    urls = [f"{BASE}/compare/semrush-alternatives", f"{BASE}/alternatives/semrush"]
    finding = row(
        "search.cannibalization",
        FINDING,
        priority="high_impact",
        urls=urls,
        verification={
            "url_patterns": [{"templates": ["/compare/{x}-alternatives", "/alternatives/{x}"]}]
        },
    )
    [decision] = plan.build_plan([finding], {})["decisions_needed"]
    values = [option["value"] for option in decision["options"]]
    assert values == ["/compare/{x}-alternatives", "/alternatives/{x}", "keep_both"]
    merged = plan.build_plan([finding], {FINDING: "/alternatives/{x}"})["repairs"][0]
    assert merged["redirects"] == [
        {"from": f"{BASE}/compare/semrush-alternatives", "to": f"{BASE}/alternatives/semrush"}
    ]


def test_decisions_are_finding_id_equals_choice():
    assert plan.parse_decisions([f"{FINDING}=allow"]) == {FINDING: "allow"}
    for bad in (["allow"], [f"{FINDING}="], ["oa_short=allow"]):
        with pytest.raises(ValueError):
            plan.parse_decisions(bad)


def test_the_plan_keeps_the_most_urgent_findings_at_its_cap():
    rows = [
        row(
            "onpage.image_alt_missing",
            f"oa_{index:020d}",
            priority="critical" if index == 34 else "long_term",
        )
        for index in range(35)
    ]
    planned = plan.build_plan(rows, {})
    assert len(planned["repairs"]) == plan.MAX_FINDINGS
    assert planned["repairs"][0]["finding_id"] == f"oa_{34:020d}"
    assert len(planned["left_out"]["over_cap"]) == 5


# --- Delivery checks -----------------------------------------------------------------------


def test_dependencies_ci_deploy_settings_and_secrets_are_off_limits():
    for path in (
        "package.json",
        "pnpm-lock.yaml",
        ".github/workflows/deploy.yml",
        ".env.local",
        "Dockerfile",
        "fly.toml",
        ".gitmodules",
        "bun.lock",
        "npm-shrinkwrap.json",
        ".npmrc",
        ".yarnrc.yml",
        "pnpm-workspace.yaml",
        "wrangler.json",
        "apps/web/.dev.vars",
    ):
        assert rules.blocked(path), path
    assert rules.blocked(".dev.vars") == "secrets"
    assert rules.blocked("app/robots.ts") is None
    assert rules.blocked("vercel.json") is None  # its redirect list only; checked below.


def test_a_host_config_may_change_only_its_redirect_list():
    before = json.dumps({"cleanUrls": True, "redirects": []}, indent=2)
    redirect = json.dumps(
        {"cleanUrls": True, "redirects": [{"source": "/a", "destination": "/b"}]}, indent=2
    )
    assert rules.check_bounds(
        [{"path": "vercel.json", "content": redirect}], {"vercel.json": before}
    )
    sneaky = json.dumps({"cleanUrls": False, "redirects": []}, indent=2)
    with pytest.raises(ValueError, match="Only the redirect list"):
        rules.check_bounds([{"path": "vercel.json", "content": sneaky}], {"vercel.json": before})


def test_a_new_host_config_holds_only_redirects():
    redirects = {"redirects": [{"source": "/a", "destination": "/b", "permanent": True}]}
    assert rules.check_bounds(
        [{"path": "vercel.json", "content": json.dumps(redirects)}], {"vercel.json": None}
    )
    toml = '[[redirects]]\nfrom = "/a"\nto = "/b"\nstatus = 301\n'
    assert rules.check_bounds([{"path": "netlify.toml", "content": toml}], {})
    # Plausible but unusable: a new file that also sets headers, builds or rewrites.
    for path, content in (
        ("vercel.json", json.dumps({**redirects, "buildCommand": "curl x | sh"})),
        ("vercel.json", json.dumps({"headers": []})),
        ("netlify.toml", toml + '[build]\ncommand = "make"\n'),
        ("netlify.toml", '[build]\ncommand = "make"\n'),
    ):
        with pytest.raises(ValueError, match="redirect list"):
            rules.check_bounds([{"path": path, "content": content}], {})


def test_bounds_on_files_lines_and_new_files():
    many = [{"path": f"app/p{index}/page.tsx", "content": "x\n"} for index in range(21)]
    with pytest.raises(ValueError, match="one to 20 files"):
        rules.check_bounds(many, {})
    huge = [{"path": "app/big.ts", "content": "x\n" * 900}]
    with pytest.raises(ValueError):
        rules.check_bounds(huge, {"app/big.ts": "y\n"})


def test_served_robots_and_sitemap_change_only_as_their_findings_call_for():
    rules.verify_robots(
        ROBOTS, ROBOTS + f"Sitemap: {BASE}/sitemap.xml\n", {"sitemaps": [f"{BASE}/sitemap.xml"]}
    )
    # A plausible but unusable patch: it adds the Sitemap line and also closes the site.
    with pytest.raises(ValueError, match="alters what"):
        rules.verify_robots(
            ROBOTS,
            "User-agent: *\nDisallow: /\n" + f"Sitemap: {BASE}/sitemap.xml\n",
            {"sitemaps": [f"{BASE}/sitemap.xml"]},
        )
    removed = SITEMAP.replace(f"  <url><loc>{BASE}/login</loc></url>\n", "")
    rules.verify_sitemap(SITEMAP, removed, {"remove": [f"{BASE}/login"], "add": []})
    with pytest.raises(ValueError):
        rules.verify_sitemap(SITEMAP, removed.replace(f"{BASE}/</loc>", f"{BASE}/x</loc>"), {})


def test_a_served_page_with_several_findings_keeps_its_visible_text():
    after = PAGE.replace("<html>", '<html lang="en">').replace(
        "</head>", '<meta name="robots" content="noindex">\n</head>'
    )
    rules.verify_html(PAGE, after, ["html_lang", "html_noindex"], f"{BASE}/login")
    rewritten = after.replace("<form></form>", "<p>Welcome back!</p><form></form>")
    with pytest.raises(ValueError, match="visible text"):
        rules.verify_html(PAGE, rewritten, ["html_lang", "html_noindex"], f"{BASE}/login")


SIGNUP = (
    "<!doctype html>\n<html>\n<head>\n<title>Pricing</title>\n"
    '<link rel="stylesheet" href="/site.css">\n</head>\n<body>\n'
    '<a href="/signup">Sign up</a>\n<form action="/subscribe"></form>\n</body>\n</html>\n'
)


def test_a_page_with_several_findings_changes_only_their_tags():
    url = f"{BASE}/pricing"
    kinds = ["html_lang", "html_description"]
    fixed = SIGNUP.replace("<html>", '<html lang="en">').replace(
        "</head>", '<meta name="description" content="Plans and prices.">\n</head>'
    )
    rules.verify_html(SIGNUP, fixed, kinds, url)
    # Each probe keeps the visible text and stays under the line cap, and fixes both findings.
    head, body = "</head>", "</body>"
    probes = [
        ("robots meta tag", fixed.replace(head, '<meta name="robots" content="noindex">\n' + head)),
        (
            "script",
            fixed.replace(body, '<script src="https://cdn.example.net/t.js"></script>\n' + body),
        ),
        ("script", fixed.replace(body, "<script>fetch('/x')</script>\n" + body)),
        ("targets", fixed.replace('href="/signup"', 'href="https://other.example/s"')),
        ("targets", fixed.replace('action="/subscribe"', 'action="/elsewhere"')),
        ("tags its findings", fixed.replace('href="/site.css"', 'href="/other.css"')),
        (
            "tags its findings",
            fixed.replace(head, '<meta http-equiv="refresh" content="0; url=/x">\n' + head),
        ),
    ]
    for reason, after in probes:
        with pytest.raises(ValueError, match=reason):
            rules.verify_html(SIGNUP, after, kinds, url)
    # A noindex finding may add its robots tag, and only noindex, follow or nofollow.
    noindex = fixed.replace("</head>", '<meta name="robots" content="noindex">\n</head>')
    rules.verify_html(SIGNUP, noindex, [*kinds, "html_noindex"], url)
    with pytest.raises(ValueError, match="noindex"):
        rules.verify_html(
            SIGNUP,
            noindex.replace('content="noindex"', 'content="noindex, noarchive"'),
            [*kinds, "html_noindex"],
            url,
        )


def prepared_batch(strict=None, overlap=()):
    binding = GitHubRepositoryBinding(uuid4(), 123, 456, "owner/site", "main", "b" * 40)
    return {
        "repository_binding": asdict(binding),
        "batch": {
            "repairs": [],
            "left_out": {},
            "strict_files": strict or {},
            "overlap_paths": list(overlap),
        },
    }


def test_changes_tin_cannot_build_must_say_so_and_open_prs_are_left_alone():
    prepared = prepared_batch(overlap=["app/layout.tsx"])
    layout = {"path": "app/login/page.tsx", "content": "export const x = 2;\n"}
    originals = {"app/login/page.tsx": "export const x = 1;\n"}
    with pytest.raises(ValueError, match="couldn't build"):
        rules.validate(patch(prepared, [layout], body="Fixes it."), prepared, originals)
    rules.validate(patch(prepared, [layout], body=f"Fixes it. {NOTE}"), prepared, originals)
    touched = {"path": "app/layout.tsx", "content": "export default 1;\n"}
    with pytest.raises(ValueError, match="open pull request"):
        rules.validate(
            patch(prepared, [touched], body=NOTE),
            prepared,
            {"app/layout.tsx": "export default 0;\n"},
        )
    deps = {"path": "package.json", "content": "{}\n"}
    with pytest.raises(ValueError, match="dependencies"):
        rules.validate(patch(prepared, [deps], body=NOTE), prepared, {"package.json": "{ }\n"})


# --- Preparation ---------------------------------------------------------------------------


def batch_execution(*, repairs, repo, site_files=None, pages=None, left_out=None):
    binding = GitHubRepositoryBinding(uuid4(), 123, 456, "owner/site", "main", "b" * 40)
    site_files = site_files or {}
    pages = pages or {}

    async def fetch_file(url, *, host, kind):
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
                truncated=False, changed_paths=("README.md",), document=b"{}"
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
        "plan": {"repairs": repairs, "left_out": left_out or {}, "decisions_needed": []},
        "repository_binding": asdict(binding),
        "input_sha256": digest({}),
    }
    return execution, selection, SimpleNamespace(id=uuid4(), project_id=uuid4())


def repair(check_id, *, urls=(), finding_id=None, **extra):
    planned = plan.build_plan([row(check_id, finding_id, urls=urls)], {})["repairs"]
    return {**planned[0], **extra}


async def test_preparation_drops_fixed_findings_and_names_files_the_diff_can_prove():
    login = f"{BASE}/login"
    fixed_page = PAGE.replace("<html>", '<html lang="en">')
    repairs = [
        repair("robots.sitemap_reference_missing", finding_id="oa_" + "2" * 20),
        repair("indexation.utility_pages_indexable", urls=[login], finding_id="oa_" + "3" * 20),
        repair("onpage.lang_missing", urls=[f"{BASE}/about"], finding_id="oa_" + "4" * 20),
        repair("discovery.possible_orphan", urls=[f"{BASE}/pricing"], finding_id="oa_" + "5" * 20),
    ]
    execution, selection, run = batch_execution(
        repairs=repairs,
        repo={"public/robots.txt": ROBOTS, "login.html": PAGE, "package.json": NEXT_PACKAGE},
        site_files={
            f"{BASE}/robots.txt": (ROBOTS, 200),
            f"{BASE}/sitemap.xml": (SITEMAP, 200),
        },
        pages={login: PAGE, f"{BASE}/about": fixed_page},
    )
    prepared = await execution._resolve_batch(run, selection)
    assert prepared["reason"] is None and prepared["policy"] == technical_fix.BATCH_POLICY
    batch = prepared["batch"]
    # /about already declares its language, so that finding is dropped as fixed.
    assert [r["id"] for r in batch["left_out"]["already_resolved"]] == ["oa_" + "4" * 20]
    assert [r["kind"] for r in batch["repairs"]] == [
        "robots_sitemap_line",
        "html_noindex",
        "internal_links",
    ]
    assert batch["strict_files"]["public/robots.txt"]["expected"] == {
        "sitemaps": [f"{BASE}/sitemap.xml"]
    }
    assert batch["strict_files"]["login.html"]["kinds"] == ["html_noindex"]
    assert batch["overlap_paths"] == ["README.md"]
    assert len(json.dumps(prepared, default=str)) < 70_000

    # Delivery: exact served files checked from the diff; the rest needs the sentence.
    fixed_robots = ROBOTS + f"Sitemap: {BASE}/sitemap.xml\n"
    noindex = PAGE.replace("</head>", '<meta name="robots" content="noindex">\n</head>')
    link = {"path": "app/page.tsx", "content": "<a href='/pricing'>Pricing</a>\n"}
    execution.integrations.github_repository_bundle.return_value = SimpleNamespace(
        archive=archive(
            {
                "public/robots.txt": ROBOTS,
                "login.html": PAGE,
                "app/page.tsx": "<a href='/'>Home</a>\n",
            }
        ),
        complete=True,
    )
    good = patch(
        prepared,
        [
            {"path": "public/robots.txt", "content": fixed_robots},
            {"path": "login.html", "content": noindex},
            link,
        ],
        body=f"Fixes three findings. {NOTE}",
    )
    await execution._validate_batch_delivery(run, good, prepared)
    assert prepared["batch"]["unproven"] is True
    bad = patch(
        prepared,
        [{"path": "login.html", "content": noindex.replace("<form></form>", "<p>Hi</p>")}],
        body=NOTE,
    )
    with pytest.raises(ValueError):
        await execution._validate_batch_delivery(run, bad, prepared)


SKIPPED_MEDIA = (
    {"path": "public/opensource/hero.mp4", "size": 3_378_075, "reason": "video"},
    {"path": "public/euphony/assets/main-LKI_ICf3.js", "size": 2_678_607, "reason": "built_asset"},
)
LARGE_SOURCE = {"path": "src/data/posts.json", "size": 2_400_000, "reason": "too_large"}


async def test_a_snapshot_without_its_large_media_prepares_and_delivers(monkeypatch):
    """Runs 43b99efd and 721f6a8d stopped here on tin-web's hero video and bundles."""
    monkeypatch.setattr("tin_lite.technical_build_profile.ARCHIVE_MAX_BYTES", 2_000)
    login = f"{BASE}/login"
    repairs = [
        repair("indexation.utility_pages_indexable", urls=[login], finding_id="oa_" + "3" * 20)
    ]
    # More source than the read bound allows: only the served text files are read.
    repo = {"login.html": PAGE, "app/data.ts": "x" * 5_000, "package.json": NEXT_PACKAGE}
    execution, selection, run = batch_execution(repairs=repairs, repo=repo, pages={login: PAGE})
    execution.integrations.github_repository_bundle.return_value = SimpleNamespace(
        archive=archive(repo), complete=True, skipped=SKIPPED_MEDIA, missing=()
    )
    prepared = await execution._resolve_batch(run, selection)
    assert prepared["reason"] is None and "repository_missing" not in prepared
    assert prepared["batch"]["strict_files"]["login.html"]["kinds"] == ["html_noindex"]
    assert technical_fix.preparation_failed(prepared) is False

    noindex = PAGE.replace("</head>", '<meta name="robots" content="noindex">\n</head>')
    await execution._validate_batch_delivery(
        run, patch(prepared, [{"path": "login.html", "content": noindex}], body=NOTE), prepared
    )
    # A fix can't write over a file Tin never read.
    video = {"path": "public/opensource/hero.mp4", "content": "not a video\n"}
    with pytest.raises(ValueError, match="hero.mp4 is a large media or built file"):
        await execution._validate_batch_delivery(run, patch(prepared, [video], body=NOTE), prepared)


async def test_a_large_source_file_ends_the_run_failed_and_names_the_file():
    login = f"{BASE}/login"
    repairs = [
        repair("indexation.utility_pages_indexable", urls=[login], finding_id="oa_" + "3" * 20)
    ]
    execution, selection, run = batch_execution(
        repairs=repairs, repo={"login.html": PAGE}, pages={login: PAGE}
    )
    execution.integrations.github_repository_bundle.return_value = SimpleNamespace(
        archive=archive({"login.html": PAGE}),
        complete=False,
        skipped=SKIPPED_MEDIA,
        missing=(LARGE_SOURCE,),
    )
    prepared = await execution._resolve_batch(run, selection)
    assert prepared["reason"] == "repository_incomplete"
    assert prepared["repository_missing"] == [LARGE_SOURCE]
    execution.integrations.github_open_pull_requests.assert_not_awaited()
    assert technical_fix.preparation_failed(prepared) is True
    assert technical_fix.preparation_summary(prepared) == (
        "Tin couldn't read every file in the repository: src/data/posts.json (2.4 MB, over "
        "the 2 MB limit for files Tin reads). No change proposed."
    )
    text = rules.report(prepared, reason="repository_incomplete").decode()
    assert "Files Tin couldn't read:" in text and "- src/data/posts.json (2.4 MB" in text
    # Many unread files: the first few are named, with the true count of the rest.
    many = [{**LARGE_SOURCE, "path": f"src/data/part-{index}.json"} for index in range(25)]
    crowded = {
        **prepared,
        **technical_fix.missing_record(SimpleNamespace(complete=False, missing=many)),
    }
    assert crowded["repository_missing_count"] == 25
    assert len(crowded["repository_missing"]) == technical_fix.MAX_NAMED_MISSING
    assert technical_fix.preparation_summary(crowded).endswith("; and 22 more. No change proposed.")
    assert "- and 5 more" in rules.report(crowded, reason="repository_incomplete").decode()
    # Delivery refuses the same snapshot and says which file.
    with pytest.raises(ValueError, match=r"src/data/posts\.json \(2\.4 MB"):
        await execution._validate_batch_delivery(
            run, patch(prepared, [{"path": "login.html", "content": PAGE}], body=NOTE), prepared
        )


async def test_nothing_left_to_fix_ends_without_a_pr():
    execution, selection, run = batch_execution(repairs=[], repo={})
    prepared = await execution._resolve_batch(run, selection)
    assert prepared["reason"] == "nothing_to_fix"
    execution.integrations.github_repository_bundle.assert_not_awaited()
    text = rules.report(prepared, reason="nothing_to_fix").decode()
    assert "Nothing in this audit is left for a technical fix" in text


def test_the_report_groups_fixes_and_lists_what_stayed_out():
    prepared = {
        "source": {"audit_run_id": "a", "audit_revision": "b" * 40},
        "repository_binding": {"repository": "owner/site", "head_sha": "c" * 40},
        "batch": {
            "repairs": [
                repair("indexation.utility_pages_indexable", urls=[f"{BASE}/login"]),
                repair("onpage.image_alt_missing", urls=[f"{BASE}/"]),
            ],
            "left_out": {
                "copy": [{"id": "oa_x", "issue": "Titles too long", "reason": "copy"}],
                "manual": [{"id": "oa_y", "issue": "CDN blocks AI", "reason": "CDN settings"}],
            },
            "unproven": True,
        },
    }
    text = rules.report(
        prepared, pull_request=SimpleNamespace(url="https://github.com/o/s/pull/9")
    ).decode()
    assert "fixes 2 audit findings" in text
    assert text.index("### Indexing directives") < text.index("### Accessibility")
    assert "## Copy, left to the content workflows" in text and "## Manual steps" in text
    assert NOTE in text


# --- The live check ------------------------------------------------------------------------


async def test_each_finding_gets_its_own_live_state_after_merge():
    now = datetime(2026, 9, 30, tzinfo=UTC)
    login = f"{BASE}/login"
    repairs = [
        repair("indexation.utility_pages_indexable", urls=[login], finding_id="oa_" + "6" * 20),
        repair("onpage.image_alt_missing", urls=[f"{BASE}/"], finding_id="oa_" + "7" * 20),
        repair("discovery.possible_orphan", urls=[f"{BASE}/p"], finding_id="oa_" + "8" * 20),
    ]
    prepared = {
        "target": {"url": f"{BASE}/", "host": HOST, "site_hosts": [HOST]},
        "batch": {"repairs": repairs},
        "reason": None,
    }
    pages = {
        login: PAGE.replace("</head>", '<meta name="robots" content="noindex">\n</head>'),
        f"{BASE}/": '<html lang="en"><body><img src="a.png"></body></html>',
    }
    live = LiveRecheck(
        database=None,
        integrations=None,
        fetch=AsyncMock(side_effect=lambda url, **_: served_page(url, pages[url])),
    )
    rows = await live._live_batch(prepared)
    assert [r["live"] for r in rows] == ["fixed", "not_fixed", "next_audit"]
    record = {
        "merged": True,
        "merged_at": (now - timedelta(hours=30)).isoformat(),
        "findings": rows,
    }
    view = present({**record, "live": "not_fixed"}, now)
    assert [r["state"] for r in view["findings"]] == ["fixed", "still_broken", "next_audit"]


async def test_a_read_right_after_the_merge_sees_it():
    # Sheepdogs: checked at 00:47, merged at 00:48:57, read again inside ten minutes. Until the
    # merge every read asks GitHub; only reading the live site waits ten minutes.
    now = datetime(2026, 10, 2, 0, 50, tzinfo=UTC)
    merged_at = "2026-10-02T00:48:57+00:00"
    state = AsyncMock(return_value={"merged": True, "merged_at": merged_at, "state": "closed"})
    live = LiveRecheck(database=None, integrations=SimpleNamespace(github_pull_request_state=state))
    unmerged = {
        "pull_request": {"repository": "owner/site", "number": 1},
        "checked_at": (now - timedelta(minutes=3)).isoformat(),
    }
    assert live._due(unmerged, now)
    prepared = {"target": {"url": f"{BASE}/", "host": HOST}, "batch": {"repairs": []}}
    record = await live._check(SimpleNamespace(project_id=uuid4()), prepared, unmerged, now)
    assert record["merged"] and record["merged_at"] == merged_at
    state.assert_awaited_once()
    # Once merged, the live site is read at most every ten minutes.
    assert not live._due(record, now + timedelta(minutes=9))
    assert live._due(record, now + timedelta(minutes=10))


# --- The traffic system --------------------------------------------------------------------


async def test_the_system_passes_the_whole_audit_under_v5(monkeypatch):
    from tin_lite.organic_system_activities import OrganicSystemActivities

    audit = {"step": "audit", "status": "succeeded", "run_id": str(uuid4())}
    audit["canonical_commit_sha"] = "a" * 40
    monkeypatch.setattr(
        "tin_lite.organic_system_activities.system_facts",
        AsyncMock(return_value={"steps": [audit]}),
    )
    preview = {
        "source": {"audit_revision": "a" * 40},
        "plan": {"repairs": [{"finding_id": FINDING}]},
    }
    batch = AsyncMock(return_value=preview)
    monkeypatch.setattr("tin_lite.organic_system_activities.TechnicalFixSources.batch", batch)
    activities = OrganicSystemActivities(
        database=None, storage=None, settings=None, integrations=None
    )
    definition = {"procedure": {"output": {"repair_policy": technical_fix.BATCH_POLICY}}}
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
    assert reason is None and inputs["finding_ids"] == [] and inputs["decisions"] == []
    assert "finding_id" not in inputs and batch.await_args.kwargs["bind"] is False
    preview["plan"]["repairs"] = []
    assert await activities._child_inputs(run, "technical") == (None, "no_eligible_findings")


# --- The preview on a real audit, and one run on disposable Postgres ------------------------


def batch_source(robots=ROBOTS):
    # Title length and the other page basics are audit policy v11 checks.
    source = site_source(
        policy="organic-audit-v11", files=audit_files(robots=robots, urls=["/", "/login"])
    )
    source.service.supported_checks = technical_fix.supported_checks(technical_fix.BATCH_POLICY)
    source.service.batch_mode = True
    return source


async def test_the_preview_sorts_a_real_audit_and_takes_decisions():
    blocking = "User-agent: OAI-SearchBot\nDisallow: /\n\n" + ROBOTS
    source = batch_source(robots=blocking)
    args = {
        "project_id": source.project.id,
        "audit_run_id": source.run.id,
        "audit_revision": source.run.canonical_commit_sha,
        "expected_repository": "owner/site",
        "repository_serves_site": True,
        "bind": False,
    }
    preview = await source.service.batch(**args)
    kinds = {r["kind"] for r in preview["plan"]["repairs"]}
    assert {"html_noindex", "html_lang", "robots_sitemap_line"} <= kinds
    [decision] = preview["decisions_needed"]
    assert decision["finding"]["check_id"] == "robots.ai_search_crawlers_blocked"
    assert preview["ask"]["how_to_answer"].startswith("Answer each decision from the codebase")
    assert {r["check_id"] for r in preview["plan"]["left_out"]["copy"]} >= {"onpage.title_length"}
    answered = await source.service.batch(**args, decisions=[f"{decision['id']}=allow"])
    assert "robots_allow_ai_search" in {r["kind"] for r in answered["plan"]["repairs"]}
    assert answered["decisions_needed"] == []


async def test_a_v5_batch_that_cannot_read_the_repository_fails_with_the_file(
    publication_db, monkeypatch
):
    source = batch_source()
    source.selection["finding_id"] = next(
        row["id"]
        for row in source.inventory["findings"]
        if row["check_id"] == "robots.sitemap_reference_missing"
    )
    f = await technical_fixture(publication_db, monkeypatch, source=source)
    preview = await source.service.batch(
        project_id=source.project.id,
        audit_run_id=source.run.id,
        audit_revision=source.run.canonical_commit_sha,
        expected_repository="owner/site",
        repository_serves_site=True,
    )
    monkeypatch.setattr(
        "tin_lite.technical_fix_execution.TechnicalFixSources.batch",
        AsyncMock(return_value=preview),
    )
    inputs = {
        "audit_run_id": str(source.run.id),
        "audit_revision": source.run.canonical_commit_sha,
        "finding_ids": [],
        "decisions": [],
        "expected_repository": "owner/site",
        "repository_serves_site": True,
        "context": "",
    }
    await f.db.pool.execute(
        "UPDATE workflow_runs SET input=$2::jsonb WHERE id=$1", f.run.id, json.dumps(inputs)
    )
    f.run = await f.db.get_run(f.run.id)
    repo = {"public/robots.txt": ROBOTS, "login.html": PAGE, "package.json": NEXT_PACKAGE}
    f.integrations.github_repository_bundle.return_value = SimpleNamespace(
        archive=archive(repo), complete=False, skipped=SKIPPED_MEDIA, missing=(LARGE_SOURCE,)
    )
    site = {f"{BASE}/robots.txt": ROBOTS, f"{BASE}/sitemap.xml": SITEMAP}
    f.execution.fetch_file = AsyncMock(side_effect=lambda url, **_: served(url, site[url]))
    f.execution.fetch = AsyncMock(side_effect=lambda url, **_: served_page(url, PAGE))

    assert await f.execution.prepare(f.run, policy=technical_fix.BATCH_POLICY) is True
    run = await f.db.get_run(f.run.id)
    assert run.status.value == "failed"
    assert "src/data/posts.json (2.4 MB, over the 2 MB limit" in run.error_message
    f.integrations.github_open_pull_requests.assert_not_awaited()
    report = await f.storage.read_canonical_artifact(
        repo_id=(await f.db.get_project(run.project_id)).state_repo_id,
        commit_sha=run.canonical_commit_sha,
        path=run.artifact_path,
    )
    assert "Tin couldn't read every file in the repository" in report.decode()
    assert "- src/data/posts.json (2.4 MB" in report.decode()


async def test_a_v5_batch_prepares_once_and_delivers_one_checked_pr(publication_db, monkeypatch):
    source = batch_source()
    source.selection["finding_id"] = next(
        row["id"]
        for row in source.inventory["findings"]
        if row["check_id"] == "robots.sitemap_reference_missing"
    )
    f = await technical_fixture(publication_db, monkeypatch, source=source)
    preview = await source.service.batch(
        project_id=source.project.id,
        audit_run_id=source.run.id,
        audit_revision=source.run.canonical_commit_sha,
        expected_repository="owner/site",
        repository_serves_site=True,
    )
    monkeypatch.setattr(
        "tin_lite.technical_fix_execution.TechnicalFixSources.batch",
        AsyncMock(return_value=preview),
    )
    inputs = {
        "audit_run_id": str(source.run.id),
        "audit_revision": source.run.canonical_commit_sha,
        "finding_ids": [],
        "decisions": [],
        "expected_repository": "owner/site",
        "repository_serves_site": True,
        "context": "",
    }
    await f.db.pool.execute(
        "UPDATE workflow_runs SET input=$2::jsonb WHERE id=$1", f.run.id, json.dumps(inputs)
    )
    f.run = await f.db.get_run(f.run.id)
    repo = {"public/robots.txt": ROBOTS, "login.html": PAGE, "package.json": NEXT_PACKAGE}
    f.integrations.github_repository_bundle.return_value = SimpleNamespace(
        archive=archive(repo), complete=True
    )
    f.integrations.github_open_pull_requests.return_value = SimpleNamespace(
        truncated=False, changed_paths=(), document=b"{}"
    )
    site = {f"{BASE}/robots.txt": ROBOTS, f"{BASE}/sitemap.xml": SITEMAP}
    f.execution.fetch_file = AsyncMock(side_effect=lambda url, **_: served(url, site[url]))
    f.execution.fetch = AsyncMock(side_effect=lambda url, **_: served_page(url, PAGE))

    assert await f.execution.prepare(f.run, policy=technical_fix.BATCH_POLICY) is False
    assert await f.execution.prepare(f.run, policy=technical_fix.BATCH_POLICY) is False
    prepared = (await f.db.get_effect(f"technical:{f.run.id}:prepare")).result
    assert prepared["batch"]["strict_files"]["public/robots.txt"]["target"] == "robots"
    reads = f.execution.fetch_file.await_count + f.execution.fetch.await_count

    await f.db.pool.execute("UPDATE workflow_runs SET lease_active=true WHERE id=$1", f.run.id)
    await f.db.pool.execute(
        "UPDATE effect_receipts SET result=$2::jsonb WHERE execution_key=$1",
        f"{f.run.id}:procedure_artifact_persist",
        json.dumps({"ephemeral_commit_sha": "e" * 40, "summary": "untrusted model summary"}),
    )
    contract = replace(
        spec(technical_fix.BATCH_POLICY),
        receipt_path_template="reports/technical-fix/{run_id}/RESULT.md",
        output_max_files=20,
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
    noindex = PAGE.replace("<html>", '<html lang="en">').replace(
        "</head>", '<meta name="robots" content="noindex">\n</head>'
    )
    proposed = patch(
        prepared,
        [
            {"path": "public/robots.txt", "content": ROBOTS + f"Sitemap: {BASE}/sitemap.xml\n"},
            {"path": "login.html", "content": noindex},
        ],
        body="Fixes the audit's findings.",
    )
    monkeypatch.setattr(
        f.storage,
        "read_procedure_checkpoint",
        AsyncMock(return_value=json.dumps(proposed).encode()),
    )
    published = AsyncMock(return_value=("f" * 40, True))
    monkeypatch.setattr(f.storage, "publish_state_document", published)
    monkeypatch.setattr(
        "tin_lite.technical_fix_execution.TechnicalFixExecution", lambda **kwargs: f.execution
    )
    create_pr = AsyncMock(
        return_value=SimpleNamespace(
            url="https://github.com/owner/site/pull/4",
            number=4,
            repository="owner/site",
            branch="tin/fixture",
        )
    )
    f.integrations.github_create_pull_request = create_pr
    f.activities._integrations = f.integrations
    await f.activities.commit_codex_procedure_artifact(str(f.run.id))
    assert create_pr.await_count == 1
    assert {item.path for item in create_pr.await_args.kwargs["files"]} == {
        "public/robots.txt",
        "login.html",
    }
    # Both served files were read again at delivery and still matched.
    assert f.execution.fetch_file.await_count + f.execution.fetch.await_count == reads + 2
    report = published.await_args.kwargs["content"].decode()
    assert "## In this pull request" in report and "Copy, left to the content workflows" in report
    assert NOTE not in report  # every changed file was proven from the diff
