"""Selected server-rendered pages, with synthetic people and no live requests."""

import httpx
import pytest
from test_connection_collection import ACTOR, FRIEND, session_envelope

from tin_lite.connection_collection import CollectionError
from tin_lite.linkedin_document import Document, prepare_source, search_results, verify_document
from tin_lite.linkedin_session import read_page, validate_session

PROFILE = {
    "publicIdentifier": "friend",
    "entityUrn": "urn:li:fsd_profile:member1",
    "firstName": "Test",
    "lastName": "Friend",
}
SOURCE = {"friend_url": FRIEND, "actor": ACTOR, "query_id": None}


def document(content):
    return (
        '<!doctype html><html lang="en"><body><header><a href="'
        + ACTOR["key"]
        + '">Owner</a></header><main>'
        + content
        + "</main></body></html>"
    )


def friend_html(degree="1st"):
    return document(
        "<section><h2>Test Friend</h2><span>"
        + degree
        + '</span><a href="https://www.linkedin.com/search/results/people/'
        '?connectionOf=%5B%22member1%22%5D">500+ connections</a></section>'
    )


def card(index, degree="2nd"):
    return (
        f'<li><a href="https://www.linkedin.com/in/person-{index}">'
        f'<span aria-hidden="true">Person {index}</span>'
        f'<span class="visually-hidden">View Person {index} profile</span></a>'
        f"<span>· {degree}</span><p>Founder</p><button>Connect</button></li>"
    )


def results(page=1, more=True):
    return document(
        "<ul>"
        + card(page)
        + card(page + 1)
        + "</ul><aside>"
        + card(99)
        + '</aside><div><button aria-current="page">'
        + str(page)
        + "</button><button "
        + ("" if more else "disabled")
        + ">Next</button></div>"
    )


