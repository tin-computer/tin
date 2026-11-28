"""One public X account's posts over MCP: read_x_user_posts through the project's X connection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_integrations import PROJECT_ID
from test_x_connection import X, connect, service

from tin_lite import x_connection
from tin_lite.integrations import (
    X_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationInputError,
    ServiceCallRefused,
)
from tin_lite.mcp_server import create_mcp_app

CURSOR = "7140dibdnow9c7btw4b7hkk1zkpfu0ko2rb2h6b3uq2u"
LONG = "What a year of research taught us about evaluation, in one thread. " * 6


def posts_body():
    return {
        "data": [
            {
                "id": "5001",
                "text": "What a year of research taught us… https://t.co/a",
                "created_at": "2026-11-20T09:00:00.000Z",
                "author_id": "777",
                "conversation_id": "5001",
                "note_post": {
                    "text": LONG,
                    "entities": {"urls": [{"expanded_url": "https://lab.example/evals"}]},
                },
                "attachments": {"media_keys": ["3_50"]},
                "public_metrics": {
                    "retweet_count": 40,
                    "repost_count": 40,
                    "reply_count": 12,
                    "like_count": 900,
                    "quote_count": 7,
                    "bookmark_count": 300,
                    "impression_count": 120000,
                },
            },
            {
                "id": "5002",
                "text": "And the second part.",
                "author_id": "777",
                "conversation_id": "5001",
                "referenced_posts": [{"type": "quoted", "id": "4900"}],
            },
            {
                "id": "5003",
                "text": "RT @peer: Benchmarks are a product decision.",
                "author_id": "777",
                "conversation_id": "5003",
                "referenced_posts": [{"type": "retweeted", "id": "4800"}],
            },
            {
                "id": "5004",
                "text": "RT @quiet_one: followers only",
                "author_id": "777",
                "referenced_posts": [{"type": "retweeted", "id": "4700"}],
            },
            {
                "id": "5005",
                "text": "https://t.co/article",
                "author_id": "777",
                "conversation_id": "5005",
                "article": {
                    "title": "Notes on evaluation",
                    "preview_text": "Why we stopped trusting leaderboards.",
                    "plain_text": "Why we stopped trusting leaderboards. " * 20,
                },
            },
        ],
        "includes": {
            "users": [
                {"id": "777", "username": "studio", "name": "Studio", "protected": False},
                {"id": "801", "username": "peer", "name": "Peer", "protected": False},
                {"id": "802", "username": "quiet_one", "name": "Quiet", "protected": True},
                {"id": "803", "username": "source", "name": "Source", "protected": False},
            ],
            "media": [
                {
                    "media_key": "3_50",
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/evals.jpg",
                    "width": 1600,
                    "height": 900,
                }
            ],
            "posts": [
                {
                    "id": "4900",
                    "text": "The original finding.",
                    "author_id": "803",
                    "conversation_id": "4900",
                },
                {
                    "id": "4800",
                    "text": "Benchmarks are a product decision. " * 4,
                    "author_id": "801",
                    "conversation_id": "4800",
                    "public_metrics": {"like_count": 51},
                },
                {"id": "4700", "text": "followers only", "author_id": "802"},
            ],
        },
        "meta": {
            "result_count": 5,
            "newest_id": "5005",
            "oldest_id": "5001",
            "next_token": CURSOR,
        },
    }


def account(username: str) -> dict:
    return {
        "id": "777",
        "username": username,
        "name": "Studio",
        "protected": username == "quiet_one",
        "verified": True,
        "created_at": "2015-03-01T00:00:00.000Z",
        "description": "We study how models are evaluated.",
        "public_metrics": {
            "followers_count": 52000,
            "following_count": 310,
            "tweet_count": 4100,
            "listed_count": 800,
            "like_count": 9000,
            "media_count": 420,
            "bogus": "x",
        },
    }


class AccountX(X):
    """The OAuth fixture plus username lookup and one account's posts."""

    def __init__(self):
        super().__init__()
        self.posts = posts_body()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/2/users/by/username/"):
            self.calls.append(request)
            username = path.rsplit("/", 1)[-1]
            if username in {"nobody", "suspended"}:
                kind = "resource-not-found" if username == "nobody" else "resource-unavailable"
                return self.reply(
                    200,
                    {
                        "errors": [
                            {
                                "detail": f"Could not find user with username: [{username}].",
                                "title": "Not Found Error",
                                "type": f"https://api.twitter.com/2/problems/{kind}",
                            }
                        ]
                    },
                )
            return self.reply(200, {"data": account(username)})
        if path == "/2/users/777/tweets":
            self.calls.append(request)
            return self.reply(200, self.posts)
        return super().__call__(request)


