"""Following, unfollowing and Lists: the connected X account acts only for the member who
connected it, one account at a time, over MCP."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_integrations import PROJECT_ID, USER_ID
from test_x_bookmarks_and_likes import feed_body
from test_x_connection import X, connect, service

from tin_lite import x_connection
from tin_lite.integrations import (
    X_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationDeliveryUnknownError,
    IntegrationInputError,
    registered_integrations,
)
from tin_lite.mcp_server import create_mcp_app

ACCOUNTS = {"builder": "703", "Quiet_One": "704", "maker": "702"}


class SocialX(X):
    """X answering account lookups, follows and Lists."""

    def __init__(self):
        super().__init__()
        self.following: set[str] = set()
        self.members: set[str] = set()
        self.refuse_follow: str | None = None
        self.follow_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        me = f"/2/users/{self.account}"
        if path.startswith("/2/users/by/username/"):
            self.calls.append(request)
            name = path.rsplit("/", 1)[1]
            match = next((k for k in ACCOUNTS if k.lower() == name.lower()), None)
            if match is None:
                return self.reply(200, {"errors": [{"title": "Not Found Error"}]})
            data = {"id": ACCOUNTS[match], "username": match, "protected": match == "Quiet_One"}
            return self.reply(200, {"data": data})
        if path == f"{me}/following" and method == "POST":
            self.calls.append(request)
            target = json.loads(request.content)["target_user_id"]
            if target == self.refuse_follow:
                return self.reply(403, {"title": "Forbidden", "type": "about:blank"})
            if self.follow_status != 200:
                return self.reply(self.follow_status, {})
            pending = target == ACCOUNTS["Quiet_One"]
            if not pending:
                self.following.add(target)
            return self.reply(200, {"data": {"following": not pending, "pending_follow": pending}})
        if path.startswith(f"{me}/following/") and method == "DELETE":
            self.calls.append(request)
            self.following.discard(path.rsplit("/", 1)[1])
            return self.reply(200, {"data": {"following": False}})
        if path == "/2/lists" and method == "POST":
            self.calls.append(request)
            body = json.loads(request.content)
            return self.reply(201, {"data": {"id": "1890001", "name": body["name"]}})
        if path == f"{me}/owned_lists":
            self.calls.append(request)
            row = {"id": "1890001", "name": "AI agents", "private": True, "member_count": 2}
            return self.reply(200, {"data": [row], "meta": {"result_count": 1}})
        if path == "/2/lists/1890001/members" and method == "POST":
            self.calls.append(request)
            self.members.add(json.loads(request.content)["user_id"])
            return self.reply(200, {"data": {"is_member": True}})
        if path.startswith("/2/lists/1890001/members/") and method == "DELETE":
            self.calls.append(request)
            self.members.discard(path.rsplit("/", 1)[1])
            return self.reply(200, {"data": {"is_member": False}})
        if path in {"/2/lists/1890001", "/2/lists/1890002"} and method == "GET":
            self.calls.append(request)
            owner = self.account if path.endswith("1") else "999"
            data = {"id": path.rsplit("/", 1)[1], "owner_id": owner}
            if path.endswith("1"):
                data["member_count"] = 42
            return self.reply(200, {"data": data})
        if path == "/2/lists/1890003" and method == "GET":
            self.calls.append(request)
            return self.reply(200, {"errors": [{"title": "Not Found Error"}]})
        if path == "/2/lists/1890001/tweets":
            self.calls.append(request)
            return self.reply(200, feed_body())
        return super().__call__(request)


def writes(api):
    return [r for r in api.calls if r.method in {"POST", "DELETE"} and "oauth2" not in str(r.url)]


def test_follows_and_lists_come_with_every_connection_and_stay_off_the_gateway() -> None:
    definition = next(d for d in registered_integrations() if d.key == X_PROVIDER)
    assert {"x.follows.write", "x.lists.manage"} <= set(definition.capabilities)
    assert x_connection.CAPABILITY_SCOPES["x.follows.write"] >= {"follows.write"}
    assert x_connection.CAPABILITY_SCOPES["x.lists.manage"] >= {"list.read", "list.write"}
    assert all("follow" not in name and "list" not in name for name in x_connection.OPERATIONS)


async def test_connecting_asks_x_for_follows_and_lists() -> None:
    integrations, _, api = await service(SocialX())
    connection, query = await connect(integrations, api)
    assert {"follows.write", "list.read", "list.write"} <= set(query["scope"][0].split())
    assert {"x.follows.write", "x.lists.manage"} <= set(
        connection.configuration["granted_capabilities"]
    )
    await integrations.close()


async def test_follow_and_unfollow_as_the_member_who_connected() -> None:
    integrations, db, api = await service(SocialX())
    await connect(integrations, api)
    with pytest.raises(IntegrationAuthorizationError, match="act as its account"):
        await integrations.x.follow(PROJECT_ID, "user_Teammate456", username="builder")
    with pytest.raises(IntegrationInputError, match="username"):
        await integrations.x.follow(PROJECT_ID, USER_ID, username="not a handle")
    with pytest.raises(IntegrationInputError, match="no account @ghost"):
        await integrations.x.follow(PROJECT_ID, USER_ID, username="@ghost")
    assert writes(api) == []

    followed = await integrations.x.follow(PROJECT_ID, USER_ID, username="@Builder")
    assert followed == {"username": "builder", "following": True, "pending": False}
    (request,) = writes(api)
    assert request.url.path == "/2/users/12345/following"
    assert json.loads(request.content) == {"target_user_id": "703"}
    # A protected account gets a follow request.
    asked = await integrations.x.follow(PROJECT_ID, USER_ID, username="quiet_one")
    assert asked == {"username": "Quiet_One", "following": False, "pending": True}

    gone = await integrations.x.unfollow(PROJECT_ID, USER_ID, username="builder")
    assert gone == {"username": "builder", "following": False}
    assert writes(api)[-1].url.path == "/2/users/12345/following/703"
    assert api.following == set()

    # A connection made before connecting asked for everything reconnects first.
    db.connections[(PROJECT_ID, X_PROVIDER)].configuration["granted_capabilities"] = [
        "x.posts.read"
    ]
    with pytest.raises(IntegrationAuthorizationError, match="manage follows and Lists"):
        await integrations.x.follow(PROJECT_ID, USER_ID, username="builder")
    await integrations.close()


async def test_a_lost_follow_result_is_uncertain_not_retried() -> None:
    integrations, _, api = await service(SocialX())
    await connect(integrations, api)
    api.follow_status = 503
    with pytest.raises(IntegrationDeliveryUnknownError):
        await integrations.x.follow(PROJECT_ID, USER_ID, username="builder")
    assert len(writes(api)) == 1
    await integrations.close()


async def test_create_a_list_add_and_remove_members_and_read_it() -> None:
    integrations, _, api = await service(SocialX())
    await connect(integrations, api)
    with pytest.raises(IntegrationInputError, match="1-25 characters"):
        await integrations.x.create_list(PROJECT_ID, USER_ID, name="x" * 26)
    with pytest.raises(IntegrationInputError, match="at most 100"):
        await integrations.x.create_list(PROJECT_ID, USER_ID, name="AI", description="d" * 101)
    with pytest.raises(IntegrationAuthorizationError, match="act as its account"):
        await integrations.x.create_list(PROJECT_ID, "user_Teammate456", name="AI agents")
    assert writes(api) == []

    made = await integrations.x.create_list(
        PROJECT_ID, USER_ID, name=" AI agents ", description="Agent builders", private=True
    )
    assert made == {
        "list_id": "1890001",
        "name": "AI agents",
        "private": True,
        "url": "https://x.com/i/lists/1890001",
    }
    assert json.loads(writes(api)[-1].content) == {
        "name": "AI agents",
        "private": True,
        "description": "Agent builders",
    }
    owned = await integrations.x.owned_lists(PROJECT_ID, USER_ID)
    assert owned["lists"][0]["list_id"] == "1890001"
    assert owned["lists"][0]["member_count"] == 2

    added = await integrations.x.list_member(
        PROJECT_ID, USER_ID, list_id="1890001", username="maker", add=True
    )
    assert added == {"list_id": "1890001", "username": "maker", "is_member": True}
    assert api.members == {"702"}
    removed = await integrations.x.list_member(
        PROJECT_ID, USER_ID, list_id="1890001", username="maker", add=False
    )
    assert removed["is_member"] is False and api.members == set()
    with pytest.raises(IntegrationInputError, match="List ID"):
        await integrations.x.list_member(
            PROJECT_ID, USER_ID, list_id="../lists", username="maker", add=True
        )

    page = await integrations.x.list_posts(PROJECT_ID, USER_ID, list_id="1890001")
    assert page["list_id"] == "1890001"
    assert page["posts"][0]["reposted_by"]["username"] == "friend"
    assert api.calls[-1].url.params["max_results"] == "25"
    await integrations.close()


async def test_list_owner_says_whether_the_connected_account_owns_a_list() -> None:
    """intel.daily reads a client's List through Tin's own X only once X says Tin owns it."""
    from tin_lite.integrations import IntegrationUpstreamError

    integrations, _, api = await service(SocialX())
    await connect(integrations, api)
    mine = await integrations.x.list_owner(PROJECT_ID, USER_ID, list_id="1890001")
    assert mine == {
        "list_id": "1890001",
        "owner_id": api.account,
        "owned": True,
        "member_count": 42,
    }
    assert api.calls[-1].url.params["list.fields"] == "owner_id,member_count"
    theirs = await integrations.x.list_owner(PROJECT_ID, USER_ID, list_id="1890002")
    assert theirs["owned"] is False and theirs["member_count"] is None
    with pytest.raises(IntegrationUpstreamError, match="incomplete result"):
        await integrations.x.list_owner(PROJECT_ID, USER_ID, list_id="1890003")
    with pytest.raises(IntegrationAuthorizationError, match="act as its account|connected"):
        await integrations.x.list_owner(PROJECT_ID, "user_Teammate456", list_id="1890001")
    with pytest.raises(IntegrationInputError, match="List ID"):
        await integrations.x.list_owner(PROJECT_ID, USER_ID, list_id="../lists")
    assert writes(api) == []
    await integrations.close()


@pytest.fixture
def mcp(monkeypatch):
    project = SimpleNamespace(id=uuid4(), state_repo_id="project-repo", canonical_branch="main")
    token = SimpleNamespace(subject="member", scopes=["openid"], client_id="mcp-client")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    db = SimpleNamespace(
        has_project_access=AsyncMock(side_effect=lambda **kw: kw["clerk_user_id"] == "member"),
        get_project=AsyncMock(return_value=project),
        record_mcp_usage=AsyncMock(),
        record_tin_user=AsyncMock(),
    )

    async def follow(project_id, member, *, username):
        if username == "refuses":
            raise IntegrationAuthorizationError("X rejected this access")
        return {"username": username, "following": True, "pending": False}

    x = SimpleNamespace(
        follow=AsyncMock(side_effect=follow),
        list_member=AsyncMock(return_value={"is_member": True}),
        create_list=AsyncMock(return_value={"list_id": "1890001"}),
    )
    runtime = SimpleNamespace(database=db, integrations=SimpleNamespace(x=x))
    settings = SimpleNamespace(
        switchboard_public_url="https://tin.test", clerk_frontend_api_url="https://clerk.test"
    )
    server, _ = create_mcp_app(settings=settings, auth=SimpleNamespace(), runtime=lambda: runtime)
    return SimpleNamespace(project=project, token=token, server=server, x=x)


def structured(result):
    return result[1] if isinstance(result, tuple) else result.structured_content


async def test_mcp_follows_one_at_a_time_and_stops_at_the_first_refusal(mcp) -> None:
    arguments = {"project_id": str(mcp.project.id), "usernames": ["a", "refuses", "c"]}
    result = structured(await mcp.server.call_tool("follow_x_accounts", arguments))
    assert [row["username"] for row in result["done"]] == ["a"]
    assert result["stopped_at"] == "refuses"
    assert result["reason"] == "X rejected this access"
    assert result["not_tried"] == ["c"]
    assert mcp.x.follow.await_count == 2

    with pytest.raises(ToolError, match="1-50"):
        await mcp.server.call_tool(
            "follow_x_accounts", {"project_id": str(mcp.project.id), "usernames": ["a"] * 51}
        )
    mcp.token.subject = "outsider"
    with pytest.raises(ToolError, match="project not found"):
        await mcp.server.call_tool("follow_x_accounts", arguments)
    assert mcp.x.follow.await_count == 2


async def test_mcp_adds_list_members_and_creates_a_list(mcp) -> None:
    project_id = str(mcp.project.id)
    result = structured(
        await mcp.server.call_tool(
            "add_x_list_members",
            {"project_id": project_id, "list_id": "1890001", "usernames": ["a", "b"]},
        )
    )
    assert len(result["done"]) == 2 and result["stopped_at"] is None
    mcp.x.list_member.assert_awaited_with(
        mcp.project.id, "member", list_id="1890001", username="b", add=True
    )
    await mcp.server.call_tool("create_x_list", {"project_id": project_id, "name": "AI agents"})
    mcp.x.create_list.assert_awaited_once_with(
        mcp.project.id, "member", name="AI agents", description=None, private=False
    )
