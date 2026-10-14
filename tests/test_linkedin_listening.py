"""LinkedIn listening: a published post read at fixed times after posting, the daily results run,
and the results files written from Tin's own readings. A synthetic Zernio, real SQL, and a
time-skipping Temporal for the timetable."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker
from test_linkedin_publish import (
    MEMBER,
    PAGE,
    URL,
    SyntheticPosting,
    SyntheticZernio,
    reply,
    zernio_client,
)
from test_linkedin_publish import activities as publish_activities
from test_linkedin_publish import setup as publish_setup
from test_private_workflows import ACTOR
from test_procedure_publication import HistoryStorage
from test_procedure_publication import publication_db as publication_db

from tin_lite import linkedin_listening as listening
from tin_lite import linkedin_listening_activities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.integrations import IntegrationAuthorizationError, IntegrationUpstreamError
from tin_lite.linkedin_listening_activities import LinkedInListeningActivities
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_inputs import WorkflowInputError
from tin_lite.workflows import LinkedInListenWorkflow, LinkedInPublishWorkflow

URN = "urn:li:share:7300000000000000000"
PAGE_URN = "urn:li:share:7300000000000000009"
DRAFT = "social/linkedin/drafts/2026-10-14-launch.json"
PUBLISHED = datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)


def numbers(impressions, reactions=0, comments=0):
    return {
        "impressions": impressions,
        "reach": impressions // 2,
        "reactions": reactions,
        "comments": comments,
        "shares": 0,
        "clicks": 3,
        "saves": 0,
        "sends": 0,
        "engagement_rate": 2.5,
    }


class ListeningZernio(SyntheticZernio):
    def __init__(self):
        super().__init__()
        # Answers per post URN, in order; the last one repeats.
        self.analytics: dict[str, list] = {}
        self.analytics_calls: list[tuple[str, str]] = []
        self.page: list[dict] | Exception = []
        self.followers: dict[str, int] | Exception = {PAGE["id"]: 1897, MEMBER["id"]: 2687}

    async def create_post(self, **kwargs):
        value = await super().create_post(**kwargs)
        return {**value, "urn": URN, "published_at": PUBLISHED.isoformat()}

    async def post_analytics(self, account_id, urn):
        self.analytics_calls.append((account_id, urn))
        answers = self.analytics.setdefault(urn, [numbers(100)])
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def page_posts(self, account_id):
        assert account_id == PAGE["id"]
        if isinstance(self.page, Exception):
            raise self.page
        return self.page

    async def follower_counts(self, account_ids):
        if isinstance(self.followers, Exception):
            raise self.followers
        return {key: value for key, value in self.followers.items() if key in account_ids}


class ListeningPosting(SyntheticPosting):
    def __init__(self, accounts=(PAGE, MEMBER)):
        super().__init__(accounts)
        self.zernio = ListeningZernio()


async def published(db, storage, posting, *, account=PAGE):
    """A post Tin published through the publish activities, with the draft it came from."""

    run_id, approval_id = await publish_setup(db, storage, posting, account=account)
    await db.pool.execute(
        "UPDATE effect_receipts SET result = result || $2::jsonb WHERE execution_key = $1",
        f"linkedin:approved:{approval_id}",
        json.dumps({"path": DRAFT, "post_id": "p1"}),
    )
    await publish_activities(db, storage, posting).execute(str(run_id))
    return await db.get_run(run_id)


async def run_row(db, project_id, key, inputs):
    workflow_id = await db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", key)
    if workflow_id is None:
        workflow_id = uuid4()
        await db.pool.execute(
            """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                    definition_path,current_commit_sha,version_label,definition)
               VALUES ($1,$2,$2,$2,'registry/workflows','l.json',$3,'1','{}')""",
            workflow_id,
            key,
            "d" * 40,
        )
    run_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflow_runs
           (id,project_id,workflow_id,executor,definition_commit_sha,
            temporal_workflow_id,thread_id,generation,fencing_token,status,lease_active,
            started_by_clerk_user_id,input)
           VALUES ($1,$2,$3,$4,$5,$6,$7,1,1,'pending',false,$8,$9::jsonb)""",
        run_id,
        project_id,
        workflow_id,
        key,
        "d" * 40,
        f"{key}:{run_id}",
        str(run_id),
        ACTOR,
        json.dumps({"project_id": str(project_id), **inputs}),
    )
    return run_id


