"""organic.traffic_snapshot offline: its data file and weekly readout, synthetic providers."""

import datetime as dt
import importlib.util
import json
import re
import sys

import pytest
from loop_workflow_fakes import context, definition, gsc, hogql, load

from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result
from tin_lite.workflow_qualification import Qualification, assess_output

KEY = "organic.traffic_snapshot"
SITE = "northpine.example"
LAST = dt.date(2026, 9, 26)


def daily(last=LAST, days=363):
    return gsc(
        [
            {
                "keys": [str(last - dt.timedelta(days=i))],
                "clicks": 10,
                "impressions": 100,
                "position": 5.0,
            }
            for i in range(days)
        ]
    )


def page(path, clicks, impressions, position=4.0):
    return {
        "keys": [f"https://{SITE}{path}"],
        "clicks": clicks,
        "impressions": impressions,
        "position": position,
    }


def query(path, text, clicks, impressions, position=3.0):
    return {
        "keys": [f"https://{SITE}{path}", text],
        "clicks": clicks,
        "impressions": impressions,
        "position": position,
    }


LANDING = [
    "path",
    "channel",
    "sessions",
    "sessions_prior",
    "pageviews",
    "pageviews_prior",
    "week",
    "week_prior",
    "week_signups",
    "week_prior_signups",
]
REFERRERS = ["channel", "host", "week", "week_prior", "visitors", "week_signups"]
SIGNUPS = [
    "path",
    "channel",
    "assistant",
    "signups",
    "signups_prior",
    "activated",
    "activated_prior",
    "open",
    "week",
    "week_prior",
    "week_activated",
    "week_prior_activated",
    "paid",
    "answered",
    "ai_answers",
]
READING = [
    "path",
    "views",
    "views_prior",
    "readers",
    "reads",
    "median_seconds",
    "engaged",
    "finished",
    "week_timed",
    "week_over15",
    "week_depth",
    "week_deep",
    "week_median",
]


def signup_row(path, channel, signups=0, prior=0, week=0, week_prior=0, **extra):
    row = dict.fromkeys(SIGNUPS, 0)
    row.update(path=path, channel=channel, assistant="", signups=signups, signups_prior=prior)
    row.update(week=week, week_prior=week_prior, **extra)
    return [row[c] for c in SIGNUPS]


def providers(**overrides):
    responses = {
        "G1": daily(),
        "G2": gsc(
            [
                page("/", 150, 1500, 2.0),
                page("/blog/invoice-template", 100, 1200),
                page("/pricing", 30, 300),
                page("/blog/unclicked", 0, 50, 12.0),
            ]
        ),
        "G3": gsc(
            [
                page("/", 140, 1400, 2.0),
                page("/blog/invoice-template", 80, 1000),
                page("/blog/unclicked", 0, 40, 14.0),
                page("/old-page", 20, 200),
            ]
        ),
        "G4": gsc(
            [
                query("/", "northpine", 120, 900),
                query("/", "invoice app", 10, 300),
                query("/blog/invoice-template", "invoice template", 70, 800),
                query("/pricing", "northpine pricing", 20, 150),
                query("/blog/unclicked", "late fee letter", 0, 30, 12.0),
            ]
        ),
        "P1": hogql(
            LANDING,
            [
                ["/", "Search", 120, 100, 300, 250, 40, 25, 6, 1],
                ["/", "Direct", 60, 0, 90, 0, 20, 15, 3, 2],
                ["/blog/invoice-template", "Search", 90, 0, 120, 0, 30, 20, 2, 0],
                ["/blog/invoice-template", "Paid", 5, 0, 5, 0, 2, 1, 0, 0],
                ["/pricing", "Referral", 10, 0, 30, 0, 4, 2, 1, 0],
                ["/", "AI assistants", 6, 0, 8, 0, 6, 3, 2, 1],
            ],
        ),
        "P2": hogql(
            REFERRERS,
            [
                ["AI assistants", "chatgpt.com", 6, 3, 6, 2],
                ["Referral", "blog.partner.example", 3, 1, 3, 1],
                ["Referral", "github.com", 1, 1, 1, 0],
            ],
        ),
        "P3": hogql(
            SIGNUPS,
            [
                signup_row("/", "Search", 8, 0, 5, 1, activated=5, open=1, week_activated=3),
                signup_row("/blog/invoice-template", "Search", 3, 0, 2, 1, activated=1),
                signup_row("/", "Direct", 0, 4, activated_prior=2),
            ],
        ),
        "P4": hogql(
            READING,
            [["/blog/invoice-template", 60, 40, 50, 80, 41.5, 50, 20, 30, 20, 28, 9, 38.0]],
        ),
    }
    responses.update(overrides)
    return responses


