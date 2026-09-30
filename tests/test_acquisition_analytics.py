"""growth.acquisition_analytics offline: synthetic PostHog and Search Console, frozen clock."""

import datetime as dt
import json

import pytest
from loop_workflow_fakes import context, definition, gsc, hogql, load

from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result
from tin_lite.workflow_qualification import Qualification, assess_output

KEY = "growth.acquisition_analytics"
SITE = "fernwick.example"
CHANNEL_COLUMNS = ["period", "medium", "ref", "src", "sessions", "visitors", "signups"]
SIGNUP_COLUMNS = [
    "period",
    "fmed",
    "fref",
    "fsrc",
    "lref",
    "answer",
    "signups",
    "activated",
    "paid",
]


def daily(last=dt.date(2026, 9, 26), days=366):
    return gsc(
        [
            {
                "keys": [str(last - dt.timedelta(days=i))],
                "clicks": 9 if i < 7 else 6,
                "impressions": 200,
                "position": 6.0,
            }
            for i in range(days)
        ]
    )


def providers(**overrides):
    responses = {
        "P1": hogql(
            CHANNEL_COLUMNS,
            [
                ["current", "", "google.com", "", 60, 55, 10],
                ["prior", "", "google.com", "", 40, 38, 0],
                ["current", "", "", "", 30, 28, 8],
                ["prior", "", "$direct", "", 25, 24, 6],
                ["current", "", "chatgpt.com", "", 6, 6, 2],
                ["prior", "", "chatgpt.com", "", 3, 3, 1],
                ["current", "", "news.ycombinator.com", "", 12, 12, 1],
                ["current", "", "blog.partner.example", "", 4, 4, 0],
            ],
        ),
        "P2": hogql(
            SIGNUP_COLUMNS,
            [
                ["current", "", "google.com", "", "google.com", "", 10, 6, 1],
                ["prior", "", "google.com", "", "google.com", "", 0, 0, 0],
                ["current", "", "", "", "", "", 8, 3, 0],
                ["prior", "", "", "", "", "", 6, 2, 0],
                ["current", "", "chatgpt.com", "", "", "chatgpt", 2, 1, 0],
                ["prior", "", "chatgpt.com", "", "", "", 1, 0, 0],
            ],
        ),
        "P3": hogql(
            [
                "period",
                "pth",
                "views",
                "readers",
                "median_s",
                "with_dur",
                "over15",
                "with_dep",
                "deep",
            ],
            [["current", "/blog/late-fees", 40, 30, 52.0, 20, 14, 18, 6]],
        ),
        "P4": hogql(
            ["period", "path", "sessions", "signups"],
            [["current", "/", 70, 12], ["prior", "/", 50, 5], ["current", "/pricing", 20, 6]],
        ),
        "G1": daily(),
        "G2": gsc(
            [
                {"keys": ["fernwick"], "clicks": 30, "impressions": 200},
                {"keys": ["invoice late fee"], "clicks": 20, "impressions": 600},
            ]
        ),
        "G3": gsc([{"keys": [f"https://{SITE}/blog/late-fees"], "clicks": 20, "impressions": 600}]),
    }
    responses.update(overrides)
    return responses


ORDINARY = {
    "brand_terms": ["fernwick", "fernwik"],
    "signup_event": "signed_up",
    "activation_event": "first_invoice_sent",
    "paid_event": "subscription_started",
    "website_hosts": [SITE],
    "content_paths": ["/blog/"],
    "min_count": 5,
    "exclude_email_domains": [SITE],
    "internal_flag_property": "person:is_internal",
    "impressions_reliable_from": "2026-04-28",
}


async def report(monkeypatch, inputs=ORDINARY, files=None, **overrides):
    module = load(KEY, monkeypatch)
    ctx = context(files=files, services=providers(**overrides))
    result = await module.run(ctx, dict(inputs))
    validate_code_result(json.dumps(result).encode(), validate_code_definition(definition(KEY)))
    return result["content"], ctx


def stored(content):
    return json.loads(content.split("## Stored numbers\n```json\n", 1)[1].split("\n```", 1)[0])


