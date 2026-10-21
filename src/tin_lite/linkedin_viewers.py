"""Read who viewed a member's LinkedIn profile, with their saved session.

Each read starts with the same /me check the other readers make, then:
- the overview, `GET /voyager/api/identity/wvmpCards`: LinkedIn's highlights of the last 90
  days (the latest viewers, notable ones, by company and title), each named viewer with their
  profile ID, degree and when they looked. A sample, not the whole list;
- the list on the profile-views page, newest first, ten at a time. The page is server-driven
  UI: its own two read-only requests (`server-request` for the first page, `pagination` for
  each next one) answer with React Server Components rows. Each viewer is an item tagged
  `viewer-list-item`, holding their name, degree, headline, "Viewed 3d ago" and a link to their
  profile; a viewer who browses privately has no link. Every page names the request for the
  next one, which the reader hands back unchanged.

A list it can't recognise is reported as `viewers_format_changed`, keeping the overview, rather
than guessed at. Also a lookup of profile IDs by public slug, for viewers the list names only
by slug: sending a connection request needs the ID.

Reads wait a random few seconds between pages, like a person reading. This module runs in the
LinkedIn sandbox image, so it imports only the standard library, httpx, pydantic and the
collection and session modules copied beside it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import unicodedata
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, unquote

from pydantic import Field, field_validator, model_validator

from tin_lite.connection_collection import ClosedModel, CollectionError
from tin_lite.linkedin_engagement import GAP_SECONDS, MEMBER_ID, _headers, _me, _sleep
from tin_lite.linkedin_invitations import TOP_CARD, relationship_state
from tin_lite.linkedin_session import _get, headers

ORIGIN = "https://www.linkedin.com"
REFERER = f"{ORIGIN}/analytics/profile-views/"
PAGE_SIZE = 10
# Pages one read may take before handing back where it stopped; the caller continues there.
PAGES_PER_READ = 10
IDS_PER_READ = 20
# The overview's cards over 90 days; a runaway guard well above what LinkedIn returns.
MAX_OVERVIEW = 60
# A list page is about 150 KB; a runaway guard well above it.
MAX_PAGE_BYTES = 3_000_000
MAX_ROWS = 20_000
PAGER = "com.linkedin.sdui.premium.wvmp.entityList"
PAGINATION = "proto.sdui.actions.requests.PaginationRequest"
PROFILE_SCREEN = "com.linkedin.sdui.flagshipnav.profile.Profile"
SCREEN = "com.linkedin.sdui.flagshipnav.home.Home"
ITEM = "viewer-list-item"
DATE_RANGE = "entityListQueryFilterPrefixWvmpSearchFilterType_DATE_RANGE"
# A public slug as People keeps it: decoded, lowercased, one path segment.
SLUG = re.compile(r"[^\s/?#\\%]{1,100}")
DEGREE = re.compile(r"[•·]?\s*(1st|2nd|3rd)\+?")
VIEWED = re.compile(r"Viewed (\d{1,3}) ?(m|min|mins|h|hr|hrs|d|w|wk|mo|y|yr|yrs) ago")
# "Viewed just now" and the like: the list says when in words for the last few minutes.
VIEWED_NOW = re.compile(r"Viewed (just now|now|today|a moment ago)")
DISTANCE = {"DISTANCE_1": "1st", "DISTANCE_2": "2nd", "DISTANCE_3": "3rd"}
REF = re.compile(r"\$L?([0-9a-f]{1,8})")
TEXT_ROW = re.compile(rb"T([0-9a-f]{1,8}),")


class Viewer(ClosedModel):
    member: str | None
    slug: str | None
    name: str = Field(min_length=1, max_length=200)
    headline: str = Field(max_length=1000)
    degree: Literal["1st", "2nd", "3rd"] | None
    # "3d" as the list says it, or the overview's exact time.
    age: str | None = Field(max_length=8)
    viewed_at: str | None = Field(max_length=40)

    @field_validator("member")
    @classmethod
    def member_valid(cls, value):
        if value is not None and not MEMBER_ID.fullmatch(value):
            raise ValueError("member")
        return value

    @field_validator("slug")
    @classmethod
    def slug_valid(cls, value):
        if value is not None and not SLUG.fullmatch(value):
            raise ValueError("slug")
        return value

    @model_validator(mode="after")
    def named(self):
        if not self.member and not self.slug:
            raise ValueError("viewer")
        if not self.age and not self.viewed_at:
            raise ValueError("when")
        return self


class ViewersResult(ClosedModel):
    """What a viewers read hands back to Tin; the controller checks it before using it."""

    viewers: list[Viewer] = Field(max_length=PAGE_SIZE * PAGES_PER_READ + MAX_OVERVIEW)
    anonymous: int = Field(ge=0)
    pages: int = Field(ge=0)
    total_views: int | None
    # The next page's request, as LinkedIn wrote it, or None at the end of the list.
    next: dict | None
    reason: Literal["viewers_format_changed"] | None
    read_at: str = Field(max_length=40)

    @field_validator("next")
    @classmethod
    def next_valid(cls, value):
        if value is not None and not next_request_valid(value):
            raise ValueError("next")
        return value


class ProfileId(ClosedModel):
    slug: str
    member: str | None
    state: Literal["connected", "invited", "none", "unknown"]

    @field_validator("slug")
    @classmethod
    def slug_valid(cls, value):
        if not SLUG.fullmatch(value):
            raise ValueError("slug")
        return value

    @field_validator("member")
    @classmethod
    def member_valid(cls, value):
        if value is not None and not MEMBER_ID.fullmatch(value):
            raise ValueError("member")
        return value


class ProfileIdsResult(ClosedModel):
    people: list[ProfileId] = Field(max_length=IDS_PER_READ)


def clean(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFC", value).split())


def next_request_valid(value) -> bool:
    if not isinstance(value, dict) or value.get("$type") != PAGINATION:
        return False
    if value.get("pagerId") != PAGER or len(json.dumps(value)) > 20_000:
        return False
    arguments = value.get("requestedArguments")
    payload = arguments.get("payload") if isinstance(arguments, dict) else None
    return isinstance(payload, dict) and type(payload.get("start")) is int


# The overview.


def overview(payload, profile_id: str) -> tuple[list[dict], int, int | None]:
    """The overview's named viewers, how many were anonymous, and LinkedIn's count of views."""

    if not isinstance(payload, dict) or not isinstance(payload.get("included"), list):
        raise CollectionError("unsupported_contract")
    included = [row for row in payload["included"] if isinstance(row, dict)]
    by_urn = {row.get("entityUrn"): row for row in included if row.get("entityUrn")}
    viewers, seen, anonymous, total = [], set(), 0, None
    for row in included:
        value = row.get("value") if isinstance(row.get("value"), dict) else {}
        kind = str(value.get("$type", ""))
        if kind.endswith("WvmpViewersCard"):
            for card in value.get("insightCards") or []:
                inner = card.get("value") if isinstance(card, dict) else None
                if isinstance(inner, dict) and str(inner.get("$type", "")).endswith(
                    "WvmpSummaryInsightCard"
                ):
                    views = inner.get("numViews")
                    total = views if type(views) is int and views >= 0 else total
        if not kind.endswith("WvmpProfileViewCard"):
            continue
        viewer = value.get("viewer") if isinstance(value.get("viewer"), dict) else {}
        if not str(viewer.get("$type", "")).endswith("FullProfileViewer"):
            anonymous += 1
            continue
        profile = viewer.get("profile") if isinstance(viewer.get("profile"), dict) else {}
        mini = by_urn.get(profile.get("*miniProfile")) or {}
        urn = str(mini.get("dashEntityUrn") or mini.get("entityUrn") or "")
        member = urn.rsplit(":", 1)[-1]
        when = value.get("viewedAt")
        if not MEMBER_ID.fullmatch(member) or member == profile_id or type(when) is not int:
            continue
        if (member, when) in seen:
            continue
        seen.add((member, when))
        slug = mini.get("publicIdentifier")
        distance = profile.get("distance") if isinstance(profile.get("distance"), dict) else {}
        name = clean(f"{clean(mini.get('firstName'))} {clean(mini.get('lastName'))}")[:200]
        if not name:
            continue
        viewers.append(
            {
                "member": member,
                "slug": (
                    slug.casefold() if isinstance(slug, str) and SLUG.fullmatch(slug) else None
                ),
                "name": name,
                "headline": clean(mini.get("occupation"))[:1000],
                "degree": DISTANCE.get(distance.get("value")),
                "age": None,
                "viewed_at": datetime.fromtimestamp(when / 1000, UTC).isoformat(),
            }
        )
    return viewers[:MAX_OVERVIEW], anonymous, total


# The list: React Server Components rows.


def rows(raw: bytes) -> dict[str, Any]:
    """The stream's rows by ID, each parsed. A `T` row is text of a stated byte length (it may
    hold JSON, or span lines); a row tagged with a letter (`I`, `E`, …) is JSON after it;
    anything else that isn't JSON is kept as text."""

    found: dict[str, Any] = {}
    i, n = 0, len(raw)
    while i < n:
        if len(found) >= MAX_ROWS:
            raise CollectionError("unsupported_contract")
        colon = raw.find(b":", i)
        if colon < 0:
            break
        key = raw[i:colon].decode("ascii", "replace").strip()
        start = colon + 1
        text = TEXT_ROW.match(raw, start)
        if text:
            begin = text.end()
            end = begin + int(text[1], 16)
            data, i = raw[begin:end], end
        else:
            end = raw.find(b"\n", start)
            end = n if end < 0 else end
            data, i = raw[start:end], end + 1
        value = data.decode("utf-8", "replace")
        for candidate in (value, value[1:] if value[:1].isalpha() else None):
            if candidate is None:
                continue
            try:
                found[key] = json.loads(candidate)
                break
            except ValueError:
                continue
        else:
            found[key] = value
    return found


