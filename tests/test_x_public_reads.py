"""Public X reads for code packages: opt-in capability, bounds, cleaning and the gateway."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from test_billing import billed as billed
from test_integrations import PROJECT_ID, RUN_ID
from test_procedure_publication import publication_db as publication_db
from test_x_connection import X, connect, service

from tin_lite import x_connection
from tin_lite.code_services import ARGUMENT_CHECKS, CodeServiceError, CodeServices
from tin_lite.code_services import OPERATIONS as GATEWAY_OPERATIONS
from tin_lite.connection_records import ServiceArgumentError, json_size
from tin_lite.integrations import (
    X_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationInputError,
    IntegrationProviderError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
    ServiceCallRefused,
    ServiceResponseTooLarge,
    parse_integration_requirements,
    registered_integrations,
)
from tin_lite.workflow_services import service_bindings

CURSOR = "b26v89c19zqg8o3fpdg5"
INVALID_QUERY = {
    "errors": [
        {
            "parameters": {"query": ["(("]},
            "message": "There were errors processing your request: missing EOF at ')' "
            "(token access1 must not be echoed)",
        }
    ],
    "title": "Invalid Request",
    "detail": "One or more parameters to your request was invalid.",
    "type": "https://api.x.com/2/problems/invalid-request",
}
CLIENT_FORBIDDEN = {
    "title": "Client Forbidden",
    "detail": "This request must be made using an approved developer account.",
    "reason": "client-not-enrolled",
    "type": "https://api.x.com/2/problems/client-forbidden",
}


def search_body():
    return {
        "data": [
            {
                "id": "1001",
                "text": "A new identity for Bloom https://t.co/a https://t.co/p",
                "created_at": "2026-10-20T10:00:00.000Z",
                "author_id": "501",
                "note_post": {
                    "text": "A new identity for Bloom, the dating app, full story below.",
                    "entities": {
                        "urls": [
                            {"start": 0, "end": 5, "expanded_url": "https://studio.example/bloom"},
                            {"start": 6, "end": 9, "expanded_url": "https://studio.example/notes"},
                        ]
                    },
                },
                "entities": {
                    "urls": [
                        {
                            "start": 24,
                            "end": 47,
                            "url": "https://t.co/a",
                            "expanded_url": "https://studio.example/bloom",
                            "unwound_url": "https://studio.example/bloom?ref=x",
                            "title": "Bloom identity",
                            "description": "Wordmark, colour and motion for Bloom.",
                        },
                        {
                            "start": 48,
                            "end": 71,
                            "url": "https://t.co/p",
                            "expanded_url": "https://x.com/studio/status/1001/photo/1",
                            "media_key": "3_77",
                        },
                        {"start": 72, "end": 80, "expanded_url": "javascript:alert(1)"},
                    ]
                },
                "attachments": {"media_keys": ["3_77", "7_78", "3_gone"]},
                "public_metrics": {
                    "repost_count": 3,
                    "reply_count": 1,
                    "like_count": 40,
                    "quote_count": 0,
                    "bookmark_count": 2,
                    "impression_count": 900,
                    "secret_metric": 7,
                },
            },
            {"id": "1002", "text": "Behind a protected account", "author_id": "502"},
            {"id": "1003", "text": "Author left out of includes", "author_id": "503"},
        ],
        "includes": {
            "users": [
                {"id": "501", "username": "studio", "name": "Studio", "protected": False},
                {"id": "502", "username": "quiet_one", "name": "Quiet", "protected": True},
                {"name": "no id"},
            ],
            "media": [
                {
                    "media_key": "3_77",
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/bloom.jpg",
                    "width": 1600,
                    "height": 900,
                },
                {
                    "media_key": "7_78",
                    "type": "video",
                    "url": "http://insecure.example/video",
                    "preview_image_url": "https://pbs.twimg.com/ext_tw_video_thumb/bloom.jpg",
                    "width": 1280,
                    "height": 720,
                },
            ],
        },
        "errors": [
            {
                "title": "Not Found Error",
                "type": "https://api.x.com/2/problems/resource-not-found",
                "resource_type": "user",
                "resource_id": "503",
            }
        ],
        "meta": {"result_count": 3, "next_token": CURSOR},
    }


class PublicX(X):
    """The OAuth fixture plus recent search, username lookup and another account's posts."""

    def __init__(self):
        super().__init__()
        self.search_status = 200
        self.search = search_body()
        self.posts_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/2/tweets/search/recent":
            self.calls.append(request)
            if self.search_status != 200:
                body = {
                    400: INVALID_QUERY,
                    403: CLIENT_FORBIDDEN,
                    429: {"title": "Too Many Requests", "type": "about:blank"},
                }.get(self.search_status, {"title": "Service Unavailable"})
                return self.reply(self.search_status, body, {"retry-after": "37"})
            return self.reply(200, self.search)
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
                                "value": username,
                                "detail": f"Could not find user with username: [{username}].",
                                "title": "Not Found Error",
                                "resource_type": "user",
                                "type": f"https://api.twitter.com/2/problems/{kind}",
                            }
                        ]
                    },
                )
            return self.reply(
                200,
                {
                    "data": {
                        "id": "777" if username == "studio" else "778",
                        "username": username,
                        "name": "Studio" if username == "studio" else "Quiet",
                        "protected": username == "quiet_one",
                    }
                },
            )
        if path in {"/2/users/777/tweets", "/2/users/778/tweets"}:
            self.calls.append(request)
            if self.posts_status != 200:
                return self.reply(self.posts_status, CLIENT_FORBIDDEN)
            return self.reply(
                200,
                {
                    "data": [
                        {
                            "id": "2001",
                            "text": "Our new identity is live.",
                            "created_at": "2026-10-21T09:00:00.000Z",
                            "attachments": {"media_keys": ["3_90"]},
                            "public_metrics": {"like_count": 12},
                        }
                    ],
                    "includes": {
                        "media": [
                            {
                                "media_key": "3_90",
                                "type": "photo",
                                "url": "https://pbs.twimg.com/media/id.jpg",
                                "width": 1200,
                                "height": 1200,
                            }
                        ]
                    },
                    "meta": {"result_count": 1},
                },
            )
        return super().__call__(request)


