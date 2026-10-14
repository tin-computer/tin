"""LinkedIn publishing: exact drafts, preview and confirm, and one approved post through a
synthetic Zernio, with real SQL receipts, admission and projection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from test_private_workflows import ACTOR, app, mcp
from test_private_workflows import fixture as project_fixture
from test_procedure_publication import HistoryStorage
from test_procedure_publication import publication_db as publication_db

from tin_lite import linkedin_publish_activities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.integrations import (
    IntegrationAuthorizationError,
    IntegrationDeliveryRefusedError,
    IntegrationDeliveryUnknownError,
    IntegrationUpstreamError,
)
from tin_lite.linkedin_posts import KEY, save_effect, validate_draft
from tin_lite.linkedin_publish_activities import LinkedInPublishActivities
from tin_lite.project_files import ProjectFileService
from tin_lite.project_media import validate_media
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_inputs import WorkflowInputError

IMAGE = (Path(__file__).parent / "fixtures" / "x_media" / "image.png").read_bytes()
PROFILE = "6ace4b5603164f43d47ce30b"
PAGE = {
    "id": "6ace4b8d89842e36a6692bc9",
    "urn": "urn:li:organization:108774412",
    "kind": "organization",
    "name": "Tin Computer",
    "active": True,
}
MEMBER = {
    "id": "6ace4b8c89842e36a6692bc8",
    "urn": "urn:li:person:AbC123",
    "kind": "personal",
    "name": "The founder",
    "active": True,
}
POST_ID = "6ace4bff89842e36a6692bd0"
TEAMMATE = "user_linkedinteammate"
URL = "https://www.linkedin.com/feed/update/urn:li:share:7300000000000000000"
DRAFT_PATH = "social/linkedin/drafts/2026-10-13-launch.json"


def draft(*, text="Exact approved text", target=PAGE["urn"], attachments=None):
    return {
        "schema_version": "tin.social.linkedin_draft.v1",
        "posts": [
            {
                "id": "p1",
                "text": text,
                "target": target,
                "readiness": "ready",
                "support": [],
                "editor_notes": "Private note, never publish.",
                "attachments": attachments or [],
                "missing_assets": [],
            }
        ],
    }


class SyntheticZernio:
    def __init__(self):
        self.accounts_list = [PAGE, MEMBER]
        self.answers: list[str] = []
        self.posts: list[dict] = []
        self.uploads: list[str] = []
        self.reads = 0
        self.settles_as = "published"

    async def accounts(self, profile_id):
        assert profile_id == PROFILE
        return self.accounts_list

    async def upload_media(self, *, filename, media_type, content):
        assert media_type == "image/png" and content == IMAGE
        self.uploads.append(filename)
        return f"https://media.zernio.com/{len(self.uploads)}.png"

    async def create_post(self, *, account_id, text, media, idempotency_key):
        self.posts.append(
            {"account_id": account_id, "text": text, "media": media, "key": idempotency_key}
        )
        answer = self.answers.pop(0) if self.answers else "published"
        if answer == "lost":
            raise IntegrationDeliveryUnknownError("Synthetic lost response")
        if answer == "refused":
            raise IntegrationDeliveryRefusedError("LinkedIn refused the post.", status=422)
        return {
            "post_id": POST_ID,
            "status": answer,
            "url": URL if answer == "published" else None,
            "error_category": "user_content" if answer == "failed" else None,
        }

    async def get_post(self, post_id):
        assert post_id == POST_ID
        self.reads += 1
        published = self.settles_as == "published"
        return {
            "post_id": post_id,
            "status": self.settles_as,
            "url": URL if published else None,
            "error_category": None if published else "user_content",
        }


class SyntheticPosting:
    def __init__(self, accounts=(PAGE, MEMBER)):
        self.id = uuid4()
        self.connected = True
        self.accounts = list(accounts)
        # Without recorded owners, the member who connected owns its profiles.
        self.owners = None
        self.zernio = SyntheticZernio()

    async def connection(self, project_id):
        assert project_id is not None
        if not self.connected:
            raise IntegrationAuthorizationError("Connect or reconnect LinkedIn posting")
        configuration = {"zernio_profile_id": PROFILE, "accounts": self.accounts}
        if self.owners is not None:
            configuration["owners"] = self.owners
        return SimpleNamespace(
            id=self.id,
            status="connected",
            external_account_id=PROFILE,
            connected_by_clerk_user_id=ACTOR,
            configuration=configuration,
        )


def test_drafts_keep_linkedin_limits():
    assert validate_draft(draft())["posts"][0]["target"] == PAGE["urn"]
    # LinkedIn counts UTF-16 units: an emoji takes two of the 3,000.
    validate_draft(draft(text="a" * 3000))
    with pytest.raises(ValueError, match="3,000"):
        validate_draft(draft(text="a" * 2999 + "🙂"))
    with pytest.raises(ValueError, match="connected LinkedIn account"):
        validate_draft(draft(target="urn:li:organization:../1"))
    many = [{"type": "image", "path": f"media/{n}.png", "alt_text": ""} for n in range(21)]
    with pytest.raises(ValueError):
        validate_draft(draft(attachments=many))
    with pytest.raises(ValueError):
        validate_draft(draft(attachments=[{"type": "video", "path": "media/demo.mp4"}]))
    with pytest.raises(ValueError, match="PNG or JPEG"):
        validate_draft(draft(attachments=[{"type": "image", "path": "media/a.gif"}]))


def test_a_drafting_batch_keeps_its_gaps_author_and_sources():
    source = {
        "id": "s1",
        "label": "Release note",
        "author": "Ada",
        "use": "publishable",
        "excerpt": "We shipped it.",
    }
    batch = {
        **draft(),
        "author_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "gaps": [{"angle": "A customer result", "reason": "Add a publishable result."}],
        "parent": {"path": DRAFT_PATH, "sha256": "a" * 64, "post_id": "p1"},
        "sources": [source, {**source, "id": "t1", "use": "trend"}],
    }
    assert validate_draft(batch)["gaps"][0]["reason"] == "Add a publishable result."
    # A run with too little evidence keeps no posts, only what it needs.
    assert validate_draft({**batch, "posts": []})["posts"] == []
    with pytest.raises(ValueError, match="note on what's missing"):
        validate_draft({**batch, "posts": [], "gaps": []})
    with pytest.raises(ValueError, match="own ID"):
        validate_draft({**batch, "sources": [source, source]})
    for field, value in (
        ("author_id", "not-a-uuid"),
        ("parent", {"path": DRAFT_PATH, "sha256": "short", "post_id": "p1"}),
        ("sources", [{**source, "use": "secret"}]),
    ):
        with pytest.raises(ValueError):
            validate_draft({**batch, field: value})
    # A hand-written draft needs none of them.
    assert validate_draft(draft())["gaps"] == []


async def admission(db):
    f = await project_fixture(db)
    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == KEY)
    definition, revision = builtin.definition, "d" * 40
    await db.upsert_workflow_system(
        system_id="organic-traffic", name="Organic traffic", display_order=1
    )
    await db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=KEY,
        title=builtin.title,
        description=builtin.description,
        executor=KEY,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha=revision,
        version_label=builtin.version_label,
        definition=definition,
    )
    f.workflow = await db.get_workflow(builtin.id)
    original_read = f.storage.read_canonical_artifact

    async def read(**kwargs):
        if kwargs["repo_id"] == "registry/workflows":
            return json.dumps(definition).encode()
        return await original_read(**kwargs)

    f.storage.read_canonical_artifact = read
    f.runtime.integrations.linkedin_posting = SyntheticPosting()
    f.runtime.project_files = ProjectFileService(database=f.db, storage=f.storage)
    return f


async def surface(publication_db, monkeypatch):
    f = await admission(publication_db)
    server = mcp(f, monkeypatch, ACTOR)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    )
    return f, client, server


def put(f, value, *, media=None):
    changes = {DRAFT_PATH: json.dumps(value).encode()}
    if media is not None:
        changes["social/linkedin/media/image.png"] = media
    f.storage.repo.edit(changes)


async def runs(f):
    return await f.db.pool.fetchval(
        "SELECT count(*) FROM workflow_runs WHERE project_id=$1 AND executor=$2",
        f.project.id,
        KEY,
    )


@pytest.mark.asyncio
async def test_http_preview_mcp_publish_and_http_replay_start_one_run(publication_db, monkeypatch):
    f, client, server = await surface(publication_db, monkeypatch)
    put(f, draft())
    base = f"/api/projects/{f.project.id}/linkedin"
    async with client:
        read = await client.get(base + "/drafts", params={"path": DRAFT_PATH})
        assert read.status_code == 200, read.text
        assert read.json()["draft"]["posts"][0]["target"] == PAGE["urn"]
        assert [item["name"] for item in read.json()["accounts"]] == ["Tin Computer", "The founder"]
        mcp_read = await server.call_tool(
            "read_linkedin_drafts", {"project_id": str(f.project.id), "path": DRAFT_PATH}
        )
        assert "Exact approved text" in str(mcp_read) and "x_draft=" in str(mcp_read)
        preview = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        assert preview.status_code == 200, preview.text
        assert preview.json()["text"] == "Exact approved text"
        assert preview.json()["account"]["name"] == "Tin Computer"
        token, request_id = preview.json()["preview_token"], str(uuid4())
        mcp_result = await server.call_tool(
            "publish_linkedin_post",
            {"project_id": str(f.project.id), "preview_token": token, "request_id": request_id},
        )
        assert "pending" in str(mcp_result)
        replay = await client.post(
            base + "/publish", json={"preview_token": token, "request_id": request_id}
        )
        assert replay.status_code == 200, replay.text
    assert await runs(f) == 1
    approved = await f.db.pool.fetch(
        "SELECT result FROM effect_receipts WHERE execution_key LIKE 'linkedin:approved:%'"
    )
    assert len(approved) == 1
    value = json.loads(approved[0]["result"])
    assert (value["account_id"], value["target"]) == (PAGE["id"], PAGE["urn"])
    assert "Private note" not in json.dumps(value)


@pytest.mark.asyncio
async def test_preview_posts_only_as_a_connected_account(publication_db, monkeypatch):
    f, client, _ = await surface(publication_db, monkeypatch)
    base = f"/api/projects/{f.project.id}/linkedin"
    async with client:
        put(f, draft(target="urn:li:organization:999"))
        other = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        assert other.status_code == 409 and "isn't connected" in other.json()["detail"]
        put(f, draft(target=""))
        unchosen = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        assert unchosen.status_code == 409 and "Choose who posts" in unchosen.json()["detail"]
        # With one connected account, an unchosen post goes out as that account.
        f.runtime.integrations.linkedin_posting.accounts = [MEMBER]
        only = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        assert only.status_code == 200 and only.json()["account"]["urn"] == MEMBER["urn"]
        f.runtime.integrations.linkedin_posting.connected = False
        gone = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        assert gone.status_code in {401, 403, 409, 424}
        read = await client.get(base + "/drafts", params={"path": DRAFT_PATH})
        assert read.status_code == 200 and read.json()["accounts"] == []
    assert await runs(f) == 0


@pytest.mark.asyncio
async def test_a_member_posts_as_a_page_or_their_own_profile_only(publication_db, monkeypatch):
    f, client, _ = await surface(publication_db, monkeypatch)
    await f.db.record_tin_user(TEAMMATE)
    await f.db.grant_project_membership(project_id=f.project.id, clerk_user_id=TEAMMATE)
    teammate = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, TEAMMATE)), base_url="https://tin.test"
    )
    base = f"/api/projects/{f.project.id}/linkedin"
    put(f, draft(target=MEMBER["urn"]))
    async with client, teammate:
        # The founder connected their own profile, so only they see and use it.
        mine = await client.get(base + "/drafts", params={"path": DRAFT_PATH})
        assert [item["urn"] for item in mine.json()["accounts"]] == [PAGE["urn"], MEMBER["urn"]]
        theirs = await teammate.get(base + "/drafts", params={"path": DRAFT_PATH})
        assert [item["urn"] for item in theirs.json()["accounts"]] == [PAGE["urn"]]
        refused = await teammate.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        assert refused.status_code == 409
        assert "Only the member who connected" in refused.json()["detail"]
        allowed = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        assert allowed.status_code == 200 and allowed.json()["account"]["urn"] == MEMBER["urn"]
        # The founder's preview is theirs: the teammate can't confirm it.
        stolen = await teammate.post(
            base + "/publish",
            json={"preview_token": allowed.json()["preview_token"], "request_id": str(uuid4())},
        )
        assert stolen.status_code == 409
        # With only the page left to them, an unchosen post goes out as the page.
        put(f, draft(target=""))
        page = await teammate.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        assert page.status_code == 200 and page.json()["account"]["urn"] == PAGE["urn"]
        # A profile someone else signed in as is never the founder's, either.
        f.runtime.integrations.linkedin_posting.owners = {MEMBER["urn"]: TEAMMATE}
        put(f, draft(target=MEMBER["urn"]))
        now = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        assert now.status_code == 409 and "Only the member" in now.json()["detail"]
    assert await runs(f) == 0


@pytest.mark.asyncio
async def test_changed_text_or_image_needs_a_new_preview(publication_db, monkeypatch):
    f, client, server = await surface(publication_db, monkeypatch)
    attachments = [
        {"type": "image", "path": "social/linkedin/media/image.png", "alt_text": "The app"}
    ]
    put(f, draft(attachments=attachments), media=IMAGE)
    base = f"/api/projects/{f.project.id}/linkedin"
    async with client:
        first = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        assert first.status_code == 200, first.text
        assert first.json()["attachments"][0]["url"].startswith(
            f"/api/projects/{f.project.id}/files/raw?"
        )
        put(f, draft(text="Updated text", attachments=attachments))
        stale = await client.post(
            base + "/publish",
            json={"preview_token": first.json()["preview_token"], "request_id": str(uuid4())},
        )
        assert stale.status_code == 409
        second = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        modified = bytearray(IMAGE)
        modified[-20] ^= 1
        put(f, draft(text="Updated text", attachments=attachments), media=bytes(modified))
        changed = await client.post(
            base + "/publish",
            json={"preview_token": second.json()["preview_token"], "request_id": str(uuid4())},
        )
        assert changed.status_code == 409
        assert await runs(f) == 0
        fresh = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        await server.call_tool(
            "publish_linkedin_post",
            {
                "project_id": str(f.project.id),
                "preview_token": fresh.json()["preview_token"],
                "request_id": str(uuid4()),
            },
        )
    assert await runs(f) == 1


@pytest.mark.asyncio
async def test_a_failed_run_that_sent_nothing_can_be_confirmed_again(publication_db, monkeypatch):
    f, client, _ = await surface(publication_db, monkeypatch)
    put(f, draft())
    base = f"/api/projects/{f.project.id}/linkedin"
    async with client:
        preview = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        first_request = {
            "preview_token": preview.json()["preview_token"],
            "request_id": str(uuid4()),
        }
        first = (await client.post(base + "/publish", json=first_request)).json()
        # Confirming the same post again while the first run is live returns that run.
        again = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        live = await client.post(
            base + "/publish",
            json={"preview_token": again.json()["preview_token"], "request_id": str(uuid4())},
        )
        assert live.json()["run_id"] == first["run_id"]
        await f.db.pool.execute(
            "UPDATE workflow_runs SET status='failed' WHERE id=$1", UUID(first["run_id"])
        )
        # A replay of the first request still names the first run...
        replay = await client.post(base + "/publish", json=first_request)
        assert replay.json()["run_id"] == first["run_id"]
        # ...and a new confirmation tries once more, since nothing was sent.
        retry = await client.post(base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"})
        second = await client.post(
            base + "/publish",
            json={"preview_token": retry.json()["preview_token"], "request_id": str(uuid4())},
        )
        assert second.status_code == 200 and second.json()["run_id"] != first["run_id"]
    assert await runs(f) == 2


@pytest.mark.asyncio
async def test_publish_starts_only_from_a_confirmed_approval(publication_db):
    f = await admission(publication_db)
    approval_id = uuid4()

    async def start(*, key=None, internal=True):
        return await start_workflow_run(
            runtime=f.runtime,
            settings=f.settings,
            workflow=f.workflow,
            project_id=f.project.id,
            started_by_clerk_user_id=ACTOR,
            start_idempotency_key=key or f"linkedin-publish:{approval_id}",
            input_payload={"approval_id": str(approval_id)},
            _linkedin_publication=internal,
        )

    with pytest.raises(WorkflowInputError, match="confirm publication"):
        await start()
    await save_effect(
        f.db, f"linkedin:approved:{approval_id}", {"project_id": str(f.project.id), "actor": ACTOR}
    )
    with pytest.raises(WorkflowInputError, match="confirm publication"):
        await start(internal=False)
    with pytest.raises(WorkflowInputError, match="confirm publication"):
        await start(key=f"linkedin-publish:{uuid4()}")
    run = await start()
    assert (await start()).id == run.id
    assert await runs(f) == 1


# Activities: one approved post through a synthetic Zernio.


async def setup(db, storage, posting, *, attachments=None, account=PAGE):
    project = await db.create_project(name="LinkedIn publisher", state_repo_id=storage.repo.id)
    await db.grant_project_membership(project_id=project.id, clerk_user_id=ACTOR)
    workflow_id = await db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", KEY)
    if workflow_id is None:
        workflow_id = uuid4()
        await db.pool.execute(
            """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
                    definition_path,current_commit_sha,version_label,definition)
               VALUES ($1,$2,'LinkedIn publish',$2,'registry/workflows','l.json',$3,'1','{}')""",
            workflow_id,
            KEY,
            "d" * 40,
        )
    approval_id, run_id = uuid4(), uuid4()
    payload = {
        "project_id": str(project.id),
        "actor": ACTOR,
        "revision": storage.repo.head,
        "text": "Exact approved text",
        "attachments": attachments or [],
        "connection_id": str(posting.id),
        "profile_id": PROFILE,
        "account_id": account["id"],
        "target": account["urn"],
        "account": {key: account[key] for key in ("id", "urn", "kind", "name")},
        "fingerprint": "f" * 64,
    }
    await save_effect(db, f"linkedin:approved:{approval_id}", payload)
    await db.pool.execute(
        """INSERT INTO workflow_runs
           (id,project_id,workflow_id,executor,definition_commit_sha,
            temporal_workflow_id,thread_id,generation,fencing_token,status,lease_active,
            started_by_clerk_user_id,input)
           VALUES ($1,$2,$3,$4,$5,$6,$7,1,1,'pending',false,$8,$9::jsonb)""",
        run_id,
        project.id,
        workflow_id,
        KEY,
        "d" * 40,
        f"{KEY}:{run_id}",
        str(run_id),
        ACTOR,
        json.dumps({"project_id": str(project.id), "approval_id": str(approval_id)}),
    )
    return run_id, str(approval_id)


def activities(db, storage, posting):
    return LinkedInPublishActivities(
        database=db, storage=storage, integrations=SimpleNamespace(linkedin_posting=posting)
    )


def attach(storage):
    path = "social/linkedin/media/image.png"
    storage.repo.edit({path: IMAGE}, message="Synthetic approved image")
    return {
        "type": "image",
        "path": path,
        "alt_text": "The app",
        **validate_media(path, IMAGE),
        "sha256": hashlib.sha256(IMAGE).hexdigest(),
    }


async def failed_message(db, run_id):
    return await db.pool.fetchval("SELECT error_message FROM workflow_runs WHERE id=$1", run_id)


@pytest.fixture(autouse=True)
def quick_settle(monkeypatch):
    monkeypatch.setattr(linkedin_publish_activities, "SETTLE_DELAY_SECONDS", 0)


@pytest.mark.asyncio
async def test_post_and_report_survive_replay(publication_db):
    storage, posting = HistoryStorage(), SyntheticPosting()
    run_id, approval_id = await setup(publication_db, storage, posting)
    work = activities(publication_db, storage, posting)
    await work.execute(str(run_id))
    await work.execute(str(run_id))
    assert len(posting.zernio.posts) == 1
    sent = posting.zernio.posts[0]
    assert sent["account_id"] == PAGE["id"] and sent["text"] == "Exact approved text"
    assert sent["key"] == f"tin-linkedin-{approval_id}"
    row = await publication_db.pool.fetchrow(
        "SELECT status,artifact_path,result_summary FROM workflow_runs WHERE id=$1", run_id
    )
    assert row["status"] == "succeeded"
    assert row["artifact_path"] == f"reports/linkedin/{run_id}.md"
    assert URL in row["result_summary"]
    receipt = await publication_db.get_effect(f"linkedin:post:{approval_id}")
    assert receipt.status == "completed" and receipt.result["value"]["post_id"] == POST_ID


@pytest.mark.asyncio
async def test_a_lost_answer_is_resent_with_the_same_key_inside_the_window(publication_db):
    storage, posting = HistoryStorage(), SyntheticPosting()
    posting.zernio.answers = ["lost"]
    run_id, approval_id = await setup(publication_db, storage, posting)
    work = activities(publication_db, storage, posting)
    with pytest.raises(IntegrationDeliveryUnknownError):
        await work.execute(str(run_id))
    assert (await publication_db.get_effect(f"linkedin:post:{approval_id}")).status == "started"
    await work.execute(str(run_id))
    keys = [post["key"] for post in posting.zernio.posts]
    assert keys == [f"tin-linkedin-{approval_id}"] * 2
    assert (await publication_db.get_run(run_id)).status.value == "succeeded"


@pytest.mark.asyncio
async def test_a_lost_answer_past_the_window_stops_and_asks_to_check(publication_db):
    storage, posting = HistoryStorage(), SyntheticPosting()
    posting.zernio.answers = ["lost"]
    run_id, approval_id = await setup(publication_db, storage, posting)
    work = activities(publication_db, storage, posting)
    with pytest.raises(IntegrationDeliveryUnknownError):
        await work.execute(str(run_id))
    old = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    await publication_db.pool.execute(
        "UPDATE effect_receipts SET result=jsonb_set(result,'{attempted_at}',to_jsonb($2::text)) "
        "WHERE execution_key=$1",
        f"linkedin:post:{approval_id}",
        old,
    )
    with pytest.raises(ApplicationError, match="Check the page"):
        await work.execute(str(run_id))
    assert len(posting.zernio.posts) == 1
    await work.failure(str(run_id))
    assert "may have published" in await failed_message(publication_db, run_id)


@pytest.mark.asyncio
async def test_a_refused_post_leaves_no_receipt(publication_db):
    storage, posting = HistoryStorage(), SyntheticPosting()
    posting.zernio.answers = ["refused"]
    run_id, approval_id = await setup(publication_db, storage, posting)
    work = activities(publication_db, storage, posting)
    with pytest.raises(IntegrationDeliveryRefusedError):
        await work.execute(str(run_id))
    assert await publication_db.get_effect(f"linkedin:post:{approval_id}") is None
    await work.failure(str(run_id))
    assert "nothing was posted" in await failed_message(publication_db, run_id)


@pytest.mark.asyncio
async def test_images_upload_once_with_alt_text(publication_db):
    storage, posting = HistoryStorage(), SyntheticPosting()
    attachment = attach(storage)
    run_id, approval_id = await setup(publication_db, storage, posting, attachments=[attachment])
    posting.zernio.answers = ["lost"]
    work = activities(publication_db, storage, posting)
    with pytest.raises(IntegrationDeliveryUnknownError):
        await work.execute(str(run_id))
    await work.execute(str(run_id))
    assert posting.zernio.uploads == ["image.png"]
    media = posting.zernio.posts[-1]["media"]
    assert [(item["url"], item["alt_text"]) for item in media] == [
        ("https://media.zernio.com/1.png", "The app")
    ]
    receipt = await publication_db.get_effect(f"linkedin:media:{approval_id}:0")
    assert receipt.status == "completed"


@pytest.mark.asyncio
async def test_a_changed_image_never_posts(publication_db):
    storage, posting = HistoryStorage(), SyntheticPosting()
    attachment = {**attach(storage), "sha256": "0" * 64}
    run_id, _ = await setup(publication_db, storage, posting, attachments=[attachment])
    work = activities(publication_db, storage, posting)
    with pytest.raises(ApplicationError, match="image changed"):
        await work.execute(str(run_id))
    assert posting.zernio.uploads == [] and posting.zernio.posts == []


@pytest.mark.asyncio
async def test_an_account_gone_from_the_profile_never_posts(publication_db):
    storage, posting = HistoryStorage(), SyntheticPosting()
    posting.zernio.accounts_list = [MEMBER]
    run_id, _ = await setup(publication_db, storage, posting)
    work = activities(publication_db, storage, posting)
    with pytest.raises(IntegrationAuthorizationError, match="no longer connected"):
        await work.execute(str(run_id))
    assert posting.zernio.posts == []


@pytest.mark.asyncio
async def test_a_profile_that_is_no_longer_the_approvers_never_posts(publication_db):
    storage, posting = HistoryStorage(), SyntheticPosting()
    posting.owners = {MEMBER["urn"]: TEAMMATE}
    run_id, _ = await setup(publication_db, storage, posting, account=MEMBER)
    work = activities(publication_db, storage, posting)
    with pytest.raises(ApplicationError, match="Only the member who connected"):
        await work.execute(str(run_id))
    assert posting.zernio.posts == []


@pytest.mark.asyncio
async def test_a_publishing_post_is_read_until_linkedin_answers(publication_db):
    storage, posting = HistoryStorage(), SyntheticPosting()
    posting.zernio.answers = ["publishing"]
    run_id, _ = await setup(publication_db, storage, posting)
    await activities(publication_db, storage, posting).execute(str(run_id))
    assert posting.zernio.reads == 1 and len(posting.zernio.posts) == 1
    assert (await publication_db.get_run(run_id)).status.value == "succeeded"


@pytest.mark.asyncio
async def test_a_post_linkedin_refused_says_why(publication_db):
    storage, posting = HistoryStorage(), SyntheticPosting()
    posting.zernio.answers = ["failed"]
    run_id, _ = await setup(publication_db, storage, posting)
    work = activities(publication_db, storage, posting)
    with pytest.raises(ApplicationError, match="refused this post's content") as raised:
        await work.execute(str(run_id))
    from tin_lite.failures import classify, detail, failure_payload

    await work.failure(
        {"run_id": str(run_id), **failure_payload(classify(raised.value), detail(raised.value))}
    )
    assert "refused this post's content" in await failed_message(publication_db, run_id)


@pytest.mark.asyncio
async def test_a_refusal_after_publishing_keeps_linkedins_reason(publication_db):
    from tin_lite.failures import classify, detail, failure_payload

    storage, posting = HistoryStorage(), SyntheticPosting()
    posting.zernio.answers = ["publishing"]
    posting.zernio.settles_as = "failed"
    run_id, _ = await setup(publication_db, storage, posting)
    work = activities(publication_db, storage, posting)
    with pytest.raises(ApplicationError) as raised:
        await work.execute(str(run_id))
    await work.failure(
        {"run_id": str(run_id), **failure_payload(classify(raised.value), detail(raised.value))}
    )
    message = await failed_message(publication_db, run_id)
    assert "refused this post's content" in message and "still publishing" not in message


async def test_project_purge_removes_only_its_linkedin_approval_chain(publication_db):
    db = publication_db
    projects = [
        await db.create_project(name="LinkedIn purge", state_repo_id=f"projects/{uuid4()}")
        for _ in range(2)
    ]
    owned = []
    for project in projects:
        approval = uuid4()
        keys = [
            f"linkedin:approved:{approval}",
            f"linkedin:preview:{uuid4()}",
            f"linkedin:confirm:{project.id}:{uuid4()}",
            f"linkedin:post:{approval}",
            f"linkedin:media:{approval}:0",
        ]
        for key in keys:
            await save_effect(db, key, {"project_id": str(project.id), "text": "Test draft"})
        owned.append(keys)
    async with db.pool.acquire() as conn:
        await db.purge_project_data(conn, project_id=projects[0].id)
        await db.purge_project_data(conn, project_id=projects[0].id)
    for key in owned[0]:
        assert await db.get_effect(key) is None
    for key in owned[1]:
        assert (await db.get_effect(key)).status == "completed"


# The Zernio client's posting calls, against a synthetic Zernio.

ZERNIO_KEY = "zernio_synthetic_key_value"
UPLOAD = "https://late-media.0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/u/1?sig=x"
PUBLIC = "https://media.zernio.com/u/1.png"


def zernio_client(handler):
    from tin_lite.zernio import Zernio

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = SimpleNamespace(zernio_api_key=SimpleNamespace(get_secret_value=lambda: ZERNIO_KEY))
    return client, Zernio(client, settings)


def reply(status, body):
    return httpx.Response(status, stream=httpx.ByteStream(json.dumps(body).encode()))


@pytest.mark.asyncio
async def test_upload_puts_bytes_without_tins_key_and_checks_hosts():
    calls = []
    public = {"url": PUBLIC}

    def handler(request):
        calls.append(request)
        if request.url.host == "zernio.com":
            assert request.headers["authorization"] == f"Bearer {ZERNIO_KEY}"
            body = json.loads(request.content)
            assert body == {"filename": "a.png", "contentType": "image/png", "size": len(IMAGE)}
            return reply(200, {"uploadUrl": UPLOAD, "publicUrl": public["url"], "expiresIn": 3600})
        assert request.method == "PUT" and request.content == IMAGE
        # The key never reaches the storage host.
        assert "authorization" not in request.headers
        assert request.headers["user-agent"].startswith("Tin")
        return httpx.Response(200)

    client, zernio = zernio_client(handler)
    async with client:
        url = await zernio.upload_media(filename="a.png", media_type="image/png", content=IMAGE)
        assert url == PUBLIC and len(calls) == 2
        public["url"] = "https://media.example.com/u/1.png"
        with pytest.raises(IntegrationUpstreamError):
            await zernio.upload_media(filename="a.png", media_type="image/png", content=IMAGE)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_create_post_sends_one_keyed_post_and_reads_every_answer():
    from tin_lite.zernio import DUPLICATE

    answers = []

    def handler(request):
        assert request.url.path == "/api/v1/posts" and request.method == "POST"
        assert request.headers["idempotency-key"] == "tin-linkedin-a1"
        body = json.loads(request.content)
        assert body["platforms"] == [{"platform": "linkedin", "accountId": PAGE["id"]}]
        assert body["publishNow"] is True and body["content"] == "Exact text"
        assert body["mediaItems"] == [{"type": "image", "url": PUBLIC, "altText": "The app"}]
        status, payload = answers.pop(0)
        return reply(status, payload)

    def post(status, entry):
        return {"post": {"_id": POST_ID, "status": status, "platforms": [entry]}}

    send = dict(
        account_id=PAGE["id"],
        text="Exact text",
        media=[{"url": PUBLIC, "alt_text": "The app"}],
        idempotency_key="tin-linkedin-a1",
    )
    client, zernio = zernio_client(handler)
    async with client:
        answers.append((201, post("published", {"platform": "linkedin", "platformPostUrl": URL})))
        assert await zernio.create_post(**send) == {
            "post_id": POST_ID,
            "status": "published",
            "url": URL,
            "error_category": None,
            # Listening reads the post by its URN, from the link when Zernio names no ID.
            "urn": "urn:li:share:7300000000000000000",
            "published_at": None,
        }
        entry = {
            "platform": "linkedin",
            "platformPostUrl": URL,
            "platformPostId": "urn:li:ugcPost:7300000000000000001",
            "publishedAt": "2026-10-14T09:00:00.000Z",
        }
        answers.append((201, post("published", entry)))
        published = await zernio.create_post(**send)
        assert published["urn"] == "urn:li:ugcPost:7300000000000000001"
        assert published["published_at"] == "2026-10-14T09:00:00+00:00"
        answers.append(
            (207, post("failed", {"platform": "linkedin", "errorCategory": "user_content"}))
        )
        failed = await zernio.create_post(**send)
        assert (failed["status"], failed["error_category"]) == ("failed", "user_content")
        answers.append((200, post("published", {"platformPostUrl": "https://evil.test/x"})))
        assert (await zernio.create_post(**send))["url"] is None
        for status, payload, error in (
            (409, {"code": "idempotency_conflict"}, IntegrationDeliveryUnknownError),
            (500, {}, IntegrationDeliveryUnknownError),
            (201, {"post": {"status": "published"}}, IntegrationDeliveryUnknownError),
            (404, {"error": "Account not found"}, IntegrationAuthorizationError),
            (422, {"error": "Invalid"}, IntegrationDeliveryRefusedError),
        ):
            answers.append((status, payload))
            with pytest.raises(error):
                await zernio.create_post(**send)
        answers.append((409, {"code": "duplicate_content", "details": {"existingPostId": POST_ID}}))
        with pytest.raises(IntegrationDeliveryRefusedError, match=DUPLICATE[:30]):
            await zernio.create_post(**send)