def work(db, storage, posting):
    return LinkedInListeningActivities(
        database=db,
        storage=storage,
        integrations=SimpleNamespace(linkedin_posting=posting, ensure_requirements=AsyncMock()),
        settings=SimpleNamespace(task_queue="listening-test"),
    )


def files(storage):
    tree = storage.repo.trees[storage.repo.head]
    return json.loads(tree[listening.RESULTS_JSON][1]), tree[listening.RESULTS_MD][1].decode()


async def run_state(db, run_id):
    return await db.pool.fetchrow(
        "SELECT status, artifact_path, result_summary, error_message FROM workflow_runs "
        "WHERE id=$1",
        run_id,
    )


async def listening_run(db, storage, posting, **kwargs):
    publish = await published(db, storage, posting, **kwargs)
    run_id = await run_row(
        db, publish.project_id, listening.LISTEN_KEY, {"publish_run_id": str(publish.id)}
    )
    return publish, str(run_id)


# One post Tin published, read at fixed times after posting.


@pytest.mark.asyncio
async def test_a_published_post_is_read_at_each_checkpoint_into_the_results_files(
    publication_db,
):
    db, storage, posting = publication_db, HistoryStorage(), ListeningPosting()
    publish, run_id = await listening_run(db, storage, posting)
    w = work(db, storage, posting)
    prepared = await w.prepare(run_id)
    assert prepared == {"published_at": PUBLISHED.isoformat()}
    assert await w.prepare(run_id) == prepared
    posting.zernio.analytics[URN] = [
        numbers(120, 4),
        numbers(480, 9, 2),
        numbers(900, 20, 3),
        numbers(1234, 25, 4),
    ]
    for checkpoint in ("1h", "4h", "24h", "72h"):
        assert await w.read({"run_id": run_id, "checkpoint": checkpoint}) == "read"
    # A retried reading reads LinkedIn no more and writes the files no more.
    writes = storage.repo.writes
    assert await w.read({"run_id": run_id, "checkpoint": "72h"}) == "read"
    assert storage.repo.writes == writes and len(posting.zernio.analytics_calls) == 4
    assert {call[0] for call in posting.zernio.analytics_calls} == {PAGE["id"]}

    document, text = files(storage)
    assert document["schema_version"] == "tin.social.linkedin_results.v1"
    (post,) = document["posts"]
    assert post["urn"] == URN and post["url"] == URL and post["source"] == "tin"
    assert post["draft"] == {"path": DRAFT, "post_id": "p1"}
    assert post["account"] == {"urn": PAGE["urn"], "kind": "organization", "name": "Tin Computer"}
    assert post["readings"]["1h"]["impressions"] == 120
    assert post["readings"]["72h"]["reactions"] == 25
    assert post["latest"]["checkpoint"] == "72h" and post["excerpt"] == "Exact approved text"
    assert "| 3 days | 1,234 | 617 | 25 | 4 | 0 | 3 | 2.50% |" in text
    assert f"From `{DRAFT}`, post p1" in text and f"[View on LinkedIn]({URL})" in text

    await w.finish(run_id)
    await w.finish(run_id)
    row = await run_state(db, UUID(run_id))
    assert row["status"] == "succeeded" and row["artifact_path"] == listening.RESULTS_MD
    assert row["result_summary"] == "After 3 days: 1,234 impressions, 25 reactions, 4 comments."


@pytest.mark.asyncio
async def test_an_unanswered_reading_is_skipped_and_a_removed_post_ends_listening(
    publication_db, monkeypatch
):
    db, storage, posting = publication_db, HistoryStorage(), ListeningPosting()
    _, run_id = await listening_run(db, storage, posting)
    w = work(db, storage, posting)
    await w.prepare(run_id)
    posting.zernio.analytics[URN] = [IntegrationUpstreamError("down"), None]
    # Before the last attempt an unanswered reading is retried by Temporal.
    monkeypatch.setattr(linkedin_listening_activities, "_attempt", lambda: 1)
    with pytest.raises(IntegrationUpstreamError):
        await w.read({"run_id": run_id, "checkpoint": "1h"})
    monkeypatch.undo()
    posting.zernio.analytics[URN] = [IntegrationUpstreamError("down"), None]
    writes = storage.repo.writes
    assert await w.read({"run_id": run_id, "checkpoint": "1h"}) == "skipped"
    assert storage.repo.writes == writes
    assert await w.read({"run_id": run_id, "checkpoint": "4h"}) == "removed"
    document, text = files(storage)
    assert document["posts"][0]["removed_at"] is not None
    assert "so Tin stopped reading it" in text and "No reading yet." not in text
    await w.finish(run_id)
    row = await run_state(db, UUID(run_id))
    assert row["status"] == "succeeded"
    assert row["result_summary"] == (
        "The post was removed from LinkedIn, so Tin stopped reading it. "
        "Skipped after 1 hour: LinkedIn didn't answer."
    )