async def public_service(*, capabilities=("x.public.read",)):
    integrations, db, api = await service(PublicX())
    connection, _ = await connect(integrations, api, capabilities=capabilities)
    return integrations, db, api, connection


def reads(api):
    """The public reads X answered; the callback's own /2/users/me identity check is not one."""
    return [
        r
        for r in api.calls
        if r.url.path.startswith(("/2/tweets/search", "/2/users/")) and r.url.path != "/2/users/me"
    ]


# ---------------------------------------------------------------- registry and opt-in


def test_public_reads_are_an_explicit_reviewed_capability() -> None:
    definition = next(d for d in registered_integrations() if d.key == X_PROVIDER)
    assert definition.capabilities == (
        "x.posts.read",
        "x.posts.publish",
        "x.media.upload",
        "x.public.read",
    )
    assert "public posts" in definition.description
    assert x_connection.CAPABILITY_SCOPES["x.public.read"] == {"tweet.read", "users.read"}
    for name, arguments in x_connection.OPERATIONS.items():
        assert GATEWAY_OPERATIONS[(X_PROVIDER, name)] == ("x.public.read", arguments)
    assert ARGUMENT_CHECKS[X_PROVIDER] is x_connection.check_arguments
    requirement = [
        {"provider_key": X_PROVIDER, "capabilities": ["x.public.read"], "required": True}
    ]
    assert parse_integration_requirements(requirement)[0].capabilities == ("x.public.read",)
    binding = {"x": {"provider_key": X_PROVIDER, "max_calls": 6, "max_response_bytes": 64000}}
    assert service_bindings(binding, requirement)[0].capabilities == ("x.public.read",)
    # Publishing is never service authority.
    with pytest.raises(ValueError, match="explicit supported"):
        service_bindings(
            binding,
            [{"provider_key": X_PROVIDER, "capabilities": ["x.posts.publish"], "required": True}],
        )