def lookups(api):
    return [r for r in api.calls if r.url.path.startswith("/2/users/by/username/")]


def post_reads(api):
    return [r for r in api.calls if r.url.path == "/2/users/777/tweets"]


async def connected():
    integrations, db, api = await service(AccountX())
    await connect(integrations, api)
    return integrations, db, api


async def test_a_page_of_one_accounts_posts_with_threads_reposts_quotes_and_articles() -> None:
    integrations, _, api = await connected()
    page = await integrations.x.account_posts(PROJECT_ID, username=" @studio ")
    (lookup,) = lookups(api)
    assert lookup.url.path == "/2/users/by/username/studio"
    assert dict(lookup.url.params) == {
        "user.fields": "created_at,description,protected,public_metrics,verified"
    }
    (request,) = post_reads(api)
    assert dict(request.url.params) == {
        "max_results": "25",
        "post.fields": (
            "created_at,public_metrics,entities,attachments,note_post,conversation_id,article"
        ),
        "expansions": "author_id,attachments.media_keys,referenced_posts",
        "user.fields": "protected",
        "media.fields": "type,url,preview_image_url,width,height",
    }
    assert page["user"] == {
        "id": "777",
        "username": "studio",
        "name": "Studio",
        "description": "We study how models are evaluated.",
        "verified": True,
        "created_at": "2015-03-01T00:00:00.000Z",
        "public_metrics": {
            "followers_count": 52000,
            "following_count": 310,
            "tweet_count": 4100,
            "listed_count": 800,
            "like_count": 9000,
            "media_count": 420,
        },
    }
    assert page["next_token"] == CURSOR
    assert page["meta"] == {"result_count": 5, "newest_id": "5005", "oldest_id": "5001"}
    assert page["left_out_protected"] == 1

    thread, second, repost, article = page["posts"]
    # The long form, not X's 280-character cut.
    assert thread["text"] == LONG
    assert thread["conversation_id"] == second["conversation_id"] == "5001"
    assert thread["public_metrics"] == {
        "repost_count": 40,
        "reply_count": 12,
        "like_count": 900,
        "quote_count": 7,
        "bookmark_count": 300,
        "impression_count": 120000,
    }
    assert [link["expanded_url"] for link in thread["urls"]] == ["https://lab.example/evals"]
    assert thread["media"][0]["url"] == "https://pbs.twimg.com/media/evals.jpg"
    assert thread["author"] == {"id": "777", "username": "studio", "name": "Studio"}
    assert thread["quoted"] is None and thread["reposted_by"] is None and thread["article"] is None
    # A quote keeps the quoted post with its author and text.
    assert second["quoted"]["text"] == "The original finding."
    assert second["quoted"]["author"]["username"] == "source"
    assert second["quoted"]["conversation_id"] == "4900"
    # A repost reads as the full post it reposts, with who reposted it.
    assert repost["id"] == "4800" and repost["text"].startswith("Benchmarks are")
    assert repost["author"]["username"] == "peer"
    assert repost["reposted_by"] == {"id": "777", "username": "studio", "name": "Studio"}
    assert repost["conversation_id"] == "4800"
    assert repost["public_metrics"] == {"like_count": 51}
    assert article["article"]["title"] == "Notes on evaluation"
    assert article["article"]["preview"] == "Why we stopped trusting leaderboards."
    assert article["article"]["text"].startswith("Why we stopped trusting leaderboards.")
    await integrations.close()