async def test_resolves_saved_friend_and_paginates_without_browser_metadata():
    seen = []

    def respond(request):
        seen.append(request)
        assert request.method == "GET" and request.url.host == "www.linkedin.com"
        if request.url.path == "/voyager/api/me":
            return httpx.Response(200, json={"miniProfile": {"publicIdentifier": "owner"}})
        if request.url.path == "/voyager/api/identity/dash/profiles":
            assert request.url.params["memberIdentity"] == "friend"
            return httpx.Response(200, json={"included": [PROFILE]})
        if request.url.path == "/in/friend":
            return httpx.Response(200, text=friend_html())
        assert request.url.path == "/search/results/people/"
        assert request.url.params["network"] == '["S"]'
        assert request.url.params["keywords"] == "founder"
        assert request.url.params["connectionOf"] == '["member1"]'
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, text=results(page, more=page == 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        session = validate_session(session_envelope())
        first = await read_page(session, SOURCE, "founder", 1, client=client, document=True)
        assert first["next_page"] and first["source"]["query_id"] is None
        assert [p["name"] for p in first["people"]] == ["Person 1", "Person 2"]
        second = await read_page(
            session, first["source"], "founder", 2, client=client, document=True
        )
        assert not second["next_page"]
        assert [p["name"] for p in second["people"]] == ["Person 2", "Person 3"]
    assert sum(r.url.path == "/in/friend" for r in seen) == 1
    assert all(r.url.path != "/voyager/api/graphql" for r in seen)


@pytest.mark.parametrize(
    "html",
    [
        friend_html().replace("member1", "unrelated"),
        friend_html().replace("<span>1st</span>", ""),
        friend_html().replace("500+ connections", "12 mutual connections"),
        friend_html().replace("Test Friend", "Other Person"),
        friend_html().replace("<section>", "<section><span>2nd</span>"),
    ],
)
def test_profile_requires_matching_friend_relationship_and_visible_list(html):
    with pytest.raises(CollectionError, match="browser_preparation_required"):
        prepare_source(Document(html), SOURCE, PROFILE, "founder")


def test_second_degree_friend_is_rejected():
    with pytest.raises(CollectionError, match="friend_not_connected"):
        prepare_source(Document(friend_html("2nd")), SOURCE, PROFILE, "")


def test_truncated_html_wrong_page_hidden_results_and_unrelated_cards_are_not_accepted():
    with pytest.raises(CollectionError, match="unsupported_search_contract"):
        Document(results()[:-8])
    with pytest.raises(CollectionError, match="unsupported_search_contract"):
        search_results(Document(results()), 2)
    hidden = results().replace("<ul>", "<ul hidden>")
    with pytest.raises(CollectionError, match="unsupported_search_contract"):
        search_results(Document(hidden), 1)
    with pytest.raises(CollectionError, match="filters_changed"):
        search_results(Document(results().replace("· 2nd", "· 1st")), 1)
    assert len(search_results(Document(results()), 1)["people"]) == 2


def test_limits_and_login_pages_stop_before_extraction():
    with pytest.raises(CollectionError, match="challenge"):
        verify_document(Document(document('<input name="session_password">')))
    with pytest.raises(CollectionError, match="rate_limited"):
        verify_document(Document(document("<h2>Commercial use limit</h2>")))


async def test_document_redirects_are_never_followed():
    from tin_lite.linkedin_document import get_document

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://example.com/checkpoint"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(CollectionError, match="challenge"):
            await get_document(client, validate_session(session_envelope()), FRIEND)
    assert len(requests) == 1


async def test_authenticated_account_must_match_before_any_document_read():
    requests = []

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"miniProfile": {"publicIdentifier": "other"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(CollectionError, match="account_changed"):
            await read_page(
                validate_session(session_envelope()), SOURCE, "", 1, client=client, document=True
            )
    assert requests == ["/voyager/api/me"]


def test_stylesheet_visibility_keeps_alternative_badges_and_hidden_cards_out():
    css = "@layer atoms { .unusedVariant { display: none; } }"
    html = friend_html().replace(
        "<span>1st</span>", '<span>1st</span><div class="unusedVariant"><p>· 2nd</p></div>'
    )
    with pytest.raises(CollectionError, match="browser_preparation_required"):
        prepare_source(Document(html), SOURCE, PROFILE, "")
    assert prepare_source(Document(html, [css]), SOURCE, PROFILE, "")["first_degree"]
    html = results().replace(
        "</ul>", '<div class="unusedVariant">' + card(9, "1st") + "</div></ul>"
    )
    assert len(search_results(Document(html, [css]), 1)["people"]) == 2


@pytest.mark.parametrize(
    "css",
    [
        ".uncertain {display:none} .uncertain {display:block}",
        ".uncertain {display:none} @media (min-width: 800px) {.uncertain {display:block}}",
    ],
)
def test_conflicting_or_responsive_visibility_is_not_guessed(css):
    html = results().replace("<ul>", '<ul class="uncertain">')
    with pytest.raises(CollectionError, match="unsupported_search_contract"):
        search_results(Document(html, [css]), 1)


def test_hidden_next_needs_a_complete_numbered_final_pager():
    html = document(
        card(1) + "<div><button>Previous</button><button>1</button>"
        '<button aria-current="page">2</button>'
        '<button class="hiddenNext">Next</button></div>'
    )
    css = ".hiddenNext { visibility: hidden; }"
    assert search_results(Document(html, [css]), 2)["next_page"] is False
    for incomplete in (
        html.replace("<button>1</button>", "<span>…</span><button>1</button>"),
        html.replace("<button>Previous</button>", ""),
        html.replace('aria-current="page">2', 'aria-current="page">1'),
    ):
        with pytest.raises(CollectionError, match="unsupported_search_contract"):
            search_results(Document(incomplete, [css]), 2)


def test_explicit_empty_first_page_and_missing_results_are_distinct():
    html = document("<h2>No results found</h2>")
    assert search_results(Document(html), 1) == {"people": [], "next_page": False}
    with pytest.raises(CollectionError, match="unsupported_search_contract"):
        search_results(Document(html), 2)
    with pytest.raises(CollectionError, match="unsupported_search_contract"):
        search_results(Document(document("<h2>Loading</h2>")), 1)


async def test_public_stylesheet_receives_no_auth_and_is_bounded_to_fixed_asset_origin():
    from tin_lite.linkedin_document import get_document

    asset = "https://static.licdn.com/aero-v1/sc/h/assets/generated-name.css"
    html = friend_html().replace("<body>", f'<link rel="stylesheet" href="{asset}"><body>')
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.host == "www.linkedin.com":
            assert "cookie" in request.headers
            return httpx.Response(200, text=html)
        assert str(request.url) == asset
        assert not {"cookie", "authorization", "csrf-token", "referer"} & set(request.headers)
        return httpx.Response(200, text="@layer atoms {.variant {display:none}}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        headers={"authorization": "synthetic-inherited-secret"},
        cookies={"session": "synthetic-inherited-cookie"},
    ) as client:
        await get_document(client, validate_session(session_envelope()), FRIEND)
    assert len(seen) == 2

    for bad_asset in (
        "https://example.com/asset.css",
        "https://static.licdn.com/aero-v1/sc/h/assets/generated-name.css?secret=anything",
        "https://static.licdn.com/../../internal",
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _, bad=bad_asset: httpx.Response(200, text=html.replace(asset, bad))
            )
        ) as client:
            with pytest.raises(CollectionError, match="unsupported_search_contract"):
                await get_document(client, validate_session(session_envelope()), FRIEND)