@pytest.mark.asyncio
async def test_a_reading_outlives_files_that_could_not_be_written(publication_db, monkeypatch):
    db, storage, posting = publication_db, HistoryStorage(), ListeningPosting()
    _, run_id = await listening_run(db, storage, posting)
    w = work(db, storage, posting)
    await w.prepare(run_id)
    written = w._publish

    async def unavailable(run, **kwargs):
        raise RuntimeError("Project files are unavailable")

    monkeypatch.setattr(w, "_publish", unavailable)
    monkeypatch.setattr(linkedin_listening_activities, "_attempt", lambda: 1)
    with pytest.raises(RuntimeError):
        await w.read({"run_id": run_id, "checkpoint": "1h"})
    # On the last attempt the reading stands, and the next write of the files includes it.
    monkeypatch.setattr(linkedin_listening_activities, "_attempt", lambda: 5)
    assert await w.read({"run_id": run_id, "checkpoint": "1h"}) == "read"
    assert len(posting.zernio.analytics_calls) == 1
    monkeypatch.setattr(w, "_publish", written)
    await w.finish(run_id)
    document, _ = files(storage)
    assert document["posts"][0]["readings"]["1h"]["impressions"] == 100
    assert (await run_state(db, UUID(run_id)))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_a_post_whose_account_left_the_connection_is_never_read(publication_db):
    db, storage, posting = publication_db, HistoryStorage(), ListeningPosting()
    _, run_id = await listening_run(db, storage, posting)
    w = work(db, storage, posting)
    await w.prepare(run_id)
    posting.accounts = [MEMBER]
    with pytest.raises(IntegrationAuthorizationError, match="Reconnect LinkedIn posting"):
        await w.read({"run_id": run_id, "checkpoint": "1h"})
    assert posting.zernio.analytics_calls == []
    await w.listen_failure(
        {
            "run_id": run_id,
            "kind": "needs_you",
            "reason": "The LinkedIn account isn't connected anymore.",
            "action": "integrations",
        }
    )
    row = await run_state(db, UUID(run_id))
    assert row["status"] == "failed"
    assert row["error_message"] == "The LinkedIn account isn't connected anymore."


@pytest.mark.asyncio
async def test_listening_reads_only_a_post_tin_published_in_the_same_project(publication_db):
    db, storage, posting = publication_db, HistoryStorage(), ListeningPosting()
    publish = await published(db, storage, posting)
    other = await db.create_project(name="Another project", state_repo_id=f"p/{uuid4()}")
    run_id = await run_row(db, other.id, listening.LISTEN_KEY, {"publish_run_id": str(publish.id)})
    with pytest.raises(Exception, match="only posts it published in this project"):
        await work(db, storage, posting).prepare(str(run_id))


async def registered(db, storage, key):
    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == key)
    await db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=key,
        title=builtin.title,
        description=builtin.description,
        executor=key,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha="d" * 40,
        version_label=builtin.version_label,
        definition=builtin.definition,
    )

    async def read(**kwargs):
        assert kwargs["repo_id"] == "registry/workflows"
        return json.dumps(builtin.definition).encode()

    storage.read_canonical_artifact = read
    return await db.get_workflow(builtin.id)


