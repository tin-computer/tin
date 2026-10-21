"""Reading who viewed a member's LinkedIn profile, from synthetic LinkedIn answers.

The list's answers copy the structure of LinkedIn's profile-views page (React Server
Components rows: one row per viewer item, references between rows, the next page's request in
a list row or a text row); every name and headline here is made up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from test_connection_collection import ACTOR, session_envelope
from test_linkedin_engagement import OWNER, me, no_wait

from tin_lite import linkedin_viewers as viewers
from tin_lite.connection_collection import CollectionError
from tin_lite.linkedin_session import validate_session

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
ARGUMENTS = "proto.sdui.actions.requests.RequestedArguments"
OVERVIEW = "com.linkedin.voyager.identity.me.wvmpOverview."


def member(index: int) -> str:
    return f"ACoAAViewer{index:04d}" + "x" * 21


def request(start: int) -> dict:
    """LinkedIn's request for the page from `start`, as a page names it."""

    keys = [
        "entityListQueryFilterPrefixWvmpSearchFilterType_DATE_RANGE",
        "entityListQueryFilterPrefixWvmpSearchFilterType_ORGANIZATION",
    ]
    return {
        "$type": viewers.PAGINATION,
        "pagerId": viewers.PAGER,
        "trigger": {"$case": "itemDistanceTrigger"},
        "retryCount": 2,
        "requestedArguments": {
            "$type": ARGUMENTS,
            "requestedStateKeys": [{"key": {"value": {"$case": "id", "id": k}}} for k in keys],
            "payload": {
                "sortType": "ProfileViewSortType_TIME_DESCENDING",
                "start": start,
                "count": 10,
                "useViewerGrid": False,
            },
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
        },
    }


def text(value, *, size="xsmall", weight="normal", nested=False):
    children = (
        [
            [
                "$",
                "$Sreact.fragment",
                "text-attr-0",
                {
                    "children": [
                        [None, value, ["$", "span", None, {"children": " "}]],
                        ["$", "$L9", None, {"aria-label": "Verified", "imageId": 304}],
                        None,
                    ]
                },
            ]
        ]
        if nested
        else [value]
    )
    return [
        "$",
        "$L21",
        None,
        {
            "maxLineCountExpression": 1,
            "textProps": {
                "fontSize": size,
                "fontWeight": weight,
                "className": "_x1 _x2",
                "children": children,
            },
        },
    ]


def profile_navigation(identity: str) -> dict:
    return {
        "$type": "proto.sdui.actions.core.Navigate",
        "value": {
            "content": {
                "$case": "screen",
                "screen": {
                    "$type": "proto.sdui.actions.core.NavigateToScreen",
                    "screenId": viewers.PROFILE_SCREEN,
                    "url": f"/in/{identity}/",
                    "requestedArguments": {
                        "payload": {"vanityName": identity, "vieweeProfileId": OWNER}
                    },
                },
            }
        },
    }


def search_navigation() -> dict:
    url = "https://www.linkedin.com/search/results/people/?facet=x"
    return {
        "$type": "proto.sdui.actions.core.Navigate",
        "value": {"content": {"$case": "url", "url": {"urlValue": {"$case": "url", "url": url}}}},
    }


def item(navigation, texts) -> list:
    """One viewer item: a click trigger that navigates, and its text elements in order."""

    return [
        "$",
        "$L13",
        None,
        {
            "children": [
                "$",
                "$L14",
                None,
                {
                    "componentKey": "k",
                    "viewTrackingSpecs": {
                        "viewName": viewers.ITEM,
                        "interactionTypes": ["SHORT_PRESS"],
                    },
                    "children": [
                        "$",
                        "$L15",
                        None,
                        {
                            "triggers": [
                                {
                                    "$type": "proto.sdui.triggers.Trigger",
                                    "action": {"actions": [navigation]},
                                }
                            ],
                            "children": ["$", "div", None, {"className": "_y1", "children": texts}],
                        },
                    ],
                },
            ]
        },
    ]


