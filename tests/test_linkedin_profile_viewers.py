"""People who viewed the founder's LinkedIn profile: reads, records, scores, IDs and the list.

The founder's saved LinkedIn session is a real one from the collection fixtures; the sandbox
reads and the model are synthetic.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_connection_collection import USER
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import NOW, Router, fixture, member

from tin_lite import linkedin_profile_viewers as viewers
from tin_lite import linkedin_requests as requests
from tin_lite import people_records as records
from tin_lite.connection_collection import CollectionError
from tin_lite.integrations import IntegrationAuthorizationError
from tin_lite.linkedin_cloud import ENGAGEMENT_OPERATION
from tin_lite.linkedin_profile_viewers_activities import LinkedInProfileViewersActivities
from tin_lite.linkedin_viewers import PAGER, PAGINATION
from tin_lite.workflows import LinkedInProfileViewersWorkflow


def request(start: int) -> dict:
    return {
        "$type": PAGINATION,
        "pagerId": PAGER,
        "requestedArguments": {"payload": {"start": start, "count": 10}},
    }


def viewer(
    name, *, slug=None, who=None, headline="Founder at Startup", degree="2nd", age="1d", at=None
):
    return {
        "member": who,
        "slug": slug,
        "name": name,
        "headline": headline,
        "degree": degree,
        "age": None if at else age,
        "viewed_at": at.isoformat() if at else None,
    }


class Cloud:
    """The LinkedIn sandbox: the overview, the list's pages, and profile IDs by slug."""

    def __init__(self, pages, *, overview=(), ids=None, errors=None, reason=None):
        self.pages, self.overview, self.ids = pages, list(overview), ids or {}
        self.errors, self.reason, self.now = errors or {}, reason, NOW
        self.calls, self.cleaned = [], []

    async def engagement(self, conn, *, execution_key, run_id, session, packet, seconds=270):
        assert session["cookies"] and packet["actor"]["key"]
        self.calls.append(packet)
        if packet["kind"] == "linkedin_profile_ids":
            return {
                "people": [
                    {"slug": s, "member": self.ids.get(s), "state": "none"} for s in packet["slugs"]
                ]
            }
        assert packet["kind"] == "linkedin_viewers"
        index = (
            packet["request"]["requestedArguments"]["payload"]["start"] // 10
            if packet["request"]
            else 0
        )
        if index in self.errors:
            raise CollectionError(self.errors[index])
        taken = self.pages[index : index + packet["pages"]]
        after = index + len(taken)
        return {
            "viewers": (self.overview if packet["overview"] else [])
            + [v for page in taken for v in page],
            "anonymous": len(taken),
            "pages": len(taken),
            "total_views": 455 if packet["overview"] else None,
            "next": request(after * 10) if after < len(self.pages) and not self.reason else None,
            "reason": self.reason,
            "read_at": self.now.isoformat(),
        }

    async def cleanup(self, sandbox_id):
        self.cleaned.append(sandbox_id)


async def viewers_run(f, *, by=None):
    workflow_id = await f.db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", viewers.KEY)
    if workflow_id is None:
        workflow_id = uuid4()
        await f.db.pool.execute(
            """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                    definition_path,current_commit_sha,version_label,definition)
               VALUES ($1,$2,$2,$2,'registry/workflows','v.json',$3,'1','{}')""",
            workflow_id,
            viewers.KEY,
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
        viewers.KEY,
        "d" * 40,
        f"{viewers.KEY}:{run_id}",
        str(run_id),
        by or USER,
        json.dumps({"project_id": str(f.project.id)}),
    )
    return str(run_id)


def work(f, cloud, router=None):
    return LinkedInProfileViewersActivities(
        database=f.db,
        storage=f.storage,
        settings=f.store.settings,
        cipher=f.cipher,
        cloud=cloud,
        router=router or Router(),
    )


async def run_through(w, run_id, *, reads=viewers.MAX_READS):
    """What the Temporal workflow does, called directly."""

    await w.viewers_prepare(run_id)
    notes = {"viewers": 0, "new": 0, "anonymous": 0, "reads": 0}
    for index in range(reads):
        read = await w.viewers_read({"run_id": run_id, "read": index})
        notes["reads"] += 1
        notes["viewers"] += read["people"] or 0
        notes["new"] += read["new"] or 0
        notes["anonymous"] += read["anonymous"] or 0
        if read.get("total_views") is not None:
            notes.setdefault("total_views", read["total_views"])
        if read.get("reason") == "viewers_format_changed":
            notes["format_changed"] = True
        if read["outcome"] == "stopped":
            notes["stopped"] = read["reason"]
            break
        if read["outcome"] != "read" or not read["more"] or not read["new"]:
            break
    await w.viewers_score(run_id)
    if not notes.get("stopped"):
        notes["looked_up"] = (await w.viewers_ids(run_id))["looked_up"]
    await w.viewers_publish({"run_id": run_id, "notes": notes})
    return notes