@pytest.mark.asyncio
async def test_publishing_prepares_one_listening_run_and_nobody_else_starts_one(publication_db):
    db, storage, posting = publication_db, HistoryStorage(), ListeningPosting()
    publish = await published(db, storage, posting)
    w = work(db, storage, posting)
    assert await w.start(str(publish.id)) is None  # Not in the catalog yet.
    template = await registered(db, storage, listening.LISTEN_KEY)
    child = await w.start(str(publish.id))
    assert child["executor"] == listening.LISTEN_KEY
    assert await w.start(str(publish.id)) == child
    run = await db.get_run(UUID(child["run_id"]))
    assert run.status.value == "pending" and run.started_by_clerk_user_id == ACTOR
    assert run.input["publish_run_id"] == str(publish.id)
    # A failed publication has nothing to read.
    await db.pool.execute("UPDATE workflow_runs SET status='failed' WHERE id=$1", publish.id)
    assert await w.start(str(publish.id)) is None
    # Only Tin starts it, from a publication.
    with pytest.raises(WorkflowInputError, match="when it publishes the post"):
        await start_workflow_run(
            runtime=SimpleNamespace(database=db, storage=storage, integrations=SimpleNamespace()),
            settings=SimpleNamespace(task_queue="listening-test"),
            workflow=template,
            project_id=publish.project_id,
            started_by_clerk_user_id=ACTOR,
            start_idempotency_key=listening.listen_start_key(publish.id),
            input_payload={"publish_run_id": str(publish.id)},
        )


# The daily run.


def page_post(urn, published, text="Made on LinkedIn"):
    return {
        "urn": urn,
        "url": f"https://www.linkedin.com/feed/update/{urn}/",
        "published_at": published.isoformat(),
        "text": text,
    }


async def daily(db, storage, posting, project_id):
    run_id = str(await run_row(db, project_id, listening.RESULTS_KEY, {}))
    w = work(db, storage, posting)
    await w.results_prepare(run_id)
    await w.results_read(run_id)
    await w.results_publish(run_id)
    return run_id


@pytest.mark.asyncio
async def test_the_daily_run_reads_the_window_finds_page_posts_and_counts_followers(
    publication_db,
):
    db, storage, posting = publication_db, HistoryStorage(), ListeningPosting()
    # Tin published this post, but its listening run never started.
    publish = await published(db, storage, posting)
    posting.zernio.page = [
        page_post(URN, PUBLISHED),
        page_post(PAGE_URN, PUBLISHED - timedelta(days=1)),
        page_post("urn:li:share:7300000000000000008", datetime.now(UTC) - timedelta(days=40)),
    ]
    posting.zernio.analytics = {URN: [numbers(800, 12)], PAGE_URN: [numbers(300, 5)]}
    run_id = await daily(db, storage, posting, publish.project_id)
    row = await run_state(db, UUID(run_id))
    assert row["status"] == "succeeded" and row["artifact_path"] == listening.RESULTS_MD
    assert row["result_summary"] == (
        "Read 2 LinkedIn posts from the last 30 days. Found 1 new page post. "
        "Followers: Tin Computer 1,897, The founder 2,687."
    )
    readings = {
        (item["urn"], item["checkpoint"])
        for item in await db.pool.fetch("SELECT urn, checkpoint FROM linkedin_post_readings")
    }
    assert readings == {(URN, "d2"), (PAGE_URN, "d3")}
    document, text = files(storage)
    assert [post["urn"] for post in document["posts"]] == [URN, PAGE_URN]
    tin, made = document["posts"]
    assert tin["source"] == "tin" and tin["draft"] == {"path": DRAFT, "post_id": "p1"}
    assert made["source"] == "linkedin" and made["draft"] is None
    assert made["latest"]["checkpoint"] == "d3" and made["latest"]["impressions"] == 300
    assert [item["followers"] for item in document["accounts"]] == [1897, 2687]
    assert "- Tin Computer (page): 1,897" in text and "- The founder (profile): 2,687" in text
    assert "Posted on LinkedIn by the page" in text and "| 3 days (latest) | 300 |" in text

    # A second run the same day keeps that day's latest numbers.
    posting.zernio.analytics = {URN: [numbers(950, 14)], PAGE_URN: [None]}
    second = await daily(db, storage, posting, publish.project_id)
    assert (await run_state(db, UUID(second)))["result_summary"].startswith(
        "Read 1 LinkedIn post from the last 30 days. "
        "1 was removed from LinkedIn, so Tin stopped reading it."
    )
    metrics = await db.pool.fetchval(
        "SELECT metrics FROM linkedin_post_readings WHERE urn=$1 AND checkpoint='d2'", URN
    )
    assert json.loads(metrics)["impressions"] == 950
    document, _ = files(storage)
    assert document["posts"][1]["removed_at"] is not None