def named(identity, name, *, degree="• 2nd", headline="Founder at Acme", viewed="Viewed 3d ago"):
    texts = [text(name, size="medium", weight="bold", nested=True)]
    if degree:
        texts.append(text(degree, size="small"))
    if headline is not None:
        texts.append(text(headline))
    if viewed:
        texts.append(text(viewed))
    texts.append(text("12 mutual connections"))
    return item(profile_navigation(identity), texts)


def private(description="Someone at a software company", viewed="Viewed 1d ago"):
    return item(
        search_navigation(), [text(description, size="medium", weight="bold"), text(viewed)]
    )


def stream(
    rows: list[tuple[str, object]], *, next_page: dict | None = None, text_row=False
) -> bytes:
    """The rows as LinkedIn sends them: imports, the list (items by reference), the items, and
    the next page's request in the list or in a text row."""

    out = bytearray(b'3:"e9e5744c902fddb98f0eb62aee5d400a"\n')
    # The client components the elements name (`$L13`), imported as LinkedIn's page does.
    for component in ("9", "10", "13", "14", "15", "21"):
        out += f'{component}:I["$3",[],"Component{component}"]\n'.encode()
    listing = {
        "items": [
            {"item": f"$L{key}"}
            for key, value in rows
            if value[0] == "$" and len(value) == 4 and "viewTrackingSpecs" in json.dumps(value)
        ]
    }
    if next_page and not text_row:
        listing["nextPageRequest"] = next_page
    out += b"e:" + json.dumps(["$", "$L10", None, listing]).encode() + b"\n"
    for key, value in rows:
        out += f"{key}:".encode() + json.dumps(value, ensure_ascii=False).encode() + b"\n"
    if next_page and text_row:
        data = json.dumps(next_page).encode()
        out += f"5:T{len(data):x},".encode() + data
    return bytes(out)


def page_one() -> bytes:
    # The headline of the first viewer is a row of its own, referenced from the item.
    first = named("ada-lovelace", "Ada Lovelace", headline=None)
    first[3]["children"][3]["children"][3]["children"][3]["children"].insert(2, "$L4a")
    return stream(
        [
            ("40", first),
            ("4a", text("Founder at Analytical Engines, Zürich")),
            ("41", private()),
            (
                "42",
                named(
                    member(2), "Grace Hopper", degree="• 1st", headline="--", viewed="Viewed 5h ago"
                ),
            ),
            ("43", named("Alan-Turing", "Alan Turing", degree=None, viewed="Viewed just now")),
            ("44", named(OWNER, "Owner Name")),
        ],
        next_page=request(10),
    )


def overview_payload() -> dict:
    def card(index, viewer, when):
        return {
            "entityUrn": f"urn:li:fs_card:{index}",
            "$type": "com.linkedin.voyager.identity.me.Card",
            "value": {
                "$type": "com.linkedin.voyager.identity.me.WvmpProfileViewCard",
                "viewer": viewer,
                "viewedAt": when,
            },
        }

    def full(index, distance):
        return {
            "$type": "com.linkedin.voyager.identity.me.FullProfileViewer",
            "profile": {
                "*miniProfile": f"urn:li:fs_miniProfile:{member(index)}",
                "distance": {"value": distance},
            },
        }

    when = int(NOW.timestamp() * 1000) - 3600_000
    minis = [
        {
            "entityUrn": f"urn:li:fs_miniProfile:{member(i)}",
            "dashEntityUrn": f"urn:li:fsd_profile:{member(i)}",
            "$type": "com.linkedin.voyager.identity.shared.MiniProfile",
            "firstName": f"First{i}",
            "lastName": f"Last{i}",
            "occupation": f"CTO at Startup {i}",
            "publicIdentifier": f"First-Last-{i}",
        }
        for i in (1, 2)
    ]
    summary = {
        "entityUrn": "urn:li:fs_wvmpCard:1",
        "value": {
            "$type": OVERVIEW + "WvmpViewersCard",
            "insightCards": [
                {
                    "value": {
                        "$type": OVERVIEW + "WvmpSummaryInsightCard",
                        "numViews": 455,
                    }
                }
            ],
        },
    }
    return {
        "data": {
            "*elements": ["urn:li:fs_wvmpCard:1"],
            "paging": {"start": 0, "count": 10, "total": 1},
        },
        "included": [
            summary,
            card(1, full(1, "DISTANCE_2"), when),
            card(2, full(1, "DISTANCE_2"), when),  # the same view in two insights
            card(3, full(2, "DISTANCE_1"), when - 86_400_000),
            card(
                4,
                {
                    "$type": "com.linkedin.voyager.identity.me.ObfuscatedProfileViewer",
                    "obfuscationString": "Someone at Example",
                },
                when,
            ),
            *minis,
        ],
    }