def listed(storage):
    tree = storage.repo.trees[storage.repo.head]
    rows = list(csv.DictReader(io.StringIO(tree[viewers.CSV_PATH][1].decode())))
    return rows, tree[viewers.SUMMARY_PATH][1].decode()


def sample():
    first = member(1)
    return Cloud(
        [
            [
                viewer("Ada Lovelace", slug="ada-lovelace", age="1h"),
                # The overview's viewer, named here only by slug: the same view.
                viewer("Person One", slug="person-one", age="1h"),
                viewer("Bob Builder", slug="bob", headline="Designer", degree="1st", age="3d"),
            ],
            [viewer("Carol Shannon", slug="carol", degree="3rd", age="2w")],
        ],
        overview=[viewer("Person One", slug="person-one", who=first, at=NOW - timedelta(hours=1))],
        ids={"ada-lovelace": member(10)},
    )


async def test_a_run_keeps_who_viewed_scores_them_finds_their_ids_and_proposes_them(
    collection_db, monkeypatch
):
    f = await fixture(collection_db)
    cloud = sample()
    # One page a read, so the second page takes a second read from where the first stopped.
    monkeypatch.setattr(viewers, "PAGES_PER_READ", 1)
    run_id = await viewers_run(f)
    notes = await run_through(work(f, cloud), run_id)
    assert notes["new"] == 4 and notes["reads"] == 2 and notes["anonymous"] == 2
    assert notes["total_views"] == 455 and notes["looked_up"] == 1
    reads = [c for c in cloud.calls if c["kind"] == "linkedin_viewers"]
    assert [(c["overview"], c["request"]) for c in reads] == [(True, None), (False, request(10))]

    people = {p["fields"]["name"]["text"]: p for p in await records.people(f.db.pool, f.project.id)}
    assert set(people) == {"Ada Lovelace", "Person One", "Bob Builder", "Carol Shannon"}
    # The overview's profile ID and the list's slug are one person, viewing once.
    one = people["Person One"]
    assert one["identifiers"] == {"linkedin_member": member(1), "linkedin_slug": "person-one"}
    assert one["events"] == {"viewed": 1}
    # A viewer who fits and is known only by slug got their profile ID, so they can be asked.
    assert people["Ada Lovelace"]["identifiers"]["linkedin_member"] == member(10)
    assert "linkedin_member" not in people["Carol Shannon"]["identifiers"]
    lookups = [c for c in cloud.calls if c["kind"] == "linkedin_profile_ids"]
    assert [c["slugs"] for c in lookups] == [["ada-lovelace", "carol"]]
    mine = await records.relationships(f.db.pool, f.project.id, USER)
    assert mine[people["Bob Builder"]["id"]]["state"] == "connected"
    assert mine[people["Ada Lovelace"]["id"]]["state"] == "none"
    # The view's date is as exact as LinkedIn says it.
    when = await f.db.pool.fetchval(
        "SELECT occurred_at FROM record_events WHERE record_id=$1 AND kind='viewed'",
        people["Carol Shannon"]["id"],
    )
    assert abs(when - (NOW - timedelta(weeks=2))) < timedelta(minutes=1)

    # Those who fit, aren't connected and have a profile ID wait in Decisions.
    async with f.db.pool.acquire() as conn:
        batches = await requests.open_batches(conn, f.project.id, USER, NOW)
    proposed = {p["name"]: p for p in batches[0]["people"]}
    assert set(proposed) == {"Ada Lovelace", "Person One"}
    assert proposed["Ada Lovelace"]["engaged"]["kind"] == "viewed"

    rows, summary = listed(f.storage)
    assert [r["Name"] for r in rows][:3] == ["Ada Lovelace", "Person One", "Carol Shannon"]
    assert rows[-1]["Name"] == "Bob Builder" and rows[-1]["Connected"] == "yes"
    assert (
        rows[0]["Views"] == "1" and rows[0]["Last viewed"] == f"{NOW - timedelta(hours=1):%Y-%m-%d}"
    )
    assert "4 people in the list, 3 not connected to you yet." in summary
    state = await f.db.pool.fetchrow(
        "SELECT status, artifact_path, result_summary FROM workflow_runs WHERE id=$1",
        UUID(run_id),
    )
    assert state["status"] == "succeeded" and state["artifact_path"] == viewers.SUMMARY_PATH
    assert state["result_summary"] == (
        "4 new profile views, 4 people new to Tin. 2 people who fit and aren't connected wait "
        "in Decisions for your connection requests."
    )
    assert "LinkedIn counts 455 views in the last 90 days. 2 of the views" in summary
    # No receipt keeps anyone's name once their page is written.
    kept = await f.db.pool.fetch(
        "SELECT result::text AS result FROM effect_receipts WHERE operation=$1",
        ENGAGEMENT_OPERATION,
    )
    assert kept and not any(name in row["result"] for name in people for row in kept)

    # The next day LinkedIn says each view is a day older: nothing new, so one read is enough.
    cloud.calls.clear()
    cloud.now = NOW + timedelta(days=1)
    for page in cloud.pages:
        for item in page:
            item["age"] = {"1h": "1d", "3d": "4d", "2w": "2w"}[item["age"]]
    again = await viewers_run(f)
    notes = await run_through(work(f, cloud), again)
    assert notes["new"] == 0 and notes["reads"] == 1
    # Read before, so the run starts with the newest pages only.
    assert [c["pages"] for c in cloud.calls if c["kind"] == "linkedin_viewers"] == [
        viewers.LATER_PAGES
    ]
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM record_events WHERE project_id=$1 AND kind='viewed'", f.project.id
        )
        == 4
    )