async def test_public_reads_need_their_own_consent_and_reconnecting_keeps_it() -> None:
    integrations, db, api = await service(PublicX())
    first, query = await connect(integrations, api)
    assert first.configuration["granted_capabilities"] == ["x.posts.read"]
    with pytest.raises(IntegrationAuthorizationError, match="additional access"):
        await integrations.x.search_recent(first, query="brand identity")
    with pytest.raises(IntegrationAuthorizationError, match="additional access"):
        await integrations.x.user_posts(first, username="studio")
    assert reads(api) == []

    opted, opted_query = await connect(integrations, api, capabilities=("x.public.read",))
    # The same scopes as reading your own posts: X's screen asks for nothing new.
    assert opted_query["scope"] == query["scope"]
    assert opted.configuration["granted_capabilities"] == ["x.posts.read", "x.public.read"]
    assert await integrations.x.connection(PROJECT_ID, "x.public.read")

    # A later publishing upgrade merges what was granted, so public reads stay.
    upgraded, _ = await connect(integrations, api, capabilities=("x.posts.publish",))
    assert set(upgraded.configuration["granted_capabilities"]) == {
        "x.posts.read",
        "x.posts.publish",
        "x.public.read",
    }
    assert x_connection.capabilities_for({"tweet.read", "users.read"}) == ["x.posts.read"]
    await integrations.close()


# ---------------------------------------------------------------- arguments


@pytest.mark.parametrize(
    ("operation", "arguments", "message"),
    [
        ("x.posts.search", {}, "query must be 1-512"),
        ("x.posts.search", {"query": "   "}, "query must be 1-512"),
        ("x.posts.search", {"query": "q" * 513}, "query must be 1-512"),
        ("x.posts.search", {"query": "launch\nidentity"}, "control characters"),
        ("x.posts.search", {"query": "launch\x00"}, "control characters"),
        ("x.posts.search", {"query": ["launch"]}, "query must be 1-512"),
        ("x.posts.search", {"query": "launch", "max_results": 9}, "from 10 to 100"),
        ("x.posts.search", {"query": "launch", "max_results": 101}, "from 10 to 100"),
        ("x.posts.search", {"query": "launch", "max_results": "20"}, "from 10 to 100"),
        ("x.posts.search", {"query": "launch", "max_results": True}, "from 10 to 100"),
        ("x.posts.search", {"query": "launch", "start_time": "2026-10-20"}, "UTC time"),
        ("x.posts.search", {"query": "launch", "end_time": "2026-02-30T00:00:00Z"}, "UTC time"),
        (
            "x.posts.search",
            {
                "query": "launch",
                "start_time": "2026-10-21T00:00:00Z",
                "end_time": "2026-10-20T00:00:00Z",
            },
            "before end_time",
        ),
        ("x.posts.search", {"query": "launch", "next_token": "../elsewhere"}, "next_token"),
        ("x.posts.search", {"query": "launch", "next_token": ""}, "next_token"),
        ("x.posts.search", {"query": "launch", "next_token": "a" * 257}, "next_token"),
        ("x.posts.search", {"query": "launch", "pagination_token": CURSOR}, "unsupported"),
        ("x.user.posts", {}, "username must be"),
        ("x.user.posts", {"username": "@studio"}, "username must be"),
        ("x.user.posts", {"username": "https://x.com/studio"}, "username must be"),
        ("x.user.posts", {"username": "a" * 16}, "username must be"),
        ("x.user.posts", {"username": "studio-one"}, "username must be"),
        ("x.user.posts", {"username": "studio", "max_results": 4}, "from 5 to 100"),
        ("x.user.posts", {"username": "studio", "exclude_replies": "yes"}, "true or false"),
        ("x.user.posts", {"username": "studio", "pagination_token": "a b"}, "pagination_token"),
        ("x.user.posts", {"username": "studio", "next_token": CURSOR}, "unsupported"),
        ("x.user.posts", {"username": "studio", "end_time": "yesterday"}, "UTC time"),
        ("x.timeline", {}, "unknown X operation"),
    ],
)
def test_malformed_arguments_fail_before_any_receipt(operation, arguments, message) -> None:
    with pytest.raises(ServiceArgumentError, match=message):
        x_connection.check_arguments(operation, arguments)


async def test_direct_reads_validate_before_calling_x() -> None:
    integrations, _, api, connection = await public_service()
    with pytest.raises(IntegrationInputError, match="query"):
        await integrations.x.search_recent(connection, query="")
    with pytest.raises(IntegrationInputError, match="username"):
        await integrations.x.user_posts(connection, username="x.com/studio")
    with pytest.raises(IntegrationInputError, match="from 5 to 100"):
        await integrations.x.user_posts(connection, username="studio", max_results=101)
    assert reads(api) == []
    await integrations.close()