@pytest.mark.asyncio
async def test_the_daily_run_keeps_what_it_can_read_and_names_the_rest(publication_db):
    db, storage, posting = publication_db, HistoryStorage(), ListeningPosting()
    publish = await published(db, storage, posting, account=MEMBER)
    posting.zernio.page = IntegrationAuthorizationError("refused")
    posting.zernio.analytics = {URN: [numbers(50)]}
    run_id = await daily(db, storage, posting, publish.project_id)
    summary = (await run_state(db, UUID(run_id)))["result_summary"]
    assert summary.startswith("Read 1 LinkedIn post from the last 30 days.")
    assert summary.endswith("Reconnect LinkedIn posting to read Tin Computer.")
    # LinkedIn answering nothing at all is retried, then fails as temporary.
    posting.zernio.page = IntegrationUpstreamError("down")
    posting.zernio.analytics = {URN: [IntegrationUpstreamError("down")]}
    posting.zernio.followers = IntegrationUpstreamError("down")
    run_id = str(await run_row(db, publish.project_id, listening.RESULTS_KEY, {}))
    w = work(db, storage, posting)
    await w.results_prepare(run_id)
    with pytest.raises(IntegrationUpstreamError):
        await w.results_read(run_id)
    # Without the connection there is nothing to read; the founder reconnects.
    posting.connected = False
    run_id = str(await run_row(db, publish.project_id, listening.RESULTS_KEY, {}))
    with pytest.raises(IntegrationAuthorizationError):
        await w.results_prepare(run_id)


@pytest.mark.asyncio
async def test_results_stay_in_their_project_and_go_with_it(publication_db):
    db = publication_db
    projects = []
    for name in ("One", "Two"):
        project = await db.create_project(name=name, state_repo_id=f"p/{uuid4()}")
        await listening.track(
            db,
            project_id=project.id,
            post={
                "urn": URN,
                "account_id": PAGE["id"],
                "account_urn": PAGE["urn"],
                "account_kind": "organization",
                "account_name": name,
                "url": URL,
                "published_at": PUBLISHED,
                "excerpt": name,
            },
        )
        await listening.record_reading(
            db, project_id=project.id, urn=URN, checkpoint="1h", values=numbers(10)
        )
        await listening.record_followers(
            db, project_id=project.id, account_urn=PAGE["urn"], day=PUBLISHED.date(), followers=5
        )
        projects.append(project)
    record = await listening.snapshot(db, projects[0].id, now=datetime.now(UTC))
    assert [post["account_name"] for post in record["posts"]] == ["One"]
    async with db.pool.acquire() as conn:
        await db.purge_project_data(conn, project_id=projects[0].id)
        await db.purge_project_data(conn, project_id=projects[0].id)
    for table in ("linkedin_posts", "linkedin_post_readings", "linkedin_follower_counts"):
        rows = await db.pool.fetch(f"SELECT project_id FROM {table}")  # noqa: S608 — fixed names
        assert [row["project_id"] for row in rows] == [projects[1].id]


def test_the_daily_summary_says_what_happened():
    from tin_lite.linkedin_listening_activities import results_summary

    quiet = {"read": 0, "new": 0, "removed": 0, "skipped": 0, "unanswered": 0}
    assert results_summary(quiet) == "No LinkedIn posts from the last 30 days to read."
    followers = [{"name": "Tin Computer", "followers": 1897}]
    assert results_summary({**quiet, "removed": 1, "followers": followers}) == (
        "1 was removed from LinkedIn, so Tin stopped reading it. Followers: Tin Computer 1,897."
    )


def test_an_empty_record_says_what_comes_next():
    now = datetime(2026, 10, 14, 5, 7, tzinfo=UTC)
    documents = listening.render({"posts": [], "followers": []}, [], now=now)
    assert json.loads(documents[listening.RESULTS_JSON])["posts"] == []
    text = documents[listening.RESULTS_MD]
    assert text.startswith("# LinkedIn results\n\nUpdated 14 Oct 2026, 05:07 UTC.")
    assert "No posts yet." in text


# Zernio's reads, against a synthetic Zernio.