def test_a_list_page_names_each_viewer_and_the_next_page():
    found, hidden, following = viewers.list_page(page_one(), OWNER)
    assert hidden == 1
    assert following == request(10)
    by = {v["slug"] or v["member"]: v for v in found}
    # The member's own profile is never a viewer.
    assert set(by) == {"ada-lovelace", member(2), "alan-turing"}
    ada = by["ada-lovelace"]
    # A slug is kept as People keeps it; a referenced row is read in place.
    assert ada == {
        "member": None,
        "slug": "ada-lovelace",
        "name": "Ada Lovelace",
        "headline": "Founder at Analytical Engines, Zürich",
        "degree": "2nd",
        "age": "3d",
        "viewed_at": None,
    }
    # A profile ID in the link names the member; "--" is no headline; mutual connections,
    # after "Viewed …", are not the headline.
    assert by[member(2)]["member"] == member(2) and by[member(2)]["slug"] is None
    assert by[member(2)]["headline"] == "" and by[member(2)]["degree"] == "1st"
    assert by[member(2)]["age"] == "5h"
    assert by["alan-turing"]["degree"] is None and by["alan-turing"]["age"] == "0m"


def test_a_later_page_names_its_next_page_in_a_text_row_and_the_last_names_none():
    rows = [("40", named("viewer-x", "Viewer X"))]
    found, _, following = viewers.list_page(
        stream(rows, next_page=request(20), text_row=True), OWNER
    )
    assert [v["slug"] for v in found] == ["viewer-x"]
    assert following == request(20)
    assert viewers.list_page(stream(rows), OWNER)[2] is None


@pytest.mark.parametrize(
    "raw",
    [
        # No viewer items at all: not a page Tin knows.
        b'3:"x"\ne:["$","$L10",null,{"items":[]}]\n',
        # A named viewer without "Viewed … ago", as on a page in another language.
        stream([("40", named("viewer-x", "Viewer X", viewed="Visto hace 3 d"))]),
    ],
)
def test_a_page_tin_cant_read_says_so_rather_than_guessing(raw):
    with pytest.raises(CollectionError, match="viewers_format_changed"):
        viewers.list_page(raw, OWNER)


def test_only_this_lists_request_is_a_next_page():
    other = {**request(10), "pagerId": "com.linkedin.sdui.feed"}
    raw = stream([("40", named("viewer-x", "Viewer X"))], next_page=other)
    assert viewers.list_page(raw, OWNER)[2] is None


def test_the_overview_names_viewers_with_their_profile_id_and_when():
    found, hidden, total = viewers.overview(overview_payload(), OWNER)
    assert hidden == 1 and total == 455
    assert [v["member"] for v in found] == [member(1), member(2)]
    first = found[0]
    assert first["slug"] == "first-last-1" and first["name"] == "First1 Last1"
    assert first["headline"] == "CTO at Startup 1" and first["degree"] == "2nd"
    assert datetime.fromisoformat(first["viewed_at"]) == NOW.replace(hour=11)
    assert found[1]["degree"] == "1st"
    with pytest.raises(CollectionError, match="unsupported_contract"):
        viewers.overview({"data": {}}, OWNER)


