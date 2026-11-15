"""The connected X account's bookmarks: their own consent, one member's, read over MCP."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_integrations import PROJECT_ID, USER_ID
from test_x_connection import X, connect, service

from tin_lite import x_connection
from tin_lite.integrations import (
    X_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationInputError,
    registered_integrations,
)
from tin_lite.mcp_server import create_mcp_app

CURSOR = "7140dibdnow9c7btw4b7hkk1zkpfu0ko2rb2h6b3uq2u"


def bookmarks_body():
    return {
        "data": [
            {
                "id": "3001",
                "text": "How we priced our first product https://t.co/a",
                "created_at": "2026-11-10T08:00:00.000Z",
                "author_id": "601",
                "note_post": {
                    "text": "How we priced our first product, and what we'd change. " * 8,
                    "entities": {"urls": [{"expanded_url": "https://maker.example/pricing"}]},
                },
                "attachments": {"media_keys": ["3_10"]},
                "referenced_posts": [{"type": "quoted", "id": "2999"}],
            },
            {"id": "3002", "text": "Followers only", "author_id": "602"},
            {
                "id": "3003",
                "text": "Quoting a post that is gone",
                "author_id": "601",
                "referenced_posts": [{"type": "quoted", "id": "2000"}],
            },
        ],
        "includes": {
            "users": [
                {"id": "601", "username": "maker", "name": "Maker", "protected": False},
                {"id": "602", "username": "quiet_one", "name": "Quiet", "protected": True},
                {"id": "603", "username": "source", "name": "Source", "protected": False},
            ],
            "media": [
                {
                    "media_key": "3_10",
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/pricing.jpg",
                    "width": 1200,
                    "height": 800,
                }
            ],
            "posts": [
                {
                    "id": "2999",
                    "text": "Pricing is a product decision.",
                    "created_at": "2026-11-01T12:00:00.000Z",
                    "author_id": "603",
                }
            ],
        },
        "errors": [
            {
                "title": "Not Found Error",
                "type": "https://api.x.com/2/problems/resource-not-found",
                "resource_type": "post",
                "resource_id": "2000",
            }
        ],
        "meta": {"result_count": 3, "next_token": CURSOR},
    }


class BookmarksX(X):
    def __init__(self):
        super().__init__()
        self.bookmarks_status = 200
        self.bookmarks = bookmarks_body()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/2/users/{self.account}/bookmarks":
            self.calls.append(request)
            if self.bookmarks_status != 200:
                return self.reply(
                    self.bookmarks_status,
                    {"title": "Client Forbidden", "type": "about:blank"},
                )
            return self.reply(200, self.bookmarks)
        return super().__call__(request)


def bookmark_reads(api):
    return [r for r in api.calls if r.url.path.endswith("/bookmarks")]


def test_bookmarks_are_their_own_capability() -> None:
    definition = next(d for d in registered_integrations() if d.key == X_PROVIDER)
    assert "x.bookmarks.read" in definition.capabilities
    assert x_connection.CAPABILITY_SCOPES["x.bookmarks.read"] == {
        "bookmark.read",
        "tweet.read",
        "users.read",
    }
    # Never inferred from scopes alone, and never a gateway operation for workflow code.
    scopes = {"bookmark.read", "tweet.read", "users.read"}
    assert "x.bookmarks.read" not in x_connection.capabilities_for(scopes)
    assert "x.bookmarks.read" in x_connection.capabilities_for(scopes, ["x.bookmarks.read"])
    assert all("bookmark" not in name for name in x_connection.OPERATIONS)


async def test_bookmarks_need_their_own_consent_and_reconnecting_keeps_it() -> None:
    integrations, _, api = await service(BookmarksX())
    first, query = await connect(integrations, api)
    assert "bookmark.read" not in query["scope"][0].split()
    with pytest.raises(IntegrationAuthorizationError, match="Allow bookmarks"):
        await integrations.x.bookmarks(PROJECT_ID, USER_ID)
    assert bookmark_reads(api) == []

    allowed, allowed_query = await connect(integrations, api, capabilities=("x.bookmarks.read",))
    assert "bookmark.read" in allowed_query["scope"][0].split()
    assert allowed.configuration["granted_capabilities"] == ["x.posts.read", "x.bookmarks.read"]
    upgraded, _ = await connect(integrations, api, capabilities=("x.posts.publish",))
    assert set(upgraded.configuration["granted_capabilities"]) == {
        "x.posts.read",
        "x.posts.publish",
        "x.bookmarks.read",
    }
    await integrations.close()


async def test_only_the_member_who_connected_reads_the_bookmarks() -> None:
    integrations, _, api = await service(BookmarksX())
    await connect(integrations, api, capabilities=("x.bookmarks.read",))
    with pytest.raises(IntegrationAuthorizationError, match="Only the member who connected X"):
        await integrations.x.bookmarks(PROJECT_ID, "user_Teammate456")
    assert bookmark_reads(api) == []
    await integrations.close()


async def test_a_page_of_bookmarks_with_quoted_posts_and_protected_ones_counted() -> None:
    integrations, _, api = await service(BookmarksX())
    await connect(integrations, api, capabilities=("x.bookmarks.read",))
    page = await integrations.x.bookmarks(
        PROJECT_ID,
        USER_ID,
        max_results=3,
        pagination_token="abc123",  # noqa: S106 — X's page cursor, not a credential
    )
    (request,) = bookmark_reads(api)
    assert request.url.path == "/2/users/12345/bookmarks"
    assert dict(request.url.params) == {
        "max_results": "3",
        "pagination_token": "abc123",
        "post.fields": "created_at,public_metrics,entities,attachments,note_post",
        "expansions": "author_id,attachments.media_keys,referenced_posts",
        "user.fields": "protected",
        "media.fields": "type,url,preview_image_url,width,height",
    }
    assert page["account"] == {"id": "12345", "username": "founder"}
    assert page["next_token"] == CURSOR
    assert page["left_out_protected"] == 1
    first, gone = page["posts"]
    assert first["text"].startswith("How we priced our first product, and what we'd change.")
    assert first["author"] == {"id": "601", "username": "maker", "name": "Maker"}
    assert first["url"] == "https://x.com/i/web/status/3001"
    assert [link["expanded_url"] for link in first["urls"]] == ["https://maker.example/pricing"]
    assert first["media"][0]["url"] == "https://pbs.twimg.com/media/pricing.jpg"
    assert first["quoted"]["text"] == "Pricing is a product decision."
    assert first["quoted"]["author"]["username"] == "source"
    # X couldn't return the quoted post; the bookmark stays.
    assert gone["id"] == "3003" and gone["quoted"] is None

    api.bookmarks = {"meta": {"result_count": 0}}
    empty = await integrations.x.bookmarks(PROJECT_ID, USER_ID)
    assert empty["posts"] == [] and empty["next_token"] is None
    assert bookmark_reads(api)[-1].url.params["max_results"] == "25"
    await integrations.close()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"max_results": 0}, "from 1 to 100"),
        ({"max_results": 101}, "from 1 to 100"),
        ({"max_results": "10"}, "from 1 to 100"),
        ({"pagination_token": "a b"}, "pagination_token"),
        ({"pagination_token": "../elsewhere"}, "pagination_token"),
    ],
)
async def test_malformed_arguments_fail_before_x_is_asked(arguments, message) -> None:
    integrations, _, api = await service(BookmarksX())
    await connect(integrations, api, capabilities=("x.bookmarks.read",))
    with pytest.raises(IntegrationInputError, match=message):
        await integrations.x.bookmarks(PROJECT_ID, USER_ID, **arguments)
    assert bookmark_reads(api) == []
    await integrations.close()


async def test_x_refusing_the_read_names_it() -> None:
    integrations, _, api = await service(BookmarksX())
    await connect(integrations, api, capabilities=("x.bookmarks.read",))
    api.bookmarks_status = 403
    with pytest.raises(IntegrationAuthorizationError, match="X rejected this access"):
        await integrations.x.bookmarks(PROJECT_ID, USER_ID)
    await integrations.close()


@pytest.fixture
def mcp(monkeypatch):
    project = SimpleNamespace(id=uuid4(), state_repo_id="project-repo", canonical_branch="main")
    token = SimpleNamespace(subject="outsider", scopes=["openid"], client_id="mcp-client")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    db = SimpleNamespace(
        has_project_access=AsyncMock(side_effect=lambda **kw: kw["clerk_user_id"] == "member"),
        get_project=AsyncMock(return_value=project),
        record_mcp_usage=AsyncMock(),
        record_tin_user=AsyncMock(),
    )
    read = AsyncMock(return_value={"account": {"id": "1"}, "posts": [], "next_token": None})
    runtime = SimpleNamespace(
        database=db, integrations=SimpleNamespace(x=SimpleNamespace(bookmarks=read))
    )
    settings = SimpleNamespace(
        switchboard_public_url="https://tin.test", clerk_frontend_api_url="https://clerk.test"
    )
    server, _ = create_mcp_app(settings=settings, auth=SimpleNamespace(), runtime=lambda: runtime)
    return SimpleNamespace(project=project, token=token, server=server, read=read)


async def test_mcp_reads_a_page_for_a_member_and_refuses_an_outsider(mcp) -> None:
    arguments = {"project_id": str(mcp.project.id), "max_results": 10}
    with pytest.raises(ToolError, match="project not found"):
        await mcp.server.call_tool("read_x_bookmarks", arguments)
    mcp.read.assert_not_awaited()

    mcp.token.subject = "member"
    result = await mcp.server.call_tool("read_x_bookmarks", arguments)
    assert '"posts": []' in str(result) or "'posts': []" in str(result)
    mcp.read.assert_awaited_once_with(
        mcp.project.id, "member", max_results=10, pagination_token=None
    )
    mcp.read.side_effect = IntegrationAuthorizationError(x_connection.NOT_YOUR_BOOKMARKS)
    with pytest.raises(ToolError, match="Only the member who connected X"):
        await mcp.server.call_tool("read_x_bookmarks", arguments)