def _resolve(values: dict, node, depth: int = 0, seen: frozenset = frozenset()):
    """A row's tree with its references to other rows (`$L1a`, `$1a`) filled in."""

    if depth > 200:
        raise CollectionError("unsupported_contract")
    if isinstance(node, str):
        match = REF.fullmatch(node)
        if match and match[1] in values and match[1] not in seen:
            return _resolve(values, values[match[1]], depth + 1, seen | {match[1]})
        return node
    if isinstance(node, list):
        return [_resolve(values, item, depth + 1, seen) for item in node]
    if isinstance(node, dict):
        return {key: _resolve(values, item, depth + 1, seen) for key, item in node.items()}
    return node


def _walk(node):
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def _marked(node) -> bool:
    for item in _walk(node):
        specs = item.get("viewTrackingSpecs")
        if isinstance(specs, dict) and specs.get("viewName") == ITEM:
            return True
    return False


def _text(children) -> str:
    """What a text element shows: its children's strings, through the elements they nest
    (`["$", type, key, props]`), never their attributes."""

    parts: list[str] = []

    def take(node):
        if isinstance(node, str):
            if not node.startswith("$"):
                parts.append(node)
        elif isinstance(node, list):
            if len(node) == 4 and node[0] == "$" and isinstance(node[3], dict):
                take(node[3].get("children"))
            else:
                for item in node:
                    take(item)
        elif isinstance(node, dict):
            take(node.get("children"))

    take(children)
    return clean("".join(parts))


