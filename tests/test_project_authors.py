from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db
from test_style_capture import SOURCE, approve, capture_fixture

from tin_lite import project_authors as authors
from tin_lite.capture_revisions import StyleProposalReview
from tin_lite.run_service import start_workflow_run
from tin_lite.writing_style import STYLE_PATH


async def save(f, author_id, *, name="Alex", version=0, guide=None, linked=False, actor=ACTOR):
    return await authors.ProjectAuthors(f.db, f.storage).save(
        project_id=f.project.id,
        actor=actor,
        author_id=author_id,
        display_name=name,
        expected_version=version,
        selected_guide=guide,
        link_to_me=linked,
    )


async def start(f, author_id, key=None):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        start_idempotency_key=key or str(uuid4()),
        input_payload={"source_path": SOURCE, "author_id": str(author_id)},
    )


async def test_identity_uses_member_binding_never_name_and_updates_are_checked(publication_db):
    f = await capture_fixture(publication_db)
    service = authors.ProjectAuthors(f.db, f.storage)
    one, two = uuid4(), uuid4()
    await save(f, one)
    await save(f, two)
    assert (await service.list(f.project.id, ACTOR))["default_author_id"] is None
    await save(f, two, version=1, linked=True)
    listed = await service.list(f.project.id, ACTOR)
    assert len(listed["authors"]) == 2
    assert listed["default_author_id"] == str(two)
    with pytest.raises(ValueError, match="already linked"):
        await save(f, one, version=1, linked=True)
    renamed = await save(f, two, name="Renamed", version=2, linked=True)
    assert renamed["id"] == str(two) and renamed["version"] == 3
    assert (await save(f, two, name="Renamed", version=2, linked=True)) == renamed
    with pytest.raises(ValueError, match="changed"):
        await save(f, two, name="Old tab", version=1)
    with pytest.raises(LookupError):
        await service.list(uuid4(), ACTOR)
    with pytest.raises(LookupError):
        await save(f, uuid4(), actor="not-a-member")


async def test_bind_guide_checks_file_and_exclusive_destination(publication_db):
    f = await capture_fixture(publication_db)
    one, two = uuid4(), uuid4()
    with pytest.raises(ValueError, match="missing"):
        await save(f, one, guide="style/missing.md")
    with pytest.raises(ValueError, match="shared"):
        await save(f, one, guide=STYLE_PATH)
    f.storage.repo.edit({"style/alex.md": b"Original authored voice"})
    author = await save(f, one, guide="style/alex.md")
    assert author["guide_path"] == "style/alex.md"
    assert (await save(f, one, version=1, name="Renamed"))["guide_path"] == "style/alex.md"
    with pytest.raises(ValueError, match="already linked"):
        await save(f, two, guide="style/alex.md")
    for raw in (b"", b"x" * 24_001, b"a\0b", b"\xff"):
        f.storage.repo.edit({"style/invalid.md": raw})
        with pytest.raises(ValueError):
            await save(f, two, guide="style/invalid.md")


async def test_capture_keeps_the_pinned_author_through_approval_and_retry(publication_db):
    f = await capture_fixture(publication_db)
    one, two = uuid4(), uuid4()
    first = await save(f, one, linked=True)
    second = await save(f, two)
    f.storage.repo.edit({STYLE_PATH: b"Shared voice", second["guide_path"]: b"Other author"})
    key = str(uuid4())
    run = await start(f, one, key)
    assert (await start(f, one, key)).id == run.id
    await f.activities.prepare(str(run.id))
    await save(f, one, name="Renamed while capturing", version=1, linked=True)
    await f.activities.extract(str(run.id))
    assert await f.activities.propose(str(run.id))
    review = await StyleProposalReview(database=f.db, storage=f.storage).view(run.id, ACTOR)
    assert review["documents"][0]["destination"] == first["guide_path"]
    f.storage.repo.edit({"style/replacement.md": b"Different guide"})
    with pytest.raises(ValueError, match="Finish or discard"):
        await save(f, one, version=2, guide="style/replacement.md", linked=True)
    assert (await start(f, one, key)).id == run.id
    await approve(f, run)
    await f.activities.publish(str(run.id))
    await f.activities.publish(str(run.id))

    async def read(path):
        return await f.storage.read_canonical_artifact(
            repo_id=f.project.state_repo_id, commit_sha=f.storage.repo.head, path=path
        )

    assert b"writing-style" in await read(first["guide_path"])
    assert await read(STYLE_PATH) == b"Shared voice"
    assert await read(second["guide_path"]) == b"Other author"
    receipt = await f.db.get_effect(authors.source_key(run.id))
    assert receipt.result["version"] == 1 and receipt.result["display_name"] == "Alex"
    assert f.router.generate.await_count == 1