@pytest.mark.asyncio
async def test_post_analytics_reads_one_post_and_names_each_refusal():
    from tin_lite.zernio import ACCOUNT_GONE, ANALYTICS_REFUSED

    answers = []

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/accounts/{PAGE['id']}/linkedin-post-analytics"
        assert request.url.params["urn"] == URN
        return reply(*answers.pop(0))

    client, zernio = zernio_client(handler)
    async with client:
        answers.append(
            (
                200,
                {
                    "postUrn": URN,
                    "analytics": {
                        "impressions": 3600,
                        "reach": 1060,
                        "likes": 22,
                        "comments": 1,
                        "shares": -4,
                        "clicks": 121,
                        "engagementRate": 3.97,
                    },
                },
            )
        )
        assert await zernio.post_analytics(PAGE["id"], URN) == {
            "impressions": 3600,
            "reach": 1060,
            "reactions": 22,
            "comments": 1,
            "shares": 0,
            "clicks": 121,
            "saves": 0,
            "sends": 0,
            "engagement_rate": 3.97,
        }
        answers.append((404, {"code": "post_not_found"}))
        assert await zernio.post_analytics(PAGE["id"], URN) is None
        for status, body, error, text in (
            (404, {"error": "Account not found"}, IntegrationAuthorizationError, ACCOUNT_GONE),
            (403, {"error": "Missing scope"}, IntegrationAuthorizationError, ANALYTICS_REFUSED),
            (402, {"error": "Analytics add-on"}, IntegrationUpstreamError, "temporarily"),
            (500, {}, IntegrationUpstreamError, "temporarily"),
        ):
            answers.append((status, body))
            with pytest.raises(error, match=text[:20]):
                await zernio.post_analytics(PAGE["id"], URN)
        with pytest.raises(ValueError):
            await zernio.post_analytics(PAGE["id"], "urn:li:share:../1")
    assert answers == []


@pytest.mark.asyncio
async def test_page_posts_and_followers_keep_only_usable_answers():
    def handler(request):
        if request.url.path.endswith("/posts"):
            return reply(
                200,
                {
                    "posts": [
                        {
                            "id": URN,
                            "createdTime": "2026-10-12T18:58:58.510Z",
                            "permalink": f"https://www.linkedin.com/feed/update/{URN}",
                            "message": "Hello",
                        },
                        {"id": "not-a-urn", "createdTime": "2026-10-12T18:58:58Z"},
                        {"id": PAGE_URN, "createdTime": "yesterday"},
                        {
                            "id": PAGE_URN,
                            "createdTime": "2026-10-11T08:00:00Z",
                            "permalink": "https://evil.test/post",
                        },
                    ]
                },
            )
        assert request.url.path == "/api/v1/accounts/follower-stats"
        assert request.url.params["accountIds"] == f"{PAGE['id']},{MEMBER['id']}"
        return reply(
            200,
            {
                "accounts": [
                    {"_id": PAGE["id"], "currentFollowers": 1897},
                    {"_id": MEMBER["id"], "currentFollowers": "many"},
                    {"_id": "6ace4b8d89842e36a6692bff", "currentFollowers": 5},
                ]
            },
        )

    client, zernio = zernio_client(handler)
    async with client:
        posts = await zernio.page_posts(PAGE["id"])
        assert posts == [
            {
                "urn": URN,
                "url": f"https://www.linkedin.com/feed/update/{URN}",
                "published_at": "2026-10-12T18:58:58.510000+00:00",
                "text": "Hello",
            },
            {
                "urn": PAGE_URN,
                "url": None,
                "published_at": "2026-10-11T08:00:00+00:00",
                "text": "",
            },
        ]
        assert await zernio.follower_counts([PAGE["id"], MEMBER["id"]]) == {PAGE["id"]: 1897}


# The timetable, on a time-skipping Temporal.


def listen_stubs(published_at, calls, *, outcome=lambda checkpoint: "read"):
    @activity.defn(name="linkedin_listen_prepare")
    async def prepare(run_id):
        calls.append(("prepare", run_id))
        return {"published_at": published_at().isoformat()}

    @activity.defn(name="linkedin_listen_read")
    async def read(control):
        calls.append((control["checkpoint"], activity.info().scheduled_time))
        return outcome(control["checkpoint"])

    @activity.defn(name="linkedin_listen_finish")
    async def finish(run_id):
        calls.append(("finish", run_id))

    @activity.defn(name="linkedin_listen_failure")
    async def failure(argument):
        raise AssertionError("Listening must not fail")

    return [prepare, read, finish, failure]