async def test_window_since_cursor_and_exclusions_pass_through() -> None:
    integrations, _, api = await connected()
    await integrations.x.account_posts(
        PROJECT_ID,
        username="studio",
        max_results=5,
        start_time="2025-11-27T00:00:00Z",
        end_time="2026-11-27T00:00:00Z",
        since_id="4000",
        exclude_reposts=True,
        pagination_token=CURSOR,
    )
    params = post_reads(api)[-1].url.params
    assert params["max_results"] == "5"
    assert params["start_time"] == "2025-11-27T00:00:00Z"
    assert params["end_time"] == "2026-11-27T00:00:00Z"
    assert params["since_id"] == "4000"
    assert params["pagination_token"] == CURSOR
    assert params["exclude"] == "retweets"

    for replies, reposts, expected in (
        (True, True, "replies,retweets"),
        (True, False, "replies"),
        (False, False, None),
    ):
        await integrations.x.account_posts(
            PROJECT_ID,
            username="studio",
            max_results=100,
            exclude_replies=replies,
            exclude_reposts=reposts,
        )
        assert post_reads(api)[-1].url.params.get("exclude") == expected
    assert post_reads(api)[-1].url.params["max_results"] == "100"
    await integrations.close()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"username": "studio x"}, "1-15 letters"),
        ({"username": "https://x.com/studio"}, "1-15 letters"),
        ({"username": "@"}, "1-15 letters"),
        ({"username": None}, "1-15 letters"),
        ({"max_results": 4}, "from 5 to 100"),
        ({"max_results": 101}, "from 5 to 100"),
        ({"max_results": "10"}, "from 5 to 100"),
        ({"start_time": "2025-11-27"}, "UTC time"),
        ({"start_time": "2026-11-27T00:00:00Z", "end_time": "2025-11-27T00:00:00Z"}, "before"),
        ({"since_id": "12ab"}, "since_id"),
        ({"exclude_reposts": "yes"}, "true or false"),
        ({"pagination_token": "../elsewhere"}, "pagination_token"),
    ],
)
async def test_malformed_arguments_fail_before_x_is_asked(arguments, message) -> None:
    integrations, _, api = await connected()
    with pytest.raises(IntegrationInputError, match=message):
        await integrations.x.account_posts(PROJECT_ID, **{"username": "studio", **arguments})
    assert lookups(api) == [] and post_reads(api) == []
    await integrations.close()


@pytest.mark.parametrize(
    ("username", "code", "message"),
    [
        ("nobody", "not_found", "found no account @nobody"),
        ("suspended", "account_unavailable", "suspended or unavailable"),
        ("quiet_one", "protected_account", "protects its posts"),
    ],
)
async def test_missing_suspended_and_protected_accounts_are_named(username, code, message) -> None:
    integrations, _, api = await connected()
    with pytest.raises(ServiceCallRefused, match=message) as refused:
        await integrations.x.account_posts(PROJECT_ID, username=username)
    assert refused.value.code == code
    # Only the lookup was read; no posts were requested.
    assert len(lookups(api)) == 1 and post_reads(api) == []
    await integrations.close()


async def test_any_member_reads_and_an_older_connection_reconnects_first() -> None:
    integrations, db, api = await connected()
    # Public posts aren't one member's: no clerk user is passed, and nothing checks who connected.
    await integrations.x.account_posts(PROJECT_ID, username="studio")
    assert len(post_reads(api)) == 1

    # A connection made before connecting asked for public reads.
    stored = db.connections[(PROJECT_ID, X_PROVIDER)]
    stored.configuration["granted_capabilities"] = ["x.posts.read", "x.posts.publish"]
    with pytest.raises(IntegrationAuthorizationError, match="read public posts yet"):
        await integrations.x.account_posts(PROJECT_ID, username="studio")
    assert len(lookups(api)) == len(post_reads(api)) == 1
    assert x_connection.allow_first("x.public.read").startswith(
        "X hasn't allowed Tin to read public posts yet. Reconnect X"
    )
    # The other messages read as before.
    assert "read its likes yet" in x_connection.allow_first("x.likes.read")
    assert "manage follows and Lists yet" in x_connection.allow_first("x.lists.manage")

    del db.connections[(PROJECT_ID, X_PROVIDER)]
    with pytest.raises(IntegrationAuthorizationError, match="Connect or reconnect X"):
        await integrations.x.account_posts(PROJECT_ID, username="studio")
    await integrations.close()


