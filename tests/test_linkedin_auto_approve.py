"""Approving the people who fit automatically: a member lets Tin approve what their own
LinkedIn reads propose, right after each read, as they would in Decisions.

The reads use the engagers and profile viewers fixtures' synthetic sandbox and model; starting
the sending run is recorded rather than dispatched, except where a real start is checked.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid5

import test_linkedin_profile_viewers as viewers_tests
from test_catalog_executor_transition import WIKI
from test_connection_collection import USER
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import Cloud, engagers_run, fixture, member, person, post, work
from test_linkedin_engagers import run_through as engagers_through
from test_linkedin_per_member import TEAMMATE, add_teammate
from test_linkedin_requests_flow import REAL_START, STARTED, client, ids
from test_linkedin_requests_flow import no_temporal as no_temporal

from tin_lite import linkedin_requests as requests
from tin_lite import people_records as records
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.linkedin_requests_service import AUTO_APPROVE, ConnectionRequests


def service(f) -> ConnectionRequests:
    runtime = getattr(f, "runtime", None) or SimpleNamespace(database=f.db, storage=f.storage)
    return ConnectionRequests(runtime, getattr(f, "settings", None) or f.store.settings)


async def read(f, people, *, by=None, n=1, approving=True) -> str:
    """An engagers run over one post; `approving` gives it the service that approves."""

    item = post(n, 3)
    w = work(f, Cloud([item], {item["thread_urn"]: people}))
    if approving:
        w.connection_requests = service(f)
    run_id = await engagers_run(f, by=by)
    await engagers_through(w, run_id)
    return run_id


async def summary(f, run_id) -> str:
    return await f.db.pool.fetchval(
        "SELECT result_summary FROM workflow_runs WHERE id = $1", UUID(str(run_id))
    )


async def statuses(f, who=USER) -> dict:
    return dict(
        await f.db.pool.fetch(
            "SELECT record_id, status FROM linkedin_requests WHERE member = $1", who
        )
    )


async def switch(f, who=USER, **change):
    async with client(f, who) as http:
        answer = await http.put(
            f"/api/projects/{f.project.id}/linkedin-requests/sending", json=change
        )
    return answer


async def test_it_is_off_by_default_and_proposals_wait_in_decisions(collection_db):
    f = await fixture(collection_db)
    run_id = await read(f, [person(1), person(4)])
    async with client(f) as http:
        state = (await http.get(f"/api/projects/{f.project.id}/linkedin-requests")).json()
    assert state["sending"]["on"] is True and state["sending"]["auto_approve"] is False
    assert sorted(p["name"] for p in state["batches"][0]["people"]) == ["Person 1", "Person 4"]
    assert set((await statuses(f)).values()) == {"proposed"}
    assert STARTED == []
    assert "2 people who fit and aren't connected wait in Decisions" in await summary(f, run_id)
    assert "Approved automatically" not in await summary(f, run_id)


async def test_turning_it_on_needs_sending_and_turning_sending_off_turns_it_off(collection_db):
    f = await fixture(collection_db)
    await read(f, [person(1)])
    refused = await switch(f, enabled=False, auto_approve=True)
    assert refused.status_code == 422 and "Turn on sending" in refused.json()["detail"]
    # Sending's own consent comes with it, as it does for limits.
    assert (await switch(f, enabled=True, auto_approve=True)).status_code == 422
    on = await switch(f, enabled=True, consent_version=1, auto_approve=True)
    assert on.status_code == 200 and on.json()["sending"]["auto_approve"] is True
    # Changing limits keeps it.
    limits = await switch(f, enabled=True, consent_version=1, limits={"daily_cap": 10})
    assert limits.json()["sending"]["auto_approve"] is True
    off = await switch(f, enabled=False)
    assert off.json()["sending"]["auto_approve"] is False
    # Turning sending on again doesn't bring it back unasked.
    again = await switch(f, enabled=True, consent_version=1)
    assert again.json()["sending"]["auto_approve"] is False


async def test_after_an_engagers_read_tin_approves_the_fits_and_starts_sending(collection_db):
    f = await fixture(collection_db)
    await switch(f, enabled=True, consent_version=1, auto_approve=True)
    # 1 and 4 fit and aren't connected; 2 is connected; 3 doesn't fit.
    run_id = await read(
        f, [person(1), person(2, degree="1st"), person(3, headline="Designer"), person(4)]
    )
    known = await ids(f)
    assert await statuses(f) == {known[member(1)]: "queued", known[member(4)]: "queued"}
    [(project_id, batch_id, who, source)] = STARTED
    assert (project_id, who, source) == (f.project.id, USER, "auto_approve")
    batch = await f.db.pool.fetchrow(
        "SELECT status, decision_request_id FROM linkedin_request_batches WHERE id = $1", batch_id
    )
    assert batch["status"] == "approved"
    assert batch["decision_request_id"] == uuid5(AUTO_APPROVE, f"auto-approve:{run_id}")
    line = await summary(f, run_id)
    assert "Approved automatically: connection requests to 2 people who fit." in line
    assert "wait in Decisions" not in line
    async with client(f) as http:
        state = (await http.get(f"/api/projects/{f.project.id}/linkedin-requests")).json()
    assert state["batches"] == []


async def test_after_a_profile_viewers_read_tin_approves_the_fits_too(collection_db, monkeypatch):
    f = await fixture(collection_db)
    await switch(f, enabled=True, consent_version=1, auto_approve=True)
    monkeypatch.setattr(viewers_tests.viewers, "PAGES_PER_READ", 1)
    w = viewers_tests.work(f, viewers_tests.sample())
    w.connection_requests = service(f)
    run_id = await viewers_tests.viewers_run(f)
    await viewers_tests.run_through(w, run_id)
    queued = await f.db.pool.fetch(
        """SELECT v.value_text AS name FROM linkedin_requests q
           JOIN record_values v ON v.record_id = q.record_id AND v.field = 'name'
           WHERE q.member = $1 AND q.status = 'queued'""",
        USER,
    )
    assert {row["name"] for row in queued} == {"Ada Lovelace", "Person One"}
    assert [source for *_rest, source in STARTED] == ["auto_approve"]
    assert await summary(f, run_id) == (
        "4 new profile views, 4 people new to Tin. Approved automatically: connection requests "
        "to 2 people who fit. Tin sends them one at a time on your schedule."
    )


async def test_with_sending_off_or_paused_nothing_is_approved(collection_db):
    f = await fixture(collection_db)
    await switch(f, enabled=True, consent_version=1, auto_approve=True)
    # A row someone left on while sending is off still approves nothing.
    await f.db.pool.execute(
        "UPDATE linkedin_sending SET enabled = false WHERE member = $1 AND auto_approve", USER
    )
    run_id = await read(f, [person(1), person(4)])
    assert set((await statuses(f)).values()) == {"proposed"} and STARTED == []
    assert "wait in Decisions" in await summary(f, run_id)

    # LinkedIn's warning holds sending until the member resumes: the batch waits for them.
    await switch(f, enabled=True, consent_version=1, auto_approve=True)
    async with f.db.pool.acquire() as conn:
        await requests.pause(conn, USER, "challenge")
    assert await service(f).auto_approve(f.project.id, USER, run_id=UUID(run_id)) == 0
    assert set((await statuses(f)).values()) == {"proposed"} and STARTED == []


async def test_a_retried_read_approves_once_and_starts_one_run(collection_db):
    f = await fixture(collection_db)
    await switch(f, enabled=True, consent_version=1, auto_approve=True)
    run_id = UUID(await read(f, [person(1), person(4)]))
    before = await statuses(f)
    [(_project, batch_id, _who, _source)] = STARTED
    # The read's publish runs again: the same decision, the same run.
    assert await service(f).auto_approve(f.project.id, USER, run_id=run_id) == 2
    assert await statuses(f) == before and len(STARTED) == 1
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM linkedin_request_batches WHERE member = $1", USER
        )
        == 1
    )

    # A retry after the decision committed but before the batch had a run gives it one, once:
    # here the member's run that still has requests waiting, so nothing new starts.
    later = UUID(await read(f, [person(5)], n=2, approving=False))
    batch = await f.db.pool.fetchval(
        "SELECT id FROM linkedin_request_batches WHERE member = $1 AND status = 'open'", USER
    )
    async with f.db.pool.acquire() as conn:
        await requests.decide(
            conn,
            f.project.id,
            batch,
            USER,
            request_id=uuid5(AUTO_APPROVE, f"auto-approve:{later}"),
            approve=True,
            people=[(await ids(f))[member(5)]],
        )
    STARTED.clear()
    for _ in range(2):
        assert await service(f).auto_approve(f.project.id, USER, run_id=later) == 1
    sends = await f.db.pool.fetch(
        "SELECT DISTINCT send_run_id FROM linkedin_request_batches WHERE member = $1", USER
    )
    assert STARTED == [] and len(sends) == 1 and sends[0]["send_run_id"] is not None
    assert batch != batch_id


async def test_it_approves_only_the_member_s_own_batch(collection_db):
    f = await fixture(collection_db)
    await add_teammate(f)
    await switch(f, enabled=True, consent_version=1, auto_approve=True)
    mine = await read(f, [person(1)])
    # The teammate didn't turn it on: their read's people wait in Decisions for them.
    theirs = await read(f, [person(8)], by=TEAMMATE, n=2)
    assert set((await statuses(f, TEAMMATE)).values()) == {"proposed"}
    assert {who for _p, _b, who, _s in STARTED} == {USER}
    # Your setting, or your read's key, never decides a teammate's batch.
    assert await service(f).auto_approve(f.project.id, TEAMMATE, run_id=UUID(theirs)) == 0
    assert await service(f).auto_approve(f.project.id, TEAMMATE, run_id=UUID(mine)) == 0
    assert set((await statuses(f, TEAMMATE)).values()) == {"proposed"}
    async with client(f, TEAMMATE) as http:
        batches = (await http.get(f"/api/projects/{f.project.id}/linkedin-requests")).json()
    assert [p["name"] for p in batches["batches"][0]["people"]] == ["Person 8"]


async def test_only_people_who_still_fit_and_wait_to_be_asked_are_approved(collection_db):
    f = await fixture(collection_db)
    run_id = UUID(await read(f, [person(1), person(4), person(5), person(6)], approving=False))
    known = await ids(f)
    # Since the read: 1 became a customer, 4 connected, 5 no longer fits.
    async with f.db.pool.acquire() as conn:
        await records.set_by_member(
            conn, f.project.id, known[member(1)], [records.Value("customer", "yes")], member=USER
        )
    await f.db.pool.execute(
        "UPDATE record_relationships SET state = 'connected' WHERE record_id = $1 AND member = $2",
        known[member(4)],
        USER,
    )
    await f.db.pool.execute(
        "UPDATE record_values SET value_number = 0.3 WHERE record_id = $1 AND field = 'icp_fit'",
        known[member(5)],
    )
    await switch(f, enabled=True, consent_version=1, auto_approve=True)
    assert await service(f).auto_approve(f.project.id, USER, run_id=run_id) == 1
    assert await statuses(f) == {
        known[member(1)]: "declined",
        known[member(4)]: "declined",
        known[member(5)]: "declined",
        known[member(6)]: "queued",
    }


async def test_the_sending_run_records_that_it_was_approved_automatically(
    collection_db, monkeypatch
):
    f = await fixture(collection_db)
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
    monkeypatch.setattr(ConnectionRequests, "_start", REAL_START)
    f.runtime = SimpleNamespace(
        database=f.db,
        storage=f.storage,
        temporal=SimpleNamespace(start_workflow=AsyncMock()),
        integrations=SimpleNamespace(ensure_requirements=AsyncMock()),
    )
    f.settings = SimpleNamespace(**vars(f.store.settings), task_queue="tin-test")
    await switch(f, enabled=True, consent_version=1, auto_approve=True)
    await read(f, [person(1)])
    run = await f.db.pool.fetchrow(
        "SELECT trigger_source, started_by_clerk_user_id FROM workflow_runs WHERE executor = $1",
        requests.KEY,
    )
    assert dict(run) == {"trigger_source": "auto_approve", "started_by_clerk_user_id": USER}
    f.runtime.temporal.start_workflow.assert_awaited_once()