def responder(pages: dict[int, bytes], seen: list):
    def respond(req: httpx.Request):
        seen.append(req)
        assert req.url.host == "www.linkedin.com"
        if req.url.path == "/voyager/api/me":
            return httpx.Response(200, json=me())
        if req.url.path == "/voyager/api/identity/wvmpCards":
            assert req.method == "GET"
            return httpx.Response(200, json=overview_payload())
        assert req.method == "POST"
        body = json.loads(req.content)
        assert req.headers["x-li-rsc-stream"] == "true"
        assert req.headers["referer"] == viewers.REFERER
        assert "x-restli-protocol-version" not in req.headers
        if req.url.path.endswith("/server-request"):
            assert req.url.params["sduiid"] == "WvmpEntityList"
            assert body == viewers.first_body()
            return httpx.Response(200, content=pages[0])
        assert req.url.path.endswith("/pagination")
        assert req.url.params["sduiid"] == viewers.PAGER
        start = body["paginationRequest"]["requestedArguments"]["payload"]["start"]
        # The page's own request, unchanged, with the client's arguments as the page builds them.
        assert body["paginationRequest"] == request(start)
        assert body["clientArguments"]["screenId"] == viewers.SCREEN
        assert body["clientArguments"]["states"][0]["key"].endswith("DATE_RANGE")
        return httpx.Response(200, content=pages[start])

    return respond


async def test_a_read_takes_the_overview_and_pages_until_the_end():
    seen = []
    pages = {
        0: page_one(),
        10: stream([("30", named("viewer-y", "Viewer Y"))], next_page=request(20), text_row=True),
        20: stream([("31", private()), ("32", named("viewer-z", "Viewer Z"))]),
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(pages, seen))) as client:
        result = await viewers.read_viewers(
            validate_session(session_envelope()),
            ACTOR,
            client=client,
            include_overview=True,
            now=NOW,
            sleep=no_wait,
        )
    parsed = viewers.ViewersResult.model_validate(result)
    assert parsed.pages == 3 and parsed.next is None and parsed.reason is None
    assert parsed.total_views == 455 and parsed.anonymous == 1 + 1 + 1
    # The overview's people first, then the list's.
    assert [v.member or v.slug for v in parsed.viewers] == [
        member(1),
        member(2),
        "ada-lovelace",
        member(2),
        "alan-turing",
        "viewer-y",
        "viewer-z",
    ]
    assert [r.url.path.rsplit("/", 1)[-1] for r in seen] == [
        "me",
        "wvmpCards",
        "server-request",
        "pagination",
        "pagination",
    ]


async def test_a_read_stops_at_its_pages_and_hands_back_where_to_go_on():
    seen = []
    pages = {
        0: page_one(),
        10: stream([("30", named("viewer-y", "Viewer Y"))], next_page=request(20)),
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(pages, seen))) as client:
        result = await viewers.read_viewers(
            validate_session(session_envelope()), ACTOR, client=client, pages=2, sleep=no_wait
        )
    assert result["pages"] == 2 and result["next"] == request(20)
    # Without the overview, a later read starts from the request it was handed.
    seen.clear()
    pages[20] = stream([("31", named("viewer-z", "Viewer Z"))])
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(pages, seen))) as client:
        later = await viewers.read_viewers(
            validate_session(session_envelope()),
            ACTOR,
            client=client,
            request=result["next"],
            sleep=no_wait,
        )
    assert [v["slug"] for v in later["viewers"]] == ["viewer-z"] and later["next"] is None
    assert [r.url.path.rsplit("/", 1)[-1] for r in seen] == ["me", "pagination"]


