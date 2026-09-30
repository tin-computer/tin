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


async def snapshot(monkeypatch, inputs=INPUTS, files=None, **overrides):
    module = load(KEY, monkeypatch)
    ctx = context(files=files, services=providers(**overrides))
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
        assert f"= '{SITE}'" in queries[step]  # host scope on every pageview read


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


async def test_no_host_input_uses_the_audited_host(monkeypatch):
    findings = {
        "schema_version": 3,
        "target_host": SITE,
        "findings": [
            {
                "id": "oa_" + "1" * 20,
                "check_id": "search.low_ctr",
                "priority": "quick_win",
                "urls": [f"https://{SITE}/pricing"],
            }
        ],
        "coverage": {"inspected_pages": 40, "sitemap_pages": 60},
    }
    files = {"reports/organic-audit/11111111-1111-4111-8111-111111111111/findings.json": findings}
    data, _ = await snapshot(monkeypatch, inputs={"signup_event": "signed_up"}, files=files)
    assert data["definitions"]["website_hosts"] == [SITE]
    assert data["definitions"]["website_hosts_source"] == "organic.audit"
    assert by_page(data)[f"{SITE}/pricing"]["audit"] == [
        ["oa_" + "1" * 20, "search.low_ctr", "quick_win"]
    ]
    assert "team visits are counted" in " ".join(data["status_reasons"])


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