async def test_a_view_on_a_later_day_counts_again(collection_db):
    f = await fixture(collection_db)
    cloud = Cloud([[viewer("Ada Lovelace", slug="ada-lovelace", age="1d")]])
    await run_through(work(f, cloud), await viewers_run(f))
    cloud.now = NOW + timedelta(days=4)
    cloud.pages[0].insert(0, viewer("Ada Lovelace", slug="ada-lovelace", age="2h"))
    cloud.pages[0][1]["age"] = "5d"
    notes = await run_through(work(f, cloud), await viewers_run(f))
    assert notes["new"] == 1
    person = (await records.people(f.db.pool, f.project.id))[0]
    assert person["events"] == {"viewed": 2}


async def test_a_list_tin_cant_read_keeps_the_overview_and_says_so(collection_db):
    f = await fixture(collection_db)
    cloud = Cloud(
        [],
        overview=[viewer("Person One", slug="person-one", who=member(1), at=NOW)],
        reason="viewers_format_changed",
    )
    run_id = await viewers_run(f)
    notes = await run_through(work(f, cloud), run_id)
    assert notes["format_changed"] and notes["new"] == 1
    summary = await f.db.pool.fetchval(
        "SELECT result_summary FROM workflow_runs WHERE id=$1", UUID(run_id)
    )
    assert summary.startswith("1 new profile view, 1 person new to Tin. 1 person who fits")
    # Tin's own problem to fix: said in the list, not put to the founder as a step.
    assert (
        "LinkedIn changed its profile-views list, so Tin read only its overview"
        in listed(f.storage)[1]
    )


async def test_a_signed_out_session_stops_the_run_with_what_to_do(collection_db):
    f = await fixture(collection_db)
    cloud = Cloud([[viewer("Ada", slug="ada")]], errors={0: "session_expired"})
    w = work(f, cloud)
    run_id = await viewers_run(f)
    await w.viewers_prepare(run_id)
    with pytest.raises(IntegrationAuthorizationError, match="saved LinkedIn session has expired"):
        await w.viewers_read({"run_id": run_id, "read": 0})


async def test_linkedin_objecting_later_keeps_what_was_read(collection_db, monkeypatch):
    f = await fixture(collection_db)
    monkeypatch.setattr(viewers, "PAGES_PER_READ", 1)
    cloud = Cloud(
        [[viewer("Ada", slug="ada")], [viewer("Bob", slug="bob")]], errors={1: "rate_limited"}
    )
    run_id = await viewers_run(f)
    notes = await run_through(work(f, cloud), run_id)
    assert notes["stopped"] == "rate_limited" and notes["new"] == 1
    # Looking up profile IDs waits for the next run.
    assert not [c for c in cloud.calls if c["kind"] == "linkedin_profile_ids"]
    summary = await f.db.pool.fetchval(
        "SELECT result_summary FROM workflow_runs WHERE id=$1", UUID(run_id)
    )
    assert summary.startswith("LinkedIn asked Tin to slow down")