# ---------------------------------------------------------------- requests and results


async def test_search_request_shape_and_cleaned_posts() -> None:
    integrations, _, api, connection = await public_service()
    page = await integrations.x.search_recent(
        connection,
        query='"brand identity" (launch OR unveil) has:media -is:retweet',
        max_results=25,
        start_time="2026-10-19T00:00:00Z",
        end_time="2026-10-24T00:00:00Z",
        next_token=CURSOR,
    )
    request = reads(api)[-1]
    assert request.method == "GET" and request.url.host == "api.x.com"
    assert request.url.path == "/2/tweets/search/recent"
    assert request.headers["Authorization"] == "Bearer access1"
    assert dict(request.url.params) == {
        "query": '"brand identity" (launch OR unveil) has:media -is:retweet',
        "max_results": "25",
        "post.fields": "created_at,public_metrics,entities,attachments,note_post",
        "expansions": "author_id,attachments.media_keys",
        "user.fields": "protected",
        "media.fields": "type,url,preview_image_url,width,height",
        "start_time": "2026-10-19T00:00:00Z",
        "end_time": "2026-10-24T00:00:00Z",
        "next_token": CURSOR,
    }
    assert page["next_token"] == CURSOR
    # The protected author's post is left out; the partial error keeps the rest of the page.
    assert [post["id"] for post in page["posts"]] == ["1001", "1003"]
    first, orphan = page["posts"]
    assert first == {
        "id": "1001",
        "url": "https://x.com/i/web/status/1001",
        "text": "A new identity for Bloom, the dating app, full story below.",
        "created_at": "2026-10-20T10:00:00.000Z",
        "author": {"id": "501", "username": "studio", "name": "Studio"},
        "public_metrics": {
            "repost_count": 3,
            "reply_count": 1,
            "like_count": 40,
            "quote_count": 0,
            "bookmark_count": 2,
            "impression_count": 900,
        },
        "urls": [
            {
                "expanded_url": "https://studio.example/bloom",
                "unwound_url": "https://studio.example/bloom?ref=x",
                "title": "Bloom identity",
                "description": "Wordmark, colour and motion for Bloom.",
            },
            {
                "expanded_url": "https://studio.example/notes",
                "unwound_url": None,
                "title": None,
                "description": None,
            },
        ],
        "media": [
            {
                "type": "photo",
                "url": "https://pbs.twimg.com/media/bloom.jpg",
                "preview_image_url": None,
                "width": 1600,
                "height": 900,
            },
            {
                "type": "video",
                "url": None,
                "preview_image_url": "https://pbs.twimg.com/ext_tw_video_thumb/bloom.jpg",
                "width": 1280,
                "height": 720,
            },
        ],
    }
    assert orphan["author"] == {"id": "503", "username": None, "name": None}
    assert orphan["urls"] == [] and orphan["media"] == [] and orphan["public_metrics"] == {}
    await integrations.close()


async def test_search_rejects_malformed_pages() -> None:
    integrations, _, api, connection = await public_service()
    api.search = {"data": [{"id": "not-a-number", "text": "x"}], "meta": {}}
    with pytest.raises(IntegrationUpstreamError, match="invalid post"):
        await integrations.x.search_recent(connection, query="launch")
    api.search = {"data": [{"id": "1", "text": None}], "meta": {}}
    with pytest.raises(IntegrationUpstreamError, match="incomplete post"):
        await integrations.x.search_recent(connection, query="launch")
    api.search = {"data": [{"id": "1", "text": "x"}], "meta": {"next_token": "../x"}}
    with pytest.raises(IntegrationUpstreamError, match="next_token"):
        await integrations.x.search_recent(connection, query="launch")
    api.search = {"data": [{"id": str(n), "text": "x"} for n in range(1, 12)], "meta": {}}
    with pytest.raises(IntegrationUpstreamError, match="invalid page"):
        await integrations.x.search_recent(connection, query="launch", max_results=10)
    api.search = {"data": [], "includes": [], "meta": {}}
    with pytest.raises(IntegrationUpstreamError, match="authors or media"):
        await integrations.x.search_recent(connection, query="launch")
    # Errors with no data are still an incomplete result for search.
    api.search = {"errors": [{"title": "Not Found Error"}]}
    with pytest.raises(IntegrationUpstreamError, match="incomplete"):
        await integrations.x.search_recent(connection, query="launch")
    # No matches is an empty page, not an error.
    api.search = {"meta": {"result_count": 0}}
    assert await integrations.x.search_recent(connection, query="launch") == {
        "posts": [],
        "next_token": None,
    }
    await integrations.close()


