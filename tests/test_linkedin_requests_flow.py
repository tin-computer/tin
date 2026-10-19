"""Connection requests end to end (#450): proposed after an engagers run, decided in
Decisions or over MCP, then sent one at a time from a synthetic LinkedIn sandbox.

The engagers run uses the collection fixture's synthetic sandbox and model; the sending
activities use a fake sandbox that answers each request as told.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from test_catalog_executor_transition import WIKI
from test_connection_collection import USER
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import (
    Cloud,
    engagers_run,
    fixture,
    member,
    person,
    post,
    run_through,
    work,
)
from test_linkedin_per_member import TEAMMATE, add_teammate
from test_people_view import mcp, structured

from tin_lite import linkedin_requests as requests
from tin_lite import linkedin_requests_activities
from tin_lite import people_records as records
from tin_lite.auth import AuthContext, require_user
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.connection_collection import CollectionError
from tin_lite.linkedin_requests_activities import LinkedInRequestsActivities
from tin_lite.linkedin_requests_api import router
from tin_lite.linkedin_requests_service import ConnectionRequests
from tin_lite.people_api import router as people_router

# Monday 19 October 2026, 10:00 in the project's time zone (UTC).
MONDAY = datetime(2026, 10, 19, 10, tzinfo=UTC)
STARTED = []
REAL_START = ConnectionRequests._start


def client(f, user=USER):
    app = FastAPI()
    app.include_router(router)
    app.include_router(people_router)
    app.state.runtime = SimpleNamespace(database=f.db, storage=f.storage)
    app.state.settings = f.store.settings
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=user,
        token_type="session_token",  # noqa: S106
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://tin.test")


async def recorded_start(self, project_id, batch_id, member, trigger_source, oauth_client_id):
    """Approving starts the sending run; here a pending run is recorded instead."""

    STARTED.append((project_id, batch_id, member, trigger_source))
    return await run_row(self.db, project_id, batch_id, member)


@pytest.fixture(autouse=True)
def no_temporal(monkeypatch):
    STARTED.clear()
    monkeypatch.setattr(ConnectionRequests, "_start", recorded_start)


async def ran(f, people, *, by=None, n=1):
    item = post(n, 3)
    await run_through(
        work(f, Cloud([item], {item["thread_urn"]: people})), await engagers_run(f, by=by)
    )
    return item


async def ids(f) -> dict[str, UUID]:
    """Each person's record ID by their LinkedIn member ID."""

    rows = await f.db.pool.fetch(
        "SELECT record_id, value FROM record_identifiers WHERE kind = 'linkedin_member'"
    )
    return {row["value"]: row["record_id"] for row in rows}


async def turn_on(http, f):
    path = f"/api/projects/{f.project.id}/linkedin-requests/sending"
    refused = await http.put(path, json={"enabled": True})
    assert refused.status_code == 422
    answer = await http.put(path, json={"enabled": True, "consent_version": 1})
    assert answer.status_code == 200, answer.text
    assert answer.json()["sending"]["on"] is True


async def test_sending_needs_cloud_reading(collection_db):
    """Sending uses the session cloud reading saves: browser-only reading can't turn it on."""

    f = await fixture(collection_db)
    await f.db.pool.execute(
        """UPDATE integration_connections
           SET configuration = jsonb_set(configuration, '{collection_permission,mode}',
                                         '"local_only"')
           WHERE project_id = $1 AND provider_key = 'network.linkedin'""",
        f.project.id,
    )
    base = f"/api/projects/{f.project.id}/linkedin-requests"
    async with client(f) as http:
        state = (await http.get(base)).json()
        refused = await http.put(f"{base}/sending", json={"enabled": True, "consent_version": 1})
    assert state["reading"] is False and state["browser_only"] is True
    assert refused.status_code == 409 and "cloud reading" in refused.text


async def test_people_who_fit_and_engaged_with_your_posts_are_proposed_once(collection_db):
    f = await fixture(collection_db)
    await add_teammate(f)
    # 1 and 4 fit and aren't connected; 2 is connected; 3 doesn't fit.
    await ran(f, [person(1), person(2, degree="1st"), person(3, headline="Designer"), person(4)])
    summary = await f.db.pool.fetchval(
        "SELECT result_summary FROM workflow_runs ORDER BY created_at DESC LIMIT 1"
    )
    assert "2 people who fit and aren't connected wait in Decisions" in summary
    base = f"/api/projects/{f.project.id}/linkedin-requests"
    async with client(f) as http:
        state = (await http.get(base)).json()
    assert state["offered"] is True and state["reading"] is True
    assert state["sending"]["on"] is False
    assert state["consent"]["version"] == 1 and "one at a time" in state["consent"]["text"]
    [batch] = state["batches"]
    assert sorted(p["name"] for p in batch["people"]) == ["Person 1", "Person 4"]
    assert batch["people"][0]["fit"] == 0.9 and batch["people"][0]["engaged"]["kind"] == "reacted"
    assert batch["first_send"] and batch["last_send"] >= batch["first_send"]

    # Someone who reacted to a teammate's post is proposed to the teammate, not to you.
    await ran(f, [person(8)], by=TEAMMATE, n=2)
    async with client(f) as http:
        mine = (await http.get(base)).json()["batches"][0]
    async with client(f, TEAMMATE) as http:
        theirs = (await http.get(base)).json()["batches"][0]
    assert "Person 8" not in {p["name"] for p in mine["people"]}
    assert [p["name"] for p in theirs["people"]] == ["Person 8"]

    # A customer is never proposed.
    known = await ids(f)
    async with f.db.pool.acquire() as conn:
        await conn.execute("DELETE FROM linkedin_requests WHERE member = $1", USER)
        await records.set_by_member(
            conn, f.project.id, known[member(4)], [records.Value("customer", "yes")], member=USER
        )
        assert await requests.propose(conn, f.project.id, USER) == 1