async def test_a_member_sees_their_own_viewers_and_a_teammates(collection_db):
    f = await fixture(collection_db)
    cloud = Cloud([[viewer("Ada Lovelace", slug="ada-lovelace", who=member(3))]])
    await run_through(work(f, cloud), await viewers_run(f))
    person = (await records.people(f.db.pool, f.project.id))[0]
    async with f.db.pool.acquire() as conn:
        mine = await records.person(conn, f.project.id, person["id"], viewer=USER)
        theirs = await records.person(conn, f.project.id, person["id"], viewer="someone-else")
        listing = await records.directory(conn, f.project.id, viewer=USER)
    assert [e["kind"] for e in mine["events"]] == ["viewed"]
    # Whose profile: the run read the LinkedIn of the member who started it.
    assert mine["events"][0]["yours"] is True and theirs["events"][0]["yours"] is False
    listed_person = next(p for p in listing if p["id"] == person["id"])
    assert listed_person["last_touch"]["kind"] == "viewed"
    assert listed_person["added"]["by"] == "profile viewers"


def stubs(calls, reads):
    @activity.defn(name="linkedin_viewers_prepare")
    async def prepare(run_id):
        calls.append("prepare")

    @activity.defn(name="linkedin_viewers_read")
    async def read(control):
        calls.append(("read", control["read"]))
        return reads[control["read"]]

    @activity.defn(name="linkedin_viewers_score")
    async def score(run_id):
        calls.append("score")

    @activity.defn(name="linkedin_viewers_ids")
    async def ids(run_id):
        calls.append("ids")
        return {"looked_up": 2, "reason": None}

    @activity.defn(name="linkedin_viewers_publish")
    async def publish(control):
        calls.append(("publish", control["notes"]))

    @activity.defn(name="linkedin_viewers_failure")
    async def failure(argument):
        raise AssertionError("The run must not fail")

    return [prepare, read, score, ids, publish, failure]


def outcome(*, new=5, more=True, kind="read", reason=None):
    return {
        "outcome": kind,
        "reason": reason,
        "people": new,
        "new": new,
        "anonymous": 1,
        "pages": 10,
        "total_views": 455,
        "more": more,
    }


@pytest.mark.parametrize(
    ("reads", "expected"),
    [
        # Reads go on while they find someone new and LinkedIn has more, up to three.
        ([outcome(), outcome(), outcome()], [0, 1, 2]),
        # A read with nobody new means the rest is older: stop there.
        ([outcome(), outcome(new=0), outcome()], [0, 1]),
        ([outcome(more=False), outcome(), outcome()], [0]),
        # LinkedIn objecting stops reading, and the lookups wait for the next run.
        ([outcome(), outcome(kind="stopped", reason="rate_limited", new=0), outcome()], [0, 1]),
    ],
)
async def test_the_workflow_reads_until_nobody_new_and_keeps_only_counts(reads, expected):
    calls = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue, run_id = f"viewers-{uuid4()}", str(uuid4())
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[LinkedInProfileViewersWorkflow],
            activities=stubs(calls, reads),
        ):
            handle = await env.client.start_workflow(
                LinkedInProfileViewersWorkflow.run,
                run_id,
                id=f"{viewers.KEY}:{run_id}",
                task_queue=queue,
            )
            await handle.result()
            history = await handle.fetch_history()
    await Replayer(workflows=[LinkedInProfileViewersWorkflow]).replay_workflow(history)
    assert [c[1] for c in calls if isinstance(c, tuple) and c[0] == "read"] == expected
    stopped = any(r["outcome"] == "stopped" for r in reads[: len(expected)])
    assert ("ids" in calls) is not stopped
    notes = next(c[1] for c in calls if isinstance(c, tuple) and c[0] == "publish")
    assert notes["new"] == sum(reads[i]["new"] for i in expected) and notes["total_views"] == 455
    # History holds counts only: no names, headlines or LinkedIn's next-page request.
    events = json.dumps([e.to_json() if hasattr(e, "to_json") else str(e) for e in history.events])
    assert PAGINATION not in events


def test_a_view_dates_as_exactly_as_linkedin_says():
    read = NOW
    assert viewers.viewed_when({"age": "3h"}, read) == (read - timedelta(hours=3), 1)
    assert viewers.viewed_when({"age": "2w"}, read) == (read - timedelta(weeks=2), 4)
    assert viewers.viewed_when({"age": "1mo"}, read) == (read - timedelta(days=30), 15)
    exact = NOW - timedelta(minutes=7)
    assert viewers.viewed_when({"viewed_at": exact.isoformat()}, read) == (exact, 1)
