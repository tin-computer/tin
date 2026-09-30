"""organic.traffic_snapshot offline: synthetic Search Console and PostHog, frozen clock."""

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


LANDING = ["path", "w", "channel", "sessions", "pageviews"]
SIGNUPS = ["path", "channel", "w", "signups", "activated", "open"]
READING = ["path", "reads", "median_seconds", "engaged", "finished"]


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
            ]
        ),
        "G5": gsc([query("/blog/unclicked", "late fee letter", 0, 30, 12.0)]),
        "P1": hogql(
            LANDING,
            [
                ["/", "current", "Search", 120, 300],
                ["/", "current", "Direct", 60, 90],
                ["/blog/invoice-template", "current", "Search", 90, 120],
                ["/blog/invoice-template", "current", "Paid", 5, 5],
                ["/pricing", "current", "Referral", 10, 30],
                ["/", "prior", "Search", 100, 250],
            ],
        ),
        "P2": hogql(
            SIGNUPS,
            [
                ["/", "Search", "current", 8, 5, 1],
                ["/blog/invoice-template", "Search", "current", 3, 1, 0],
                ["/", "Direct", "prior", 4, 2, 0],
            ],
        ),
        "P3": hogql(READING, [["/blog/invoice-template", 80, 41.5, 50, 20]]),
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


def test_manifest_fits_the_code_contract_and_the_eight_call_limit():
    spec = validate_code_definition(definition(KEY))
    assert spec.output_path == "analytics/traffic-snapshot.json"
    services = definition(KEY)["code"]["services"]
    assert sum(binding["max_calls"] for binding in services.values()) <= 8


def test_channels_match_the_analytics_brief_and_both_copies_are_identical():
    brief = (
        REPOSITORY_ROOT
        / "workflow_packages/product.analytics_brief/skills/product-analytics/CALCULATIONS.md"
    ).read_text()
    snapshot_copy = (REPOSITORY_ROOT / "workflow_packages" / KEY / "channels.py").read_text()
    growth_copy = (
        REPOSITORY_ROOT / "workflow_packages/growth.acquisition_analytics/channels.py"
    ).read_text()
    assert snapshot_copy == growth_copy
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
    totals = data["totals"]
    assert totals["search"]["current"][:2] == [280, 2800]
    assert totals["visits"]["current"]["sessions"] == 285
    assert totals["visits"]["prior"]["sessions"] == 100
    assert totals["signups"]["current"] == 11 and totals["activated"]["current"] == 6
    search = data["definitions"]["channels"].index("Search")
    assert totals["search_sessions_per_click"] == round(210 / 280, 4)
    pages = by_page(data)
    home = pages[f"{SITE}/"]
    assert home["search"]["branded_clicks"] == 120
    assert home["search"]["hidden_clicks"] == 150 - 130
    assert home["visits"]["current"]["by_channel"][search] == 120
    assert pages[f"{SITE}/pricing"]["flags"] == ["new_in_search"]
    assert [row[0] for row in data["dropped_pages"]] == [f"{SITE}/old-page"]
    assert pages[f"{SITE}/blog/invoice-template"]["reading"]["median_seconds"] == 41.5
    assert data["definitions"]["brand_source"] == "host"
    assert [c["step"] for c in ctx.services.calls] == [
        "G1",
        "G2",
        "G3",
        "G4",
        "G5",
        "P1",
        "P2",
        "P3",
    ]


async def test_sessions_are_counted_by_posthog_session_not_by_person(monkeypatch):
    """Sessions ran about 5% high: a visitor who signed in mid-session had two distinct IDs."""
    _, ctx = await snapshot(monkeypatch)
    landing = next(c for c in ctx.services.calls if c["step"] == "P1")["arguments"]["query"]
    assert "GROUP BY session)" in landing
    assert "GROUP BY distinct_id" not in landing
    assert "argMin(w, timestamp) AS w" in landing  # a session counts once, in its first window
    assert "countIf(NOT sessionless) AS sessions" in landing
    for step in ("P1", "P2", "P3"):
        query = next(c for c in ctx.services.calls if c["step"] == step)["arguments"]["query"]
        assert "endsWith(" in query and f"'@{SITE}'" in query  # team exclusion in every read
        assert len(query.encode()) <= 8000 and query.rstrip().split()[-2] == "LIMIT"


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
    data, _ = await snapshot(monkeypatch, P2=hogql(SIGNUPS, []))
    assert data["totals"]["signups"]["current"] is None
    assert "check the name" in " ".join(data["status_reasons"])


async def test_unusable_posthog_rows_leave_visits_unmeasured(monkeypatch):
    """A plausible response with list rows but no columns is not data."""
    broken = {"rows": [["/", "current", "Search", 120, 300]], "has_more": False}
    data, _ = await snapshot(monkeypatch, P1=broken)
    assert data["totals"]["visits"]["current"]["sessions"] is None
    assert by_page(data)[f"{SITE}/"]["visits"]["current"]["sessions"] is None
    assert any(c["step"] == "P1" and c["outcome"] == "incomplete" for c in data["calls"])


async def test_contract_errors_fail_the_run(monkeypatch):
    error = ValueError("The service request differs from its declared contract: bad argument.")
    with pytest.raises(ValueError, match="declared contract"):
        await snapshot(monkeypatch, G1=error)


async def test_no_provider_data_keeps_the_previous_snapshot(monkeypatch):
    failed = ValueError("Search Console refused the read")
    overrides = {step: failed for step in ("G1", "G2", "G3", "P1", "P2", "P3")}
    with pytest.raises(RuntimeError, match="previous snapshot stays"):
        await snapshot(monkeypatch, **overrides)


CASES = {
    "ordinary": ({}, INPUTS),
    "truncated_prior": (
        {"G3": gsc([page("/", 140, 1400, 2.0)], truncated=True, next_start_row=1)},
        {"website_hosts": [SITE], "signup_event": "signed_up"},
    ),
    "unseen_signup": ({"P2": hogql(SIGNUPS, [])}, INPUTS),
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