@pytest.mark.asyncio
@pytest.mark.parametrize("removed", [False, True])
async def test_listening_reads_at_each_checkpoint_after_posting(removed):
    calls, posted = [], {}
    outcome = (lambda checkpoint: "removed" if checkpoint == "4h" else "read") if removed else None
    async with await WorkflowEnvironment.start_time_skipping() as env:
        posted["at"] = await env.get_current_time()
        queue, run_id = f"listen-{uuid4()}", str(uuid4())
        stubs = listen_stubs(
            lambda: posted["at"], calls, **({"outcome": outcome} if outcome else {})
        )
        async with Worker(
            env.client, task_queue=queue, workflows=[LinkedInListenWorkflow], activities=stubs
        ):
            handle = await env.client.start_workflow(
                LinkedInListenWorkflow.run,
                run_id,
                id=f"social.linkedin_listen:{run_id}",
                task_queue=queue,
            )
            await handle.result()
            history = await handle.fetch_history()
    await Replayer(workflows=[LinkedInListenWorkflow]).replay_workflow(history)
    readings = [(name, when) for name, when in calls if name not in {"prepare", "finish"}]
    expected = (
        [("1h", 1), ("4h", 4)] if removed else [("1h", 1), ("4h", 4), ("24h", 24), ("72h", 72)]
    )
    assert [name for name, _ in readings] == [name for name, _ in expected]
    for (_, when), (_, hours) in zip(readings, expected, strict=True):
        assert timedelta(hours=hours) <= when - posted["at"] < timedelta(hours=hours, minutes=1)
    assert calls[-1] == ("finish", run_id)
    # Only identifiers and small control facts enter history.
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            for payload in event.activity_task_scheduled_event_attributes.input.payloads:
                value = json.loads(payload.data)
                assert value == run_id or set(value) == {"run_id", "checkpoint"}


@workflow.defn(name="social.linkedin_publish")
class PublishBeforeListening:
    """social.linkedin_publish as it shipped before listening."""

    @workflow.run
    async def run(self, run_id: str) -> None:
        await workflow.execute_activity(
            "linkedin_publish_execute",
            run_id,
            start_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


def publish_stubs(start):
    @activity.defn(name="linkedin_publish_execute")
    async def execute(run_id):
        return None

    @activity.defn(name="linkedin_publish_failure")
    async def failure(argument):
        raise AssertionError("Publishing must not fail")

    @activity.defn(name="linkedin_listen_start")
    async def listen_start(run_id):
        return start(run_id)

    return [execute, failure, listen_start]


@pytest.mark.asyncio
async def test_publishing_starts_listening_and_older_publications_replay_unchanged():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue, run_id = f"publish-{uuid4()}", str(uuid4())
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[PublishBeforeListening],
            activities=publish_stubs(lambda run_id: None),
            # Defined here, among test imports Temporal's sandbox refuses.
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await env.client.start_workflow(
                "social.linkedin_publish", run_id, id=f"old:{run_id}", task_queue=queue
            )
            await handle.result()
            before = await handle.fetch_history()
        await Replayer(workflows=[LinkedInPublishWorkflow]).replay_workflow(before)

        calls, child_id, listen_id = [], str(uuid4()), f"social.linkedin_listen:{uuid4()}"
        answers = iter(
            [
                {
                    "run_id": child_id,
                    "executor": "social.linkedin_listen",
                    "temporal_workflow_id": listen_id,
                },
                None,
            ]
        )

        def start(run_id):
            answer = next(answers, "fail")
            if answer == "fail":
                raise RuntimeError("listening is unavailable")
            return answer

        posted = datetime.now(UTC) - timedelta(days=5)
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[LinkedInPublishWorkflow, LinkedInListenWorkflow],
            activities=[*publish_stubs(start), *listen_stubs(lambda: posted, calls)],
        ):
            for name in ("listens", "nothing", "fails"):
                handle = await env.client.start_workflow(
                    LinkedInPublishWorkflow.run, run_id, id=f"{name}:{run_id}", task_queue=queue
                )
                await handle.result()
                history = await handle.fetch_history()
                await Replayer(workflows=[LinkedInPublishWorkflow]).replay_workflow(history)
            await env.client.get_workflow_handle(listen_id).result()
    assert calls[0] == ("prepare", child_id) and calls[-1] == ("finish", child_id)
