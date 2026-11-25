"""Saving a member's LinkedIn home feed: the reads, the posts, their details and the files.

LinkedIn's answers copy the structure of its normalized Voyager JSON (entities by URN in
`included`, the page's order in `*elements`), as a real feed answered on 25 November 2026;
every name, post and number here is made up. The member's saved session is a real one from the
collection fixtures; the sandbox is synthetic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_connection_collection import ACTOR, USER, session_envelope
from test_connection_collection import collection_db as collection_db
from test_linkedin_engagement import OWNER, me, no_wait
from test_linkedin_engagers import fixture

from tin_lite import linkedin_feed as reader
from tin_lite import linkedin_feed_store as store
from tin_lite.connection_collection import CollectionError
from tin_lite.integrations import IntegrationAuthorizationError
from tin_lite.linkedin_feed_activities import LinkedInFeedActivities
from tin_lite.linkedin_session import validate_session
from tin_lite.workflows import LinkedInFeedWorkflow

NOW = datetime(2026, 11, 25, 15, tzinfo=UTC)
# LinkedIn's pagination cursor for the next feed page.
CURSOR = "page-2"


def person(index: int) -> str:
    return f"ACoAAFeed{index:04d}" + "x" * 23


def ident(when: datetime, n: int = 0) -> int:
    return (int(when.timestamp() * 1000) << 22) + n


def text_view(value: str, **extra) -> dict:
    return {"text": value, "$type": "com.linkedin.voyager.common.TextViewModel", **extra}


def actor(*, name, headline, member=None, company=None, degree="2nd") -> dict:
    if member is not None:
        attribute = {"*miniProfile": f"urn:li:fs_miniProfile:{member}", "type": "PROFILE_FULLNAME"}
        urn, url = "urn:li:member:12345", f"https://www.linkedin.com/in/{name.lower()}?mini=1"
    else:
        attribute = {"*miniCompany": f"urn:li:fs_miniCompany:{company}", "type": "COMPANY_NAME"}
        urn, url = f"urn:li:company:{company}", f"https://www.linkedin.com/company/{company}/posts"
    return {
        "urn": urn,
        "name": text_view(name, attributes=[attribute]),
        "description": text_view(headline),
        "subDescription": text_view("2h • "),
        "supplementaryActorInfo": text_view(f" • {degree}") if member else None,
        "navigationContext": {"actionTarget": url},
        "$type": "com.linkedin.voyager.feed.render.ActorComponent",
    }


def update(
    n: int,
    *,
    thread: str,
    shown: str | None = None,
    who: dict,
    words="A post",
    reason=None,
    likes=12,
    comments=3,
    image=False,
    promoted=False,
):
    """One feed update, its social detail and its counts. `shown` is the update's own activity
    when it differs from the post's (someone liked a post, a ugcPost's activity)."""

    shown = shown or (f"urn:li:{thread}" if thread.startswith("activity:") else None)
    ref = f"urn:li:fs_updateV2:({shown or thread},MAIN_FEED,EMPTY,DEFAULT,false)"
    detail = f"urn:li:fs_socialDetail:urn:li:{thread}"
    counts = f"urn:li:fs_socialActivityCounts:urn:li:{thread}"
    content = None
    if image:
        content = {
            "images": [
                {
                    "attributes": [
                        {
                            "vectorImage": {
                                "rootUrl": "https://media.licdn.com/dms/image/v2/X/feedshare-",
                                "artifacts": [
                                    {"width": 20, "fileIdentifyingUrlPathSegment": "small"},
                                    {"width": 1200, "fileIdentifyingUrlPathSegment": "large"},
                                ],
                            }
                        }
                    ]
                }
            ],
            "$type": "com.linkedin.voyager.feed.render.ImageComponent",
        }
    who = dict(who)
    if promoted:
        who["subDescription"] = text_view("Promoted")
    entities = [
        {
            "entityUrn": ref,
            "$type": "com.linkedin.voyager.feed.render.UpdateV2",
            "updateMetadata": {
                "urn": shown or f"urn:li:activity:{n}",
                "shareUrn": "urn:li:share:1",
                "trackingData": {"requestId": "r", "trackingId": "t"},
            },
            "actor": who,
            "header": {"text": text_view(reason)} if reason else None,
            "commentary": {"text": text_view(words)},
            "content": content,
            "socialContent": {"shareUrl": f"https://www.linkedin.com/posts/x-{n}?utm_source=share"},
            "*socialDetail": detail,
        },
        {
            "entityUrn": detail,
            "threadId": thread,
            "urn": f"urn:li:{thread}",
            "*totalSocialActivityCounts": counts,
            "$type": "com.linkedin.voyager.feed.SocialDetail",
        },
        {
            "entityUrn": counts,
            "numLikes": likes,
            "numComments": comments,
            "numShares": 1,
            "reactionTypeCounts": [
                {"reactionType": "LIKE", "count": likes - 2},
                {"reactionType": "PRAISE", "count": 2},
            ],
        },
    ]
    return ref, entities


def feed(updates, *, cursor=CURSOR, total=600) -> dict:
    data = {
        "metadata": {"paginationToken": cursor, "sort": "RELEVANCE"},
        "paging": {"count": 10, "start": 0, "total": total},
        "*elements": [ref for ref, _ in updates],
    }
    return {"data": data, "included": [e for _, entities in updates for e in entities]}


ADA = actor(name="Ada", headline="Founder at Acme", member=person(1), degree="1st")
ACME = actor(name="Acme", headline="2,000 followers", company=4242)


def sample_page(start=0):
    when = NOW - timedelta(hours=3)
    return feed(
        [
            update(
                1,
                thread=f"activity:{ident(when, start + 1)}",
                who=ADA,
                image=True,
                words="Line one\n\nLine two",
            ),
            # Bo liked Ada's post: the update's own activity is the like, the post is the thread.
            update(
                2,
                thread=f"activity:{ident(when, start + 2)}",
                shown=f"urn:li:activity:{ident(NOW, start + 2)}",
                who=ADA,
                reason="Bo Builder likes this",
            ),
            # A company's ugcPost, shown through its own activity.
            update(
                3,
                thread=f"ugcPost:{ident(when - timedelta(days=4), start + 3)}",
                shown=f"urn:li:activity:{ident(when, start + 3)}",
                who=ACME,
                reason="Suggested",
                comments=0,
            ),
            update(4, thread=f"activity:{ident(when, start + 4)}", who=ACME, promoted=True),
        ]
    )


# The reader, on LinkedIn's answers.


def test_a_feed_page_names_each_post_by_its_thread():
    page = sample_page()
    page["data"]["*elements"].append("urn:li:fs_updateV2:(urn:li:activity:9,MAIN_FEED)")
    posts, token, seen, skipped = reader.feed_page(page, start=20)
    assert token == CURSOR and seen == 5 and skipped == 1
    assert [p["position"] for p in posts] == [20, 21, 22, 23]
    first, liked, company, promoted = posts
    assert first["author"] == {
        "kind": "person",
        "id": person(1),
        "name": "Ada",
        "headline": "Founder at Acme",
        "url": "https://www.linkedin.com/in/ada",
        "degree": "1st",
    }
    assert first["text"] == "Line one\n\nLine two"
    assert first["media"]["kind"] == "image"
    assert first["media"]["images"] == ["https://media.licdn.com/dms/image/v2/X/feedshare-large"]
    assert first["counts"] == {
        "reactions": 12,
        "comments": 3,
        "reposts": 1,
        "by_reaction": {"LIKE": 10, "PRAISE": 2},
    }
    assert first["share_url"] == "https://www.linkedin.com/posts/x-1"
    # A liked post is the post, dated by its own activity, not the like.
    assert liked["reason"] == "Bo Builder likes this"
    assert liked["activity_urn"] == "urn:li:" + liked["thread"]
    assert liked["published_at"] == (NOW - timedelta(hours=3)).isoformat()
    # A ugcPost is dated and linked by the activity that showed it.
    assert company["thread"].startswith("ugcPost:") and company["author"]["kind"] == "company"
    assert company["author"]["id"] == "4242" and company["author"]["degree"] is None
    assert company["url"] == f"https://www.linkedin.com/feed/update/{company['activity_urn']}/"
    assert company["published_at"] == (NOW - timedelta(hours=3)).isoformat()
    assert promoted["promoted"] and not first["promoted"]


def test_a_feed_page_tin_cant_read_is_refused_not_guessed():
    with pytest.raises(CollectionError) as error:
        reader.feed_page({"data": {"paging": {}}, "included": []}, start=0)
    assert error.value.code == "unsupported_contract"


def comment(n: int, thread: str, *, member=None, words="Nice one") -> list[dict]:
    urn = f"urn:li:comment:({thread},{7000 + n})"
    ref = f"urn:li:fs_objectComment:({7000 + n},{thread})"
    detail, counts = f"urn:li:fs_socialDetail:{urn}", f"urn:li:fs_socialActivityCounts:{urn}"
    return [
        {
            "entityUrn": ref,
            "urn": urn,
            "$type": "com.linkedin.voyager.feed.Comment",
            "commenterForDashConversion": {
                "commenterProfileId": member or person(10 + n),
                "title": text_view(f"Commenter {n}"),
                "subtitle": "Head of Growth",
                "supplementaryActorInfoV2": text_view(" • 3rd+"),
                "navigationUrl": f"https://www.linkedin.com/in/c{n}",
                "actorUnion": {"profileUrn": f"urn:li:fsd_profile:{member or person(10 + n)}"},
            },
            "commentV2": text_view(words),
            "createdTime": int(NOW.timestamp() * 1000),
            "pinned": False,
            "edited": n == 2,
            "*socialDetail": detail,
        },
        {"entityUrn": detail, "*totalSocialActivityCounts": counts},
        {"entityUrn": counts, "numLikes": n, "numComments": 0},
    ]


def comments(thread: str, n: int, total: int | None = None) -> dict:
    entities = [comment(i, thread) for i in range(1, n + 1)]
    return {
        "data": {
            "paging": {"count": 10, "start": 0, "total": total if total is not None else n},
            "*elements": [e[0]["entityUrn"] for e in entities],
        },
        "included": [x for e in entities for x in e],
    }


def test_a_posts_comments_keep_who_said_what():
    found, total = reader.comments_page(comments("activity:7531336088905240576", 2, 20))
    assert total == 20 and len(found) == 2
    assert found[1] == {
        "id": "7002",
        "author": {
            "kind": "person",
            "id": person(12),
            "name": "Commenter 2",
            "headline": "Head of Growth",
            "url": "https://www.linkedin.com/in/c2",
            "degree": "3rd",
        },
        "text": "Nice one",
        "created_at": NOW.isoformat(),
        "reactions": 2,
        "replies": 0,
        "pinned": False,
        "edited": True,
    }


def reactions(start: int, total: int, n: int = 10) -> dict:
    refs, included = [], []
    for i in range(start, start + n):
        ref = f"urn:li:fsd_reaction:({i})"
        refs.append(ref)
        included.append(
            {
                "entityUrn": ref,
                "actorUrn": f"urn:li:fsd_profile:{person(100 + i)}",
                "reactionType": "LIKE",
                "reactorLockup": {
                    "title": {"text": f"Reactor {i}"},
                    "subtitle": {"text": "Engineer"},
                    "label": {"text": "· 2nd"},
                },
            }
        )
    return {
        "data": {"paging": {"start": start, "count": 10, "total": total}, "*elements": refs},
        "included": included,
    }


def responder(seen: list, *, pages=None, failing=None):
    pages = pages or {}

    def respond(req):
        seen.append(req)
        assert req.method == "GET"
        if req.url.path == "/voyager/api/me":
            return httpx.Response(200, json=me())
        if failing and req.url.path == failing[0]:
            return httpx.Response(failing[1])
        if req.url.path == reader.FEED_PATH:
            assert req.url.params["q"] == "feed" and req.url.params["count"] == "10"
            assert req.headers["referer"] == "https://www.linkedin.com/feed/"
            return httpx.Response(200, json=pages[int(req.url.params["start"])])
        if req.url.path == reader.COMMENTS_PATH:
            thread = req.url.params["updateId"]
            assert req.url.params["q"] == "comments"
            return httpx.Response(200, json=comments(thread, 3, 7))
        assert req.url.path == reader.REACTIONS_PATH
        assert req.url.params["threadUrn"].startswith("urn:li:")
        start = int(req.url.params["start"])
        return httpx.Response(200, json=reactions(start, 25, min(10, 25 - start)))

    return respond


async def test_a_feed_read_carries_on_with_linkedins_token():
    seen = []
    pages = {0: sample_page(0), 10: sample_page(10), 20: sample_page(20)}
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(seen, pages=pages))) as c:
        result = await reader.read_feed(
            validate_session(session_envelope()), ACTOR, client=c, pages=2, sleep=no_wait, now=NOW
        )
    assert result["pages"] == 2 and result["next_start"] == 20 and result["token"] == CURSOR
    assert len(result["posts"]) == 8
    feed_requests = [r for r in seen if r.url.path == reader.FEED_PATH]
    assert "paginationToken" not in feed_requests[0].url.params
    assert feed_requests[1].url.params["paginationToken"] == CURSOR
    reader.FeedResult.model_validate(result)


async def test_the_feed_ends_when_linkedin_has_nothing_more():
    seen = []
    pages = {0: sample_page(0), 10: feed([], cursor=None)}
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(seen, pages=pages))) as c:
        result = await reader.read_feed(
            validate_session(session_envelope()), ACTOR, client=c, pages=3, sleep=no_wait
        )
    assert result["pages"] == 2 and result["next_start"] is None and result["token"] is None


async def test_details_read_first_comments_and_first_reactors():
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(seen))) as c:
        result = await reader.read_details(
            validate_session(session_envelope()),
            ACTOR,
            client=c,
            posts=[
                {"thread": "activity:7531336088905240576", "comments": True, "reactor_pages": 2},
                {"thread": "ugcPost:7531297777272778753", "comments": False, "reactor_pages": 1},
            ],
            sleep=no_wait,
        )
    first, second = result["posts"]
    assert len(first["comments"]) == 3 and first["comments_total"] == 7
    assert len(first["reactors"]) == 20 and first["reactors_total"] == 25
    assert second["comments"] is None and len(second["reactors"]) == 10
    threads = [r.url.params.get("threadUrn") for r in seen if r.url.path == reader.REACTIONS_PATH]
    assert threads == ["urn:li:activity:7531336088905240576"] * 2 + [
        "urn:li:ugcPost:7531297777272778753"
    ]
    # One /me check for the whole read.
    assert sum(r.url.path == "/voyager/api/me" for r in seen) == 1
    reader.DetailsResult.model_validate(result)


async def test_a_details_read_past_its_requests_is_refused():
    posts = [{"thread": "activity:7531336088905240576", "comments": True, "reactor_pages": 10}] * 3
    with pytest.raises(CollectionError):
        await reader.read_details({}, ACTOR, client=None, posts=posts, sleep=no_wait)


@pytest.mark.parametrize(
    ("status", "code"), [(401, "session_expired"), (429, "rate_limited"), (999, "access_denied")]
)
async def test_linkedin_refusals_stop_the_read(status, code):
    seen = []
    handler = responder(seen, failing=(reader.FEED_PATH, status))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(CollectionError) as error:
            await reader.read_feed(
                validate_session(session_envelope()), ACTOR, client=c, sleep=no_wait
            )
    assert error.value.code == code


# The workflow's activities, with a synthetic sandbox.


class Cloud:
    """The LinkedIn sandbox: the member's feed, a page at a time, and posts' details."""

    def __init__(self, pages, *, errors=None, fail_once=None):
        self.pages, self.errors = pages, errors or {}
        # A kind whose first read fails with this reason, then reads normally.
        self.fail_once = dict(fail_once or {})
        self.calls, self.cleaned = [], []

    async def engagement(self, conn, *, execution_key, run_id, session, packet, seconds=270):
        assert session["cookies"] and packet["actor"]["key"]
        self.calls.append(packet)
        if packet["kind"] in self.errors:
            raise CollectionError(self.errors[packet["kind"]])
        if packet["kind"] in self.fail_once:
            raise CollectionError(self.fail_once.pop(packet["kind"]))
        if packet["kind"] == "linkedin_feed_details":
            return {
                "posts": [
                    {
                        "thread": p["thread"],
                        "comments": [] if p["comments"] else None,
                        "comments_total": 3 if p["comments"] else None,
                        "reactors": [
                            {
                                "member": person(200),
                                "name": "Reactor",
                                "headline": "Engineer",
                                "degree": "2nd",
                                "reaction": "LIKE",
                            }
                        ]
                        if p["reactor_pages"]
                        else None,
                        "reactors_total": 12 if p["reactor_pages"] else None,
                    }
                    for p in packet["posts"]
                ],
                "read_at": NOW.isoformat(),
            }
        assert packet["kind"] == "linkedin_feed"
        posts, start, read = [], packet["start"], 0
        for _ in range(packet["pages"]):
            if start not in self.pages:
                break
            found, _token, _seen, _skipped = reader.feed_page(self.pages[start], start=start)
            posts += found
            start += 10
            read += 1
        more = start in self.pages
        return {
            "posts": posts,
            "pages": read,
            "skipped": 0,
            "next_start": start if more else None,
            "token": "tok" if more else None,
            "read_at": NOW.isoformat(),
        }

    async def cleanup(self, sandbox_id):
        self.cleaned.append(sandbox_id)


async def feed_run(f, *, by=None, **options):
    workflow_id = await f.db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", store.KEY)
    if workflow_id is None:
        workflow_id = uuid4()
        await f.db.pool.execute(
            """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                    definition_path,current_commit_sha,version_label,definition)
               VALUES ($1,$2,$2,$2,'registry/workflows','f.json',$3,'1','{}')""",
            workflow_id,
            store.KEY,
            "f" * 40,
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
        store.KEY,
        "f" * 40,
        f"{store.KEY}:{run_id}",
        str(run_id),
        by or USER,
        json.dumps({"project_id": str(f.project.id), **options}),
    )
    return str(run_id)


def work(f, cloud):
    return LinkedInFeedActivities(
        database=f.db,
        storage=f.storage,
        settings=f.store.settings,
        cipher=f.cipher,
        cloud=cloud,
        router=None,
    )


async def run_through(w, run_id):
    """What the Temporal workflow does, called directly."""

    await w.feed_prepare(run_id)
    notes = {"posts": 0, "new": 0, "skipped": 0, "detailed": 0, "reads": 0, "detail_reads": 0}
    for index in range(store.FEED_READS):
        read = await w.feed_read({"run_id": run_id, "read": index})
        notes["reads"] += read["outcome"] == "read" and bool(read["posts"])
        notes["posts"] += read["posts"] or 0
        notes["new"] += read["new"] or 0
        if read["outcome"] == "stopped":
            notes["stopped"] = read["reason"]
            break
        if read["outcome"] != "read" or not read["more"]:
            break
    if not notes.get("stopped"):
        for index in range(store.DETAIL_READS):
            read = await w.feed_details({"run_id": run_id, "read": index})
            notes["detail_reads"] += bool(read["detailed"])
            notes["detailed"] += read["detailed"] or 0
            notes["failed"] = notes.get("failed", 0) + (read.get("failed") or 0)
            if read["outcome"] == "stopped":
                notes["stopped"] = read["reason"]
                break
            if read["outcome"] != "read" or not read["more"]:
                break
    await w.feed_publish({"run_id": run_id, "notes": notes})
    return notes


def files(storage):
    tree = storage.repo.trees[storage.repo.head]
    return {path: body for path, (_mode, body) in tree.items() if path.startswith(store.FOLDER)}


def pages_of(n: int) -> dict:
    return {i * 10: sample_page(i * 10) for i in range(n)}


async def test_a_run_saves_the_feed_with_new_posts_details(collection_db):
    f = await fixture(collection_db)
    cloud = Cloud(pages_of(5))
    run_id = await feed_run(f, pages=4, reactors=15)
    notes = await run_through(work(f, cloud), run_id)
    # Four pages over two reads (three, then one), four posts a page.
    feed_reads = [c for c in cloud.calls if c["kind"] == "linkedin_feed"]
    assert [(c["start"], c["pages"], c["token"]) for c in feed_reads] == [
        (0, 3, None),
        (30, 1, "tok"),
    ]
    assert notes["posts"] == 16 and notes["new"] == 16
    # The promoted posts aren't read further. The rest are: 15 reactors of a post's 12 take
    # two pages, and a post without comments isn't asked for them.
    reads = [c["posts"] for c in cloud.calls if c["kind"] == "linkedin_feed_details"]
    details = [p for posts in reads for p in posts]
    assert len(details) == 12 and notes["detailed"] == 12
    assert all(p["reactor_pages"] == 2 for p in details)
    company = [p for p in details if p["thread"].startswith("ugcPost:")]
    assert len(company) == 4 and not any(p["comments"] for p in company)
    assert all(sum(reader.cost(p) for p in posts) <= reader.DETAIL_REQUESTS for posts in reads)
    written = files(f.storage)
    (json_path,) = [p for p in written if p.endswith(".json")]
    assert json_path.startswith(f"{store.FOLDER}/") and run_id[:8] in json_path
    document = json.loads(written[json_path])
    assert document["schema"] == store.SCHEMA and len(document["posts"]) == 16
    first = document["posts"][0]
    assert first["comments"] == [] and first["comments_total"] == 3
    assert first["reactors"][0]["name"] == "Reactor" and first["reactors_total"] == 12
    digest = written[json_path.replace(".json", ".md")].decode()
    assert "16 posts read from your LinkedIn feed; comments and reactors read for 12." in digest
    assert "Bo Builder likes this" in digest and "> Line one" in digest
    run = await f.db.get_run(run_id)
    assert run.status.value == "succeeded"


async def test_a_post_seen_again_keeps_its_details_and_isnt_read_twice(collection_db):
    f = await fixture(collection_db)
    first = Cloud(pages_of(1))
    await run_through(work(f, first), await feed_run(f, pages=1))
    again = Cloud(pages_of(1))
    notes = await run_through(work(f, again), await feed_run(f, pages=1))
    assert notes["posts"] == 4 and notes["new"] == 0 and notes["detailed"] == 0
    assert [c["kind"] for c in again.calls] == ["linkedin_feed"]
    async with f.db.pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM linkedin_feed_sightings")
        assert len(rows) == 8
        kept = await conn.fetchval(
            "SELECT count(*) FROM linkedin_feed_posts WHERE reactors IS NOT NULL"
        )
        assert kept == 3


async def test_reactors_zero_keeps_only_comments(collection_db):
    f = await fixture(collection_db)
    cloud = Cloud(pages_of(1))
    await run_through(work(f, cloud), await feed_run(f, pages=1, reactors=0))
    details = [p for c in cloud.calls if c["kind"] == "linkedin_feed_details" for p in c["posts"]]
    assert all(p["reactor_pages"] == 0 and p["comments"] for p in details) and len(details) == 2


async def test_a_signed_out_session_stops_the_first_read_with_what_to_do(collection_db):
    f = await fixture(collection_db)
    w = work(f, Cloud(pages_of(1), errors={"linkedin_feed": "session_expired"}))
    run_id = await feed_run(f)
    await w.feed_prepare(run_id)
    with pytest.raises(IntegrationAuthorizationError, match="saved LinkedIn session has expired"):
        await w.feed_read({"run_id": run_id, "read": 0})


async def test_linkedin_objecting_to_details_keeps_the_posts(collection_db):
    f = await fixture(collection_db)
    cloud = Cloud(pages_of(1), errors={"linkedin_feed_details": "rate_limited"})
    run_id = await feed_run(f, pages=1)
    notes = await run_through(work(f, cloud), run_id)
    assert notes["stopped"] == "rate_limited" and notes["posts"] == 4
    written = files(f.storage)
    digest = next(body for path, body in written.items() if path.endswith(".md")).decode()
    assert "LinkedIn asked Tin to slow down" in digest
    async with f.db.pool.acquire() as conn:
        # Unread details wait for a later run.
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM linkedin_feed_posts WHERE details_read_at IS NULL"
            )
            == 3
        )


async def test_a_details_read_that_fails_skips_its_posts_and_goes_on(collection_db):
    f = await fixture(collection_db)
    cloud = Cloud(pages_of(6), fail_once={"linkedin_feed_details": "cloud_failed"})
    run_id = await feed_run(f, pages=6)
    w = work(f, cloud)
    notes = await run_through(w, run_id)
    reads = [c["posts"] for c in cloud.calls if c["kind"] == "linkedin_feed_details"]
    # Eighteen posts to read, thirty requests: two reads. The first read's posts are skipped,
    # the rest read by the next one.
    assert len(reads) == 2 and len(reads[0]) + len(reads[1]) == 18
    assert notes["detailed"] == len(reads[1]) and notes["failed"] == len(reads[0])
    digest = next(b for p, b in files(f.storage).items() if p.endswith(".md")).decode()
    assert "comments and reactors read for" in digest
    async with f.db.pool.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT count(*) FROM linkedin_feed_posts WHERE details_read_at IS NULL"
        )


async def test_a_retried_failed_read_settles_the_same_posts(collection_db):
    f = await fixture(collection_db)
    w = work(f, Cloud(pages_of(6), fail_once={"linkedin_feed_details": "cloud_failed"}))
    run_id = await feed_run(f, pages=6)
    await w.feed_prepare(run_id)
    for index in range(2):
        await w.feed_read({"run_id": run_id, "read": index})

    async def waiting():
        return await f.db.pool.fetchval(
            "SELECT count(*) FROM linkedin_feed_posts WHERE details_read_at IS NULL"
        )

    first = await w.feed_details({"run_id": run_id, "read": 0})
    left = await waiting()
    again = await w.feed_details({"run_id": run_id, "read": 0})
    assert first["failed"] == again["failed"] == 14 and first["more"] and left == 6
    assert await waiting() == left


async def test_a_later_run_reads_details_an_earlier_one_left(collection_db):
    f = await fixture(collection_db)
    first = Cloud(pages_of(1), errors={"linkedin_feed_details": "rate_limited"})
    await run_through(work(f, first), await feed_run(f, pages=1))
    # The next day's feed shows other posts; yesterday's still get their details.
    later = Cloud({0: sample_page(10)})
    notes = await run_through(work(f, later), await feed_run(f, pages=1))
    threads = [
        p["thread"] for c in later.calls if c["kind"] == "linkedin_feed_details" for p in c["posts"]
    ]
    assert len(threads) == 6 and notes["detailed"] == 6
    async with f.db.pool.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT count(*) FROM linkedin_feed_posts WHERE details_read_at IS NULL"
        )


async def test_one_posts_server_error_skips_only_that_post():
    seen = []
    handler = responder(seen)

    def respond(req):
        if req.url.params.get("updateId") == "activity:7531336088905240576":
            return httpx.Response(500)
        return handler(req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
        result = await reader.read_details(
            validate_session(session_envelope()),
            ACTOR,
            client=c,
            posts=[
                {"thread": "activity:7531336088905240576", "comments": True, "reactor_pages": 1},
                {"thread": "ugcPost:7531297777272778753", "comments": True, "reactor_pages": 1},
            ],
            sleep=no_wait,
        )
    broken, fine = result["posts"]
    assert broken["unread"] == "cloud_failed" and broken["comments"] is None
    assert fine["unread"] is None and len(fine["comments"]) == 3 and len(fine["reactors"]) == 10


async def test_linkedin_objecting_on_one_post_still_stops_the_read():
    seen = []
    handler = responder(seen)

    def respond(req):
        if req.url.path == reader.REACTIONS_PATH:
            return httpx.Response(429)
        return handler(req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
        with pytest.raises(CollectionError) as error:
            await reader.read_details(
                validate_session(session_envelope()),
                ACTOR,
                client=c,
                posts=[
                    {
                        "thread": "activity:7531336088905240576",
                        "comments": False,
                        "reactor_pages": 1,
                    }
                ],
                sleep=no_wait,
            )
    assert error.value.code == "rate_limited"


def test_options_stay_inside_their_bounds():
    assert store.options({}) == (store.PAGES_DEFAULT, store.REACTORS_DEFAULT)
    assert store.options({"pages": 99, "reactors": -1}) == (store.PAGES_MAX, 0)
    assert store.options({"pages": "x", "reactors": 250}) == (10, store.REACTORS_MAX)


# The Temporal workflow.


def stubs(calls, reads, details):
    @activity.defn(name="linkedin_feed_prepare")
    async def prepare(run_id):
        calls.append("prepare")

    @activity.defn(name="linkedin_feed_read")
    async def read(control):
        calls.append(("read", control["read"]))
        return reads[control["read"]]

    @activity.defn(name="linkedin_feed_details")
    async def detail(control):
        calls.append(("details", control["read"]))
        return details[control["read"]]

    @activity.defn(name="linkedin_feed_publish")
    async def publish(control):
        calls.append(("publish", control["notes"]))

    @activity.defn(name="linkedin_feed_failure")
    async def failure(argument):
        raise AssertionError("The run must not fail")

    return [prepare, read, detail, publish, failure]


def page_read(*, more=True, kind="read", reason=None, posts=30):
    return {
        "outcome": kind,
        "reason": reason,
        "posts": posts,
        "new": posts,
        "skipped": 0,
        "pages": 3,
        "detailed": None,
        "more": more,
    }


def detail_read(*, more=True, kind="read", reason=None):
    return {"outcome": kind, "reason": reason, "detailed": 10, "more": more}


@pytest.mark.parametrize(
    ("reads", "details", "expected"),
    [
        (
            [page_read(), page_read(more=False)],
            [detail_read(), detail_read(more=False)],
            ([0, 1], [0, 1]),
        ),
        # LinkedIn objecting to the feed stops all reading; what was read is written.
        ([page_read(), page_read(kind="stopped", reason="challenge", posts=0)], [], ([0, 1], [])),
        ([page_read(more=False)], [detail_read(kind="stopped", reason="rate_limited")], ([0], [0])),
    ],
)
async def test_the_workflow_reads_the_feed_then_details_and_keeps_only_counts(
    reads, details, expected
):
    calls = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue, run_id = f"feed-{uuid4()}", str(uuid4())
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[LinkedInFeedWorkflow],
            activities=stubs(calls, reads, details),
        ):
            handle = await env.client.start_workflow(
                LinkedInFeedWorkflow.run, run_id, id=f"{store.KEY}:{run_id}", task_queue=queue
            )
            await handle.result()
            history = await handle.fetch_history()
    await Replayer(workflows=[LinkedInFeedWorkflow]).replay_workflow(history)
    steps = [c for c in calls if isinstance(c, tuple)]
    assert [n for kind, n in steps if kind == "read"] == expected[0]
    assert [n for kind, n in steps if kind == "details"] == expected[1]
    notes = next(n for kind, n in steps if kind == "publish")
    assert notes["posts"] == sum(reads[i]["posts"] for i in expected[0])
    assert notes.get("stopped") == next(
        (r["reason"] for r in [*reads, *details] if r["outcome"] == "stopped"), None
    )


def test_the_workflow_reads_as_many_times_as_the_module_says():
    assert LinkedInFeedWorkflow.feed_reads == store.FEED_READS
    assert LinkedInFeedWorkflow.detail_reads == store.DETAIL_READS
    assert OWNER  # the reactions parser leaves the member's own reaction out