async def test_only_you_approve_and_only_with_sending_on(collection_db):
    f = await fixture(collection_db)
    await add_teammate(f)
    await ran(f, [person(1), person(4)])
    known = await ids(f)
    base = f"/api/projects/{f.project.id}/linkedin-requests"
    async with client(f) as http:
        batch = (await http.get(base)).json()["batches"][0]
        approve = f"{base}/batches/{batch['id']}/approve"
        decision = {"request_id": str(uuid4()), "people": [str(known[member(1)])]}
        off = await http.post(approve, json=decision)
        assert off.status_code == 409 and "Turn on sending" in off.json()["detail"]
    async with client(f, TEAMMATE) as http:
        # A teammate's LinkedIn wouldn't send these, so they can't decide them.
        assert (await http.post(approve, json=decision)).status_code == 404
    async with client(f, "user_outsider") as http:
        assert (await http.get(base)).status_code == 404
    async with client(f) as http:
        await turn_on(http, f)
        assert (await http.post(approve, json={"request_id": str(uuid4())})).status_code == 422
        answer = await http.post(approve, json=decision)
        assert answer.status_code == 200, answer.text
        assert answer.json()["queued"] == 1 and answer.json()["run_id"]
        # The same request repeats the same approval; another one is refused.
        assert (await http.post(approve, json=decision)).json()["repeated"] is True
        assert (
            await http.post(approve, json={**decision, "request_id": str(uuid4())})
        ).status_code == 409
        assert (await http.get(base)).json()["batches"] == []
    statuses = dict(
        await f.db.pool.fetch(
            "SELECT record_id, status FROM linkedin_requests WHERE member = $1", USER
        )
    )
    assert statuses == {known[member(1)]: "queued", known[member(4)]: "declined"}
    # A repeated approval asks to start the same run again; its start key makes it one run.
    assert {(m, source) for _p, _b, m, source in STARTED} == {(USER, "manual")}
    assert len({b for _p, b, _m, _c in STARTED}) == 1
    # Nobody decided is proposed again.
    await ran(f, [person(1), person(4)], n=3)
    async with client(f) as http:
        assert (await http.get(base)).json()["batches"] == []


async def test_approving_starts_one_real_run_and_a_failed_start_reopens_the_batch(
    collection_db, monkeypatch
):
    f = await fixture(collection_db)
    await ran(f, [person(1), person(4)])
    known = await ids(f)
    # The catalog's own definition, published at a fixed revision.
    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == requests.KEY)
    definition, _files = builtin.definition_and_files_with_wiki(WIKI)
    await f.db.pool.execute(
        """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                definition_path,current_commit_sha,version_label,definition)
           VALUES ($1,$2,$2,$2,'registry/workflows',$3,$4,'1',$5::jsonb)""",
        requests.WORKFLOW_ID,
        requests.KEY,
        builtin.definition_path,
        "e" * 40,
        json.dumps(definition),
    )
    temporal = SimpleNamespace(start_workflow=AsyncMock())
    f.runtime = SimpleNamespace(
        database=f.db,
        storage=f.storage,
        temporal=temporal,
        integrations=SimpleNamespace(ensure_requirements=AsyncMock()),
    )
    base = f"/api/projects/{f.project.id}/linkedin-requests"

    # Starting fails before any run exists: the batch is back in Decisions, unchanged.
    async def broken(*_args):
        raise RuntimeError("no start")

    monkeypatch.setattr(ConnectionRequests, "_start", broken)
    async with client(f) as http:
        await turn_on(http, f)
        batch = (await http.get(base)).json()["batches"][0]
        approve = f"{base}/batches/{batch['id']}/approve"
        failed = await http.post(
            approve, json={"request_id": str(uuid4()), "people": [str(known[member(1)])]}
        )
        assert failed.status_code == 503 and "nothing was approved" in failed.json()["detail"]
        again = (await http.get(base)).json()["batches"]
    assert [b["id"] for b in again] == [batch["id"]]
    assert sorted(p["name"] for p in again[0]["people"]) == ["Person 1", "Person 4"]

    # The real start: a dashboard approval records a manual run, recorded on the batch.
    monkeypatch.setattr(ConnectionRequests, "_start", REAL_START)
    app_client = client(f)
    app_client._transport.app.state.runtime = f.runtime
    app_client._transport.app.state.settings = SimpleNamespace(
        **vars(f.store.settings), task_queue="tin-test"
    )
    decision = {"request_id": str(uuid4()), "people": [str(known[member(1)])]}
    async with app_client as http:
        answer = await http.post(approve, json=decision)
        assert answer.status_code == 200, answer.text
        assert (await http.post(approve, json=decision)).json()["run_id"] == answer.json()["run_id"]
    run = await f.db.get_run(UUID(answer.json()["run_id"]))
    assert run.executor == requests.KEY and run.trigger_source == "manual"
    assert run.trigger_client is None and run.input == {"batch_id": batch["id"]}
    # A repeated approval re-sends the start of a still-pending run, under the same workflow ID.
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE start_idempotency_key = $1",
            f"linkedin-requests:{batch['id']}",
        )
        == 1
    )
    assert {call.kwargs["id"] for call in temporal.start_workflow.await_args_list} == {
        run.temporal_workflow_id
    }