def _texts(tree) -> list[str]:
    """Each text element in the item, in reading order."""

    found = []

    def visit(node):
        if isinstance(node, dict):
            props = node.get("textProps")
            if isinstance(props, dict):
                text = _text(props.get("children"))
                if text:
                    found.append(text)
                return
            for item in node.values():
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(tree)
    return found


def _profile(tree) -> str | None:
    """The viewer's profile, from the item's navigation to LinkedIn's profile screen."""

    for item in _walk(tree):
        if item.get("screenId") != PROFILE_SCREEN or not isinstance(item.get("url"), str):
            continue
        match = re.fullmatch(r"(?:https://www\.linkedin\.com)?/in/([^/?#]{1,300})/?", item["url"])
        if match:
            return unquote(match[1])
    return None


def viewer_item(tree, profile_id: str) -> dict | None:
    """One list item as a viewer; None for someone browsing privately, and an empty dict for
    an item that is neither (the member's own profile)."""

    identity = _profile(tree)
    if identity is None:
        return None
    texts = _texts(tree)
    position = next(
        (i for i, t in enumerate(texts) if VIEWED.fullmatch(t) or VIEWED_NOW.fullmatch(t)), None
    )
    # An English page says "Viewed 3d ago"; anything else is a page Tin can't read yet.
    if position is None:
        raise CollectionError("viewers_format_changed")
    viewed = VIEWED.fullmatch(texts[position])
    degree = next((m[1] for t in texts if (m := DEGREE.fullmatch(t))), None)
    # Name first, then the headline; what follows "Viewed …" (mutual connections) isn't theirs.
    rest = [t for t in texts[:position] if not DEGREE.fullmatch(t)]
    if not rest:
        raise CollectionError("viewers_format_changed")
    name, headline = rest[0], (rest[1] if len(rest) > 1 else "")
    member = identity if MEMBER_ID.fullmatch(identity) else None
    slug = None if member else identity.casefold()
    # The member's own profile, or a link Tin can't keep: no viewer, and no private one either.
    if member == profile_id or (slug and not SLUG.fullmatch(slug)):
        return {}
    return {
        "member": member,
        "slug": slug,
        "name": name[:200],
        "headline": "" if headline in {"--", "-"} else headline[:1000],
        "degree": degree,
        "age": f"{viewed[1]}{viewed[2]}"[:8] if viewed else "0m",
        "viewed_at": None,
    }