INPUTS = {
    "website_hosts": [SITE],
    "exclude_email_domains": [SITE],
    "signup_event": "signed_up",
    "activation_event": "first_invoice_sent",
}


async def snapshot(monkeypatch, inputs=INPUTS, files=None, connections=None, **overrides):
    module = load(KEY, monkeypatch)
    ctx = context(files=files, services=providers(**overrides), connections=connections)
    result = await module.run(ctx, dict(inputs))
    spec = validate_code_definition(definition(KEY))
    validate_code_result(json.dumps(result).encode(), spec)
    return json.loads(result["content"]), ctx


def by_page(data):
    return {row["page"]: row for row in data["pages"]}


def readout(data):
    return data["readout"]["markdown"]


def test_manifest_fits_the_code_contract_and_the_eight_call_limit():
    spec = validate_code_definition(definition(KEY))
    assert spec.output_path == "analytics/traffic-snapshot.json"
    assert spec.model_routes == ()  # numbers only, no model
    # Search Console is required; PostHog is optional, so a site without it still runs.
    required = {
        r["provider_key"]: r["required"] for r in definition(KEY)["integration_requirements"]
    }
    assert required == {"analytics.gsc": True, "analytics.posthog": False}
    services = definition(KEY)["code"]["services"]
    assert {name: b["max_calls"] for name, b in services.items()} == {"gsc": 4, "posthog": 4}
    assert sum(binding["max_calls"] for binding in services.values()) <= 8


def test_channels_match_the_analytics_brief():
    brief = (
        REPOSITORY_ROOT
        / "workflow_packages/product.analytics_brief/skills/product-analytics/CALCULATIONS.md"
    ).read_text()
    snapshot_copy = (REPOSITORY_ROOT / "workflow_packages" / KEY / "channels.py").read_text()
    for name in (
        "CHANNELS",
        "PAID_MEDIUM",
        "EMAIL_MEDIUM",
        "SOCIAL_MEDIUM",
        "AI_DOMAINS",
        "SEARCH_DOMAINS",
        "SOCIAL_DOMAINS",
    ):
        pattern = re.compile(rf"^{name} = (.*?)(?=^\S)", re.M | re.S)
        assert pattern.search(brief).group(1) == pattern.search(snapshot_copy).group(1), name


