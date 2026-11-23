"""RB2B's webhook: a project's own address, the payloads it keeps, and a visitor's run."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from test_connection_collection import USER
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import Router, fixture

from tin_lite import people_icp, rb2b
from tin_lite import people_records as records
from tin_lite.auth import AuthContext, require_user
from tin_lite.integrations import CredentialCipher
from tin_lite.rb2b_activities import RB2BActivities

SEEN = datetime(2026, 11, 20, 8, 52, tzinfo=UTC)


def payload(**changes):
    base = {
        "LinkedIn URL": "https://www.linkedin.com/in/jane-doe-1234/",
        "First Name": "Jane",
        "Last Name": "Doe",
        "Title": "Founder",
        "Company Name": "Acme Software",
        "Business Email": "jane@acme.test",
        "Website": "https://acme.test ",
        "Industry": "Software",
        "Employee Count": "11-50",
        "Estimate Revenue": "$5M",
        "City": "Austin",
        "State": "Texas",
        "Zipcode": "73301",
        "Seen At": SEEN.isoformat(),
        "Referrer": "",
        "Captured URL": "https://www.acme.test/pricing",
        "Tags": "Hot Page",
    }
    base.update(changes)
    return base


# The payload.


def test_a_payload_reads_under_tins_names_with_empty_values_as_none():
    found = rb2b.lead(payload())
    assert found["name"] == "Jane Doe" and found["company"] == "Acme Software"
    assert found["website"] == "https://acme.test" and found["referrer"] is None
    assert found["location"] == "Austin, Texas" and found["at"] == SEEN
    assert found["repeat"] is False
    assert rb2b.lead(payload(is_repeat_visitor=True))["repeat"] is True
    assert rb2b.lead(payload(is_repeat_visit=True))["repeat"] is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-20T08:52:00Z", datetime(2026, 11, 20, 8, 52, tzinfo=UTC)),
        ("2026-11-20T08:52:00", datetime(2026, 11, 20, 8, 52, tzinfo=UTC)),
        # RB2B's documented sample isn't ISO 8601; its date and time still read.
        ("2024-01-01T12:34:56:00.00+00.00", datetime(2024, 1, 1, 12, 34, 56, tzinfo=UTC)),
        ("2026-11-20T03:52:00-05:00", datetime(2026, 11, 20, 8, 52, tzinfo=UTC)),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_seen_at_reads_leniently_and_without_a_zone_as_utc(value, expected):
    assert rb2b.seen_at(value) == expected


def test_a_payload_is_a_person_a_company_or_rb2bs_test():
    assert rb2b.kind(rb2b.lead(payload())) == "person"
    company = payload(
        **{"First Name": None, "Last Name": "", "Title": None},
        **{"LinkedIn URL": "https://www.linkedin.com/company/acme/"},
    )
    assert rb2b.kind(rb2b.lead(company)) == "company"
    test = payload(**{"First Name": "RB2B", "Last Name": "Test Payload"})
    assert rb2b.kind(rb2b.lead(test)) == "test"


def test_the_same_view_twice_is_one_visit():
    one, again = rb2b.lead(payload()), rb2b.lead(payload(Tags="Hot Lead"))
    assert rb2b.dedupe_key(one) == rb2b.dedupe_key(again)
    later = rb2b.lead(payload(**{"Seen At": (SEEN + timedelta(minutes=1)).isoformat()}))
    assert rb2b.dedupe_key(later) != rb2b.dedupe_key(one)


def test_a_person_is_found_by_their_profile_and_a_real_email():
    ids = rb2b.identifiers(rb2b.lead(payload()))
    assert ids == {"linkedin_slug": "jane-doe-1234", "email": "jane@acme.test"}
    masked = rb2b.identifiers(rb2b.lead(payload(**{"Business Email": "x@****"})))
    assert "email" not in masked
    with pytest.raises(records.RecordError):
        rb2b.identifiers(rb2b.lead(payload(**{"LinkedIn URL": None})))


def test_how_a_visit_arrived():
    assert rb2b.channel("google", "cpc", "c", "data platform", None, None) == {
        "kind": "ad",
        "label": "Google Ads",
        "detail": "keyword “data platform”",
    }
    assert rb2b.channel(None, None, None, None, "syndicatedsearch.goog", None)["kind"] == "ad"
    assert rb2b.channel(None, None, None, None, "chatgpt.com", None)["label"] == "ChatGPT"
    assert rb2b.channel(None, None, None, None, "www.google.com", None)["kind"] == "organic"
    assert rb2b.channel(None, None, None, None, "youtube.com", None)["kind"] == "social"
    assert rb2b.channel(None, None, None, None, "$direct", None)["kind"] == "direct"


def test_a_visit_from_posthog_reads_as_one_line():
    found = rb2b.lead(payload())
    info = {
        "session_id": "s1",
        "events": [
            [
                "2026-11-20T08:52:00Z",
                "$pageview",
                "/pricing",
                None,
                None,
                "google",
                "cpc",
                None,
                "data platform",
                None,
                None,
            ],
            [
                "2026-11-20T08:53:30Z",
                "$pageview",
                "/vs/snowflake/",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ],
        ],
        "active_ms": 30_000,
        "other_sessions": 2,
        "first_touch": {"kind": "organic", "label": "Google search", "detail": ""},
    }
    seen = rb2b.visit(found, info, "https://us.posthog.com/project/1/replay/")
    assert seen["summary"].startswith("Ad: Google Ads, keyword “data platform” · read /pricing")
    assert "2 other visits in 90 days" in seen["summary"]
    assert "first came via Organic search: Google" in seen["summary"]
    assert seen["replay_url"] == "https://us.posthog.com/project/1/replay/s1"
    assert seen["stopped_at"] == "/vs/snowflake/"
    unmatched = rb2b.unmatched(found, 0, why="PostHog isn't connected")
    assert unmatched["summary"] == "On /pricing (PostHog isn't connected) · identified by RB2B"
    assert unmatched["channel"]["kind"] == "unknown"


# The webhook and the card.


def cipher():
    return CredentialCipher(base64.urlsafe_b64encode(b"k" * 32).decode())


def client(f, monkeypatch, *, user=USER, start=None):
    from tin_lite import rb2b_api

    started = []

    async def fake_start(**kwargs):
        started.append(kwargs)
        if start is not None:
            return await start(**kwargs)
        return SimpleNamespace(id=await visitor_run(f, kwargs["input_payload"]["visit_id"]))

    monkeypatch.setattr("tin_lite.run_service.start_workflow_run", fake_start)
    app = FastAPI()
    app.include_router(rb2b_api.router)
    app.state.runtime = SimpleNamespace(
        database=f.db, storage=f.storage, integrations=SimpleNamespace(_cipher=f.rb2b_cipher)
    )
    app.state.settings = SimpleNamespace(switchboard_public_url="https://tin.test/")
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=user,
        token_type="session_token",  # noqa: S106
    )
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://tin.test")
    http.started = started
    return http


async def visitor_run(f, visit_id) -> UUID:
    workflow_id = await f.db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", rb2b.KEY)
    if workflow_id is None:
        workflow_id = rb2b.WORKFLOW_ID
        await f.db.pool.execute(
            """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                    definition_path,current_commit_sha,version_label,definition)
               VALUES ($1,$2,$2,$2,'registry/workflows','r.json',$3,'1','{}')""",
            workflow_id,
            rb2b.KEY,
            "d" * 40,
        )
    run_id = uuid4()
    await f.db.pool.execute(
        """INSERT INTO workflow_runs
           (id,project_id,workflow_id,executor,definition_commit_sha,temporal_workflow_id,
            thread_id,generation,fencing_token,status,lease_active,started_by_clerk_user_id,input)
           VALUES ($1,$2,$3,$4,$5,$6,$7,1,1,'pending',false,$8,$9::jsonb)""",
        run_id,
        f.project.id,
        workflow_id,
        rb2b.KEY,
        "d" * 40,
        f"{rb2b.KEY}:{run_id}",
        str(run_id),
        USER,
        json.dumps({"project_id": str(f.project.id), "visit_id": str(visit_id)}),
    )
    return run_id


async def rb2b_fixture(db):
    f = await fixture(db)
    f.rb2b_cipher = cipher()
    await f.db.pool.execute(
        """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                definition_path,current_commit_sha,version_label,definition)
           VALUES ($1,$2,$2,$2,'registry/workflows','r.json',$3,'1','{}')
           ON CONFLICT DO NOTHING""",
        rb2b.WORKFLOW_ID,
        rb2b.KEY,
        "d" * 40,
    )
    return f


async def set_up(http, f) -> str:
    response = await http.post(f"/api/projects/{f.project.id}/rb2b")
    assert response.status_code == 200, response.text
    return response.json()["url"]


async def test_a_project_gets_its_own_address_and_rb2b_always_hears_200(collection_db, monkeypatch):
    f = await rb2b_fixture(collection_db)
    path = f"/api/projects/{f.project.id}/rb2b"
    async with client(f, monkeypatch) as http:
        assert (await http.get(path)).json() == {"set_up": False}
        url = await set_up(http, f)
        assert url.startswith("https://tin.test/webhooks/rb2b/") and "//webhooks" not in url
        status = (await http.get(path)).json()
        assert status["url"] == url and status["set_up_by_you"] is True
        # Only the hash finds the project; the token itself is sealed.
        row = await f.db.pool.fetchrow("SELECT * FROM rb2b_hooks WHERE project_id=$1", f.project.id)
        token = url.rsplit("/", 1)[1]
        assert row["token_hash"] == rb2b.token_hash(token)
        assert token.encode() not in bytes(row["token_ciphertext"])

        # Anything posted to an address nobody has, or that isn't RB2B's JSON, is 200 and kept
        # nowhere.
        for target, body in (
            ("/webhooks/rb2b/not-a-token", json.dumps(payload())),
            (url.removeprefix("https://tin.test"), "not json"),
            (url.removeprefix("https://tin.test"), "[1, 2]"),
        ):
            response = await http.post(target, content=body)
            assert response.status_code == 200 and response.content == b""
        assert await f.db.pool.fetchval("SELECT count(*) FROM rb2b_visits") == 0

        # A new address replaces the old one.
        newer = await set_up(http, f)
        assert newer != url
        await http.post(url.removeprefix("https://tin.test"), json=payload())
        assert await f.db.pool.fetchval("SELECT count(*) FROM rb2b_visits") == 0

    async with client(f, monkeypatch, user="user_outsider") as http:
        assert (await http.get(path)).status_code == 404
        assert (await http.post(path)).status_code == 404


async def test_each_payload_is_kept_once_and_only_people_start_a_run(collection_db, monkeypatch):
    f = await rb2b_fixture(collection_db)
    async with client(f, monkeypatch) as http:
        hook = (await set_up(http, f)).removeprefix("https://tin.test")
        assert (await http.post(hook, json=payload())).status_code == 200
        assert (await http.post(hook, json=payload(Tags="again"))).status_code == 200
        company = payload(
            **{"First Name": None, "Last Name": None, "Title": None},
            **{"LinkedIn URL": "https://www.linkedin.com/company/acme/"},
        )
        await http.post(hook, json=company)
        test = {"First Name": "RB2B", "Last Name": "Test Payload", "Seen At": "2026-11-23T10:00Z"}
        await http.post(hook, json=payload(**test))
        visits = await f.db.pool.fetch("SELECT kind, run_id FROM rb2b_visits ORDER BY received_at")
        assert [v["kind"] for v in visits] == ["person", "company", "test"]
        assert visits[0]["run_id"] is not None and visits[1]["run_id"] is None
        assert len(http.started) == 1
        assert http.started[0]["start_idempotency_key"].startswith("rb2b:")
        assert http.started[0]["_rb2b"] is True and http.started[0]["trigger_source"] == "api"
        status = (await http.get(f"/api/projects/{f.project.id}/rb2b")).json()
        assert status["people"] == 1 and status["companies"] == 1 and status["waiting"] == 0
        assert status["last_test_at"] and status["last_received_at"]


async def test_a_run_that_cant_start_waits_for_the_next_visitor(collection_db, monkeypatch):
    f = await rb2b_fixture(collection_db)
    failing = {"on": True}

    async def start(**kwargs):
        if failing["on"]:
            from tin_lite.billing_contracts import BillingError

            raise BillingError("credits_exhausted", "Add credits to keep reading visitors.")
        return SimpleNamespace(id=await visitor_run(f, kwargs["input_payload"]["visit_id"]))

    async with client(f, monkeypatch, start=start) as http:
        hook = (await set_up(http, f)).removeprefix("https://tin.test")
        await http.post(hook, json=payload())
        status = (await http.get(f"/api/projects/{f.project.id}/rb2b")).json()
        assert status["waiting"] == 1 and status["start_error"]
        failing["on"] = False
        later = (SEEN + timedelta(hours=1)).isoformat()
        await http.post(hook, json=payload(**{"Seen At": later}))
        status = (await http.get(f"/api/projects/{f.project.id}/rb2b")).json()
        assert status["waiting"] == 0 and status["start_error"] is None


# A visitor's run.


async def test_a_visitor_is_kept_read_rated_and_listed(collection_db, monkeypatch):
    f = await rb2b_fixture(collection_db)
    async with client(f, monkeypatch) as http:
        hook = (await set_up(http, f)).removeprefix("https://tin.test")
        await http.post(hook, json=payload())
    run_id = str(
        await f.db.pool.fetchval("SELECT run_id FROM rb2b_visits WHERE project_id=$1", f.project.id)
    )
    router = Router()
    work = RB2BActivities(
        database=f.db, storage=f.storage, router=router, integrations=SimpleNamespace()
    )
    kept = await work.record(run_id)
    assert kept["outcome"] == "person" and 0 <= kept["wait"] <= 600
    # Repeating a step changes nothing.
    assert (await work.record(run_id))["outcome"] == "person"
    await work.enrich(run_id)
    await work.enrich(run_id)
    await work.rate(run_id)
    await work.publish({"run_id": run_id, "outcome": kept["outcome"]})

    person = (await records.people(f.db.pool, f.project.id))[0]
    fields = person["fields"]
    assert fields["name"]["text"] == "Jane Doe"
    assert fields["headline"]["text"] == "Founder at Acme Software"
    assert fields["company"]["reason"] == "From RB2B, website https://acme.test"
    assert fields["location"]["text"] == "Austin, Texas"
    assert fields["web_tier"]["text"] == "lead"
    assert "PostHog isn't connected" in fields["web_summary"]["text"]
    assert person["events"] == {"visited": 1}
    # Rated with web search, against the ICP, as one person.
    assert fields["icp_fit"]["number"] == 4 and fields["intent"]["text"] == "warm"
    assert len(router.requests) == 1 and router.requests[0].web_search_calls
    sent = json.loads(router.requests[0].messages[0].content)["person"]
    assert sent["did"][0]["kind"] == "visited"
    assert fields["icp_fit"]["definition"].endswith(f":v{people_icp.SCORER}")

    tree = f.storage.repo.trees[f.storage.repo.head]
    listed = tree[rb2b.LIST_PATH][1].decode()
    assert "Jane Doe" in listed and "4 · Strong fit, warm" in listed
    state = await f.db.pool.fetchrow(
        "SELECT status, result_summary FROM workflow_runs WHERE id=$1", UUID(run_id)
    )
    assert state["status"] == "succeeded"
    assert state["result_summary"] == "Jane Doe, Founder at Acme Software: 4 · Strong fit, warm."


async def test_a_visitor_the_founder_forgot_is_left_out(collection_db, monkeypatch):
    f = await rb2b_fixture(collection_db)
    async with f.db.pool.acquire() as conn:
        await records.ensure_fields(conn, f.project.id)
        found = await records.upsert_person(
            conn, f.project.id, {"linkedin_slug": "jane-doe-1234"}, source="test"
        )
        await records.forget(conn, f.project.id, found)
    async with client(f, monkeypatch) as http:
        hook = (await set_up(http, f)).removeprefix("https://tin.test")
        await http.post(hook, json=payload(**{"Business Email": None}))
    run_id = str(await f.db.pool.fetchval("SELECT run_id FROM rb2b_visits"))
    work = RB2BActivities(
        database=f.db, storage=f.storage, router=Router(), integrations=SimpleNamespace()
    )
    kept = await work.record(run_id)
    assert kept == {"outcome": "forgotten", "wait": 0}
    await work.publish({"run_id": run_id, "outcome": kept["outcome"]})
    assert await records.people(f.db.pool, f.project.id) == []
    summary = await f.db.pool.fetchval(
        "SELECT result_summary FROM workflow_runs WHERE id=$1", UUID(run_id)
    )
    assert "asked Tin to forget" in summary