def list_page(raw: bytes, profile_id: str) -> tuple[list[dict], int, dict | None]:
    """One page of the list: the named viewers, how many browsed privately, and the request
    for the next page (None at the end)."""

    values = rows(raw)
    viewers, anonymous, items = [], 0, 0
    for key, value in values.items():
        if not _marked(value):
            continue
        tree = _resolve(values, value, seen=frozenset({key}))
        # A row holding the whole list (it marks every item) is not one item.
        marks = sum(
            1
            for item in _walk(tree)
            if isinstance(item.get("viewTrackingSpecs"), dict)
            and item["viewTrackingSpecs"].get("viewName") == ITEM
        )
        if marks != 1:
            continue
        items += 1
        found = viewer_item(tree, profile_id)
        if found is None:
            anonymous += 1
        elif found:
            viewers.append(found)
    request = next(
        (
            item
            for value in values.values()
            for item in _walk(value)
            if item.get("$type") == PAGINATION and item.get("pagerId") == PAGER
        ),
        None,
    )
    if not items or items > PAGE_SIZE * 3:
        raise CollectionError("viewers_format_changed")
    if request is not None and not next_request_valid(request):
        raise CollectionError("viewers_format_changed")
    return viewers, anonymous, request


def first_body() -> dict:
    """The profile-views page's own first request: newest first, the date-range filter empty."""

    state_keys = [{"key": {"value": {"$case": "id", "id": DATE_RANGE}}}]
    payload = {
        "sortType": "ProfileViewSortType_TIME_DESCENDING",
        "filterTypeList": ["WvmpSearchFilterType_DATE_RANGE"],
        "dateRangeSelectionFilters": {"key": DATE_RANGE, "namespace": "MemoryNamespace"},
    }
    metadata = {"$type": "proto.sdui.common.RequestMetadata"}
    arguments = "proto.sdui.actions.requests.RequestedArguments"
    states = _states()
    return {
        "requestId": "WvmpEntityList",
        "serverRequest": {
            "requestId": "WvmpEntityList",
            "requestedArguments": {
                "$type": arguments,
                "requestedStateKeys": state_keys,
                "payload": payload,
                "requestMetadata": metadata,
            },
            "isApfcEnabled": False,
            "isStreaming": False,
            "rumPageKey": "",
        },
        "states": states,
        "requestedArguments": {
            "$type": arguments,
            "requestedStateKeys": state_keys,
            "payload": payload,
            "requestMetadata": metadata,
            "states": states,
            "screenId": SCREEN,
            "knownTemplateIds": [],
        },
    }


def _states() -> list[dict]:
    # The page sets only the date-range filter (to none) before it asks; the others stay unset.
    return [
        {
            "key": DATE_RANGE,
            "namespace": "MemoryNamespace",
            "value": [],
            "originalProtoCase": "stringListValue",
            "protoKey": {"$type": "proto.sdui.Key", "value": {"$case": "id", "id": DATE_RANGE}},
        }
    ]


def next_body(request: dict) -> dict:
    """The page's pagination request: the next-page request the last page named, with the
    client's arguments as the page builds them."""

    arguments = request["requestedArguments"]
    return {
        "pagerId": request["pagerId"],
        "clientArguments": {
            **arguments,
            "states": _states(),
            "screenId": SCREEN,
            "knownTemplateIds": [],
        },
        "paginationRequest": request,
    }


