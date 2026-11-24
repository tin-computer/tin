"""people.rate: rate everyone unrated who did something, once, and list them."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from test_connection_collection import collection_db as collection_db
from test_linkedin_engagers import Router, fixture

from tin_lite import people_rate
from tin_lite import people_records as records
from tin_lite.people_rate_activities import PeopleRateActivities
from tin_lite.people_records import Value


async def rate_run(f):
    from test_connection_collection import USER

    workflow_id = await f.db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", people_rate.KEY)
    if workflow_id is None:
        workflow_id = uuid4()
        await f.db.pool.execute(
            """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                    definition_path,current_commit_sha,version_label,definition)
               VALUES ($1,$2,$2,$2,'registry/workflows','r.json',$3,'1','{}')""",
            workflow_id,
            people_rate.KEY,
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
        people_rate.KEY,
        "d" * 40,
        f"{people_rate.KEY}:{run_id}",
        str(run_id),
        USER,
        json.dumps({"project_id": str(f.project.id)}),
    )
    return str(run_id)


async def person(f, name, headline, *, event="visited", member_fit=None):
    async with f.db.pool.acquire() as conn:
        await records.ensure_fields(conn, f.project.id)
        record_id = await records.upsert_person(
            conn, f.project.id, {"linkedin_slug": name.lower().replace(" ", "-")}, source="test"
        )
        await records.set_values(
            conn,
            f.project.id,
            record_id,
            [Value("name", name), Value("headline", headline)],
            set_by_kind="run",
            set_by="test",
        )
        if member_fit is not None:
            await records.set_values(
                conn,
                f.project.id,
                record_id,
                [Value("icp_fit", member_fit, "Our call")],
                set_by_kind="member",
                set_by="member-1",
            )
        await records.add_event(
            conn, f.project.id, record_id, kind=event, event_key=f"{event}:{record_id}", detail={}
        )
    return record_id


async def through(work, run_id):
    counts = await work.score(run_id)
    await work.publish({"run_id": run_id, **counts})
    return counts


async def test_everyone_unrated_who_did_something_is_rated_once_and_listed(collection_db):
    f = await fixture(collection_db)
    ada = await person(f, "Ada Byron", "Founder at Acme")
    bob = await person(f, "Bob Stone", "Student")
    kept = await person(f, "Cy Ray", "Founder at Kept", member_fit=2)
    # An imported connection did nothing worth rating.
    await person(f, "Dee Link", "Founder at Elsewhere", event="connected")
    router = Router()
    work = PeopleRateActivities(database=f.db, storage=f.storage, router=router)

    run_id = await rate_run(f)
    counts = await through(work, run_id)
    assert counts["left"] == 0 and counts["available"] and counts["has_icp"]
    assert len(router.requests) == 2  # Ada and Bob only

    people = {p["id"]: p for p in await records.people(f.db.pool, f.project.id)}
    assert people[ada]["fields"]["icp_fit"]["number"] == 4
    assert people[bob]["fields"]["icp_fit"]["number"] == 1
    # The member's own rating stays.
    assert people[kept]["fields"]["icp_fit"]["number"] == 2

    listed = f.storage.repo.trees[f.storage.repo.head][people_rate.PATH][1].decode()
    assert listed.index("Ada Byron") < listed.index("Bob Stone")
    assert "4 · Strong fit" in listed and "Cy Ray" not in listed and "Dee Link" not in listed
    state = await f.db.pool.fetchrow(
        "SELECT status, result_summary FROM workflow_runs WHERE id=$1", UUID(run_id)
    )
    assert state["status"] == "succeeded"
    assert state["result_summary"] == "Rated 2 people, 1 a good or strong fit."

    # A second run finds nobody left to rate.
    again = await rate_run(f)
    await through(work, again)
    assert len(router.requests) == 2
    summary = await f.db.pool.fetchval(
        "SELECT result_summary FROM workflow_runs WHERE id=$1", UUID(again)
    )
    assert summary == "Everyone who did something already has a current rating."


async def test_without_the_model_nobody_is_rated_and_the_run_says_why(collection_db):
    f = await fixture(collection_db)
    await person(f, "Ada Byron", "Founder at Acme")

    class NoModel(Router):
        configured_providers = frozenset()

    work = PeopleRateActivities(database=f.db, storage=f.storage, router=NoModel())
    run_id = await rate_run(f)
    await through(work, run_id)
    summary = await f.db.pool.fetchval(
        "SELECT result_summary FROM workflow_runs WHERE id=$1", UUID(run_id)
    )
    assert summary == "Rating isn't available on this deployment, so nobody was rated."


async def test_without_an_icp_file_nobody_is_rated_and_the_run_says_why(collection_db):
    g = await fixture(collection_db, icp=None)
    await person(g, "Bo Lee", "Founder at Beta")
    work = PeopleRateActivities(database=g.db, storage=g.storage, router=Router())
    run_id = await rate_run(g)
    await through(work, run_id)
    summary = await g.db.pool.fetchval(
        "SELECT result_summary FROM workflow_runs WHERE id=$1", UUID(run_id)
    )
    assert summary == "Add strategy/ICP.md so Tin can rate people against it."


def test_the_report_lists_best_first_and_says_who_is_left():
    text = people_rate.report(
        [{"name": "A | B", "headline": "CFO", "fit": 4, "intent": "hot", "reason": "Fits."}],
        rated=1,
        left=3,
    )
    assert "| A / B | CFO | 4 · Strong fit | hot | Fits. |" in text
    assert "3 more still need a rating" in text
    assert json.dumps(people_rate.INPUT_SCHEMA)