async def test_user_posts_resolve_the_username_then_read_its_own_posts() -> None:
    integrations, _, api, connection = await public_service()
    page = await integrations.x.user_posts(
        connection, username="studio", max_results=5, pagination_token=CURSOR
    )
    lookup, timeline = reads(api)
    assert lookup.url.path == "/2/users/by/username/studio"
    assert dict(lookup.url.params) == {"user.fields": "protected"}
    assert timeline.url.path == "/2/users/777/tweets"
    assert dict(timeline.url.params) == {
        "max_results": "5",
        "post.fields": "created_at,public_metrics,entities,attachments,note_post",
        "expansions": "attachments.media_keys",
        "media.fields": "type,url,preview_image_url,width,height",
        "exclude": "replies,retweets",
        "pagination_token": CURSOR,
    }
    assert page["account"] == {"id": "777", "username": "studio", "name": "Studio"}
    assert page["next_token"] is None
    [post] = page["posts"]
    assert post["author"] == page["account"]
    assert post["media"][0]["url"] == "https://pbs.twimg.com/media/id.jpg"
    assert post["public_metrics"] == {"like_count": 12}

    await integrations.x.user_posts(connection, username="studio", exclude_replies=False)
    assert reads(api)[-1].url.params["exclude"] == "retweets"
    assert reads(api)[-1].url.params["max_results"] == "10"
    await integrations.close()


@pytest.mark.parametrize(
    ("username", "code", "message"),
    [
        ("nobody", "not_found", "found no account @nobody"),
        ("suspended", "account_unavailable", "suspended or unavailable"),
        ("quiet_one", "protected_account", "protects its posts"),
    ],
)
async def test_unreadable_accounts_are_named_refusals(username, code, message) -> None:
    integrations, _, api, connection = await public_service()
    with pytest.raises(ServiceCallRefused, match=message) as refused:
        await integrations.x.user_posts(connection, username=username)
    assert refused.value.code == code
    # Only the lookup was read; no posts were requested.
    assert [r.url.path for r in reads(api)] == [f"/2/users/by/username/{username}"]
    if code != "protected_account":
        assert "Could not find user" in refused.value.provider_error.message
    await integrations.close()


# ---------------------------------------------------------------- gateway adapter


async def test_gateway_call_fits_the_bound_and_receipts_the_read() -> None:
    integrations, db, api, connection = await public_service()
    whole = await integrations.x.call(
        "x.posts.search",
        {"query": "brand identity"},
        connection=connection,
        run_id=RUN_ID,
        execution_key="run:code-service:a",
        max_response_bytes=64_000,
    )
    assert [post["id"] for post in whole["posts"]] == ["1001", "1003"]
    assert whole["truncated"] is False and whole["next_token"] == CURSOR
    bound = json_size({**whole, "posts": whole["posts"][:1]}) + 10
    fitted = await integrations.x.call(
        "x.posts.search",
        {"query": "brand identity"},
        connection=connection,
        run_id=RUN_ID,
        execution_key="run:code-service:b",
        max_response_bytes=bound,
    )
    assert [post["id"] for post in fitted["posts"]] == ["1001"]
    assert fitted["truncated"] is True and json_size(fitted) <= bound
    with pytest.raises(ServiceResponseTooLarge):
        await integrations.x.call(
            "x.posts.search",
            {"query": "brand identity"},
            connection=connection,
            run_id=RUN_ID,
            execution_key="run:code-service:c",
            max_response_bytes=500,
        )
    receipts = {call["execution_key"]: call for call in db.calls}
    assert receipts["run:code-service:a"]["status"] == "completed"
    assert receipts["run:code-service:a"]["capability"] == "x.public.read"
    assert receipts["run:code-service:a"]["provider_key"] == X_PROVIDER
    assert receipts["run:code-service:b"]["response_summary"] == {
        "post_count": 2,
        "returned_count": 1,
        "truncated": True,
        "has_more": True,
    }
    assert receipts["run:code-service:c"]["response_summary"]["returned_count"] == 0
    assert "access1" not in json.dumps(db.calls, default=str)
    await integrations.close()