def test_manifest_fits_the_code_contract_and_the_eight_call_limit():
    spec = validate_code_definition(definition(KEY))
    assert spec.output_path == "analytics/GROWTH_ANALYTICS.md"
    services = definition(KEY)["code"]["services"]
    assert sum(binding["max_calls"] for binding in services.values()) <= 8


async def test_ordinary_week(monkeypatch):
    content, ctx = await report(monkeypatch)
    assert content.startswith("# ")
    assert "20 people signed up this week against 7 last week" in content
    assert "Ranked by activated signups" in content
    assert "| Search | 60 vs 40 |" in content
    assert "| ChatGPT | 6 vs 3 |" in content
    assert "news.ycombinator.com (community): 12 sessions" in content
    assert "Brand terms: 30 clicks; other queries: 20." in content
    assert "(impressions unreliable before 2026-04-28)" in content
    assert "Holm correction" in content
    numbers = stored(content)
    assert numbers["totals"]["signups"] == {"current": 20, "prior": 7}
    assert numbers["channels"]["Search"] == {"sessions": 60, "signups": 10}
    queries = {c["step"]: c["arguments"].get("query", "") for c in ctx.services.calls}
    for step in ("P1", "P2", "P3", "P4"):
        assert "'@fernwick.example'" in queries[step]  # team exclusion in every read
        assert "person.properties['is_internal']" in queries[step]
        assert len(queries[step].encode()) <= 8000


async def test_the_snapshot_fills_in_events_hosts_and_exclusions(monkeypatch):
    snapshot = {
        "schema": "tin.traffic_snapshot/1",
        "generated_at": "2026-09-28T13:00:00Z",
        "definitions": {
            "website_hosts": [SITE],
            "brand_terms": ["fernwick"],
            "signup_event": "signed_up",
            "activation_event": "first_invoice_sent",
            "exclusions": [{"property": "person:email", "op": "suffix", "value": [SITE]}],
        },
        "pages": [],
    }
    content, ctx = await report(
        monkeypatch, inputs={}, files={"analytics/traffic-snapshot.json": snapshot}
    )
    assert "Signup event: signed_up; activation: first_invoice_sent" in content
    assert "emails at fernwick.example" in content
    channels = next(c for c in ctx.services.calls if c["step"] == "P1")["arguments"]["query"]
    assert f"= '{SITE}'" in channels


async def test_direct_heavy_signups_route_to_the_signup_source_workflow(monkeypatch):
    rows = [["current", "", "", "", "", "", 30, 3, 0], ["prior", "", "", "", "", "", 25, 2, 0]]
    content, _ = await report(monkeypatch, P2=hogql(SIGNUP_COLUMNS, rows))
    assert 'Ask new signups "How did you hear about us?"' in content
    assert "Owner: growth.signup_source." in content


async def test_rows_without_columns_are_not_data(monkeypatch):
    """A plausible PostHog answer with list rows but no columns leaves channels unknown."""
    broken = {"rows": [["current", "", "google.com", "", 60, 55, 10]], "has_more": False}
    content, _ = await report(monkeypatch, P1=broken)
    assert "Channel rows are unknown this week" in content
    assert "P1, rows without columns" in content


async def test_contract_errors_fail_the_run(monkeypatch):
    error = ValueError("The service request differs from its declared contract: bad argument.")
    with pytest.raises(ValueError, match="declared contract"):
        await report(monkeypatch, P1=error)


THIN = {
    "P1": hogql(CHANNEL_COLUMNS, [["current", "", "google.com", "", 12, 12, 0]]),
    "P2": hogql(SIGNUP_COLUMNS, []),
    "G1": daily(),
}
NO_SUMS = {
    "P2": hogql(
        SIGNUP_COLUMNS,
        [
            ["current", "", "google.com", "", "google.com", "", 109, 40, 3],
            ["current", "cpc", "google.com", "", "google.com", "", 9, 2, 1],
            ["prior", "", "google.com", "", "google.com", "", 90, 30, 2],
        ],
    )
}
CASES = {
    "ordinary": ({}, ORDINARY),
    "thin": (THIN, {"min_count": 100}),
    "no_cross_source_sums": (
        NO_SUMS,
        {"signup_event": "signed_up", "paid_event": "subscription_started"},
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
        verdict = assess_output(case, status="succeeded", content=result["content"].encode())
        assert verdict["status"] == "passed", (case.id, verdict["checks"])
