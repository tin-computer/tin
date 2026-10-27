"""product.visitor_journeys: the reviewed journeys resource, the visitor-journal.v1 check and
People from a published report. No provider or model calls; PostHog is the offline fake."""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from connection_fakes import FakePostHogConnection, posthog_objects
from test_procedure_publication import publication_db as publication_db

from tin_lite import people_records as records
from tin_lite import visitor_journal, visitor_people
from tin_lite.code_services import CodeServiceError
from tin_lite.community import ContributedPackage, validate, validate_files
from tin_lite.posthog_connection import check_hogql

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/product.visitor_journeys"
RESOURCE = PACKAGE / "skills/visitor-journeys/JOURNEYS.md"
NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
REPLAY = "https://us.posthog.com/project/412951/replay/"
ANA, BEN, CARA = (str(UUID(int=n)) for n in (1, 2, 3))
TEAM, BOT, PASSER, BROWSER, ACCOUNT = (str(UUID(int=n)) for n in (4, 5, 6, 7, 8))


@pytest.fixture(scope="module")
def j():
    blocks = re.findall(r"```python\n(.*?)\n```", RESOURCE.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 1
    namespace = {"__name__": "journeys"}
    # Only the fixed package resource executes here, never report or provider text.
    exec(compile(blocks[0], str(RESOURCE), "exec"), namespace)  # noqa: S102
    return SimpleNamespace(**namespace)


@pytest.fixture
def c(j):
    return j.settings(
        {
            "lookback_hours": 24,
            "website_hosts": ["https://Example.com/"],
            "product_hosts": ["app.example.com"],
            "exclude_email_domains": ["example.com"],
            "internal_people": ["cofounder@gmail.com"],
        },
        now=NOW,
    )


@pytest.fixture
def m(j):
    value = j.default_map()
    value["key_events"].update(
        signup=["account_created"],
        intent=["pricing_plan_clicked", "book_cta_clicked"],
        activation=["first_report_viewed"],
        failure=["estimate_failed"],
    )
    value["steps"] = ["$pageview", "pricing_plan_clicked", "account_created", "first_report_viewed"]
    value["funnel"] = [
        {"label": "Unique visitors", "events": ["$pageview"]},
        {"label": "Clicked pricing", "events": ["pricing_plan_clicked"]},
        {"label": "Signed up", "events": ["account_created"]},
    ]
    value["hosts"]["ignore"] = ["example-git-main.vercel.app"]
    value["bots"]["countries"] = ["cn"]
    value["replay_url"] = REPLAY
    return j.check_map(value)


def census_row(person_id, **values):
    row = {
        "person_id": person_id,
        "email": None,
        "name": None,
        "first_seen": "2026-10-16T09:00:00Z",
        "last_seen": "2026-10-16T09:30:00Z",
        "sessions": 1,
        "pageviews": 1,
        "paths": 1,
        "path_sample": ["/"],
        "entry_path": "/",
        "last_path": "/",
        "referrer": "$direct",
        "utm_source": None,
        "utm_campaign": None,
        "first_touch": "$direct",
        "country": "GB",
        "city": "Leeds",
        "device": "Desktop",
        "browser": "Chrome",
        "key_events": [],
        "rage_clicks": 0,
        "dead_clicks": 0,
        "errors": 0,
        "hosts": ["example.com"],
        "session_ids": ["01a1-session-" + person_id[-4:]],
    }
    return {**row, **values}


@pytest.fixture
def census(j):
    return [
        census_row(
            ANA,
            email="Ana@Fieldnotes.dev",
            name="Ana Souza",
            pageviews=14,
            paths=6,
            key_events=["pricing_plan_clicked", "account_created"],
            referrer="devpost.com",
            utm_source="hackathon",
        ),
        census_row(
            BEN,
            email="ben@quanta.io",
            name="Ben Ode",
            pageviews=4,
            paths=3,
            key_events=["book_cta_clicked"],
        ),
        census_row(CARA, email="cara@studio.design", pageviews=2, paths=2),
        census_row(TEAM, email="cofounder@gmail.com", pageviews=40, paths=12),
        census_row(BOT, country="CN"),
        census_row(PASSER, referrer="www.google.com"),
        census_row(
            BROWSER,
            pageviews=3,
            paths=2,
            key_events=["estimate_failed", "$rageclick"],
            rage_clicks=2,
        ),
        census_row(
            ACCOUNT,
            pageviews=0,
            paths=0,
            path_sample=[],
            key_events=["estimate_failed"],
            referrer=None,
            browser=None,
            hosts=[],
        ),
    ]


@pytest.fixture
def people(j, c, m, census):
    prior = [
        {
            "person_id": CARA,
            "prior_sessions": 6,
            "prior_days": 4,
            "first_ever": "2026-09-01T10:00:00Z",
            "last_before": "2026-10-10T10:00:00Z",
        }
    ]
    return j.people(census, prior, c, m)


# ---------------------------------------------------------------- settings, map and requests


def test_settings_freeze_the_window_and_refuse_bad_inputs(j, c):
    assert (c["start"], c["end"], c["today"]) == (
        "2026-10-15 14:00:00",
        "2026-10-16 14:00:00",
        "2026-10-16",
    )
    assert c["website_hosts"] == ["example.com"] and c["max_people_detailed"] == 12
    for bad in (
        {"lookback_hours": 0},
        {"lookback_hours": 169},
        {"website_hosts": ["not a host"]},
        {"max_people_detailed": 31},
        {"exclude_email_domains": ["@x"]},
        {"notes": "x" * 2001},
    ):
        with pytest.raises(j.ContractError):
            j.settings(bad, now=NOW)


def test_the_map_refuses_names_a_query_would_quote_unsafely(j, m):
    for broken in (
        {"key_events": {"signup": ["x' OR 1=1 --"]}},
        {"key_events": {"surprise": ["a"]}},
        {"identity": {"email": "email) OR (1"}},
        {"steps": ["never_seen"]},
        {"replay_url": "https://evil.example/replay/"},
    ):
        with pytest.raises(j.ContractError):
            j.check_map({**copy.deepcopy(m), **broken})
    assert j.check_map(m) == m


def test_every_request_passes_tins_hogql_guard_and_scopes_hosts_and_the_team(j, c, m):
    steps = ["project", "inventory", "hosts", "census", "prior", "frictions", "loops", "paths"]
    payloads = [j.request(c, m, step) for step in steps]
    payloads.append(j.request(c, m, "timelines1", [ANA, BEN]))
    payloads.append(j.request(c, m, "recordings", session_ids=["01a1-session-1"] * 2))
    assert payloads[0] == {
        "service": "analytics",
        "step": "project",
        "operation": "project.read",
        "arguments": {},
    }
    for payload in payloads[1:]:
        assert payload["operation"] == "query.hogql"
        assert check_hogql(payload["arguments"]["query"]) == payload["arguments"]["query"]
    census = payloads[3]["arguments"]["query"]
    assert "properties.$host IN ('example.com', 'app.example.com')" in census
    assert (
        "endsWith(lower(coalesce(toString(person.properties.email), '')), '@example.com')" in census
    )
    assert "'account_created'" in census and "HAVING pageviews > 0" in census
    assert "toDateTime('2026-10-15 14:00:00')" in census
    paths = payloads[7]["arguments"]["query"]
    # Website pages only, the team and bot countries out, and key events joined by session.
    assert "properties.$host IN ('example.com')" in paths and "'app.example.com'" not in paths
    assert "NOT IN ('cn')" in paths and "@example.com" in paths
    assert "countIf(event IN ('account_created')) > 0 AS signup" in paths
    timeline = payloads[8]["arguments"]["query"]
    # Each person's first and last events, so a long day never loses where they stopped.
    assert "row_number() OVER (PARTITION BY person_id ORDER BY timestamp) AS n" in timeline
    assert "WHERE n <= 30 OR n > total - 30" in timeline
    recordings = payloads[9]["arguments"]["query"]
    assert "FROM raw_session_replay_events" in recordings
    assert "session_id IN ('01a1-session-1') GROUP BY session_id" in recordings
    for bad in ([], ["bad id!"], [f"01a1-session-{n:04d}" for n in range(121)]):
        with pytest.raises(j.ContractError):
            j.request(c, m, "recordings", session_ids=bad)
    with pytest.raises(j.ContractError):
        j.request(c, m, "timelines1", ["not-a-uuid"])
    with pytest.raises(j.ContractError):
        j.request(c, m, "timelines1", [str(UUID(int=n)) for n in range(16)])


def test_drift_names_silent_key_events_and_likely_renames(j, m):
    inventory = [
        {"event": name, "n": 9, "people": 3, "first_at": "", "last_at": ""}
        for name in (
            "$pageview",
            "account_created",
            "pricing_plan_selected",
            "first_report_viewed",
            "book_cta_clicked",
            "estimate_failed",
        )
    ]
    assert j.check_drift(m, inventory) == {
        "silent": ["pricing_plan_clicked"],
        "new": ["pricing_plan_selected"],
    }


def path_row(entry, route, exit_, sessions, **values):
    return {
        "entry": entry,
        "route": route,
        "exit": exit_,
        "sessions": sessions,
        "people": sessions,
        "known": 0,
        "pageviews": 1.0,
        "bounced": 0,
        "intent": 0,
        "signup": 0,
        "rage": 0,
        **values,
    }


def test_site_paths_show_where_visitors_come_in_and_leave(j):
    columns = list(j.PATHS)
    rows = [
        path_row("/", "/", "/", 40, bounced=38, known=2),
        path_row("/", "/ > /about", "/about", 9, pageviews=2.2),
        path_row("/", "/ > /pricing", "/pricing", 5, intent=3, pageviews=2.0),
        path_row("/blog/a", "/blog/a", "/blog/a", 6, bounced=6),
        path_row("/blog/a", "/blog/a > /", "/", 1, rage=1),
        path_row("/", "/ > /pricing > /sign-up", "/sign-up", 2, intent=2, signup=2),
    ]
    found = j.path_rows({"columns": columns, "rows": [[r[k] for k in columns] for r in rows]})
    site = j.site_paths(found, has_more=True)
    assert {k: site[k] for k in ("sessions", "known", "bounced", "intent", "signup", "rage")} == {
        "sessions": 63,
        "known": 2,
        "bounced": 44,
        "intent": 5,
        "signup": 2,
        "rage": 1,
    }
    assert site["landing"][:2] == [
        {"path": "/", "sessions": 56, "bounced": 38},
        {"path": "/blog/a", "sessions": 7, "bounced": 6},
    ]
    # Exits count browsing sessions that never reached intent or signup; one-page visits and
    # sessions that went on to sign up are not leaving from where they ended.
    assert site["exits"] == [
        {"path": "/about", "sessions": 9},
        {"path": "/pricing", "sessions": 2},
        {"path": "/", "sessions": 1},
    ]
    assert [(r["route"], r["reached"]) for r in site["routes"]] == [
        ("/ > /about", None),
        ("/ > /pricing", "intent"),
        ("/ > /pricing > /sign-up", "signup"),
    ]
    assert site["truncated"] is True
    with pytest.raises(j.ContractError):
        j.path_rows({"columns": columns[:-1], "rows": []})


# ---------------------------------------------------------------- people, journeys, frictions


def test_every_person_gets_one_tier_and_only_identified_people_are_named(j, people):
    tiers = {p["person_id"]: p["tier"] for p in people}
    assert tiers == {
        ANA: "new_user",
        BEN: "lead",
        CARA: "user",
        TEAM: "team",
        BOT: "bot",
        PASSER: "passer_by",
        BROWSER: "browser",
        ACCOUNT: "account",
    }
    ana = next(p for p in people if p["person_id"] == ANA)
    assert ana["email"] == "ana@fieldnotes.dev" and "intent" in ana["flags"]
    assert next(p for p in people if p["person_id"] == CARA)["flags"] == ["returning"]
    assert j.counts(people) == {
        "people": 8,
        "identified": 4,
        "lead": 1,
        "new_user": 1,
        "user": 1,
        "browser": 1,
        "passer_by": 1,
        "account": 1,
        "team": 1,
        "bot": 1,
    }
    # New users and failures are read first, then anonymous browsers; the team, bots,
    # passers-by and server-only accounts never, even with a failure.
    assert j.choose(people, 2)[:2] == [ANA, BROWSER]
    assert set(j.choose(people, 30)) == {ANA, BEN, CARA, BROWSER}
    with pytest.raises(j.ContractError):
        j.named(next(p for p in people if p["person_id"] == BROWSER), "anonymous")


def test_a_journey_keeps_what_the_person_did_and_folds_noise(j, m):
    at = iter(f"2026-10-16T09:{n:02d}:00Z" for n in range(60))

    def row(event, **values):
        return {
            "person": ANA,
            "timestamp": next(at),
            "event": event,
            "session": "01a1-session-1",
            "host": "example.com",
            "path": "/pricing",
            "element": "",
            "message": "",
            "scroll": None,
            **values,
        }

    rows = [
        row("$pageview"),
        row("pricing_section_viewed"),
        row("$autocapture", element="Pro plan"),
        row("pricing_plan_clicked"),
        row("$autocapture"),
        row("$autocapture", element="Continue"),
        row("$autocapture", element="Continue"),
        row("$rageclick"),
        row("estimate_failed", message="timeout"),
    ]
    found = j.journey(
        rows, m, j.recordings({"columns": ["session_id"], "rows": [["01a1-session-1"]]})
    )
    assert [(s["label"], s["times"]) for s in found["steps"]] == [
        ("opened /pricing", 1),
        ("clicked Pro plan on /pricing", 1),
        ("pricing_plan_clicked (intent)", 1),
        ("clicked Continue on /pricing", 2),
        ("rage-clicked on /pricing", 1),
        ("estimate_failed (failure): timeout", 1),
    ]
    assert found["other"] == 2 and found["replays"] == [REPLAY + "01a1-session-1"]
    recorded = {"01a1-session-1", "bad id!"}
    assert j.replay(m, "01a1-session-1", recorded) == REPLAY + "01a1-session-1"
    assert j.replay({"replay_url": None}, "01a1-session-1", recorded) is None
    assert j.replay(m, "bad id!", recorded) is None
    # A session PostHog did not record opens to "Recording not found": no link.
    assert j.replay(m, "01a1-session-1", frozenset()) is None
    assert j.journey(rows, m)["replays"] == []


def test_a_long_journey_keeps_its_end_and_marks_the_events_in_between(j, m):
    def row(n, event, **values):
        return {
            "person": ANA,
            "timestamp": f"2026-10-16T09:{n % 60:02d}:00Z",
            "event": event,
            "session": "01a1-session-1",
            "host": "example.com",
            "path": "/pricing",
            "element": "",
            "message": "",
            "scroll": None,
            "n": n,
            "total": 140,
            **values,
        }

    rows = [row(1, "$pageview"), row(2, "$autocapture", element="Pro plan")]
    rows += [
        row(139, "$autocapture", element="Pay"),
        row(140, "estimate_failed", message="timeout"),
    ]
    found = j.journey(rows, m)
    assert [s["label"] for s in found["steps"]] == [
        "opened /pricing",
        "clicked Pro plan on /pricing",
        "... 136 events in between not read",
        "clicked Pay on /pricing",
        "estimate_failed (failure): timeout",
    ]


def test_frictions_need_two_people_unless_the_product_failed_or_a_signup_was_at_stake(j, m, people):
    rows = [
        {
            "event": "$rageclick",
            "path": "/pricing",
            "element": "Pro plan",
            "message": "",
            "n": 3,
            "people": 1,
            "sessions": 1,
            "first_at": "a",
            "last_at": "b",
            "person_ids": [ANA],
            "session_ids": ["s-1"],
        },
        {
            "event": "$exception",
            "path": "/",
            "element": "",
            "message": "Object Not Found Matching Id",
            "n": 1,
            "people": 1,
            "sessions": 1,
            "first_at": "a",
            "last_at": "b",
            "person_ids": [PASSER],
            "session_ids": [],
        },
        {
            "event": "estimate_failed",
            "path": "/estimate",
            "element": "",
            "message": "",
            "n": 4,
            "people": 1,
            "sessions": 1,
            "first_at": "a",
            "last_at": "b",
            "person_ids": [BROWSER],
            "session_ids": [],
        },
        {
            "event": "$rageclick",
            "path": "/admin",
            "element": "",
            "message": "",
            "n": 9,
            "people": 1,
            "sessions": 1,
            "first_at": "a",
            "last_at": "b",
            "person_ids": [TEAM],
            "session_ids": [],
        },
    ]
    loops = [{"path": "/connect", "people": 2, "sessions": 2, "most": 4, "person_ids": [ANA, CARA]}]
    found = j.frictions(rows, loops, j.stalls(people, m), people)
    shown = {(f["kind"], f["page"], f["detail"]): (f["reportable"], f["people"]) for f in found}
    assert shown == {
        ("rage_click", "/pricing", "Pro plan"): (True, 1),  # a new user, on the way to signup
        ("error", "/", "Object Not Found Matching Id"): (False, 1),  # one anonymous visitor
        ("failure", "/estimate", "estimate_failed"): (True, 1),  # the product failed
        ("loop", "/connect", "opened 4 times in one session"): (True, 2),
        ("stall", "", "reached account_created, never first_report_viewed"): (True, 1),
    }
    # The team's rage clicks are not a friction; visiting without starting is not a stall.
    assert not any(f["page"] == "/admin" or "reached $pageview" in f["detail"] for f in found)


def test_the_ledger_carries_frictions_from_new_to_ongoing_to_fixed(j, c, people):
    friction = {
        "id": "f1",
        "kind": "loop",
        "page": "/connect",
        "detail": "x",
        "people": 2,
        "reportable": True,
    }
    day1 = j.carry({"frictions": [], "people": []}, people, [friction], c)
    assert [(f["status"], f["days"]) for f in day1["frictions"]] == [("new", ["2026-10-16"])]
    assert {p["email"]: p["came_back"] for p in day1["people"]} == {
        "ana@fieldnotes.dev": False,
        "ben@quanta.io": False,
        "cara@studio.design": False,
    }
    c2 = j.settings({}, now=datetime(2026, 10, 17, 14, 0, tzinfo=UTC))
    day2 = j.carry(day1, people[:1], [friction], c2)
    assert [(f["status"], len(f["days"])) for f in day2["frictions"]] == [("ongoing", 2)]
    assert (
        next(p for p in day2["people"] if p["email"] == "ana@fieldnotes.dev")["came_back"] is True
    )
    quiet = j.carry(day2, [], [], j.settings({}, now=datetime(2026, 10, 20, 14, 0, tzinfo=UTC)))
    assert quiet["frictions"][0]["status"] == "ongoing"  # three days quiet: not yet fixed
    fixed = j.carry(day2, [], [], j.settings({}, now=datetime(2026, 10, 23, 14, 0, tzinfo=UTC)))
    assert fixed["frictions"][0]["status"] == "fixed"
    gone = j.carry(day2, [], [], j.settings({}, now=datetime(2027, 2, 1, 14, 0, tzinfo=UTC)))
    assert gone == {"frictions": [], "people": []}


# ---------------------------------------------------------------- the report and Tin's check


DEFAULT = object()
# Unique people per step, by days before the window's end: 0 is the last 24 hours.
FUNNEL_RESULT = {
    "columns": ["ago", "step1", "step2", "step3"],
    "rows": [[0, 4, 1, 1], [1, 3, 1, 0], [3, 7, 2, 1]],
}


def report(
    j,
    c,
    m,
    people,
    *,
    status="complete",
    reason="",
    requests=None,
    named=None,
    body=None,
    brief=DEFAULT,
    frictions=(),
    funnel=DEFAULT,
    previous=None,
):
    named = (
        named
        if named is not None
        else [
            j.named(
                p,
                f"{p['name'] or p['email']} came from {p['referrer']}.",
                "Email them.",
                "/connect",
                j.replay(m, p["session_ids"][0], set(p["session_ids"])),
            )
            for p in people
            if p["tier"] in j.NAMED_TIERS
        ]
    )
    requests = (
        requests
        if requests is not None
        else [
            {
                "step": "census",
                "operation": "query.hogql",
                "outcome": "ok",
                "rows": 7,
                "truncated": False,
            },
            {
                "step": "frictions",
                "operation": "query.hogql",
                "outcome": "ok",
                "rows": 2,
                "truncated": False,
            },
            {
                "step": "funnel",
                "operation": "query.hogql",
                "outcome": "ok",
                "rows": 3,
                "truncated": False,
            },
        ]
    )
    if funnel is DEFAULT:
        answered = any(r["step"] == "funnel" and r["outcome"] == "ok" for r in requests)
        funnel = j.funnel(FUNNEL_RESULT, m) if answered else None
    ledger = j.carry({"frictions": [], "people": []}, people, [], c)
    site = j.site_paths([path_row("/", "/", "/", 4, bounced=4)])
    if brief is DEFAULT:
        brief = j.brief(
            [(named[0]["email"], "Signed up from the hackathon page.")] if named else [],
            [("Most visitors left after the homepage.", None)],
        )
    evidence = j.assemble(
        c,
        status,
        reason,
        requests,
        people,
        named,
        list(frictions),
        ledger,
        m,
        site=site,
        brief=brief,
        funnel=funnel,
        previous=previous,
    )
    top = "\n\n" + j.render_brief(evidence["brief"]) if brief is not None else ""
    text = (
        body
        if body is not None
        else "# Visitors\n\nStatus: "
        + status
        + top
        + "\n\n## People to act on today\n\n"
        + "\n".join(f"- {n['email']}" for n in named)
    )
    return text + j.render_evidence(evidence)


def test_the_resource_and_tin_agree_on_the_contract(j):
    assert j.VERSION == visitor_journal.VALIDATOR and j.EVIDENCE_MARKER == visitor_journal.MARKER
    assert set(j.STATUSES) == visitor_journal.STATUSES
    assert set(j.NAMED_TIERS) == visitor_journal.NAMED_TIERS == set(records.WEB_TIERS)
    assert (
        j.SUMMARY_LIMIT == visitor_journal.SUMMARY_LIMIT
        and j.MAX_NAMED == visitor_journal.MAX_PEOPLE
    )


def test_a_complete_report_passes_both_checks_and_names_its_people(j, c, m, people):
    text = report(j, c, m, people)
    assert j.check(text)["counts"]["people"] == 8
    outcome = visitor_journal.read(text)
    assert (outcome.status, outcome.measured_nothing, outcome.window_end) == (
        "complete",
        None,
        "2026-10-16 14:00:00",
    )
    assert [(p.email, p.tier) for p in outcome.people] == [
        ("ana@fieldnotes.dev", "new_user"),
        ("ben@quanta.io", "lead"),
        ("cara@studio.design", "user"),
    ]
    assert outcome.people[0].replay_url == REPLAY + "01a1-session-0001"
    assert visitor_journal.summary(outcome) == (
        "Yesterday: 4 unique visitors, 1 clicked pricing, 1 signed up."
    )
    plain = visitor_journal.read(report(j, c, m, people, brief=None))
    assert plain.brief is None
    assert visitor_journal.summary(plain) == "1 new user, 1 lead, 1 user among 8 visitors."
    assert j.previous(text)["map"] == m
    assert j.previous(text)["site"]["landing"] == [{"path": "/", "sessions": 4, "bounced": 4}]


FAILURE = {
    "id": "aaaaaaaaaaaa",
    "kind": "failure",
    "page": "",
    "detail": "checkout_failed: card declined",
    "people": 2,
    "reportable": True,
}
UNLABELED = {**FAILURE, "id": "bbbbbbbbbbbb", "kind": "dead_click", "page": "/c", "detail": ""}


def test_the_brief_opens_with_the_funnel_then_visitors_then_observations(j, c, m, people):
    b = j.brief(
        [
            ("ben@quanta.io", "Clicked pricing twice and left."),
            ("cara@studio.design", "Back again."),
        ],
        [
            ("There may be a bug in the pricing calculator: Ben clicked it twice.", None),
            ("Checkout failed for 2 people.", FAILURE["id"]),
        ],
    )
    text = report(j, c, m, people, brief=b, frictions=[FAILURE, UNLABELED])
    j.check(text)
    head = text.split(j.EVIDENCE_MARKER)[0]
    assert "| | Unique visitors | Clicked pricing | Signed up |" in head
    assert "| Yesterday | 4 | 1 | 1 |" in head
    assert "| Last 7 days, daily average | 2 | 0.6 | 0.3 |" in head
    assert (
        head.index("Status: complete") < head.index("**Some visitors**") < head.index("## People")
    )
    assert "(ben@quanta.io): Clicked pricing twice and left. [Replay](" + REPLAY in head
    brief = visitor_journal.read(text).brief
    assert brief.steps[0] == visitor_journal.Step("Unique visitors", 4, 2.0)
    assert [v.email for v in brief.visitors] == ["ben@quanta.io", "cara@studio.design"]
    assert brief.visitors[0].replay_url.startswith(REPLAY) and len(brief.observations) == 2
    # A step the product never fires is not tracked, which is not zero.
    fn = j.funnel(FUNNEL_RESULT, m, [{"event": "$pageview"}])
    assert [(x["yesterday"], x["average"]) for x in fn["steps"]] == [
        (4, 2.0),
        (None, None),
        (None, None),
    ]
    assert "| Yesterday | 4 | not tracked | not tracked |" in j.render_brief(
        {"funnel": fn, "visitors": [], "observations": []}
    )
    query = j.request(c, m, "funnel")["arguments"]["query"]
    assert check_hogql(query) == query and "uniqIf(person_id, event = '$pageview')" in query
    assert "toDateTime('2026-10-09 14:00:00')" in query  # seven days before the window's end


def test_the_check_refuses_a_brief_tin_could_not_stand_behind(j, c, m, people):
    frictions = [FAILURE, UNLABELED]
    cases = [
        (None, "needs a brief"),
        (j.brief([], [("Checkout is broken.", None)]), "may be a bug"),
        (j.brief([], [("The candidate list fails.", UNLABELED["id"])]), "may be a bug"),
        (j.brief([], [("The census shows Ben.", None)]), "founder's words"),
    ]
    for b, message in cases:
        with pytest.raises(j.ContractError, match=message):
            j.check(report(j, c, m, people, brief=b, frictions=frictions))
    with pytest.raises(j.ContractError, match="not a named person"):
        report(j, c, m, people, brief=j.brief([("zed@nowhere.dev", "Hi.")], []))
    other = j.check_map({**m, "funnel": m["funnel"][:2]})
    with pytest.raises(j.ContractError, match="map's funnel"):
        j.check(report(j, c, m, people, funnel=j.funnel(FUNNEL_RESULT, other)))
    no_query = [{"step": "census", "operation": "query.hogql", "outcome": "ok"}]
    with pytest.raises(j.ContractError, match="answered funnel query"):
        j.check(report(j, c, m, people, requests=no_query, funnel=j.funnel(FUNNEL_RESULT, m)))
    # Refused: the table says so instead of counting zeros.
    assert "Visitors were not measured today." in report(j, c, m, people, requests=no_query)
    text = report(j, c, m, people)
    moved = text.replace("\n\n## People to act on today", "", 1).replace(
        "Status: complete\n\n", "Status: complete\n\n## People to act on today\n\n", 1
    )
    with pytest.raises(j.ContractError, match="under the Status line"):
        j.check(moved)
    with pytest.raises(j.ContractError, match="funnel starts with visitors"):
        j.check_map({**m, "funnel": m["funnel"][1:]})


def test_the_brief_says_only_what_is_new_since_the_last_one(j, c, m, people):
    ben, cara = "ben@quanta.io", "cara@studio.design"
    gap = "Signups are not tracked, so the table cannot count them."
    last = j.previous(
        report(j, c, m, people, brief=j.brief([(ben, "Clicked pricing.")], [(gap, None)]))
    )
    assert last["brief"]["visitors"][0]["email"] == ben and last["window"]["end"] == c["end"]
    # The next morning's run: its window overlaps the last one, as after a run started by hand.
    later = {**last, "window": {"start": "2026-10-15 09:00:00", "end": "2026-10-16 10:00:00"}}
    with pytest.raises(j.ContractError, match="ben@quanta.io was in the last brief"):
        report(j, c, m, people, brief=j.brief([(ben, "Clicked pricing.")], []), previous=later)
    with pytest.raises(j.ContractError, match="already said"):
        report(j, c, m, people, brief=j.brief([], [(gap.upper(), None)]), previous=later)
    # Someone new, and an observation that says what changed, are fine.
    j.check(
        report(
            j,
            c,
            m,
            people,
            brief=j.brief([(cara, "Back again.")], [("Signups are tracked now.", None)]),
            previous=later,
        )
    )
    # Ben came back after the last brief's window ended (his last activity is 09:30).
    earlier = {**later, "window": {"start": "2026-10-15 08:00:00", "end": "2026-10-16 09:00:00"}}
    j.check(
        report(
            j,
            c,
            m,
            people,
            brief=j.brief([(ben, "Came back and booked a call.")], []),
            previous=earlier,
        )
    )
    # The first run, or a report from before 1.3.0, has no brief to compare with.
    assert j.previous(None)["brief"] is None
    j.check(report(j, c, m, people, previous=j.previous(None)))


def with_work(j, m, people):
    """The default named people, with what a web search found about Ana's work."""
    work = j.profile(
        company="Fieldnotes",
        role="Founder",
        about="a research notebook for UX teams.",  # its own period is not doubled
        linkedin_url="https://www.linkedin.com/in/anasouza",
        company_url="https://fieldnotes.dev",
        basis="name and fieldnotes.dev email",
    )
    return [
        j.named(
            p,
            f"{p['name'] or p['email']} came from {p['referrer']}.",
            "Email them.",
            "/connect",
            j.replay(m, p["session_ids"][0], set(p["session_ids"])),
            work=work if p["email"] == "ana@fieldnotes.dev" else None,
        )
        for p in people
        if p["tier"] in j.NAMED_TIERS
    ]


def test_new_users_open_the_brief_with_where_they_work(j, c, m, people):
    ana, ben, cara = "ana@fieldnotes.dev", "ben@quanta.io", "cara@studio.design"
    named = with_work(j, m, people)
    # New: Ana signed up, Ben gave an email for the first time; Cara has been here before.
    assert [(n["email"], n["new"]) for n in named] == [(ana, True), (ben, True), (cara, False)]
    b = j.brief(
        [(cara, "Back again.")],
        [],
        [(ana, "Came from the hackathon and created an account."), (ben, "Booked a call.")],
    )
    text = report(j, c, m, people, named=named, brief=b)
    j.check(text)
    head = text.split(j.EVIDENCE_MARKER)[0]
    assert head.index("**New users**") < head.index("**Some visitors**")
    assert (
        "- **Ana Souza** (ana@fieldnotes.dev): Founder at Fieldnotes; a research notebook for UX "
        "teams. Came from the hackathon and created an account. "
        "[LinkedIn](https://www.linkedin.com/in/anasouza) · [Fieldnotes](https://fieldnotes.dev) · "
        "[Replay](" + REPLAY + "01a1-session-0001)"
    ) in head
    assert "- **Ben Ode** (ben@quanta.io): Booked a call. [Replay](" in head
    brief = visitor_journal.read(text).brief
    assert [v.email for v in brief.new] == [ana, ben] and [v.email for v in brief.visitors] == [
        cara
    ]
    assert brief.new[0].work.headline == "Founder at Fieldnotes"
    assert brief.new[0].work.linkedin_url == "https://www.linkedin.com/in/anasouza"
    assert brief.new[1].work is None
    # Nobody new is said plainly.
    quiet = report(j, c, m, people, named=named, brief=j.brief([(cara, "Back again.")], []))
    assert "**New users**\n\n- Nobody new signed up or left an email." in quiet
    # Someone who is not new, a link that is not a profile, a role nothing ties to them.
    with pytest.raises(j.ContractError, match="not a named new person"):
        report(j, c, m, people, named=named, brief=j.brief([], [], [(cara, "Back again.")]))
    with pytest.raises(j.ContractError, match="not a LinkedIn profile"):
        j.profile(linkedin_url="https://evil.example/in/anasouza", basis="name")
    with pytest.raises(j.ContractError, match="basis"):
        j.profile(company="Fieldnotes", role="Founder")
    assert j.profile() is None and j.profile(company="Fieldnotes")["company"] == "Fieldnotes"
    # Turned off: no work at all.
    assert c["look_up_new_people"] is True
    off = j.settings({"look_up_new_people": False}, now=NOW)
    assert off["look_up_new_people"] is False
    with pytest.raises(j.ContractError, match="turned off looking up"):
        report(j, {**c, "look_up_new_people": False}, m, people, named=named, brief=b)
    # Tin keeps only a LinkedIn profile and an https site, and work only for new people.
    tampered = text.replace("https://www.linkedin.com/in/anasouza", "javascript:alert(1)")
    assert visitor_journal.read(tampered).people[0].work.linkedin_url is None
    rows = [{**n, "new": False} for n in named]
    assert (
        visitor_journal.read(report(j, c, m, people, named=rows, brief=None)).people[0].work is None
    )
    # Tin enforces the switch and the basis itself, whatever the report says.
    assert visitor_journal.work_allowed({}) and visitor_journal.work_allowed(None)
    assert not visitor_journal.work_allowed({"look_up_new_people": False})
    off_read = visitor_journal.read(text, work=False)
    assert off_read.people[0].work is None and off_read.brief.new[0].work is None
    untied = text.replace('"basis": "name and fieldnotes.dev email"', '"basis": ""')
    loose = visitor_journal.read(untied).people[0].work
    assert (loose.company, loose.role, loose.linkedin_url) == ("Fieldnotes", "", None)
    # A new person the last brief listed, with nothing since, is left out like any visitor.
    last = j.previous(text)
    with pytest.raises(j.ContractError, match="ana@fieldnotes.dev was in the last brief"):
        report(
            j, c, m, people, named=named, brief=j.brief([], [], [(ana, "Again.")]), previous=last
        )


def test_the_check_refuses_what_tin_would_not_publish_or_trust(j, c, m, people):
    with pytest.raises(j.ContractError, match="Status:"):
        j.check(report(j, c, m, people).replace("Status: complete", "Status: done"))
    with pytest.raises(j.ContractError, match="not in the report"):
        j.check(report(j, c, m, people, body="# Visitors\n\nStatus: complete\n"))
    truncated = [
        {
            "step": "census",
            "operation": "query.hogql",
            "outcome": "ok",
            "rows": 1000,
            "truncated": True,
        }
    ]
    with pytest.raises(j.ContractError, match="incomplete"):
        j.check(report(j, c, m, people, requests=truncated))
    j.check(
        report(
            j,
            c,
            m,
            people,
            status="incomplete",
            reason="The census hit 1000 people.",
            requests=truncated,
        )
    )
    forged = [
        {
            **j.named(people[0], "x"),
            "replay_url": "https://us.posthog.com/project/9/replay/abcdefgh",
        }
    ]
    with pytest.raises(j.ContractError, match="replay"):
        j.check(report(j, c, m, people, named=forged))


def test_a_plausible_but_unusable_report_publishes_without_writing_strangers(j, c, m, people):
    """The model names an anonymous visitor, a person with no email, a person the report never
    mentions and a hand-written replay link. Tin keeps the report and writes only the real one."""
    good = j.named(people[0], "Signed up from the hackathon page.")
    rows = [
        {**good, "replay_url": "https://us.posthog.com/project/9/replay/abcdefgh"},
        {**good, "email": "not-an-email", "summary": "x"},
        {**good, "email": "visitor@anonymous.example", "tier": "browser"},
        {**good, "email": "ghost@nowhere.dev"},
        {**good, "email": "ana@fieldnotes.dev", "summary": ""},
    ]
    text = report(
        j,
        c,
        m,
        people,
        named=rows,
        body="# Visitors\n\nStatus: complete\n\nana@fieldnotes.dev visitor@anonymous.example",
    )
    outcome = visitor_journal.read(text)
    assert [(p.email, p.replay_url) for p in outcome.people] == [("ana@fieldnotes.dev", None)]
    # A brief of the wrong shape is dropped, never fatal; it never names a stranger.
    broken = report(j, c, m, people).replace('"brief": {', '"brief": [], "was": {', 1)
    assert visitor_journal.read(broken).brief is None
    stranger = visitor_journal._brief(
        {"visitors": [{"email": "ghost@nowhere.dev", "line": "Hi."}]}, outcome.people
    )
    assert stranger is None


def test_every_query_refused_measured_nothing_and_bad_envelopes_raise(j, c, m, people):
    refused = [
        {
            "step": "census",
            "operation": "query.hogql",
            "outcome": "refused",
            "rows": 0,
            "truncated": False,
        }
    ]
    outcome = visitor_journal.read(
        report(
            j,
            c,
            m,
            people,
            status="incomplete",
            reason="PostHog refused every query.",
            requests=refused,
            named=[],
        )
    )
    assert outcome.measured_nothing and "refused" in visitor_journal.failure(outcome)
    config = visitor_journal.read(
        report(
            j,
            c,
            m,
            people,
            status="invalid configuration",
            reason="lookback_hours must be 1-168",
            requests=[],
            named=[],
        )
    )
    assert config.measured_nothing is None
    assert (
        visitor_journal.summary(config)
        == "Visitor journeys could not start: lookback_hours must be 1-168."
    )
    nobody = visitor_journal.read(
        report(j, c, m, [], status="nothing to report", reason="Only the team came.", named=[])
    )
    assert visitor_journal.summary(nobody) == "Nobody outside the team came in the window."
    text = report(j, c, m, people)
    for broken in (
        text.replace(visitor_journal.MARKER, ""),
        text + visitor_journal.MARKER,
        text.replace('"provider": "analytics.posthog"', '"provider": "analytics.other"'),
        text.replace('"status_reason": ""', '"status_reason": "why"'),
        text.replace("```json\n", "```json\n{"),
    ):
        with pytest.raises(ValueError):
            visitor_journal.validate(broken)


# ---------------------------------------------------------------- the package and its binding


def package_files(key="product.visitor_journeys", **changes):
    manifest = json.loads((PACKAGE / "workflow.json").read_text())
    manifest["definition"]["key"] = key
    if "path_template" in changes:
        manifest["definition"]["procedure"]["output"]["path_template"] = changes["path_template"]
    files = {
        f"workflow_packages/{key}/{p.relative_to(PACKAGE)}": p.read_bytes()
        for p in PACKAGE.rglob("*")
        if p.is_file()
    }
    files[f"workflow_packages/{key}/workflow.json"] = json.dumps(manifest).encode()
    return files, f"workflow_packages/{key}/workflow.json"


async def test_the_package_validates_and_binds_its_validator_to_its_own_report():
    await validate(ContributedPackage("product.visitor_journeys", PACKAGE))
    files, path = package_files()
    definition, _ = await validate_files(files, definition_path=path)
    assert definition["procedure"]["services"]["analytics"]["max_calls"] == 16
    # The validator writes People, so only this package at its own report path may use it.
    for files, path in (
        package_files("custom.visitor_journeys"),
        package_files(path_template="reports/people/{run_id}.md"),
    ):
        with pytest.raises(ValueError, match="visitor journal"):
            await validate_files(files, definition_path=path)


async def test_the_catalog_pins_the_reviewed_package():
    from test_public_workflows import PublishedSnapshots
    from test_registry_recipe_publication import WIKI, catalog_database

    from tin_lite import catalog, public_workflows
    from tin_lite.procedures import load_pinned_codex_procedure

    entry = next(
        w for w in public_workflows.PUBLIC_WORKFLOWS if w.key == "product.visitor_journeys"
    )
    assert entry.public_mcp.name == "start_visitor_journeys"
    db, storage = catalog_database(), PublishedSnapshots()
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    row = db.rows[entry.id]
    assert row.definition["schedule_modes"] == ["on_demand", "daily", "weekly"]
    procedure = await load_pinned_codex_procedure(
        storage=storage,
        repo_id=row.definition_repo_id,
        commit_sha=row.current_commit_sha,
        definition_path=row.definition_path,
    )
    run_id = uuid4()
    assert (
        procedure.resolve_inputs(inputs={}, run_id=run_id).output_path
        == f"reports/visitors/{run_id}.md"
    )
    assert procedure.skill_files["visitor-journeys/JOURNEYS.md"] == RESOURCE.read_bytes()


async def test_the_qualification_contract_covers_the_package():
    from tin_lite.workflow_qualification import Qualification, check_package

    contract = Qualification.model_validate_json(
        (ROOT / "workflow_evals/product.visitor_journeys/qualification.json").read_bytes()
    )
    files = {str(p.relative_to(ROOT)): p.read_bytes() for p in PACKAGE.rglob("*") if p.is_file()}
    checked = await check_package(
        files, "workflow_packages/product.visitor_journeys/workflow.json", contract
    )
    assert checked["cost"]["configured_ceiling_usd"] == "5"


async def test_the_resource_drives_the_offline_posthog_gateway(j, c, m):
    """The payloads go through the gateway's argument checks; a refusal is recorded, not zero."""
    fake = FakePostHogConnection(
        posthog_objects(), service="analytics", max_calls=16, refuse={"census": "budget_exceeded"}
    )
    info = j.project_info(await fake.call(**j.request(c, m, "project")))
    assert info["replay_url"] == "https://eu.posthog.com/project/101/replay/"
    assert info["capture"]["session_recording_opt_in"] is True
    with pytest.raises(CodeServiceError) as refused:
        await fake.call(**j.request(c, m, "census"))
    line = j.outcome("census", "query.hogql", error=refused.value)
    assert line["outcome"] == "refused" and line["rows"] == 0
    assert [call["operation"] for call in fake.calls] == ["project.read", "query.hogql"]


# ---------------------------------------------------------------- People


async def project(db) -> UUID:
    created = await db.create_project(name="Synthetic visitors", state_repo_id="p/" + uuid4().hex)
    return created.id


async def test_a_published_report_writes_its_people_once(j, c, m, people, publication_db):
    project_id = await project(publication_db)
    outcome = visitor_journal.read(report(j, c, m, people, named=with_work(j, m, people)))
    async with publication_db.pool.acquire() as conn:
        forgotten = await records.upsert_person(
            conn, project_id, {"email": "cara@studio.design"}, source="test"
        )
        await records.forget(conn, project_id, forgotten)
        linkedin = await records.upsert_person(
            conn, project_id, {"email": "BEN@quanta.io"}, source="test"
        )
        await records.ensure_fields(conn, project_id)
        await records.set_values(
            conn,
            project_id,
            linkedin,
            [records.Value("name", "Benedict Ode")],
            set_by_kind="run",
            set_by="engagers-run",
        )
        assert await visitor_people.write(conn, project_id, None, outcome) == 2
        assert (
            await visitor_people.write(conn, project_id, None, outcome) == 2
        )  # a retry adds nothing
        listed = await records.directory(conn, project_id, viewer="member")
        events = await conn.fetchval(
            "SELECT count(*) FROM record_events WHERE project_id = $1", project_id
        )
    by_email = {p["email"]: p for p in listed}
    assert set(by_email) == {"ana@fieldnotes.dev", "ben@quanta.io"}  # Cara was forgotten
    assert events == 2
    ana = by_email["ana@fieldnotes.dev"]
    assert ana["name"] == "Ana Souza" and ana["added"]["by"] == "visitors"
    assert (
        ana["fields"]["web_tier"]["value"] == "new_user"
        and ana["fields"]["web_tier"]["reason"] == "/connect"
    )
    assert ana["fields"]["web_summary"]["value"] == "Ana Souza came from devpost.com."
    assert ana["headline"] == "Founder at Fieldnotes"  # what a web search found
    assert ana["last_touch"]["kind"] == "visited"
    # A name another source set stays.
    assert by_email["ben@quanta.io"]["name"] == "Benedict Ode"
    async with publication_db.pool.acquire() as conn:
        detail = await records.person(conn, project_id, ana["id"], viewer="member")
    [visit] = detail["events"]
    assert (visit["kind"], visit["action"], visit["stopped_at"]) == (
        "visited",
        "Email them.",
        "/connect",
    )
    assert visit["replay_url"] == REPLAY + "01a1-session-0001"


async def test_people_never_fails_a_published_run(publication_db, caplog):
    run = SimpleNamespace(id=uuid4(), project_id=uuid4())
    await visitor_people.record(database=publication_db, run=run, content=b"not a report")
    assert "could not update People" in caplog.text


def test_an_email_identifier_is_checked_and_compared_without_case():
    assert records._pairs({"email": " Ana@Fieldnotes.dev "}) == [("email", "ana@fieldnotes.dev")]
    for bad in ("ana", "ana@", "@fieldnotes.dev", "ana@fieldnotes"):
        with pytest.raises(records.RecordError):
            records._pairs({"email": bad})


def test_ignored_host_families_scope_queries_when_no_hosts_are_named(j, m):
    c = j.settings({}, now=NOW)
    wide = j.check_map(
        {
            **m,
            "hosts": {
                "website": [],
                "product": [],
                "ignore": ["*.Vercel.app", "staging.example.com"],
            },
        }
    )
    assert wide["hosts"]["ignore"] == ["*.vercel.app", "staging.example.com"]
    census = j.request(c, wide, "census")["arguments"]["query"]
    assert check_hogql(census)
    assert (
        "((properties.$host NOT IN ('staging.example.com') AND "
        "NOT endsWith(properties.$host, '.vercel.app')) OR properties.$host IS NULL)"
    ) in census
    with pytest.raises(j.ContractError):
        j.check_map({**m, "hosts": {"ignore": ["*vercel.app"]}})


def test_an_interrupted_journal_is_kept_as_partial_text_and_never_published():
    from tin_lite import interrupted_procedure
    from tin_lite.procedures import (
        PinnedCodexProcedure,
        SandboxProfile,
        validate_procedure_artifact,
    )

    spec = PinnedCodexProcedure(
        workflow_key="product.visitor_journeys",
        prompt="",
        entry_skill="visitor-journeys",
        skill_files={},
        output_path="reports/visitors/run.md",
        output_media_type="text/markdown",
        output_validator="visitor-journal.v1",
        output_max_bytes=256000,
        sandbox=SandboxProfile(profile="isolated"),
    )
    partial = b"# Visitors\n\nStatus: incomplete\n\n## People to act on today\n"
    assert interrupted_procedure.eligible(spec)
    with pytest.raises(ValueError):
        validate_procedure_artifact(partial, spec=spec)
    validate_procedure_artifact(partial, spec=interrupted_procedure._partial(spec))


WEEK_FUNNEL = {
    "columns": ["ago", "step1", "step2", "step3"],
    "rows": [[0, 40, 9, 3], [1, 30, 6, 2], [2, 20, 4, 1], [4, 10, 2, 1], [5, 99, 9, 9]],
}


def test_a_week_reads_seven_days_against_the_four_before_and_keeps_a_longer_brief(j, c, m, people):
    from tin_lite import run_summaries

    week = j.settings(
        {"period": "week", "lookback_hours": 24, "website_hosts": ["example.com"]}, now=NOW
    )
    assert (week["period"], week["start"], week["lookback_hours"]) == (
        "week",
        "2026-10-09 14:00:00",
        168,
    )
    with pytest.raises(j.ContractError, match="period"):
        j.settings({"period": "month"}, now=NOW)
    query = j.request(week, m, "funnel")["arguments"]["query"]
    assert check_hogql(query) == query and "604800) AS ago" in query and "LIMIT 5" in query
    assert "toDateTime('2026-09-11 14:00:00')" in query  # five weeks before the window's end
    fn = j.funnel(WEEK_FUNNEL, m, period="week")
    # This week against the 4 weeks before; the row past them is ignored.
    assert (fn["period"], fn["steps"][0]["yesterday"], fn["steps"][0]["average"]) == (
        "week",
        40,
        15.0,
    )
    long = "Signed up, connected GitHub and ran three audits across the week. " * 5
    b = j.brief([("ben@quanta.io", long)], [("Most visitors left after the homepage.", None)])
    text = report(j, week, m, people, brief=b, funnel=fn)
    j.check(text)
    head = text.split(j.EVIDENCE_MARKER)[0]
    assert "| This week | 40 | 9 | 3 |" in head
    assert "| The 4 weeks before, weekly average | 15 | 3 | 1 |" in head
    brief = visitor_journal.read(text).brief
    assert brief.period == "week" and len(brief.visitors[0].line) > 300
    summary = run_summaries.from_output("visitor-journal.v1", text)
    assert summary["headline"].startswith("This week: 40 unique visitors")
    assert [row[0] for row in summary["table"]["rows"]] == [
        "This week",
        "The 4 weeks before, weekly average",
    ]
    # A day keeps its shorter lines, and a week's table never goes in a day's brief.
    day = visitor_journal.read(report(j, c, m, people, brief=b)).brief
    assert len(day.visitors[0].line) <= 240
    with pytest.raises(j.ContractError, match="period"):
        report(j, week, m, people, brief=b, funnel=j.funnel(FUNNEL_RESULT, m))