async def test_gateway_call_settles_x_refusals_with_what_x_said() -> None:
    integrations, db, api, connection = await public_service()

    async def search(key):
        return await integrations.x.call(
            "x.posts.search",
            {"query": "(("},
            connection=connection,
            run_id=RUN_ID,
            execution_key=key,
            max_response_bytes=64_000,
        )

    api.search_status = 400
    with pytest.raises(IntegrationProviderError, match=r"HTTP 400") as invalid:
        await search("k400")
    detail = invalid.value.provider_error
    assert (detail.provider, detail.status, detail.type) == ("X", 400, "invalid-request")
    assert "missing EOF" in detail.message and "access1" not in detail.message

    api.search_status = 403
    with pytest.raises(ServiceCallRefused, match="X refused this read") as forbidden:
        await search("k403")
    assert forbidden.value.code == "permission_denied"
    assert forbidden.value.provider_error.type == "client-forbidden"
    assert forbidden.value.provider_error.code == "client-not-enrolled"

    api.search_status = 429
    with pytest.raises(IntegrationRateLimitedError) as limited:
        await search("k429")
    assert limited.value.code == "rate_limited" and limited.value.retry_after == 37

    api.search_status = 503
    with pytest.raises(IntegrationProviderError, match="could not complete") as unavailable:
        await search("k503")
    assert unavailable.value.provider_error.status == 503

    api.posts_status = 403
    with pytest.raises(ServiceCallRefused) as account_forbidden:
        await integrations.x.call(
            "x.user.posts",
            {"username": "studio"},
            connection=connection,
            run_id=RUN_ID,
            execution_key="kposts",
            max_response_bytes=64_000,
        )
    assert account_forbidden.value.code == "permission_denied"
    assert {call["execution_key"]: call["error_code"] for call in db.calls} == {
        "k400": "provider_request_failed",
        "k403": "permission_denied",
        "k429": "rate_limited",
        "k503": "provider_request_failed",
        "kposts": "permission_denied",
    }
    assert all(call["status"] == "failed" for call in db.calls)
    await integrations.close()


async def test_gateway_call_without_the_capability_is_a_settled_refusal() -> None:
    integrations, db, api, connection = await public_service(capabilities=None)
    with pytest.raises(ServiceCallRefused, match="additional access") as refused:
        await integrations.x.call(
            "x.user.posts",
            {"username": "studio"},
            connection=connection,
            run_id=RUN_ID,
            execution_key="kgate",
            max_response_bytes=64_000,
        )
    assert refused.value.code == "reauthorization_required"
    assert reads(api) == []
    assert db.calls[-1]["status"] == "failed"
    await integrations.close()


async def test_code_services_adapter_dispatches_to_the_x_connection() -> None:
    integrations, _, api, connection = await public_service()
    gateway = CodeServices(
        database=None, integrations=integrations, authorize=None, managed=SimpleNamespace()
    )
    run = SimpleNamespace(id=RUN_ID, project_id=PROJECT_ID)
    page = await gateway.adapter(
        X_PROVIDER,
        "x.user.posts",
        {"username": "studio", "max_results": 5},
        run,
        connection,
        "run:code-service:d",
        max_response_bytes=64_000,
    )
    assert page["account"]["username"] == "studio" and page["truncated"] is False
    assert [r.url.path for r in reads(api)] == [
        "/2/users/by/username/studio",
        "/2/users/777/tweets",
    ]
    await integrations.close()


# ---------------------------------------------------------------- real Postgres gateway


