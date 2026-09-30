"""Browser and MCP X controls share one project-bound service; media stays browser-only."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.integrations import X_PROVIDER, registered_integrations
from tin_lite.mcp_server import create_mcp_app
from tin_lite.x_posts import MAX_DRAFT_BYTES, XPosts, validate_draft

ORIGINAL_X_SAVE = XPosts.save


@pytest.fixture
def surface(monkeypatch):
    project = SimpleNamespace(id=uuid4(), state_repo_id="project-repo", canonical_branch="main")
    token = SimpleNamespace(subject="outsider", scopes=["openid"], client_id="mcp-client")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    db = SimpleNamespace(
        has_project_access=AsyncMock(side_effect=lambda **kw: kw["clerk_user_id"] == "member"),
        get_project=AsyncMock(return_value=project),
        record_mcp_usage=AsyncMock(),
        record_tin_user=AsyncMock(),
    )
    runtime = SimpleNamespace(
        database=db,
        storage=SimpleNamespace(),
        project_files=SimpleNamespace(
            upload=AsyncMock(
                return_value={
                    "path": "social/x/media/image.png",
                    "revision": "b" * 40,
                    "media_type": "image/png",
                    "bytes": 45,
                    "width": 1,
                    "height": 1,
                }
            )
        ),
    )
    settings = SimpleNamespace(
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.test",
    )
    server, _ = create_mcp_app(settings=settings, auth=SimpleNamespace(), runtime=lambda: runtime)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = runtime
    app.state.settings = settings
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=token.subject,
        token_type="session_token",  # noqa: S106 — synthetic token
    )
    mocks = {
        "read": AsyncMock(
            return_value={
                "path": "social/x/draft.json",
                "revision": "a" * 40,
                "draft": {"schema_version": "tin.social.x_draft.v1", "posts": []},
            }
        ),
        "save": AsyncMock(
            return_value={
                "path": "social/x/draft.json",
                "revision": "b" * 40,
                "draft": {"schema_version": "tin.social.x_draft.v1", "posts": []},
            }
        ),
        "preview": AsyncMock(
            return_value={
                "preview_token": str(uuid4()),
                "text": "Approved copy",
                "account": {"id": "123", "username": "tin"},
                "attachments": [],
            }
        ),
        "publish": AsyncMock(return_value={"id": str(uuid4()), "status": "pending"}),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(XPosts, name, mock)
    return SimpleNamespace(
        project=project, token=token, runtime=runtime, server=server, app=app, mocks=mocks
    )


@pytest.mark.asyncio
async def test_x_http_and_mcp_deny_outsider_before_draft_or_upload(surface):
    base = f"/api/projects/{surface.project.id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.app), base_url="https://tin.test"
    ) as client:
        response = await client.get(base + "/x/drafts", params={"path": "social/x/draft.json"})
        assert response.status_code == 404
        response = await client.post(
            base + "/files/upload",
            params={
                "path": "social/x/media/image.png",
                "expected_revision": "a" * 40,
                "request_id": str(uuid4()),
            },
            content=b"image",
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 404
    with pytest.raises(ToolError, match="project not found"):
        await surface.server.call_tool(
            "read_x_drafts", {"project_id": str(surface.project.id), "path": "social/x/draft.json"}
        )
    surface.mocks["read"].assert_not_awaited()
    surface.runtime.project_files.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_x_http_and_mcp_share_exact_draft_preview_publish_service(surface):
    surface.token.subject = "member"
    project_id = str(surface.project.id)
    base = f"/api/projects/{project_id}/x"
    path = "social/x/draft.json"
    request_id = str(uuid4())
    preview_token = str(uuid4())
    draft = {"schema_version": "tin.social.x_draft.v1", "account_id": "123", "posts": []}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.app), base_url="https://tin.test"
    ) as client:
        assert (await client.get(base + "/drafts", params={"path": path})).status_code == 200
        assert (
            await client.put(
                base + "/drafts",
                json={
                    "path": path,
                    "expected_revision": "a" * 40,
                    "request_id": request_id,
                    "draft": draft,
                },
            )
        ).status_code == 200
        assert (
            await client.post(base + "/preview", json={"path": path, "post_id": "p1"})
        ).status_code == 200
        assert (
            await client.post(
                base + "/publish",
                json={
                    "preview_token": preview_token,
                    "request_id": request_id,
                },
            )
        ).status_code == 200
    for name, args in (
        ("read_x_drafts", {"project_id": project_id, "path": path}),
        (
            "save_x_draft",
            {
                "project_id": project_id,
                "path": path,
                "expected_revision": "a" * 40,
                "request_id": request_id,
                "draft": draft,
            },
        ),
        ("preview_x_post", {"project_id": project_id, "path": path, "post_id": "p1"}),
        (
            "publish_x_post",
            {"project_id": project_id, "preview_token": preview_token, "request_id": request_id},
        ),
    ):
        await surface.server.call_tool(name, args)
    assert surface.mocks["read"].await_count == 2
    assert surface.mocks["save"].await_count == 2
    assert surface.mocks["preview"].await_count == 2
    assert surface.mocks["publish"].await_count == 2
    assert surface.mocks["publish"].await_args_list[0].kwargs["client_id"] == "browser"
    assert surface.mocks["publish"].await_args_list[1].kwargs["client_id"] == "mcp-client"
    result = await surface.server.call_tool(
        "prepare_x_media_upload",
        {
            "project_id": project_id,
            "path": path,
        },
    )
    assert "x_draft=social%2Fx%2Fdraft.json" in str(result)


@pytest.mark.asyncio
async def test_upload_stream_is_bounded_and_passes_raw_bytes_only_to_project_file_service(surface):
    surface.token.subject = "member"
    project_id = str(surface.project.id)
    params = {
        "path": "social/x/media/image.png",
        "expected_revision": "a" * 40,
        "request_id": str(uuid4()),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.app), base_url="https://tin.test"
    ) as client:
        wrong_type = await client.post(
            f"/api/projects/{project_id}/files/upload",
            params=params,
            content=b"bytes",
            headers={"Content-Type": "text/plain"},
        )
        assert wrong_type.status_code == 422
        oversized = await client.post(
            f"/api/projects/{project_id}/files/upload",
            params=params,
            content=b"x" * 5_000_001,
            headers={"Content-Type": "image/png"},
        )
        assert oversized.status_code == 413
        surface.runtime.project_files.upload.assert_not_awaited()
        accepted = await client.post(
            f"/api/projects/{project_id}/files/upload",
            params=params,
            content=b"raw image bytes",
            headers={"Content-Type": "image/png"},
        )
        assert accepted.status_code == 200
    sent = surface.runtime.project_files.upload.await_args.kwargs
    assert sent["content"] == b"raw image bytes"
    assert sent["project"].id == surface.project.id
    assert sent["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_x_callback_checks_pending_project_membership_before_exchanging_code(surface):
    connection = SimpleNamespace(
        id=uuid4(),
        project_id=surface.project.id,
        provider_key=X_PROVIDER,
        status="connected",
        external_account_label="@tin",
        configuration={"granted_capabilities": ["x.posts.read"]},
        created_at=None,
        last_checked_at=None,
        last_error_code=None,
    )
    provider = SimpleNamespace(
        pending_project=AsyncMock(return_value=surface.project.id),
        complete=AsyncMock(return_value=connection),
    )
    definition = next(item for item in registered_integrations() if item.key == X_PROVIDER)
    surface.runtime.integrations = SimpleNamespace(
        x=provider,
        _definition=lambda key: definition,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.app), base_url="https://tin.test"
    ) as client:
        outsider = await client.post(
            "/api/integrations/x/complete", json={"state": "bound", "code": "one-use"}
        )
        assert outsider.status_code == 404
        provider.complete.assert_not_awaited()
        surface.token.subject = "member"
        accepted = await client.post(
            "/api/integrations/x/complete", json={"state": "bound", "code": "one-use"}
        )
        assert accepted.status_code == 200
        assert accepted.json()["external_account_label"] == "@tin"
        provider.complete.assert_awaited_once_with(
            state="bound", code="one-use", clerk_user_id="member"
        )


@pytest.mark.asyncio
async def test_saved_json_size_must_fit_the_same_bounded_read_as_generated_drafts(surface):
    surface.token.subject = "member"
    draft = {
        "schema_version": "tin.social.x_draft.v1",
        "account_id": "",
        "posts": [
            {
                "id": "p1",
                "text": "One",
                "readiness": "ready",
                "support": [
                    {"source_path": "notes/a.md", "excerpt": "x" * 3900} for _ in range(16)
                ],
                "editor_notes": "",
                "attachments": [],
                "missing_assets": [],
            }
        ],
    }
    # The normalized object fits, but indentation used for the persisted file does not.
    validate_draft(draft)
    import json

    assert len(json.dumps(draft, ensure_ascii=False, indent=2).encode()) > MAX_DRAFT_BYTES
    surface.runtime.project_files.commit = AsyncMock()
    with pytest.raises(ValueError, match="too large to save and read back"):
        await ORIGINAL_X_SAVE(
            XPosts(surface.runtime, surface.app.state.settings),
            surface.project.id,
            "member",
            path="social/x/draft.json",
            expected_revision="a" * 40,
            request_id=uuid4(),
            draft=draft,
        )
    surface.runtime.project_files.commit.assert_not_awaited()