class InviteCloud:
    """The LinkedIn sandbox for sending: answers each request and relationship read as told."""

    def __init__(self, db, *, outcomes=None, states=None):
        self.db = db
        self.outcomes, self.states = outcomes or {}, states or {}
        self.calls, self.cleaned = [], []

    async def engagement(self, conn, *, execution_key, run_id, session, packet, seconds=270):
        assert session["cookies"] and packet["actor"]["key"]
        self.calls.append(packet)
        await self.db.save_effect_progress(
            conn, execution_key=execution_key, result={"sandbox_id": f"sandbox-{len(self.calls)}"}
        )
        if packet["kind"] == "linkedin_relationships":
            return {
                "people": [
                    {"member": m, "state": self.states.get(m, "invited")} for m in packet["members"]
                ]
            }
        answer = self.outcomes.get(packet["member"], "sent")
        if isinstance(answer, Exception):
            raise answer
        return {
            "member": packet["member"],
            "outcome": answer,
            "invitation": "urn:li:fsd_invitation:9" if answer == "sent" else None,
        }

    async def cleanup(self, sandbox_id):
        self.cleaned.append(sandbox_id)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        clock = self

        class Fixed(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now if tz else clock.now.replace(tzinfo=None)

        return Fixed


async def approved(f, *people_indexes, run_people=(1, 4)):
    await ran(f, [person(i) for i in run_people])
    known = await ids(f)
    async with client(f) as http:
        await turn_on(http, f)
        batch = (await http.get(f"/api/projects/{f.project.id}/linkedin-requests")).json()[
            "batches"
        ][0]
        answer = await http.post(
            f"/api/projects/{f.project.id}/linkedin-requests/batches/{batch['id']}/approve",
            json={
                "request_id": str(uuid4()),
                "people": [str(known[member(i)]) for i in people_indexes],
            },
        )
        assert answer.status_code == 200, answer.text
    return UUID(batch["id"]), known


async def requests_run(f, batch_id: UUID, *, by=USER) -> str:
    """The run sending the batch: the one its approval started, else a new one."""

    held = await f.db.pool.fetchval(
        "SELECT send_run_id FROM linkedin_request_batches WHERE id = $1", batch_id
    )
    return str(held or await run_row(f.db, f.project.id, batch_id, by))


async def run_row(db, project_id, batch_id: UUID, by: str) -> UUID:
    workflow_id = await db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", requests.KEY)
    if workflow_id is None:
        workflow_id = uuid4()
        await db.pool.execute(
            """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                    definition_path,current_commit_sha,version_label,definition)
               VALUES ($1,$2,$2,$2,'registry/workflows','r.json',$3,'1','{}')""",
            workflow_id,
            requests.KEY,
            "e" * 40,
        )
    run_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflow_runs
           (id,project_id,workflow_id,executor,definition_commit_sha,temporal_workflow_id,
            thread_id,generation,fencing_token,status,lease_active,started_by_clerk_user_id,input)
           VALUES ($1,$2,$3,$4,$5,$6,$7,1,1,'pending',false,$8,$9::jsonb)""",
        run_id,
        project_id,
        workflow_id,
        requests.KEY,
        "e" * 40,
        f"{requests.KEY}:{run_id}",
        str(run_id),
        by,
        json.dumps({"project_id": str(project_id), "batch_id": str(batch_id)}),
    )
    return run_id


def sender(f, cloud):
    return LinkedInRequestsActivities(
        database=f.db, storage=f.storage, settings=f.store.settings, cipher=f.cipher, cloud=cloud
    )


async def status(f, record_id):
    return await f.db.pool.fetchval(
        "SELECT status FROM linkedin_requests WHERE record_id = $1", record_id
    )


async def test_approved_requests_go_one_at_a_time_and_acceptances_are_read(
    collection_db, monkeypatch
):
    f = await fixture(collection_db)
    batch, known = await approved(f, 1, 4)
    clock = Clock(MONDAY)
    monkeypatch.setattr(linkedin_requests_activities, "datetime", clock())
    cloud = InviteCloud(f.db)
    acts = sender(f, cloud)
    run_id = await requests_run(f, batch)
    await acts.prepare(run_id)

    step = await acts.next(run_id)
    assert step["action"] == "send"
    first = await acts.send({"run_id": run_id, "request_id": step["request_id"]})
    assert first["outcome"] == "sent"
    assert [c["kind"] for c in cloud.calls] == ["linkedin_invite"]
    # The next waits its turn: never two together.
    after = await acts.next(run_id)
    assert after["action"] == "wait" and after["seconds"] >= 15 * 60
    target = cloud.calls[0]["member"]
    sent_to = known[target]
    assert await status(f, sent_to) == "sent"
    # A retried send for the same request asks LinkedIn nothing more.
    again = await acts.send({"run_id": run_id, "request_id": step["request_id"]})
    assert again["outcome"] == "sent" and len(cloud.calls) == 1

    # The person reads as invited in People, with the request on their timeline.
    async with client(f) as http:
        person_view = (await http.get(f"/api/projects/{f.project.id}/people/{sent_to}")).json()
    assert person_view["fields"]["linkedin_relationship"]["value"] == "pending"
    assert person_view["fields"]["linkedin_relationship"]["by"] == "requests"
    assert person_view["events"][0]["kind"] == "invited"

    clock.now = MONDAY + timedelta(hours=1)
    monkeypatch.setattr(linkedin_requests_activities, "datetime", clock())
    step = await acts.next(run_id)
    assert step["action"] == "send"
    await acts.send({"run_id": run_id, "request_id": step["request_id"]})
    assert len(cloud.calls) == 2

    # A day later Tin reads who accepted.
    clock.now = MONDAY + timedelta(days=1, hours=1)
    monkeypatch.setattr(linkedin_requests_activities, "datetime", clock())
    cloud.states = {target: "connected"}
    assert (await acts.next(run_id))["action"] == "check"
    checked = await acts.check(run_id)
    assert checked == {"outcome": "read", "accepted": 1, "pending": 1, "ended": 0}
    assert await status(f, sent_to) == "accepted"
    async with client(f) as http:
        person_view = (await http.get(f"/api/projects/{f.project.id}/people/{sent_to}")).json()
        week = (await http.get(f"/api/projects/{f.project.id}/linkedin-requests")).json()["week"]
    assert person_view["fields"]["linkedin_relationship"]["value"] == "connected"
    assert [e["kind"] for e in person_view["events"][:2]] == ["accepted", "invited"]
    assert week["accepted"] == 1 and week["queued"] == 0

    # When every request is settled or past three weeks, the run finishes with a report.
    await f.db.pool.execute(
        "UPDATE linkedin_requests SET attempted_at = attempted_at - interval '22 days' "
        "WHERE status = 'sent'"
    )
    clock.now = MONDAY + timedelta(days=2)
    monkeypatch.setattr(linkedin_requests_activities, "datetime", clock())
    assert (await acts.next(run_id))["action"] in {"check", "done"}
    await acts.check(run_id)
    assert (await acts.next(run_id))["action"] == "done"
    await acts.finish(run_id)
    run = await f.db.get_run(UUID(run_id))
    assert run.status.value == "succeeded"
    assert run.result_summary == "2 connection requests sent, 1 accepted."
    repo = f.storage.repo
    report = repo.trees[repo.head]["people/connection-requests.md"][1].decode()
    assert "| Person 1 |" in report and "accepted" in report


async def test_linkedin_s_warning_pauses_sending_until_you_resume(collection_db, monkeypatch):
    f = await fixture(collection_db)
    batch, known = await approved(f, 1, 4)
    monkeypatch.setattr(linkedin_requests_activities, "datetime", Clock(MONDAY)())
    cloud = InviteCloud(
        f.db,
        outcomes={member(1): CollectionError("challenge"), member(4): CollectionError("challenge")},
    )
    acts = sender(f, cloud)
    run_id = await requests_run(f, batch)
    await acts.prepare(run_id)
    step = await acts.next(run_id)
    paused = await acts.send({"run_id": run_id, "request_id": step["request_id"]})
    assert paused["outcome"] == "challenge"
    # Nothing went out, so the request waits its turn again; nothing more is sent.
    assert sorted([await status(f, known[member(1)]), await status(f, known[member(4)])]) == [
        "queued",
        "queued",
    ]
    assert await acts.next(run_id) == {"action": "wait", "seconds": 6 * 3600}
    base = f"/api/projects/{f.project.id}/linkedin-requests"
    async with client(f) as http:
        state = (await http.get(base)).json()
        assert state["sending"]["paused"] is True and state["sending"]["held"] is True
        assert state["pause_text"] == "LinkedIn asked to verify your account"
        assert (await http.post(f"{base}/sending/resume")).json()["sending"]["paused"] is False
    cloud.outcomes = {}
    assert (await acts.next(run_id))["action"] == "send"


async def test_linkedin_s_weekly_limit_holds_sending_until_next_week(collection_db, monkeypatch):
    f = await fixture(collection_db)
    batch, known = await approved(f, 1)
    monkeypatch.setattr(linkedin_requests_activities, "datetime", Clock(MONDAY)())
    acts = sender(f, InviteCloud(f.db, outcomes={member(1): "limit"}))
    run_id = await requests_run(f, batch)
    await acts.prepare(run_id)
    step = await acts.next(run_id)
    assert (await acts.send({"run_id": run_id, "request_id": step["request_id"]}))[
        "outcome"
    ] == "limit"
    assert await status(f, known[member(1)]) == "queued"
    held = await f.db.pool.fetchval(
        "SELECT paused_until FROM linkedin_sending WHERE member = $1", USER
    )
    assert held == MONDAY + timedelta(days=7) - timedelta(hours=1)


async def test_a_send_that_may_have_gone_out_is_never_sent_again(collection_db, monkeypatch):
    f = await fixture(collection_db)
    batch, known = await approved(f, 1)
    clock = Clock(MONDAY)
    monkeypatch.setattr(linkedin_requests_activities, "datetime", clock())
    cloud = InviteCloud(f.db, outcomes={member(1): CollectionError("cloud_failed")})
    acts = sender(f, cloud)
    run_id = await requests_run(f, batch)
    await acts.prepare(run_id)
    step = await acts.next(run_id)
    await acts.send({"run_id": run_id, "request_id": step["request_id"]})
    assert await status(f, known[member(1)]) == "uncertain"
    # The next relationship read settles it: never sent, so it is left, not retried.
    clock.now = MONDAY + timedelta(days=1)
    monkeypatch.setattr(linkedin_requests_activities, "datetime", clock())
    cloud.states = {member(1): "none"}
    assert (await acts.next(run_id))["action"] == "check"
    await acts.check(run_id)
    assert await status(f, known[member(1)]) == "not_sent"
    assert (await acts.next(run_id))["action"] == "done"
    assert [c["kind"] for c in cloud.calls].count("linkedin_invite") == 1


async def test_approvals_not_sent_within_two_weeks_lapse(collection_db, monkeypatch):
    f = await fixture(collection_db)
    batch, known = await approved(f, 1)
    await f.db.pool.execute(
        "UPDATE linkedin_requests SET queued_at = now() - interval '15 days' WHERE status='queued'"
    )
    acts = sender(f, InviteCloud(f.db))
    run_id = await requests_run(f, batch)
    await acts.prepare(run_id)
    assert (await acts.next(run_id))["action"] == "done"
    assert await status(f, known[member(1)]) == "lapsed"


async def test_mcp_lists_and_approves_your_requests(collection_db, monkeypatch):
    f = await fixture(collection_db)
    await ran(f, [person(1), person(4)])
    known = await ids(f)
    async with client(f) as http:
        await turn_on(http, f)
    server = mcp(f, monkeypatch)
    project = str(f.project.id)
    listed = structured(await server.call_tool("list_open_decisions", {"project_id": project}))
    [batch] = listed["connection_requests"]
    assert batch["sending_on"] is True
    assert sorted(p["name"] for p in batch["people"]) == ["Person 1", "Person 4"]
    with pytest.raises(ToolError, match="invalid"):
        await server.call_tool(
            "approve_connection_requests",
            {
                "project_id": project,
                "batch_id": batch["batch_id"],
                "person_ids": [str(uuid4())],
                "request_id": str(uuid4()),
            },
        )
    approved_ = structured(
        await server.call_tool(
            "approve_connection_requests",
            {
                "project_id": project,
                "batch_id": batch["batch_id"],
                "person_ids": [str(known[member(4)])],
                "request_id": str(uuid4()),
            },
        )
    )
    assert approved_["status"] == "approved" and approved_["queued"] == 1
    assert STARTED[-1][3] == "mcp"
    listed = structured(await server.call_tool("list_open_decisions", {"project_id": project}))
    assert listed["connection_requests"] == []


async def test_the_workflow_follows_each_step_and_keeps_only_ids_in_history():
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, Worker

    from tin_lite.workflows import LinkedInRequestsWorkflow

    steps = [
        {"action": "send", "request_id": str(uuid4())},
        {"action": "wait", "seconds": 3600},
        {"action": "send", "request_id": str(uuid4())},
        {"action": "check"},
        {"action": "done"},
    ]
    calls = []

    @activity.defn(name="linkedin_requests_prepare")
    async def prepare(run_id):
        calls.append("prepare")

    @activity.defn(name="linkedin_requests_next")
    async def next_(run_id):
        calls.append(("next", activity.info().scheduled_time))
        return steps.pop(0)

    @activity.defn(name="linkedin_requests_send")
    async def send(control):
        calls.append(("send", control["request_id"]))
        return {"outcome": "sent", "status": "sent"}

    @activity.defn(name="linkedin_requests_check")
    async def check(run_id):
        calls.append("check")
        return {"outcome": "read", "accepted": 0, "pending": 2, "ended": 0}

    @activity.defn(name="linkedin_requests_finish")
    async def finish(run_id):
        calls.append("finish")

    @activity.defn(name="linkedin_requests_failure")
    async def failure(argument):
        raise AssertionError("Sending must not fail")

    run_id = str(uuid4())
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"requests-{uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[LinkedInRequestsWorkflow],
            activities=[prepare, next_, send, check, finish, failure],
        ):
            handle = await env.client.start_workflow(
                LinkedInRequestsWorkflow.run,
                run_id,
                id=f"{requests.KEY}:{run_id}",
                task_queue=queue,
            )
            await handle.result()
            history = await handle.fetch_history()
    await Replayer(workflows=[LinkedInRequestsWorkflow]).replay_workflow(history)
    kinds = [c if isinstance(c, str) else c[0] for c in calls]
    assert kinds == [
        "prepare",
        "next",
        "send",
        "next",
        "next",
        "send",
        "next",
        "check",
        "next",
        "finish",
    ]
    nexts = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "next"]
    # The hour's wait was slept in the workflow before asking again.
    assert nexts[2] - nexts[1] >= timedelta(hours=1)
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            for payload in event.activity_task_scheduled_event_attributes.input.payloads:
                value = json.loads(payload.data)
                assert value == run_id or set(value) == {"run_id", "request_id"}


async def test_people_you_choose_in_people_are_queued_and_sent_by_the_same_run(
    collection_db, monkeypatch
):
    f = await fixture(collection_db)
    # 1 and 4 fit and aren't connected, so they wait in Decisions; 2 is your connection; 3
    # doesn't fit.
    await ran(f, [person(1), person(2, degree="1st"), person(3, headline="Designer"), person(4)])
    known = await ids(f)
    people_path = f"/api/projects/{f.project.id}/people"
    base = f"/api/projects/{f.project.id}/linkedin-requests"
    choice = {"request_id": str(uuid4()), "people": [str(known[member(3)]), str(known[member(1)])]}
    async with client(f) as http:
        listed = {p["name"]: p for p in (await http.get(people_path)).json()["people"]}
        # Anyone Tin can name on LinkedIn who isn't your connection, whatever their fit.
        assert {name for name, p in listed.items() if p["requestable"]} == {
            "Person 1",
            "Person 3",
            "Person 4",
        }
        off = await http.post(f"{base}/send", json=choice)
        assert off.status_code == 409 and "Turn on sending" in off.json()["detail"]
        assert (await http.get(base)).json()["next_open"] is None
        await turn_on(http, f)
        assert (await http.get(base)).json()["next_open"]
        connected = {"request_id": str(uuid4()), "people": [str(known[member(2)])]}
        refused = await http.post(f"{base}/send", json=connected)
        assert refused.status_code == 422 and "can't get a request" in refused.json()["detail"]
        assert (await http.post(f"{base}/send", json={**choice, "people": []})).status_code == 422
        answer = await http.post(f"{base}/send", json=choice)
        assert answer.status_code == 200, answer.text
        sent = answer.json()
        assert sent["status"] == "approved" and sent["queued"] == 2 and not sent["repeated"]
        assert sent["first_send"] < sent["last_send"]
        # The same request repeats the same choice.
        repeated = (await http.post(f"{base}/send", json=choice)).json()
        assert repeated["repeated"] is True and repeated["batch_id"] == sent["batch_id"]
        listed = {p["name"]: p for p in (await http.get(people_path)).json()["people"]}
        batches = (await http.get(base)).json()["batches"]
    assert listed["Person 1"]["request"] == "queued" and not listed["Person 1"]["requestable"]
    assert listed["Person 4"]["request"] is None and listed["Person 4"]["requestable"]
    # Person 1 waited in Decisions; choosing them took them out, and Person 4 still waits.
    assert [p["name"] for p in batches[0]["people"]] == ["Person 4"]
    rows = await f.db.pool.fetch(
        "SELECT record_id, batch_id FROM linkedin_requests WHERE status = 'queued' "
        "ORDER BY queued_at, id"
    )
    assert [row["record_id"] for row in rows] == [known[member(3)], known[member(1)]]
    assert {row["batch_id"] for row in rows} == {UUID(sent["batch_id"])}
    assert {(b, m, source) for _p, b, m, source in STARTED} == {
        (UUID(sent["batch_id"]), USER, "manual")
    }

    # The sending run sends them like an approved batch, in the order you chose them.
    monkeypatch.setattr(linkedin_requests_activities, "datetime", Clock(MONDAY)())
    cloud = InviteCloud(f.db)
    acts = sender(f, cloud)
    run_id = await requests_run(f, UUID(sent["batch_id"]))
    await acts.prepare(run_id)
    step = await acts.next(run_id)
    assert step["action"] == "send"
    await acts.send({"run_id": run_id, "request_id": step["request_id"]})
    assert [call["member"] for call in cloud.calls] == [member(3)]
    async with client(f) as http:
        listed = {p["name"]: p for p in (await http.get(people_path)).json()["people"]}
    assert listed["Person 3"]["fields"]["linkedin_relationship"]["value"] == "pending"
    assert listed["Person 3"]["invited_at"] and listed["Person 3"]["request"] is None


async def test_each_member_sees_their_own_linkedin_and_can_ask_a_teammates_connections(
    collection_db,
):
    f = await fixture(collection_db)
    await add_teammate(f)
    await ran(f, [person(1), person(2, degree="1st")])
    path = f"/api/projects/{f.project.id}/people"
    async with client(f, TEAMMATE) as http:
        theirs = {p["name"]: p for p in (await http.get(path)).json()["people"]}
    # Tin hasn't read the teammate's LinkedIn: no relationship shows, and both can be asked.
    assert "linkedin_relationship" not in theirs["Person 2"]["fields"]
    assert theirs["Person 1"]["requestable"] and theirs["Person 2"]["requestable"]
    async with client(f) as http:
        mine = {p["name"]: p for p in (await http.get(path)).json()["people"]}
    assert mine["Person 2"]["fields"]["linkedin_relationship"]["value"] == "connected"
    assert not mine["Person 2"]["requestable"]

    # The teammate's own run reads their LinkedIn; yours stays as it was.
    await ran(f, [person(1, degree="1st")], by=TEAMMATE, n=2)
    async with client(f, TEAMMATE) as http:
        theirs = {p["name"]: p for p in (await http.get(path)).json()["people"]}
    async with client(f) as http:
        mine = {p["name"]: p for p in (await http.get(path)).json()["people"]}
    assert theirs["Person 1"]["fields"]["linkedin_relationship"]["value"] == "connected"
    assert not theirs["Person 1"]["requestable"]
    assert mine["Person 1"]["fields"]["linkedin_relationship"]["value"] == "none"
    assert mine["Person 1"]["requestable"]


async def test_customers_and_people_you_asked_cant_be_chosen_and_a_failed_start_queues_nothing(
    collection_db, monkeypatch
):
    f = await fixture(collection_db)
    await ran(f, [person(1), person(4), person(5)])
    known = await ids(f)
    base = f"/api/projects/{f.project.id}/linkedin-requests"
    async with f.db.pool.acquire() as conn:
        await records.set_by_member(
            conn, f.project.id, known[member(5)], [records.Value("customer", "yes")], member=USER
        )

    async def broken(*_args):
        raise RuntimeError("no start")

    async with client(f) as http:
        await turn_on(http, f)
        customer = {"request_id": str(uuid4()), "people": [str(known[member(5)])]}
        assert (await http.post(f"{base}/send", json=customer)).status_code == 422
        # Sending can't start: nothing stays queued, and Person 1 waits in Decisions again.
        # Person 5, a customer since they were proposed, no longer does.
        monkeypatch.setattr(ConnectionRequests, "_start", broken)
        choice = {"request_id": str(uuid4()), "people": [str(known[member(1)])]}
        failed = await http.post(f"{base}/send", json=choice)
        assert failed.status_code == 503 and "nothing was queued" in failed.json()["detail"]
        batches = (await http.get(base)).json()["batches"]
        assert sorted(p["name"] for p in batches[0]["people"]) == ["Person 1", "Person 4"]
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM linkedin_request_batches WHERE status = 'approved'"
            )
            == 0
        )
        # The same choice works once sending starts, and then Person 1 can't be chosen again.
        monkeypatch.undo()
        STARTED.clear()
        monkeypatch.setattr(ConnectionRequests, "_start", recorded_start)
        assert (await http.post(f"{base}/send", json=choice)).status_code == 200
        again = {"request_id": str(uuid4()), "people": [str(known[member(1)])]}
        assert (await http.post(f"{base}/send", json=again)).status_code == 422


async def test_you_can_cancel_a_request_before_it_goes(collection_db, monkeypatch):
    f = await fixture(collection_db)
    await ran(f, [person(1), person(3, headline="Designer"), person(4)])
    known = await ids(f)
    people_path = f"/api/projects/{f.project.id}/people"
    base = f"/api/projects/{f.project.id}/linkedin-requests"
    one, three, four = (known[member(n)] for n in (1, 3, 4))
    async with client(f) as http:
        await turn_on(http, f)
        choice = {"request_id": str(uuid4()), "people": [str(three), str(one)]}
        assert (await http.post(f"{base}/send", json=choice)).status_code == 200
        # The panel says when it goes, after the request queued ahead of it.
        detail = (await http.get(f"{people_path}/{one}")).json()
        first = (await http.get(f"{people_path}/{three}")).json()
        assert detail["request"] == "queued" and not detail["queued"]["sending"]
        assert first["queued"]["at"] < detail["queued"]["at"]
        cancelled = await http.post(f"{base}/people/{one}/cancel")
        assert cancelled.status_code == 200 and cancelled.json() == {"cancelled": 1}
        detail = (await http.get(f"{people_path}/{one}")).json()
        # They can be chosen again.
        assert detail["request"] is None and detail["requestable"]
        again = await http.post(f"{base}/people/{one}/cancel")
        assert again.status_code == 404 and "No request" in again.json()["detail"]
    cancelled_id = await f.db.pool.fetchval(
        "SELECT id FROM linkedin_requests WHERE record_id = $1", one
    )
    # The sending run passes over it, even if it read it as queued a moment before.
    async with f.db.pool.acquire() as conn, conn.transaction():
        assert await requests.reserve(conn, cancelled_id, MONDAY) == {"skip": True}
    assert (
        await f.db.pool.fetchval("SELECT status FROM linkedin_requests WHERE id = $1", cancelled_id)
        == "declined"
    )
    # One LinkedIn is being asked right now can't be taken back.
    await f.db.pool.execute(
        "UPDATE linkedin_requests SET status = 'sending' WHERE record_id = $1", three
    )
    async with client(f) as http:
        busy = await http.post(f"{base}/people/{three}/cancel")
    assert busy.status_code == 409 and "sending this request right now" in busy.json()["detail"]

    # Over MCP, the same.
    server = mcp(f, monkeypatch)
    project = str(f.project.id)
    await server.call_tool(
        "send_connection_requests",
        {"project_id": project, "person_ids": [str(four)], "request_id": str(uuid4())},
    )
    answer = structured(
        await server.call_tool(
            "cancel_connection_request", {"project_id": project, "person_id": str(four)}
        )
    )
    assert answer == {"cancelled": 1}
    with pytest.raises(ToolError, match="not_found"):
        await server.call_tool(
            "cancel_connection_request", {"project_id": project, "person_id": str(four)}
        )


async def test_someone_marked_a_customer_after_they_were_queued_isnt_asked(collection_db):
    f = await fixture(collection_db)
    await ran(f, [person(1), person(4)])
    known = await ids(f)
    one, four = known[member(1)], known[member(4)]
    base = f"/api/projects/{f.project.id}/linkedin-requests"
    async with client(f) as http:
        await turn_on(http, f)
        choice = {"request_id": str(uuid4()), "people": [str(one), str(four)]}
        assert (await http.post(f"{base}/send", json=choice)).status_code == 200
        # Marking them a customer in People takes their request out of the queue at once.
        marked = await http.patch(
            f"/api/projects/{f.project.id}/people/{one}", json={"customer": True}
        )
        assert marked.status_code == 200 and marked.json()["request"] is None
    status = "SELECT status FROM linkedin_requests WHERE record_id = $1"
    assert await f.db.pool.fetchval(status, one) == "declined"
    # However they became one, the sender checks again just before it sends.
    async with f.db.pool.acquire() as conn:
        await records.set_by_member(
            conn, f.project.id, four, [records.Value("customer", "yes")], member=USER
        )
    request_id = await f.db.pool.fetchval(
        "SELECT id FROM linkedin_requests WHERE record_id = $1", four
    )
    async with f.db.pool.acquire() as conn, conn.transaction():
        assert await requests.reserve(conn, request_id, MONDAY) == {"skip": True}
    assert await f.db.pool.fetchval(status, four) == "declined"


async def test_mcp_lists_who_you_can_ask_and_sends_your_choice(collection_db, monkeypatch):
    f = await fixture(collection_db)
    await ran(f, [person(1), person(2, degree="1st")])
    known = await ids(f)
    async with client(f) as http:
        await turn_on(http, f)
    server = mcp(f, monkeypatch)
    project = str(f.project.id)
    listed = structured(await server.call_tool("list_people", {"project_id": project}))
    can = {p["name"]: p["can_request"] for p in listed["people"]}
    assert can == {"Person 1": True, "Person 2": False}
    sent = structured(
        await server.call_tool(
            "send_connection_requests",
            {
                "project_id": project,
                "person_ids": [str(known[member(1)])],
                "request_id": str(uuid4()),
            },
        )
    )
    assert sent["status"] == "approved" and sent["queued"] == 1
    assert STARTED[-1][3] == "mcp"
    listed = structured(await server.call_tool("list_people", {"project_id": project}))
    assert {p["name"]: p["request_queued"] for p in listed["people"]}["Person 1"] is True
    with pytest.raises(ToolError, match="invalid"):
        await server.call_tool(
            "send_connection_requests",
            {
                "project_id": project,
                "person_ids": [str(known[member(2)])],
                "request_id": str(uuid4()),
            },
        )


async def test_the_migration_gives_each_run_s_reading_to_the_member_who_started_it(
    collection_db,
):
    """Before 071 a run wrote the relationship as one shared field; the migration moves each
    reading to the member who started the run, and leaves the old field unread."""

    from pathlib import Path

    f = await fixture(collection_db)
    await ran(f, [person(1), person(2, degree="1st")])
    known = await ids(f)
    run_id = await f.db.pool.fetchval(
        "SELECT id FROM workflow_runs WHERE executor = 'social.linkedin_engagers'"
    )
    async with f.db.pool.acquire() as conn:
        await conn.execute("DELETE FROM record_relationships")
        for who, state in ((1, "none"), (2, "connected")):
            await records.set_values(
                conn,
                f.project.id,
                known[member(who)],
                [records.Value("linkedin_relationship", state, "as before")],
                set_by_kind="run",
                set_by=str(run_id),
            )
        migration = Path(__file__).parents[1] / "migrations" / "071_member_relationships.sql"
        await conn.execute(migration.read_text())
        mine = await records.relationships(conn, f.project.id, USER)
    assert {key: (row["state"], row["set_by"]) for key, row in mine.items()} == {
        known[member(1)]: ("none", str(run_id)),
        known[member(2)]: ("connected", str(run_id)),
    }


async def sent_by(f, batch_id) -> str:
    return str(
        await f.db.pool.fetchval(
            "SELECT send_run_id FROM linkedin_request_batches WHERE id = $1", UUID(str(batch_id))
        )
    )


async def test_a_member_s_requests_wait_in_one_line_sent_by_one_run(collection_db, monkeypatch):
    f = await fixture(collection_db)
    batch, known = await approved(f, 1, 4)
    first_run = await requests_run(f, batch)
    await ran(f, [person(5), person(6)], n=2)
    known = await ids(f)
    base = f"/api/projects/{f.project.id}/linkedin-requests"
    people_path = f"/api/projects/{f.project.id}/people"
    choice = {"request_id": str(uuid4()), "people": [str(known[member(5)])]}
    async with client(f) as http:
        picked = await http.post(f"{base}/send", json=choice)
        assert picked.status_code == 200, picked.text
        # Chosen in People while approved ones still wait: they join the same run, behind them.
        assert len(STARTED) == 1
        assert await sent_by(f, picked.json()["batch_id"]) == first_run
        repeated = await http.post(f"{base}/send", json=choice)
        assert repeated.json()["repeated"] and len(STARTED) == 1
        ahead = [
            (await http.get(f"{people_path}/{known[member(n)]}")).json()["queued"]["at"]
            for n in (1, 4)
        ]
        five = (await http.get(f"{people_path}/{known[member(5)]}")).json()["queued"]["at"]
    assert max(ahead) < five

    # The one run sends them in the order they were queued.
    clock = Clock(MONDAY)
    cloud = InviteCloud(f.db)
    acts = sender(f, cloud)
    await acts.prepare(first_run)
    for hour in range(3):
        clock.now = MONDAY + timedelta(hours=hour)
        monkeypatch.setattr(linkedin_requests_activities, "datetime", clock())
        step = await acts.next(first_run)
        assert step["action"] == "send"
        await acts.send({"run_id": first_run, "request_id": step["request_id"]})
    sent = [call["member"] for call in cloud.calls]
    assert sorted(sent[:2]) == [member(1), member(4)] and sent[2] == member(5)

    # With nothing left to send, the run only reads who accepted: a new choice starts a run.
    async with client(f) as http:
        later = {"request_id": str(uuid4()), "people": [str(known[member(6)])]}
        answer = await http.post(f"{base}/send", json=later)
        assert answer.status_code == 200, answer.text
    assert len(STARTED) == 2
    assert await sent_by(f, answer.json()["batch_id"]) != first_run
    async with f.db.pool.acquire() as conn:
        assert len(await requests.checking(conn, UUID(first_run), clock.now)) == 3


async def test_two_choices_at_once_share_one_run(collection_db, monkeypatch):
    f = await fixture(collection_db)

    # The first start is slow, so the second choice arrives while it is still starting.
    async def slow_start(self, *args):
        await asyncio.sleep(0.3)
        return await recorded_start(self, *args)

    monkeypatch.setattr(ConnectionRequests, "_start", slow_start)
    await ran(f, [person(1), person(4)])
    known = await ids(f)
    base = f"/api/projects/{f.project.id}/linkedin-requests"
    async with client(f) as http:
        await turn_on(http, f)
        answers = await asyncio.gather(
            *(
                http.post(
                    f"{base}/send",
                    json={"request_id": str(uuid4()), "people": [str(known[member(n)])]},
                )
                for n in (1, 4)
            )
        )
    assert [a.status_code for a in answers] == [200, 200]
    assert len(STARTED) == 1
    runs = {await sent_by(f, a.json()["batch_id"]) for a in answers}
    assert len(runs) == 1


async def test_a_failed_run_lapses_every_batch_it_held(collection_db):
    f = await fixture(collection_db)
    batch, known = await approved(f, 1)
    run_id = await requests_run(f, batch)
    await ran(f, [person(5)], n=2)
    known = await ids(f)
    async with client(f) as http:
        choice = {"request_id": str(uuid4()), "people": [str(known[member(5)])]}
        assert (
            await http.post(f"/api/projects/{f.project.id}/linkedin-requests/send", json=choice)
        ).status_code == 200
    assert len(STARTED) == 1
    await sender(f, InviteCloud(f.db)).failure(run_id)
    assert [await status(f, known[member(n)]) for n in (1, 5)] == ["lapsed", "lapsed"]
    async with client(f) as http:
        listed = {
            p["name"]: p
            for p in (await http.get(f"/api/projects/{f.project.id}/people")).json()["people"]
        }
    assert listed["Person 5"]["requestable"] and listed["Person 5"]["request"] is None


@pytest.mark.parametrize("new_history", [False, True])
async def test_a_long_line_carries_on_as_the_same_run(monkeypatch, new_history):
    from temporalio import workflow

    from tin_lite.domain import LINKEDIN_REQUESTS_STEPS
    from tin_lite.workflows import LinkedInRequestsWorkflow

    class Continued(workflow.ContinueAsNewError):
        pass

    calls = []
    run_id = str(uuid4())

    async def execute(name, argument, **_):
        calls.append(name)
        if name == "linkedin_requests_next":
            return {"action": "wait", "seconds": 60}

    async def sleep(_duration):
        pass

    def carry_on(argument):
        assert argument == run_id
        raise Continued()

    monkeypatch.setattr(workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow, "sleep", sleep)
    monkeypatch.setattr(workflow, "patched", lambda name: new_history)
    monkeypatch.setattr(workflow, "continue_as_new", carry_on)
    if new_history:
        with pytest.raises(Continued):
            await LinkedInRequestsWorkflow().run(run_id)
        assert "linkedin_requests_finish" not in calls
    else:
        # Older histories finished when the steps ran out.
        await LinkedInRequestsWorkflow().run(run_id)
        assert calls[-1] == "linkedin_requests_finish"
    assert calls.count("linkedin_requests_next") == LINKEDIN_REQUESTS_STEPS
    assert "linkedin_requests_failure" not in calls


async def test_people_name_whose_post_and_whose_request(collection_db):
    f = await fixture(collection_db)
    await add_teammate(f)
    await ran(f, [person(1)])
    await ran(f, [person(2)], by=TEAMMATE, n=2)
    known = await ids(f)
    async with f.db.pool.acquire() as conn:
        await records.add_note(
            conn, f.project.id, known[member(2)], "Met at a meetup.", author=TEAMMATE
        )
    path = f"/api/projects/{f.project.id}/people"
    looked_up = []

    class Profiles:
        async def member_profile(self, member):
            looked_up.append(member)
            return {"display_name": "Mira Jones", "email": "mira@example.com"}

    async def read(profiles):
        http = client(f)
        http._transport.app.state.auth = profiles
        async with http:
            listed = {p["name"]: p for p in (await http.get(path)).json()["people"]}
            one = (await http.get(f"{path}/{known[member(2)]}")).json()
        return listed, one

    listed, one = await read(Profiles())
    # Your own posts read as yours; a teammate's by their first name. Only teammates are
    # looked up, and no member ID leaves Tin.
    assert listed["Person 1"]["last_touch"]["who"] == "you"
    assert listed["Person 2"]["last_touch"] == {
        "kind": "reacted",
        "at": listed["Person 2"]["last_touch"]["at"],
        "who": "Mira",
    }
    assert [event["who"] for event in one["events"]] == ["Mira"]
    # Notes say whose too.
    assert listed["Person 2"]["note"]["who"] == "Mira"
    assert [(note["by"], note["who"]) for note in one["notes"]] == [("a teammate", "Mira")]
    assert set(looked_up) == {TEAMMATE}
    assert TEAMMATE not in json.dumps([listed, one])

    # Without a name to read, they stay "a teammate".
    class Unnamed:
        async def member_profile(self, member):
            raise httpx.ConnectError("no Clerk")

    listed, one = await read(Unnamed())
    assert listed["Person 2"]["last_touch"]["who"] == "a teammate"
    assert one["events"][0]["who"] == "a teammate"