async def _post(client, session, action: str, sduiid: str, body: dict) -> bytes:
    request_headers = headers(session)
    request_headers.pop("x-restli-protocol-version", None)
    request_headers.update(
        {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": ORIGIN,
            "referer": REFERER,
            "x-li-rsc-stream": "true",
        }
    )
    span = base64.b64encode(secrets.token_bytes(8)).decode()
    async with client.stream(
        "POST",
        f"{ORIGIN}/flagship-web/rsc-action/actions/{action}",
        params={"sduiid": sduiid, "parentSpanId": span},
        content=json.dumps(body, separators=(",", ":")).encode(),
        headers=request_headers,
        follow_redirects=False,
    ) as response:
        status = response.status_code
        if status == 429:
            raise CollectionError("rate_limited")
        if status == 401:
            raise CollectionError("session_expired")
        if 300 <= status < 400:
            location = response.headers.get("location", "")
            raise CollectionError(
                "challenge"
                if any(v in location for v in ("checkpoint", "challenge"))
                else "session_expired"
            )
        if status in {403, 999}:
            raise CollectionError("access_denied")
        if 400 <= status < 500:
            # The page's request itself was refused: LinkedIn changed what it takes.
            raise CollectionError("viewers_format_changed")
        if status != 200:
            raise CollectionError("cloud_failed")
        body_bytes = bytearray()
        async for chunk in response.aiter_bytes():
            body_bytes.extend(chunk)
            if len(body_bytes) > MAX_PAGE_BYTES:
                raise CollectionError("viewers_format_changed")
        return bytes(body_bytes)


async def read_viewers(
    session,
    actor,
    *,
    client,
    request: dict | None = None,
    include_overview: bool = False,
    pages: int = PAGES_PER_READ,
    now: datetime | None = None,
    gap=GAP_SECONDS,
    sleep=asyncio.sleep,
) -> dict:
    """Up to `pages` pages of the list, from its start or from the request the last read handed
    back; with `include_overview`, the overview first. `next` says where to go on."""

    if request is not None and not next_request_valid(request):
        raise CollectionError("unsupported_contract")
    profile_id = await _me(client, session, actor)
    viewers, anonymous, total, reason, read = [], 0, None, None, 0
    if include_overview:
        await _sleep(gap, sleep)
        try:
            payload = await _get(
                client,
                session,
                "/voyager/api/identity/wvmpCards",
                request_headers=_headers(
                    session, page="d_flagship3_profile_view_base", referer=REFERER
                ),
            )
            viewers, anonymous, total = overview(payload, profile_id)
        except CollectionError as error:
            # The overview is a bonus; only what stops every read stops this one.
            if error.code not in {"unsupported_contract", "unsupported_search_contract"}:
                raise
    for _ in range(max(1, min(pages, PAGES_PER_READ))):
        await _sleep(gap, sleep)
        try:
            if request is None:
                raw = await _post(client, session, "server-request", "WvmpEntityList", first_body())
            else:
                raw = await _post(client, session, "pagination", PAGER, next_body(request))
            found, hidden, request = list_page(raw, profile_id)
        except CollectionError as error:
            if error.code != "viewers_format_changed":
                raise
            reason, request = error.code, None
            break
        read += 1
        viewers.extend(found)
        anonymous += hidden
        if request is None:
            break
    return {
        "viewers": viewers,
        "anonymous": anonymous,
        "pages": read,
        "total_views": total,
        "next": request,
        "reason": reason,
        "read_at": (now or datetime.now(UTC)).isoformat(),
    }


def profile_id_of(payload, slug: str) -> str | None:
    """The profile ID behind a public slug, from LinkedIn's answer for that slug."""

    for item in _walk(payload):
        urn = item.get("entityUrn")
        public = item.get("publicIdentifier")
        if (
            isinstance(urn, str)
            and urn.startswith("urn:li:fsd_profile:")
            and isinstance(public, str)
            and public.casefold() == slug.casefold()
        ):
            member = urn.rsplit(":", 1)[1]
            if MEMBER_ID.fullmatch(member):
                return member
    return None


async def read_profile_ids(
    session, actor, *, client, slugs: list[str], gap=GAP_SECONDS, sleep=asyncio.sleep
) -> dict:
    """Each slug's profile ID and where the member stands with them, for up to IDS_PER_READ."""

    if not 0 < len(slugs) <= IDS_PER_READ or not all(SLUG.fullmatch(s) for s in slugs):
        raise CollectionError("unsupported_contract")
    await _me(client, session, actor)
    people = []
    for slug in slugs:
        await _sleep(gap, sleep)
        payload = await _get(
            client,
            session,
            "/voyager/api/identity/dash/profiles",
            {"q": "memberIdentity", "memberIdentity": slug, "decorationId": TOP_CARD},
            request_headers=_headers(
                session,
                page="d_flagship3_profile_view_base",
                referer=f"{ORIGIN}/in/{quote(slug, safe='-._~')}/",
            ),
        )
        member = profile_id_of(payload, slug)
        people.append(
            {
                "slug": slug,
                "member": member,
                "state": relationship_state(payload, member) if member else "unknown",
            }
        )
    return {"people": people}
