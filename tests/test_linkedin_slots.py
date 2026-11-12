"""LinkedIn posting slots: a post made up next from its exact preview, the slot that posts it
as the member who picked it or passes without a run, and the first comment that goes with it."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from test_linkedin_publish import (
    DRAFT_PATH,
    MEMBER,
    PAGE,
    activities,
    admission,
    draft,
    put,
    runs,
)
from test_linkedin_publish import quick_settle as quick_settle
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite import linkedin_slots
from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.integrations import IntegrationAuthorizationError
from tin_lite.linkedin_posts import LinkedInPosts, validate_draft
from tin_lite.run_service import start_workflow_run
from tin_lite.schedules import WorkflowSchedule
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs

COMMENT = "Explore Tin: https://tin.computer/"


def batch(*, first_comment=COMMENT, text="Exact approved text", target=PAGE["urn"]):
    value = draft(text=text, target=target)
    post = {**value["posts"][0], "first_comment": first_comment}
    second = {**post, "id": "p2", "text": "The second post", "first_comment": ""}
    return {**value, "posts": [post, second]}


async def fixture(db, monkeypatch, *, slots_at: datetime | None = None):
    f = await admission(db)
    slots = next(item for item in BUILTIN_WORKFLOWS if item.key == linkedin_slots.KEY)
    await db.upsert_registry_workflow(
        workflow_id=slots.id,
        key=slots.key,
        title=slots.title,
        description=slots.description,
        executor=slots.key,
        definition_repo_id="registry/workflows",
        definition_path=slots.definition_path,
        current_commit_sha="d" * 40,
        version_label=slots.version_label,
        definition=slots.definition,
    )
    f.server = mcp(f, monkeypatch, ACTOR)
    f.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    )
    f.base = f"/api/projects/{f.project.id}/linkedin"
    f.configured = None
    if slots_at is not None:
        f.configured = await save_slots(f, slots_at)
    f.common = TinActivities(
        database=f.db,
        storage=f.storage,
        sandboxes=None,
        settings=f.settings,
        integrations=f.runtime.integrations,
        temporal=SimpleNamespace(
            get_schedule_handle=Mock(return_value=SimpleNamespace(pause=AsyncMock()))
        ),
    )
    return f


async def save_slots(f, next_run_at):
    workflow = await f.db.get_workflow(linkedin_slots.WORKFLOW_ID)
    schedule = WorkflowSchedule.model_validate(
        {**linkedin_slots.DEFAULT_SCHEDULE, "timezone": "Europe/Istanbul"}
    )
    configured = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=workflow.id,
        definition_commit_sha="d" * 40,
        name="LinkedIn posting slots",
        inputs=normalize_workflow_inputs(
            schema=workflow.definition["input_schema"], project_id=f.project.id, inputs={}
        ),
        input_schema=workflow.definition["input_schema"],
        schedule=schedule.model_dump(mode="json"),
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
        pinned_definition=workflow.definition,
    )
    return await f.db.project_workflow_synced(
        project_workflow_id=configured.id,
        temporal_schedule_id=f"tin-lite-project-workflow:{configured.id}",
        next_run_at=next_run_at,
    )


def occurrence(configured, name="slot-1"):
    return {
        "project_workflow_id": str(configured.id),
        "scheduled_for": datetime.now(UTC).isoformat(),
        "occurrence_id": name,
    }


async def pick(f, post_id="p1"):
    preview = await f.client.post(
        f.base + "/preview", json={"path": DRAFT_PATH, "post_id": post_id}
    )
    assert preview.status_code == 200, preview.text
    return await f.client.post(
        f.base + "/up-next",
        json={"preview_token": preview.json()["preview_token"], "request_id": str(uuid4())},
    )


async def events(f, kind):
    return await f.db.pool.fetch(
        "SELECT summary FROM activity_events WHERE project_id=$1 AND event_type=$2",
        f.project.id,
        kind,
    )


def test_a_first_comment_keeps_linkedins_comment_limit():
    assert validate_draft(batch())["posts"][0]["first_comment"] == COMMENT
    # Drafts written before first comments still read: the field is optional.
    assert validate_draft(draft())["posts"][0]["first_comment"] == ""
    validate_draft(batch(first_comment="a" * 1250))
    with pytest.raises(ValueError, match="1,250"):
        validate_draft(batch(first_comment="a" * 1251))
    with pytest.raises(ValueError, match="control characters"):
        validate_draft(batch(first_comment="a\x07b"))


@pytest.mark.asyncio
async def test_the_first_comment_is_previewed_approved_and_sent(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    put(f, batch())
    posts = LinkedInPosts(f.runtime, f.settings)
    async with f.client:
        preview = await f.client.post(
            f.base + "/preview", json={"path": DRAFT_PATH, "post_id": "p1"}
        )
        assert preview.json()["first_comment"] == COMMENT
        # A post without one keeps the snapshot, and so the approval, it had before.
        bare = await posts.snapshot(f.project.id, ACTOR, DRAFT_PATH, "p2")
        assert "first_comment" not in bare
        published = await f.client.post(
            f.base + "/publish",
            json={"preview_token": preview.json()["preview_token"], "request_id": str(uuid4())},
        )
        assert published.status_code == 200, published.text
    posting = f.runtime.integrations.linkedin_posting
    await activities(f.db, f.storage, posting).execute(published.json()["run_id"])
    assert posting.zernio.posts[0]["first_comment"] == COMMENT
    run = await f.db.get_run(UUID(published.json()["run_id"]))
    report = f.storage.repo.trees[f.storage.repo.head][run.artifact_path][1].decode()
    assert "## First comment" in report and COMMENT in report


@pytest.mark.asyncio
async def test_zernio_gets_the_first_comment_on_the_linkedin_entry():
    from test_linkedin_publish import POST_ID, reply, zernio_client

    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return reply(200, {"post": {"_id": POST_ID, "status": "pending", "platforms": []}})

    client, zernio = zernio_client(handler)
    async with client:
        send = dict(account_id=PAGE["id"], text="Exact text", media=[], idempotency_key="k")
        await zernio.create_post(**send, first_comment=COMMENT)
        await zernio.create_post(**send)
    assert bodies[0]["platforms"] == [
        {
            "platform": "linkedin",
            "accountId": PAGE["id"],
            "platformSpecificData": {"firstComment": COMMENT},
        }
    ]
    assert bodies[1]["platforms"] == [{"platform": "linkedin", "accountId": PAGE["id"]}]


@pytest.mark.asyncio
async def test_a_post_goes_up_next_only_with_slots_and_replaces_the_last(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    put(f, batch())
    async with f.client:
        early = await pick(f)
        assert early.status_code == 409 and "posting slots" in early.json()["detail"]
        f.configured = await save_slots(f, datetime.now(UTC) + timedelta(days=2))
        first = await pick(f)
        assert first.status_code == 200, first.text
        assert first.json()["up_next"]["post_id"] == "p1"
        # "Pick a post" opens the newest batch.
        assert first.json()["batch"] == DRAFT_PATH
        assert first.json()["up_next"]["title"] == "Exact approved text"
        assert first.json()["slots"]["schedule"]["weekdays"] == ["monday", "wednesday", "friday"]
        second = await pick(f, "p2")
        assert second.json()["up_next"]["post_id"] == "p2"
        # The reader shows it from any batch.
        read = await f.client.get(f.base + "/drafts", params={"path": DRAFT_PATH})
        assert read.json()["up_next"]["post_id"] == "p2"
        cleared = await f.client.delete(f.base + "/up-next")
        assert cleared.json()["up_next"] is None
        # Slots post as a page, never a member's own profile.
        put(f, batch(target=MEMBER["urn"]))
        personal = await pick(f)
        assert personal.status_code == 409 and "as a page" in personal.json()["detail"]
    assert await runs(f) == 0


@pytest.mark.asyncio
async def test_a_slot_with_nothing_up_next_passes_without_a_run(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, slots_at=datetime.now(UTC))
    async with f.client:
        assert await f.common.dispatch_scheduled_workflow(occurrence(f.configured)) == {}
    assert await runs(f) == 0
    saved = await f.db.get_project_workflow(f.configured.id)
    assert saved.status == "active" and saved.next_run_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_a_slot_posts_the_post_up_next_as_the_member_who_picked_it(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch, slots_at=datetime.now(UTC))
    put(f, batch())
    async with f.client:
        assert (await pick(f)).status_code == 200
        dispatched = await f.common.dispatch_scheduled_workflow(occurrence(f.configured))
        # A retried activity finds the run it started.
        again = await f.common.dispatch_scheduled_workflow(occurrence(f.configured))
        assert again == dispatched
        run = await f.db.get_run(UUID(dispatched["run_id"]))
        assert dispatched["executor"] == "social.linkedin_publish"
        assert run.started_by_clerk_user_id == ACTOR and run.trigger_source == "schedule"
        assert run.project_workflow_id is None
        posting = f.runtime.integrations.linkedin_posting
        await activities(f.db, f.storage, posting).execute(dispatched["run_id"])
        assert posting.zernio.posts[0]["first_comment"] == COMMENT
        # Once out, it is nobody's next post, and the next slot passes.
        state = await f.client.get(f.base + "/up-next")
        assert state.json()["up_next"] is None
        assert await f.common.dispatch_scheduled_workflow(occurrence(f.configured, "slot-2")) == {}
    assert await runs(f) == 1


@pytest.mark.asyncio
async def test_a_post_changed_after_its_pick_leaves_up_next(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, slots_at=datetime.now(UTC))
    put(f, batch())
    async with f.client:
        assert (await pick(f)).status_code == 200

        # Saved in the reader: it leaves up next as soon as the edit is saved.
        async def commit(*, changes, **_):
            f.storage.repo.edit({item["path"]: item["content"].encode() for item in changes})
            return SimpleNamespace(revision=f.storage.repo.head)

        f.runtime.project_files = SimpleNamespace(commit=commit)
        read = await f.client.get(f.base + "/drafts", params={"path": DRAFT_PATH})
        edited = batch(text="Edited after the pick")
        saved = await f.client.put(
            f.base + "/drafts",
            json={
                "path": DRAFT_PATH,
                "expected_revision": read.json()["revision"],
                "request_id": str(uuid4()),
                "draft": edited,
            },
        )
        assert saved.status_code == 200, saved.text
        assert (await f.client.get(f.base + "/up-next")).json()["up_next"] is None
        assert len(await events(f, "linkedin_up_next_changed")) == 1
        # Changed some other way (an agent's commit): the slot checks the exact post and passes.
        assert (await pick(f)).status_code == 200
        put(f, batch(text="Changed by a commit"))
        assert await f.common.dispatch_scheduled_workflow(occurrence(f.configured)) == {}
        assert (await f.client.get(f.base + "/up-next")).json()["up_next"] is None
    assert await runs(f) == 0
    assert len(await events(f, "linkedin_up_next_changed")) == 2


@pytest.mark.asyncio
async def test_a_failed_slot_keeps_the_post_up_next_for_the_next_slot(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, slots_at=datetime.now(UTC))
    put(f, batch())
    posting = f.runtime.integrations.linkedin_posting
    async with f.client:
        assert (await pick(f)).status_code == 200
        posting.connected = False
        first = await f.common.dispatch_scheduled_workflow(occurrence(f.configured))
        work = activities(f.db, f.storage, posting)
        with pytest.raises(IntegrationAuthorizationError):
            await work.execute(first["run_id"])
        await work.failure(first["run_id"])
        assert (await f.db.get_run(UUID(first["run_id"]))).status.value == "failed"
        assert (await f.client.get(f.base + "/up-next")).json()["up_next"]["post_id"] == "p1"
        # Reconnected: the next slot tries again under a new attempt of the same approval.
        posting.connected = True
        second = await f.common.dispatch_scheduled_workflow(occurrence(f.configured, "slot-2"))
        assert second["run_id"] != first["run_id"]
        await work.execute(second["run_id"])
    assert len(posting.zernio.posts) == 1
    assert await runs(f) == 2


@pytest.mark.asyncio
async def test_decisions_ask_for_a_pick_the_day_before_an_empty_slot(publication_db, monkeypatch):
    soon = datetime.now(UTC) + timedelta(hours=3)
    f = await fixture(publication_db, monkeypatch, slots_at=soon)
    put(f, batch())
    async with f.client:
        state = await f.client.get(f.base + "/up-next")
        notice = state.json()["notice"]
        assert notice["ready"] == 2 and notice["waiting_for_cards"] == 0
        assert notice["path"] == DRAFT_PATH and notice["timezone"] == "Europe/Istanbul"
        listed = structured(
            await f.server.call_tool("list_open_decisions", {"project_id": str(f.project.id)})
        )
        assert listed["linkedin_slot"]["ready"] == 2
        assert (await pick(f)).status_code == 200
        assert (await f.client.get(f.base + "/up-next")).json()["notice"] is None
        await f.client.delete(f.base + "/up-next")
        await f.db.pool.execute(
            "UPDATE project_workflows SET next_run_at=$2 WHERE id=$1",
            f.configured.id,
            datetime.now(UTC) + timedelta(days=3),
        )
        assert (await f.client.get(f.base + "/up-next")).json()["notice"] is None


@pytest.mark.asyncio
async def test_slots_never_start_by_hand(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, slots_at=datetime.now(UTC))
    workflow = await f.db.get_workflow(linkedin_slots.WORKFLOW_ID)
    with pytest.raises(WorkflowInputError, match="publish it"):
        await start_workflow_run(
            runtime=f.runtime,
            settings=f.settings,
            workflow=workflow,
            project_id=f.project.id,
            started_by_clerk_user_id=ACTOR,
            input_payload={},
            project_workflow_id=f.configured.id,
        )
    await f.client.aclose()


@pytest.mark.asyncio
async def test_the_calendar_names_the_post_the_next_slot_posts(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, slots_at=datetime.now(UTC) + timedelta(days=1))
    put(f, batch())
    today = datetime.now(UTC).date()
    monday = today + timedelta(days=7 - today.weekday())
    week = {"start": monday.isoformat(), "timezone": "Europe/Istanbul"}
    async with f.client:
        empty = await f.client.get(f"/api/projects/{f.project.id}/week", params=week)
        assert empty.status_code == 200, empty.text
        slots = [
            item
            for item in empty.json()["occurrences"]
            if item["workflow_key"] == linkedin_slots.KEY
        ]
        # The slots after the next one post whatever is picked by then.
        assert [item["detail"] for item in slots] == [
            "Nothing up next, so it passes",
            "Posts the next pick",
            "Posts the next pick",
        ]
        assert (await pick(f)).status_code == 200
        picked = await f.client.get(f"/api/projects/{f.project.id}/week", params=week)
        slots = [
            item
            for item in picked.json()["occurrences"]
            if item["workflow_key"] == linkedin_slots.KEY
        ]
        assert [item["detail"] for item in slots] == [
            "Posts: Exact approved text",
            "Posts the next pick",
            "Posts the next pick",
        ]
