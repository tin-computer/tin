"""people.rate: rate everyone unrated who did something, once, and list them."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
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


async def test_a_second_run_while_one_is_going_or_past_the_days_guard_rates_nobody(
    collection_db, monkeypatch
):
    f = await fixture(collection_db)
    await person(f, "Ada Byron", "Founder at Acme")
    router = Router()
    work = PeopleRateActivities(database=f.db, storage=f.storage, router=router)
    first = await rate_run(f)
    second = await rate_run(f)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET created_at = now() - interval '1 minute' WHERE id=$1",
        UUID(first),
    )
    counts = await work.score(second)
    assert counts["held"] == "running" and not router.requests
    await work.publish({"run_id": second, **counts})
    summary = await f.db.pool.fetchval(
        "SELECT result_summary FROM workflow_runs WHERE id=$1", UUID(second)
    )
    assert summary.startswith("Another rating run for this project is still going")

    await f.db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", UUID(first))
    monkeypatch.setattr(people_rate, "RATED_PER_DAY", 0)
    third = await rate_run(f)
    counts = await work.score(third)
    assert counts["held"] == "daily" and not router.requests


async def test_site_visitors_are_rated_before_older_people_who_only_engaged(
    collection_db, monkeypatch
):
    from tin_lite import people_icp

    f = await fixture(collection_db)
    engaged = await person(f, "Eve Older", "Founder at Older", event="reacted")
    visitor = await person(f, "Vic Newer", "Founder at Newer")
    monkeypatch.setattr(people_icp, "MAX_SCORED", 1)
    router = Router()
    work = PeopleRateActivities(database=f.db, storage=f.storage, router=router)

    counts = await through(work, await rate_run(f))
    assert counts["rated"] == 1 and counts["left"] == 1
    people = {p["id"]: p for p in await records.people(f.db.pool, f.project.id)}
    assert people[visitor]["fields"]["icp_fit"]["number"] == 4
    assert people[engaged]["fields"].get("icp_fit", {}).get("number") is None


async def test_a_github_star_puts_someone_in_social_and_gets_them_rated(collection_db):
    f = await fixture(collection_db)
    star = await person(f, "Sam Star", "Founder at Starco", event="starred")
    async with f.db.pool.acquire() as conn:
        await records.add_event(
            conn,
            f.project.id,
            star,
            kind="starred",
            event_key="starred:acme/tool:samstar",
            detail={"repository": "acme/tool", "login": "samstar"},
        )
        listed = {p["id"]: p for p in await records.directory(conn, f.project.id, viewer="m")}
        shown = await records.person(conn, f.project.id, star, viewer="m")
    assert listed[star]["social"] is True and listed[star]["visited"] is False
    assert listed[star]["last_touch"]["kind"] == "starred"
    assert {e["repository"] for e in shown["events"]} == {"", "acme/tool"}

    router = Router()
    work = PeopleRateActivities(database=f.db, storage=f.storage, router=router)
    counts = await through(work, await rate_run(f))
    assert counts["rated"] == 1 and len(router.requests) == 1


class Flaky(Router):
    """The rating model, failing the first `fails` calls the way Gemini does when a rating
    searches past its deadline (a 504)."""

    def __init__(self, fails):
        super().__init__()
        self.fails = fails

    async def generate(self, route, request, *, timeout_seconds):
        from tin_lite.model_providers import ModelProviderError

        if self.fails:
            self.fails -= 1
            self.requests.append(request)
            raise ModelProviderError("504 Gateway Timeout")
        return await super().generate(route, request, timeout_seconds=timeout_seconds)


async def test_a_rating_that_times_out_is_asked_once_more_lighter_in_the_same_run(collection_db):
    from tin_lite import people_icp

    f = await fixture(collection_db)
    ada = await person(f, "Ada Byron", "Founder at Acme")
    router = Flaky(fails=1)
    work = PeopleRateActivities(database=f.db, storage=f.storage, router=router)

    counts = await through(work, await rate_run(f))
    assert counts["left"] == 0 and len(router.requests) == 2
    first, again = router.requests
    assert first.web_search_calls == people_icp.SEARCHES
    assert again.web_search_calls == people_icp.RETRY_SEARCHES
    assert again.reasoning_effort == people_icp.RETRY_REASONING
    people = {p["id"]: p for p in await records.people(f.db.pool, f.project.id)}
    assert people[ada]["fields"]["icp_fit"]["number"] == 4


async def test_a_person_neither_call_rates_is_tried_again_by_the_next_run(collection_db):
    f = await fixture(collection_db)
    ada = await person(f, "Ada Byron", "Founder at Acme")
    router = Flaky(fails=2)
    work = PeopleRateActivities(database=f.db, storage=f.storage, router=router)

    counts = await through(work, await rate_run(f))
    assert counts["left"] == 1 and len(router.requests) == 2
    people = {p["id"]: p for p in await records.people(f.db.pool, f.project.id)}
    assert people[ada]["fields"]["icp_fit"]["number"] is None

    await through(work, await rate_run(f))
    assert len(router.requests) == 3
    people = {p["id"]: p for p in await records.people(f.db.pool, f.project.id)}
    assert people[ada]["fields"]["icp_fit"]["number"] == 4


# The Temporal workflow: rounds until nobody is left.


def round_(rated, left, settled=None, held=""):
    return {
        "rated": rated,
        "left": left,
        "settled": rated if settled is None else settled,
        "available": True,
        "has_icp": True,
        "held": held,
    }


@pytest.mark.parametrize(
    ("rounds", "expected_rounds", "rated"),
    [
        # A backlog is rated round after round until nobody is left.
        ([round_(100, 250), round_(100, 150), round_(100, 50), round_(50, 0)], 4, 350),
        # A round that settles nobody (the model failing everyone) ends the run.
        ([round_(100, 250), round_(40, 210, settled=0), round_(100, 110)], 2, 140),
        # The daily guard ends it.
        ([round_(100, 250), round_(0, 250, held="daily")], 2, 100),
        # Nothing left after the first round: one round.
        ([round_(12, 0)], 1, 12),
    ],
)
async def test_the_workflow_rates_in_rounds_until_nobody_is_left(rounds, expected_rounds, rated):
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, Worker

    from tin_lite.workflows import PeopleRateWorkflow

    calls, published = [], []

    @activity.defn(name="people_rate_score")
    async def score(run_id):
        calls.append(run_id)
        return rounds[len(calls) - 1]

    @activity.defn(name="people_rate_phones")
    async def phones(run_id):
        return {"looked_up": 0, "found": 0, "left": 0, "held": False}

    @activity.defn(name="people_rate_publish")
    async def publish(control):
        published.append(control)

    @activity.defn(name="people_rate_failure")
    async def failure(argument):
        raise AssertionError("The run must not fail")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue, run_id = f"rate-{uuid4()}", str(uuid4())
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[PeopleRateWorkflow],
            activities=[score, phones, publish, failure],
        ):
            handle = await env.client.start_workflow(
                PeopleRateWorkflow.run, run_id, id=f"{people_rate.KEY}:{run_id}", task_queue=queue
            )
            await handle.result()
            history = await handle.fetch_history()
    await Replayer(workflows=[PeopleRateWorkflow]).replay_workflow(history)
    assert len(calls) == expected_rounds
    assert published[0]["rated"] == rated
    assert published[0]["left"] == rounds[expected_rounds - 1]["left"]


@pytest.mark.parametrize(
    ("steps", "expected_steps", "found"),
    [
        # Phones are looked up step after step until no lead is left without one.
        ([(5, 5, 20), (5, 4, 15), (5, 3, 0)], 3, 12),
        # A step that looks nobody up (Delegate refusing everyone) ends it.
        ([(5, 2, 20), (0, 0, 20)], 2, 2),
        # The daily guard ends it.
        ([(5, 5, 20, True)], 1, 5),
    ],
)
async def test_the_workflow_finds_phones_for_leads_after_rating(steps, expected_steps, found):
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, Worker

    from tin_lite.workflows import PeopleRateWorkflow

    calls, published = [], []

    @activity.defn(name="people_rate_score")
    async def score(run_id):
        return round_(3, 0)

    @activity.defn(name="people_rate_phones")
    async def phones(run_id):
        calls.append(run_id)
        looked, got, left, *held = steps[len(calls) - 1]
        return {"looked_up": looked, "found": got, "left": left, "held": bool(held)}

    @activity.defn(name="people_rate_publish")
    async def publish(control):
        published.append(control)

    @activity.defn(name="people_rate_failure")
    async def failure(argument):
        raise AssertionError("The run must not fail")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue, run_id = f"rate-{uuid4()}", str(uuid4())
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[PeopleRateWorkflow],
            activities=[score, phones, publish, failure],
        ):
            handle = await env.client.start_workflow(
                PeopleRateWorkflow.run, run_id, id=f"{people_rate.KEY}:{run_id}", task_queue=queue
            )
            await handle.result()
            history = await handle.fetch_history()
    await Replayer(workflows=[PeopleRateWorkflow]).replay_workflow(history)
    assert len(calls) == expected_steps
    assert published[0]["phones"] == found


def test_the_summary_says_how_many_leads_got_phones():
    said = people_rate.summary(rated=4, good=2, left=0, available=True, has_icp=True, phones=2)
    assert said == "Rated 4 people, 2 a good or strong fit. Found phone numbers for 2 leads."