async def test_a_list_tin_cant_read_keeps_the_overview_and_says_why():
    seen = []
    pages = {0: b'3:"x"\ne:["$","$L10",null,{"items":[]}]\n'}
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder(pages, seen))) as client:
        result = await viewers.read_viewers(
            validate_session(session_envelope()),
            ACTOR,
            client=client,
            include_overview=True,
            sleep=no_wait,
        )
    assert result["reason"] == "viewers_format_changed" and result["pages"] == 0
    assert [v["member"] for v in result["viewers"]] == [member(1), member(2)]
    assert result["next"] is None


@pytest.mark.parametrize(
    ("status", "location", "code"),
    [
        (302, "https://www.linkedin.com/checkpoint/challenge/x", "challenge"),
        (401, "", "session_expired"),
        (429, "", "rate_limited"),
        (403, "", "access_denied"),
        (400, "", "viewers_format_changed"),
    ],
)
async def test_linkedin_refusals_stop_the_read(status, location, code):
    def respond(req):
        if req.url.path == "/voyager/api/me":
            return httpx.Response(200, json=me())
        return httpx.Response(status, headers={"location": location} if location else {})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if code == "viewers_format_changed":
            # The page refusing its own request means it changed: said, not raised.
            result = await viewers.read_viewers(
                validate_session(session_envelope()), ACTOR, client=client, sleep=no_wait
            )
            assert result["reason"] == code and result["viewers"] == []
            return
        with pytest.raises(CollectionError, match=code):
            await viewers.read_viewers(
                validate_session(session_envelope()), ACTOR, client=client, sleep=no_wait
            )


async def test_a_profile_id_is_looked_up_by_slug_with_the_relationship():
    seen = []

    def respond(req):
        seen.append(req)
        if req.url.path == "/voyager/api/me":
            return httpx.Response(200, json=me())
        assert req.url.path == "/voyager/api/identity/dash/profiles"
        slug = req.url.params["memberIdentity"]
        if slug == "nobody":
            return httpx.Response(200, json={"data": {}, "included": []})
        found = member(7)
        return httpx.Response(
            200,
            json={
                "data": {},
                "included": [
                    {
                        "entityUrn": f"urn:li:fsd_profile:{found}",
                        "publicIdentifier": "Ada-Lovelace",
                    },
                    {
                        "entityUrn": f"urn:li:fsd_memberRelationship:{found}",
                        "memberRelationshipUnion": {"noConnection": {"noInvitation": {}}},
                    },
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await viewers.read_profile_ids(
            validate_session(session_envelope()),
            ACTOR,
            client=client,
            slugs=["ada-lovelace", "nobody"],
            sleep=no_wait,
        )
    assert viewers.ProfileIdsResult.model_validate(result).people[0].member == member(7)
    assert result["people"] == [
        {"slug": "ada-lovelace", "member": member(7), "state": "none"},
        {"slug": "nobody", "member": None, "state": "unknown"},
    ]
    with pytest.raises(CollectionError, match="unsupported_contract"):
        await viewers.read_profile_ids(None, ACTOR, client=None, slugs=["a/b"])


def test_a_result_is_checked_before_tin_uses_it():
    good = {
        "viewers": [],
        "anonymous": 0,
        "pages": 1,
        "total_views": None,
        "next": request(10),
        "reason": None,
        "read_at": NOW.isoformat(),
    }
    viewers.ViewersResult.model_validate(good)
    for bad in (
        {**good, "next": {**request(10), "pagerId": "other"}},
        {
            **good,
            "viewers": [
                {
                    "member": None,
                    "slug": None,
                    "name": "X",
                    "headline": "",
                    "degree": None,
                    "age": "1d",
                    "viewed_at": None,
                }
            ],
        },
        {
            **good,
            "viewers": [
                {
                    "member": "not-an-id",
                    "slug": None,
                    "name": "X",
                    "headline": "",
                    "degree": None,
                    "age": "1d",
                    "viewed_at": None,
                }
            ],
        },
    ):
        with pytest.raises(ValueError):
            viewers.ViewersResult.model_validate(bad)