async def test_unbound_or_other_project_author_never_starts_compute(publication_db):
    f = await capture_fixture(publication_db)
    with pytest.raises(ValueError, match="Choose an author"):
        await start(f, uuid4())
    assert f.router.generate.await_count == 0
    async with f.db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM workflow_runs") == 0
    fake_run = SimpleNamespace(
        id=uuid4(), input={"author_id": str(uuid4())}, project_id=f.project.id
    )
    with pytest.raises(ValueError, match="no pinned"):
        await authors.capture_destination(f.db, fake_run)


async def test_http_and_mcp_share_the_binding_and_membership_contract(publication_db, monkeypatch):
    f = await capture_fixture(publication_db)
    author_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/api/projects/{f.project.id}/authors/{author_id}",
            json={"display_name": "Alex", "expected_version": 0, "link_to_me": True},
        )
        assert response.status_code == 200, response.text
        expected = response.json()
        listed = await client.get(f"/api/projects/{f.project.id}/authors")
        assert listed.json()["default_author_id"] == str(author_id)
        assert listed.headers["cache-control"] == "no-store"
        guide = await client.get(
            f"/api/projects/{f.project.id}/writing-style/guide?author_id={author_id}"
        )
        assert guide.json()["path"] == expected["guide_path"]
        assert guide.json()["capture_inputs"] == {"author_id": str(author_id)}
        assert (await client.get(f"/api/projects/{uuid4()}/authors")).status_code == 404
    server = mcp(f, monkeypatch)
    result = structured(
        await server.call_tool("list_project_authors", {"project_id": str(f.project.id)})
    )
    assert result["authors"] == [expected]


async def test_changed_binding_before_admission_refuses_unpinned_capture(
    publication_db, monkeypatch
):
    f = await capture_fixture(publication_db)
    author_id = uuid4()
    await save(f, author_id)
    original = authors.ProjectAuthors.resolve

    async def race(self, *args):
        selected = await original(self, *args)
        await save(f, author_id, name="Changed before admission", version=1)
        return selected

    monkeypatch.setattr(authors.ProjectAuthors, "resolve", race)
    with pytest.raises(ValueError, match="author changed"):
        await start(f, author_id)
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
    assert f.router.generate.await_count == 0


async def test_members_cannot_claim_someone_elses_author(publication_db):
    f = await capture_fixture(publication_db)
    author_id = uuid4()
    await save(f, author_id, linked=True)
    await f.db.record_tin_user("user_other")
    await f.db.grant_project_membership(project_id=f.project.id, clerk_user_id="user_other")
    with pytest.raises(ValueError, match="another member"):
        await save(f, author_id, version=1, actor="user_other", linked=True)
    renamed = await save(f, author_id, name="Alex renamed", version=1, actor="user_other")
    assert renamed["member_clerk_user_id"] == ACTOR


async def test_uppercase_uuid_keeps_capture_destination(publication_db):
    f = await capture_fixture(publication_db)
    author_id = uuid4()
    author = await save(f, author_id)
    run = await start(f, str(author_id).upper())
    assert await authors.capture_destination(f.db, run) == author["guide_path"]