def test_classify_follows_the_fixed_order(monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    path = REPOSITORY_ROOT / "workflow_packages" / KEY / "channels.py"
    spec = importlib.util.spec_from_file_location("snapshot_channels", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    channels = module.classify
    own = [SITE]
    assert channels("cpc", "google.com", own) == "Paid"
    assert channels("", "www.google.com", own) == "Search"
    assert channels("", "chatgpt.com", own) == "AI assistants"
    assert channels("", "news.ycombinator.com", own) == "Social"
    assert channels("", f"app.{SITE}", own) == "Internal"
    assert channels("", "mail.google.com", own) == "Email"
    assert channels("", "", own, source="chatgpt") == "AI assistants"
    assert channels("", "$direct", own) == "Direct"
    assert channels("", None, own) == "Unknown"
    assert channels("", "somesite.example", own) == "Referral"


async def test_ordinary_snapshot(monkeypatch):
    data, ctx = await snapshot(monkeypatch)
    assert data["schema"] == "tin.traffic_snapshot/1"
    assert data["windows"]["current"] == ["2026-08-30", "2026-09-26"]
    assert data["windows"]["weeks"] == {
        "current": ["2026-09-20", "2026-09-26"],
        "prior": ["2026-09-13", "2026-09-19"],
    }
    totals = data["totals"]
    assert totals["search"]["current"][:2] == [280, 2800]
    assert totals["visits"]["current"]["sessions"] == 291
    assert totals["visits"]["prior"]["sessions"] == 100
    assert totals["signups"]["current"] == 11 and totals["activated"]["current"] == 6
    assert totals["signups"]["prior"] == 4 and totals["activated"]["open"] == 1
    search = data["definitions"]["channels"].index("Search")
    assert totals["search_sessions_per_click"] == round(210 / 280, 4)
    pages = by_page(data)
    home = pages[f"{SITE}/"]
    assert home["search"]["branded_clicks"] == 120
    assert home["search"]["hidden_clicks"] == 150 - 130
    assert home["visits"]["current"]["by_channel"][search] == 120
    assert pages[f"{SITE}/blog/unclicked"]["search"]["queries"][0][0] == "late fee letter"
    assert pages[f"{SITE}/pricing"]["flags"] == ["new_in_search"]
    assert [row[0] for row in data["dropped_pages"]] == [f"{SITE}/old-page"]
    assert pages[f"{SITE}/blog/invoice-template"]["reading"]["median_seconds"] == 41.5
    assert data["definitions"]["brand_source"] == "host"
    assert data["status"] == "complete"
    # Four Search Console and four PostHog reads serve the data and the readout.
    assert [c["step"] for c in ctx.services.calls] == [
        "G1",
        "G2",
        "G3",
        "G4",
        "P1",
        "P2",
        "P3",
        "P4",
    ]


async def test_the_readout_reports_the_week_from_the_same_reads(monkeypatch):
    data, _ = await snapshot(monkeypatch)
    text = readout(data)
    assert data["readout"]["schema"] == "tin.growth_readout/1"
    assert text.startswith("# ")
    assert "7 people signed up this week against 2 the week before" in text
    assert "Ranked by first-touch activated signups" in text
    assert "| Search | 70 vs 45 | 8 vs 1 | 7 | 3 |" in text
    assert "Direct is a measurement gap, not a channel: 20 sessions" in text
    assert "- ChatGPT: 6 sessions vs 3" in text
    assert "github.com (community): 1 sessions vs 1" in text
    assert "Named referrers: blog.partner.example 3, github.com 1." in text
    assert "brand searches 140 clicks; other searches 80; hidden" in text
    assert "| /blog/invoice-template | 60 vs 40 | 50 | 38 s |" in text
    assert "| / | 66 vs 43 | 11 of 66 (16.7%)" in text
    assert "Holm correction" in text and "no model call" in text
    # No audit was attached, and AI assistants sent visitors: measure again.
    assert "**Measure AI visibility again.**" in text
    assert "Owner: organic.audit" in text and "visibility.audit" not in text
    growth = data["growth"]
    assert growth["week"] == ["2026-09-20", "2026-09-26"]
    assert growth["history"]["signups"] == [["2026-09-20", 7]]
    assert growth["calls"]["total sessions"] == "change (up)"


async def test_next_week_reads_back_its_history_and_calls(monkeypatch):
    first, _ = await snapshot(monkeypatch)
    previous = {**first, "growth": {**first["growth"], "history": {"signups": [["2026-09-13", 2]]}}}
    files = {"analytics/traffic-snapshot.json": previous}
    data, _ = await snapshot(monkeypatch, files=files)
    assert data["growth"]["history"]["signups"] == [["2026-09-13", 2], ["2026-09-20", 7]]
    # A rerun in the same week replaces that week's point.
    again, _ = await snapshot(monkeypatch, files={"analytics/traffic-snapshot.json": data})
    assert again["growth"]["history"]["signups"] == [["2026-09-13", 2], ["2026-09-20", 7]]
    assert again["previous"]["generated_at"] == data["generated_at"]


async def test_direct_heavy_signups_route_to_the_signup_source_workflow(monkeypatch):
    rows = [signup_row("/", "Direct", 30, 25, 9, 7)]
    data, _ = await snapshot(monkeypatch, P3=hogql(SIGNUPS, rows))
    assert 'Ask new signups "How did you hear about us?"' in readout(data)
    assert "Owner: growth.signup_source." in readout(data)


async def test_a_falling_page_routes_to_content_refresh(monkeypatch):
    current = gsc([page("/", 150, 1500, 2.0), page("/blog/invoice-template", 30, 1200)])
    data, _ = await snapshot(monkeypatch, G2=current)
    assert "**Refresh a falling page.** Evidence: /blog/invoice-template: 30" in readout(data)
    assert "Owner: content.refresh." in readout(data)


async def test_sessions_are_counted_by_posthog_session_not_by_person(monkeypatch):
    """Sessions ran about 5% high: a visitor who signed in mid-session had two distinct IDs."""
    _, ctx = await snapshot(monkeypatch)
    queries = {c["step"]: c["arguments"].get("query", "") for c in ctx.services.calls}
    landing = queries["P1"]
    assert "GROUP BY session HAVING" in landing
    assert "GROUP BY distinct_id" not in landing
    # A session counts once, in the bucket of its first pageview.
    assert "argMinIf(w, timestamp, event = '$pageview') AS w" in landing
    assert "countIf(NOT sessionless AND w = 'w0') AS week" in landing
    for step in ("P1", "P2", "P3", "P4"):
        query = queries[step]
        assert "NOT match(" in query and "northpine\\\\.example" in query  # team exclusion
        assert len(query.encode()) <= 8000 and query.rstrip().split()[-2] == "LIMIT"
    for step in ("P1", "P2", "P4"):
        # Host scope on every pageview read, in both spellings: page keys drop www.
        assert f"IN ('{SITE}', 'www.{SITE}')" in queries[step]


async def test_worst_case_queries_fit_posthogs_8000_byte_limit(monkeypatch):
    module = load(KEY, monkeypatch)
    window = module.windows(LAST, False)
    hosts = [f"host{i}-a-long-marketing-site-name.example.com" for i in range(5)]
    inputs = {
        "exclude_email_domains": [f"team{i}-company-domain.example.com" for i in range(10)],
        "internal_flag_property": "person:is_internal_team_member",
    }
    _, excluded = module.exclusions(inputs, [])
    sql = module.queries(window, hosts, excluded, "a" * 60, "b" * 60, "c" * 60, 30)
    assert max(len(q.encode()) for q in sql.values()) <= 8000


AUDIT_RUN = "11111111-1111-4111-8111-111111111111"


def audit_summary(host=SITE, run_id=AUDIT_RUN):
    """An organic audit summary as policy v12 and later write it (LATEST.json)."""
    from tin_lite.organic_audit_summary import PAGE_COLUMNS

    rows = [("/pricing", [0]), ("/missing-page", [0]), ("/", [])]
    return {
        "schema_version": 1,
        "kind": "organic_audit_summary",
        "run_id": run_id,
        "host": host,
        "policy_version": "organic-audit-v13",
        "coverage": {"status": "complete", "inspected_pages": 40, "sitemap_pages": 60},
        "findings": {
            "by_check": [
                {"check": "search.low_ctr", "priority": "quick_win", "pages": 2, "listed": 2},
                {
                    "check": "crawl.sitemap_missing",
                    "priority": "high_impact",
                    "pages": 1,
                    "listed": 0,
                },
            ]
        },
        "pages": {
            "columns": list(PAGE_COLUMNS),
            "rows": [
                [{"path": path, "checks": checks}.get(c) for c in PAGE_COLUMNS]
                for path, checks in rows
            ],
            "total": len(rows),
        },
        "truncated": False,
    }


async def test_no_host_input_uses_the_audited_host(monkeypatch):
    files = {"reports/organic-audit/LATEST.json": audit_summary()}
    data, ctx = await snapshot(monkeypatch, inputs={"signup_event": "signed_up"}, files=files)
    assert data["definitions"]["website_hosts"] == [SITE]
    assert data["definitions"]["website_hosts_source"] == "organic.audit"
    # The summary names checks, not finding IDs.
    assert by_page(data)[f"{SITE}/pricing"]["audit"] == [[None, "search.low_ctr", "quick_win"]]
    audit = data["audit"]
    assert audit["status"] == "attached" and audit["run_id"] == AUDIT_RUN
    assert audit["pages_with_findings"] == 1 and audit["not_listed"] == 1
    # A site-level check names no page row; it is kept apart, not spread over pages.
    assert audit["unlisted_checks"] == [["crawl.sitemap_missing", "high_impact", 1, 0]]
    assert not any(path.endswith("findings.json") for path in ctx.files.reads)
    assert "team visits are counted" in " ".join(data["status_reasons"])


async def test_an_audit_of_another_site_is_not_attached(monkeypatch):
    files = {"reports/organic-audit/LATEST.json": audit_summary(host="elsewhere.example")}
    inputs = {"signup_event": "signed_up", "website_hosts": [SITE]}
    data, _ = await snapshot(monkeypatch, inputs=inputs, files=files)
    assert data["audit"]["status"] == "other_host"
    assert by_page(data)[f"{SITE}/pricing"]["audit"] == []
    assert "summary is for elsewhere.example" in " ".join(data["status_reasons"])


async def test_a_named_audit_without_a_summary_is_said_so(monkeypatch):
    old = "22222222-2222-4222-8222-222222222222"
    files = {
        "reports/organic-audit/LATEST.json": audit_summary(),
        f"reports/organic-audit/{old}/findings.json": {"schema_version": 3},
    }
    inputs = {"signup_event": "signed_up", "audit_run_id": old}
    data, _ = await snapshot(monkeypatch, inputs=inputs, files=files)
    assert data["audit"]["status"] == "none"
    assert "has no SUMMARY.json" in " ".join(data["status_reasons"])


async def test_truncated_prior_read_is_never_a_zero(monkeypatch):
    prior = gsc([page("/", 140, 1400, 2.0)], truncated=True, next_start_row=1)
    data, _ = await snapshot(monkeypatch, G3=prior)
    assert data["status"] == "partial"
    pricing = by_page(data)[f"{SITE}/pricing"]
    assert pricing["search"]["prior"] is None
    assert pricing["search"]["prior_note"].startswith("The prior read ended at 140 clicks")
    assert "new_in_search" not in pricing["flags"]


async def test_unseen_signup_event_stays_null(monkeypatch):
    data, _ = await snapshot(monkeypatch, P3=hogql(SIGNUPS, []))
    assert data["totals"]["signups"]["current"] is None
    assert "check the name" in " ".join(data["status_reasons"])
    assert "Signups are unknown this week." in readout(data)


async def test_unusable_posthog_rows_leave_visits_unmeasured(monkeypatch):
    """A plausible response with list rows but no columns is not data."""
    broken = {"rows": [["/", "Search", 120, 100, 300, 250, 40, 25, 6, 1]], "has_more": False}
    data, _ = await snapshot(monkeypatch, P1=broken)
    assert data["totals"]["visits"]["current"]["sessions"] is None
    assert by_page(data)[f"{SITE}/"]["visits"]["current"]["sessions"] is None
    assert any(c["step"] == "P1" and c["outcome"] == "incomplete" for c in data["calls"])
    assert "Channel rows are unknown this week." in readout(data)
    assert "Landing pages are unknown this week." in readout(data)


@pytest.mark.parametrize("state", ["not_connected", "needs_attention"])
async def test_without_posthog_the_snapshot_is_search_only(monkeypatch, state):
    data, ctx = await snapshot(monkeypatch, connections={"posthog": state})
    # Only Search Console is read, and a missing optional connection is not a partial run.
    assert [c["step"] for c in ctx.services.calls] == ["G1", "G2", "G3", "G4"]
    assert [c["step"] for c in data["calls"]] == ["G1", "G2", "G3", "G4"]
    assert data["status"] == "complete" and data["status_reasons"] == []
    assert data["definitions"]["sources"] == {"search_console": "connected", "posthog": state}
    totals = data["totals"]
    assert totals["search"]["current"][:2] == [280, 2800]
    assert totals["visits"]["current"]["sessions"] is None  # unknown, never zero
    assert totals["signups"]["current"] is None and totals["reading"]["reads"] is None
    home = by_page(data)[f"{SITE}/"]
    assert home["search"]["branded_clicks"] == 120
    assert home["visits"]["current"]["sessions"] is None
    assert home["signups"]["first_touch"] == [None, None]
    text = readout(data)
    assert text.startswith("# Search this week")
    assert "PostHog is not read." in text and "## Search" in text
    for heading in ("## Where people came from", "## Content", "## Landing pages", "## Paid"):
        assert heading not in text
    assert "signed up" not in text and "Signups are unknown" not in text
    if state == "not_connected":
        assert "PostHog is not connected, so this readout covers search only" in text
    else:
        assert "PostHog needs attention in Integrations" in text


async def test_without_posthog_a_falling_page_still_routes_to_content_refresh(monkeypatch):
    current = gsc([page("/", 150, 1500, 2.0), page("/blog/invoice-template", 30, 1200)])
    data, _ = await snapshot(monkeypatch, connections={"posthog": "not_connected"}, G2=current)
    text = readout(data)
    assert "**Refresh a falling page.** Evidence: /blog/invoice-template: 30" in text
    assert "Measure AI visibility again" not in text  # AI referrals come from PostHog


async def test_without_posthog_no_search_data_keeps_the_previous_snapshot(monkeypatch):
    failed = ValueError("Search Console refused the read")
    overrides = {step: failed for step in ("G1", "G2", "G3", "G4")}
    with pytest.raises(RuntimeError, match="previous snapshot stays"):
        await snapshot(monkeypatch, connections={"posthog": "not_connected"}, **overrides)


async def test_contract_errors_fail_the_run(monkeypatch):
    error = ValueError("The service request differs from its declared contract: bad argument.")
    with pytest.raises(ValueError, match="declared contract"):
        await snapshot(monkeypatch, G1=error)


async def test_no_provider_data_keeps_the_previous_snapshot(monkeypatch):
    failed = ValueError("Search Console refused the read")
    overrides = {step: failed for step in ("G1", "G2", "G3", "G4", "P1", "P2", "P3", "P4")}
    with pytest.raises(RuntimeError, match="previous snapshot stays"):
        await snapshot(monkeypatch, **overrides)


THIN = {
    "P1": hogql(LANDING, [["/", "Search", 12, 0, 20, 0, 12, 0, 0, 0]]),
    "P2": hogql(REFERRERS, []),
}
NO_SUMS = {
    "P1": hogql(
        LANDING,
        [
            ["/", "Search", 400, 380, 900, 850, 180, 150, 100, 80],
            ["/", "Paid", 40, 30, 60, 40, 20, 15, 9, 6],
        ],
    ),
    "P3": hogql(
        SIGNUPS,
        [
            signup_row("/", "Search", 300, 280, 109, 90, activated=120),
            signup_row("/", "Paid", 20, 15, 9, 6, activated=4, paid=3),
        ],
    ),
}
CASES = {
    "ordinary": ({}, INPUTS),
    "truncated_prior": (
        {"G3": gsc([page("/", 140, 1400, 2.0)], truncated=True, next_start_row=1)},
        {"website_hosts": [SITE], "signup_event": "signed_up"},
    ),
    "unseen_signup": ({"P3": hogql(SIGNUPS, [])}, INPUTS),
    "thin": (THIN, {"website_hosts": [SITE], "min_count": 100}),
    "no_cross_source_sums": (
        NO_SUMS,
        {"website_hosts": [SITE], "signup_event": "signed_up", "paid_event": "subscribed"},
    ),
}


async def test_qualification_cases_pass_on_their_fixtures(monkeypatch):
    raw = (REPOSITORY_ROOT / "workflow_evals" / KEY / "qualification.json").read_text()
    qualification = Qualification.model_validate_json(raw)
    assert {case.id for case in qualification.cases} == set(CASES)
    for case in qualification.cases:
        overrides, inputs = CASES[case.id]
        assert case.inputs == inputs
        module = load(KEY, monkeypatch)
        result = await module.run(context(services=providers(**overrides)), dict(case.inputs))
        report = assess_output(case, status="succeeded", content=result["content"].encode())
        assert report["status"] == "passed", (case.id, report["checks"])


def weekly_daily(totals, last=LAST, days=363):
    """Daily rows whose seven-day weeks, newest first, sum to `totals`; earlier days get 10."""
    rows = []
    for i in range(days):
        week, offset = divmod(i, 7)
        if week < len(totals):
            clicks = totals[week] // 7 + (totals[week] % 7 if offset == 0 else 0)
        else:
            clicks = 10
        rows.append(
            {
                "keys": [str(last - dt.timedelta(days=i))],
                "clicks": clicks,
                "impressions": clicks * 12,
                "position": 9.0,
            }
        )
    return gsc(rows)


async def test_a_realistic_query_read_fits_the_binding_and_completes(monkeypatch):
    """500 page × search rows overflowed the old 64000-byte bound on every real run."""
    from tin_lite.integrations import _fit_search_console_rows

    module = load(KEY, monkeypatch)
    long_paths = [f"/blog/how-to-edit-text-in-images-without-photoshop-part-{i}" for i in range(30)]
    rows = [
        {
            "keys": [f"https://www.{SITE}{path}", f"edit text in image without photoshop {j}"],
            "clicks": 1,
            "impressions": 20,
            "ctr": 0.05,
            "position": 12.333333333333334,
        }
        for path in long_paths
        for j in range(15)
    ]
    bound = definition(KEY)["code"]["services"]["gsc"]["max_response_bytes"]
    payload = {"rows": rows, "responseAggregationType": "byPage"}
    fitted = _fit_search_console_rows(
        payload, max_response_bytes=bound, start_row=0, clamped=False, sent_limit=module.G4_ROWS
    )
    assert "truncated" not in fitted and len(fitted["rows"]) == 450
    old = _fit_search_console_rows(
        payload, max_response_bytes=64000, start_row=0, clamped=False, sent_limit=500
    )
    assert old["truncated"] and len(old["rows"]) < 450  # what production saw
    # A row ran about 200 bytes: the full G4 page fits the bound.
    assert module.G4_ROWS * 220 <= bound

    current = gsc([page(path, 15, 300, 6.0) for path in long_paths])
    data, ctx = await snapshot(
        monkeypatch, connections={"posthog": "not_connected"}, G2=current, G4=fitted
    )
    g4 = next(c for c in ctx.services.calls if c["step"] == "G4")
    assert g4["arguments"]["row_limit"] == module.G4_ROWS
    assert {c["step"]: c["outcome"] for c in data["calls"]}["G4"] == "ok"
    assert data["status"] == "complete"
    assert data["totals"]["search"]["hidden_clicks"] is not None


async def test_the_query_read_matches_each_page_in_both_host_spellings(monkeypatch):
    _, ctx = await snapshot(monkeypatch)
    g4 = next(c for c in ctx.services.calls if c["step"] == "G4")
    expression = g4["arguments"]["dimension_filters"][0]["expression"]
    match = re.compile(expression)
    assert match.search(f"https://www.{SITE}/")
    assert match.search(f"https://{SITE}/?ref=newsletter")
    assert match.search(f"https://{SITE}/pricing/")
    # Anchored: the home page no longer pulls in every URL on the site.
    assert not match.search(f"https://{SITE}/pricing-old")
    assert not match.search(f"https://{SITE}/careers")


async def test_week_to_week_swings_are_not_called_a_change(monkeypatch):
    """A site whose weeks swing widely: 790 against 636 is ordinary for it, not a change."""
    swings = [790, 636, 962, 773, 553, 582, 641, 597, 529, 700, 680, 720]
    data, _ = await snapshot(
        monkeypatch, connections={"posthog": "not_connected"}, G1=weekly_daily(swings)
    )
    text = readout(data)
    assert "790 search clicks this week against 636" in text
    assert "within normal variation" in text
    assert "more than normal weekly variation" not in text
    assert "search clicks 790 vs 636: dispersion φ = " in text and "11 earlier weeks" in text
    assert data["growth"]["calls"]["search clicks"] == "within normal variation"


async def test_a_real_jump_is_still_called_in_plain_words(monkeypatch):
    """A small site: 76 against 26 after weeks of about 25 is a change."""
    jump = [76, 26, 22, 28, 25, 12, 20, 24, 22, 26, 23, 25]
    data, _ = await snapshot(
        monkeypatch, connections={"posthog": "not_connected"}, G1=weekly_daily(jump)
    )
    text = readout(data)
    head = text.split("\n")[2]
    assert "up 192%, more than normal weekly variation" in head
    assert "adjusted p" not in text and "change (up)" not in text
    assert "Beyond normal variation: search clicks 76 vs 26 (up 192%" in head
    assert "p < 0.001 after Holm" in text  # the p-value stays in the data notes
    assert data["growth"]["calls"]["search clicks"] == "change (up)"  # stored state


async def test_without_four_earlier_weeks_the_test_says_so(monkeypatch):
    data, _ = await snapshot(
        monkeypatch, connections={"posthog": "not_connected"}, G1=weekly_daily([90, 40], days=20)
    )
    assert "fewer than 4 earlier weeks; φ = 1" in readout(data)


async def test_no_decision_reads_as_a_sentence(monkeypatch):
    data, _ = await snapshot(monkeypatch, connections={"posthog": "not_connected"})
    head = readout(data).split("\n")[2]
    assert head.endswith("Nothing needs a decision this week.")
    assert "0 things" not in readout(data)


async def test_partial_months_are_marked(monkeypatch):
    data, _ = await snapshot(monkeypatch, connections={"posthog": "not_connected"})
    months = {m["month"]: m for m in data["search_months"]}
    assert months["2026-09"]["partial"] == "end"
    assert months["2026-09"]["from"] == "2026-09-01" and months["2026-09"]["to"] == "2026-09-26"
    assert months["2026-08"]["partial"] is None and months["2026-08"]["days"] == 31
    assert "2026-09 260 clicks (1 Sep–26 Sep, 26 days so far)" in readout(data)
    assert "2026-08 310 clicks," in readout(data)
    module = load(KEY, monkeypatch)
    assert module.month_end("2028-02") == dt.date(2028, 2, 29)
    assert module.month_end("2026-12") == dt.date(2026, 12, 31)


async def test_the_www_spelling_is_the_same_page_not_a_new_one(monkeypatch):
    current = gsc(
        [
            page("/", 150, 1500, 2.0),
            {"keys": [f"https://www.{SITE}/"], "clicks": 1, "impressions": 2, "position": 3.0},
            page("/blog/invoice-template", 100, 1200),
        ]
    )
    data, _ = await snapshot(monkeypatch, connections={"posthog": "not_connected"}, G2=current)
    pages = by_page(data)
    assert f"www.{SITE}/" not in pages
    home = pages[f"{SITE}/"]
    assert home["search"]["current"][:2] == [151, 1502]
    assert home["variants"] == 2 and "new_in_search" not in home["flags"]
    assert f"www.{SITE}" not in readout(data)


async def test_a_spaced_or_dotted_brand_search_counts_as_branded(monkeypatch):
    queries = gsc(
        [
            query("/", "north pine.example", 50, 400),
            query("/", "North-Pine invoices", 30, 300),
            query("/", "invoice app", 10, 300),
        ]
    )
    data, _ = await snapshot(monkeypatch, connections={"posthog": "not_connected"}, G4=queries)
    home = by_page(data)[f"{SITE}/"]
    assert home["search"]["branded_clicks"] == 80
    assert data["definitions"]["brand_compact"] == ["northpine"]
    # A plain-word input term matches the same way; a regular expression still works.
    plain = {"website_hosts": [SITE], "brand_terms": ["North Pine"]}
    data, _ = await snapshot(
        monkeypatch, inputs=plain, connections={"posthog": "not_connected"}, G4=queries
    )
    assert by_page(data)[f"{SITE}/"]["search"]["branded_clicks"] == 80
    pattern = {"website_hosts": [SITE], "brand_terms": [r"north\s?pine"]}
    data, _ = await snapshot(
        monkeypatch, inputs=pattern, connections={"posthog": "not_connected"}, G4=queries
    )
    assert data["definitions"]["brand_compact"] == []
    assert by_page(data)[f"{SITE}/"]["search"]["branded_clicks"] == 50
