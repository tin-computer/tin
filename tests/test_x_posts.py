"""Real SQL admission through HTTP and MCP, with synthetic project Files and X account."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_private_workflows import ACTOR, app, mcp
from test_procedure_publication import publication_db as publication_db
from test_x_admission import fixture as publish_fixture

from tin_lite.project_files import ProjectFileService
from tin_lite.x_posts import KEY


class SyntheticX:
    def __init__(self):
        self.connection_id = uuid4()

    async def connection(self, project_id, capability=None):
        assert project_id is not None
        assert capability in {"x.posts.publish", "x.media.upload"}
        return SimpleNamespace(
            id=self.connection_id,
            external_account_id="12345",
            external_account_label="@founder",
        )


def draft(*, text="Exact approved text", attachments=None):
    return {
        "schema_version": "tin.social.x_draft.v1",
        "account_id": "12345",
        "posts": [
            {
                "id": "p1",
                "text": text,
                "readiness": "ready",
                "support": [],
                "editor_notes": "Private note, never publish.",
                "attachments": attachments or [],
                "missing_assets": [],
            }
        ],
    }


async def setup(publication_db, monkeypatch):
    f = await publish_fixture(publication_db)
    f.runtime.integrations.x = SyntheticX()
    f.runtime.project_files = ProjectFileService(database=f.db, storage=f.storage)
    server = mcp(f, monkeypatch, ACTOR)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    )
    return f, client, server


def put(f, value, *, media=None):
    changes = {"social/x/draft.json": json.dumps(value).encode()}
    if media is not None:
        changes["social/x/media/image.png"] = media
    f.storage.repo.edit(changes)


@pytest.mark.asyncio
async def test_http_preview_mcp_publish_and_http_replay_create_one_real_admitted_run(
    publication_db, monkeypatch
):
    f, client, server = await setup(publication_db, monkeypatch)
    put(f, draft())
    base = f"/api/projects/{f.project.id}/x"
    async with client:
        read = await client.get(base + "/drafts", params={"path": "social/x/draft.json"})
        assert read.status_code == 200, read.text
        assert read.json()["draft"]["posts"][0]["text"] == "Exact approved text"
        mcp_read = await server.call_tool(
            "read_x_drafts",
            {
                "project_id": str(f.project.id),
                "path": "social/x/draft.json",
            },
        )
        assert "Exact approved text" in str(mcp_read)
        preview = await client.post(
            base + "/preview",
            json={
                "path": "social/x/draft.json",
                "post_id": "p1",
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["text"] == "Exact approved text"
        assert preview.json()["account"] == {"id": "12345", "username": "founder"}
        token, request_id = preview.json()["preview_token"], str(uuid4())
        mcp_result = await server.call_tool(
            "publish_x_post",
            {
                "project_id": str(f.project.id),
                "preview_token": token,
                "request_id": request_id,
            },
        )
        assert "pending" in str(mcp_result)
        replay = await client.post(
            base + "/publish",
            json={
                "preview_token": token,
                "request_id": request_id,
            },
        )
        assert replay.status_code == 200, replay.text
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM workflow_runs WHERE project_id=$1 AND executor=$2",
                f.project.id,
                KEY,
            )
            == 1
        )
        approved = await f.db.pool.fetchval(
            "SELECT count(*) FROM effect_receipts WHERE execution_key LIKE 'x:approved:%'"
        )
        assert approved == 1


@pytest.mark.asyncio
async def test_exact_preview_rejects_changed_text_and_media_before_real_run_admission(
    publication_db, monkeypatch
):
    f, client, server = await setup(publication_db, monkeypatch)
    image = (Path(__file__).parent / "fixtures/x_media/image.png").read_bytes()
    attachments = [{"type": "image", "path": "social/x/media/image.png", "alt_text": "Demo"}]
    put(f, draft(attachments=attachments), media=image)
    base = f"/api/projects/{f.project.id}/x"
    async with client:
        first = await client.post(
            base + "/preview", json={"path": "social/x/draft.json", "post_id": "p1"}
        )
        assert first.status_code == 200, first.text
        put(f, draft(text="Updated text", attachments=attachments))
        stale_text = await client.post(
            base + "/publish",
            json={
                "preview_token": first.json()["preview_token"],
                "request_id": str(uuid4()),
            },
        )
        assert stale_text.status_code == 409
        second = await client.post(
            base + "/preview", json={"path": "social/x/draft.json", "post_id": "p1"}
        )
        assert second.status_code == 200, second.text
        modified = bytearray(image)
        modified[-20] ^= 1
        put(f, draft(text="Updated text", attachments=attachments), media=bytes(modified))
        stale_media = await client.post(
            base + "/publish",
            json={
                "preview_token": second.json()["preview_token"],
                "request_id": str(uuid4()),
            },
        )
        assert stale_media.status_code == 409
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM workflow_runs WHERE project_id=$1 AND executor=$2",
                f.project.id,
                KEY,
            )
            == 0
        )
        fresh = await client.post(
            base + "/preview", json={"path": "social/x/draft.json", "post_id": "p1"}
        )
        assert fresh.status_code == 200, fresh.text
        await server.call_tool(
            "publish_x_post",
            {
                "project_id": str(f.project.id),
                "preview_token": fresh.json()["preview_token"],
                "request_id": str(uuid4()),
            },
        )
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM workflow_runs WHERE project_id=$1 AND executor=$2",
                f.project.id,
                KEY,
            )
            == 1
        )