async def test_an_article_x_shapes_differently_reads_as_none() -> None:
    integrations, _, api = await connected()
    api.posts = {
        "data": [
            {"id": "6001", "text": "a", "author_id": "777", "article": {"cover_media": "3_1"}},
            {"id": "6002", "text": "b", "author_id": "777", "article": "not an object"},
        ],
        "includes": {"users": [{"id": "777", "username": "studio", "protected": False}]},
        "meta": {"result_count": 2, "newest_id": "not-an-id"},
    }
    page = await integrations.x.account_posts(PROJECT_ID, username="studio", max_results=5)
    assert [post["article"] for post in page["posts"]] == [None, None]
    assert [post["conversation_id"] for post in page["posts"]] == [None, None]
    assert page["meta"] == {"result_count": 2, "newest_id": None, "oldest_id": None}
    assert page["next_token"] is None
    await integrations.close()


async def test_other_reads_keep_their_shape() -> None:
    """Threads and Articles are this tool's; the gateway's and the feed's posts are unchanged."""
    authors = {"777": {"id": "777", "username": "studio", "name": "Studio", "protected": False}}
    post = {"id": "7001", "text": "t", "author_id": "777", "conversation_id": "7001"}
    plain = x_connection._public_post(post, authors, {}, None)
    assert "conversation_id" not in plain and "article" not in plain


@pytest.fixture
def mcp(monkeypatch):
    project = SimpleNamespace(id=uuid4(), state_repo_id="project-repo", canonical_branch="main")
    token = SimpleNamespace(subject="outsider", scopes=["openid"], client_id="mcp-client")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    db = SimpleNamespace(
        has_project_access=AsyncMock(
            side_effect=lambda **kw: kw["clerk_user_id"] in {"member", "teammate"}
        ),
        get_project=AsyncMock(return_value=project),
        record_mcp_usage=AsyncMock(),
        record_tin_user=AsyncMock(),
    )
    read = AsyncMock(
        return_value={"user": {"id": "777"}, "posts": [], "next_token": None, "meta": {}}
    )
    runtime = SimpleNamespace(
        database=db, integrations=SimpleNamespace(x=SimpleNamespace(account_posts=read))
    )
    settings = SimpleNamespace(
        switchboard_public_url="https://tin.test", clerk_frontend_api_url="https://clerk.test"
    )
    server, _ = create_mcp_app(settings=settings, auth=SimpleNamespace(), runtime=lambda: runtime)
    return SimpleNamespace(project=project, token=token, server=server, read=read)


async def test_mcp_reads_for_any_member_and_refuses_an_outsider(mcp) -> None:
    arguments = {
        "project_id": str(mcp.project.id),
        "username": "@evanjconrad",
        "max_results": 5,
        "start_time": "2025-11-27T00:00:00Z",
        "exclude_reposts": True,
    }
    with pytest.raises(ToolError, match="project not found"):
        await mcp.server.call_tool("read_x_user_posts", arguments)
    mcp.read.assert_not_awaited()

    mcp.token.subject = "teammate"
    result = await mcp.server.call_tool("read_x_user_posts", arguments)
    assert "posts" in str(result)
    mcp.read.assert_awaited_once_with(
        mcp.project.id,
        username="@evanjconrad",
        max_results=5,
        start_time="2025-11-27T00:00:00Z",
        end_time=None,
        since_id=None,
        exclude_replies=False,
        exclude_reposts=True,
        pagination_token=None,
    )
    mcp.read.side_effect = ServiceCallRefused(
        "@quiet_one protects its posts; Tin reads only public posts.", code="protected_account"
    )
    with pytest.raises(ToolError, match="protects its posts"):
        await mcp.server.call_tool("read_x_user_posts", arguments)
    mcp.read.side_effect = IntegrationAuthorizationError(x_connection.allow_first("x.public.read"))
    with pytest.raises(ToolError, match="read public posts yet"):
        await mcp.server.call_tool("read_x_user_posts", arguments)