async def test_gateway_reads_public_posts_with_receipts_replay_and_settled_refusals(
    billed, monkeypatch
):
    """Real Postgres receipts: gating, replay per step, a refusal that does not wedge the run."""
    from dataclasses import replace

    from temporalio.testing import ActivityEnvironment
    from test_private_workflows import ACTOR
    from test_project_connections import definition, prepared, public_dns

    from tin_lite.workflow_code import validate_code_definition

    f = billed
    service, _, code, run_id = await prepared(f, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(b'{"accounts":[]}'))
        )
    ) as crm:
        code.services.client, code.services.resolver = crm, public_dns
        with pytest.raises(RuntimeError, match="worker loss"):
            await ActivityEnvironment().run(code.execute, run_id)
    api = PublicX()
    service._settings.x_oauth_client_id = "synthetic-client"
    service._settings.x_oauth_client_secret = SecretStr("synthetic-secret")
    original = service._client
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(api))

    async def authorize(capabilities=None):
        start = await service.start_connect(
            project_id=f.project.id,
            provider_key=X_PROVIDER,
            clerk_user_id=ACTOR,
            capabilities=capabilities,
        )
        query = parse_qs(urlsplit(start.authorization_url).query)
        api.scopes, api.challenge = query["scope"][0], query["code_challenge"][0]
        return await service.x.complete(state=query["state"][0], code="c", clerk_user_id=ACTOR)

    try:
        await authorize()
        run, workflow, _, _, _ = await code.selected(run_id)
        body = definition()
        body["integration_requirements"] = [
            {"provider_key": X_PROVIDER, "capabilities": ["x.public.read"], "required": True}
        ]
        body["code"]["services"] = {
            "x": {"provider_key": X_PROVIDER, "max_calls": 3, "max_response_bytes": 1100}
        }
        changed, spec = replace(workflow, definition=body), validate_code_definition(body)

        def call(conn, step, operation, arguments):
            return code.services.call(
                conn=conn,
                run=run,
                workflow=changed,
                spec=spec,
                payload={
                    "service": "x",
                    "step": step,
                    "operation": operation,
                    "arguments": arguments,
                },
            )

        async with f.db.pool.acquire() as conn:
            # Connected for its own posts only: the gateway refuses before any receipt.
            with pytest.raises(CodeServiceError, match="permission was removed"):
                await call(conn, "early", "x.posts.search", {"query": "brand identity"})
            assert reads(api) == []
        await authorize(("x.public.read",))
        async with f.db.pool.acquire() as conn:
            with pytest.raises(CodeServiceError, match="contract: X username must be"):
                await call(conn, "bad", "x.user.posts", {"username": "@studio"})
            with pytest.raises(CodeServiceError, match="declared contract"):
                await call(conn, "bad", "x.posts.search", {"query": "a", "until_id": "1"})
            first = await call(conn, "search", "x.posts.search", {"query": "brand identity"})
            assert [p["id"] for p in first["posts"]] == ["1001"] and first["truncated"] is True
            assert json_size(first) <= 1100 and len(reads(api)) == 1
            assert await call(conn, "search", "x.posts.search", {"query": "brand identity"}) == (
                first
            )
            assert len(reads(api)) == 1
            api.search_status = 400
            with pytest.raises(CodeServiceError, match="X said: invalid-request") as refused:
                await call(conn, "broken", "x.posts.search", {"query": "(("})
            assert refused.value.code == "provider_error"
            assert refused.value.provider_error["type"] == "invalid-request"
            api.search_status = 200
            account = await call(conn, "account", "x.user.posts", {"username": "studio"})
            assert account["account"]["id"] == "777" and account["posts"][0]["id"] == "2001"
            with pytest.raises(CodeServiceError, match="service-call limit"):
                await call(conn, "fifth", "x.posts.search", {"query": "more"})
        rows = await f.db.pool.fetch(
            """SELECT operation, status, result FROM effect_receipts
               WHERE operation IN ('code_service_call_v1', 'external_usage_v1')"""
        )
        assert rows and all(row["status"] == "completed" for row in rows)
        assert "access1" not in json.dumps([dict(r) for r in rows], default=str)
        usage = [json.loads(r["result"]) for r in rows if r["operation"] == "external_usage_v1"]
        assert {u["provider"] for u in usage if u["endpoint"].startswith("x.")} == {X_PROVIDER}
        receipts = await f.db.pool.fetch(
            "SELECT status, error_code, capability FROM integration_call_receipts "
            "WHERE provider_key=$1",
            X_PROVIDER,
        )
        assert sorted((r["status"], r["error_code"]) for r in receipts) == [
            ("completed", None),
            ("completed", None),
            ("failed", "provider_request_failed"),
        ]
        assert {r["capability"] for r in receipts} == {"x.public.read"}
    finally:
        await service._client.aclose()
        service._client = original
        await service.close()
