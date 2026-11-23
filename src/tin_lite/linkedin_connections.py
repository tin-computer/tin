"""Read a member's own LinkedIn connections, with their saved session.

Each read starts with the same /me check the other readers make, then:
- the connections list, `GET /voyager/api/relationships/dash/connections`, newest first,
  `PAGE_SIZE` at a time, the request LinkedIn's own My Network page makes. Each connection has
  when it was made and the person: profile ID, public slug, first and last name, headline;
- for the profiles Tin is asked about, `GET /voyager/api/identity/dash/profiles` with the full
  profile a profile page loads: the person's positions (company, title, dates), headline and
  where they are. The top card has the current company but no title or location.

A list it can't recognise is refused as `unsupported_contract` rather than guessed at. A
profile that shows no current position is reported as such, not filled in.

Reads wait a random few seconds between requests, like a person reading. This module runs in
the LinkedIn sandbox image, so it imports only the standard library, httpx, pydantic and the
collection and session modules copied beside it.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from pydantic import Field, field_validator

from tin_lite.connection_collection import ClosedModel, CollectionError
from tin_lite.linkedin_engagement import GAP_SECONDS, MEMBER_ID, _headers, _me, _sleep
from tin_lite.linkedin_session import _get

ORIGIN = "https://www.linkedin.com"
REFERER = f"{ORIGIN}/mynetwork/invite-connect/connections/"
PATH = "/voyager/api/relationships/dash/connections"
DECORATION = "com.linkedin.voyager.dash.deco.web.mynetwork.ConnectionListWithProfile-16"
# The full profile a profile page loads. The top card names the current company but not the
# title or the location, so positions are read from this one.
FULL_PROFILE = "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-93"
PAGE_SIZE = 40
# Pages per read: 400 connections, with pauses, inside the sandbox's five minutes.
PAGES_PER_READ = 10
# Profiles per read, one request each, with pauses: at most three minutes of waiting.
PROFILES_PER_READ = 20
SLUG = re.compile(r"[^\s/?#\\%]{1,100}")
PROFILE = "urn:li:fsd_profile:"
POSITION = re.compile(r"urn:li:fsd_profilePosition:\((ACo[A-Za-z0-9_-]{20,80}),")


class Connection(ClosedModel):
    member: str
    slug: str | None
    first_name: str = Field(max_length=200)
    last_name: str = Field(max_length=200)
    headline: str = Field(max_length=1000)
    # When the connection was made, as LinkedIn recorded it.
    connected_at: str = Field(max_length=40)

    @field_validator("member")
    @classmethod
    def member_valid(cls, value):
        if not MEMBER_ID.fullmatch(value):
            raise ValueError("member")
        return value

    @field_validator("slug")
    @classmethod
    def slug_valid(cls, value):
        if value is not None and not SLUG.fullmatch(value):
            raise ValueError("slug")
        return value


class ConnectionsResult(ClosedModel):
    """What a connections read hands back to Tin; the controller checks it before using it."""

    connections: list[Connection] = Field(max_length=PAGE_SIZE * PAGES_PER_READ)
    pages: int = Field(ge=0)
    # Where the next read starts, or None at the end of the list.
    next: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    # Connections LinkedIn listed without a profile Tin can name, such as a closed account.
    unnamed: int = Field(ge=0)
    read_at: str = Field(max_length=40)


class Position(ClosedModel):
    member: str
    # Whether LinkedIn answered with this person's profile at all.
    found: bool
    company: str = Field(max_length=300)
    position: str = Field(max_length=300)
    location: str = Field(max_length=300)
    headline: str = Field(max_length=1000)

    @field_validator("member")
    @classmethod
    def member_valid(cls, value):
        if not MEMBER_ID.fullmatch(value):
            raise ValueError("member")
        return value


class PositionsResult(ClosedModel):
    people: list[Position] = Field(max_length=PROFILES_PER_READ)
    read_at: str = Field(max_length=40)


def clean(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFC", value).split())


def _walk(node):
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def _entities(payload) -> dict[str, dict]:
    """Every entity in LinkedIn's answer by its URN, inline or in `included`."""

    found: dict[str, dict] = {}
    for item in _walk(payload):
        urn = item.get("entityUrn")
        if isinstance(urn, str) and urn not in found:
            found[urn] = item
    return found


def _member(urn) -> str | None:
    if isinstance(urn, str) and urn.startswith(PROFILE):
        member = urn[len(PROFILE) :]
        if MEMBER_ID.fullmatch(member):
            return member
    return None


def _when(value) -> str | None:
    """LinkedIn's millisecond timestamp as an ISO time, or None when it isn't one."""

    if type(value) is not int or not 946_684_800_000 <= value <= 4_102_444_800_000:
        return None
    return datetime.fromtimestamp(value / 1000, UTC).isoformat()


def connections_page(payload) -> tuple[list[dict], int, int | None, int]:
    """A page's connections in LinkedIn's order, how many it listed, LinkedIn's total and how
    many it listed without a profile Tin can name."""

    if not isinstance(payload, dict):
        raise CollectionError("unsupported_contract")
    entities = _entities(payload)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    order = data.get("*elements")
    if isinstance(order, list):
        items = [entities.get(urn) for urn in order if isinstance(urn, str)]
    elif isinstance(data.get("elements"), list):
        items = data["elements"]
    else:
        raise CollectionError("unsupported_contract")
    paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
    total = paging.get("total") if type(paging.get("total")) is int else None
    connections, unnamed = [], 0
    for item in items:
        if not isinstance(item, dict) or "createdAt" not in item:
            unnamed += 1
            continue
        ref = item.get("connectedMember") or item.get("*connectedMemberResolutionResult")
        profile = item.get("connectedMemberResolutionResult")
        if not isinstance(profile, dict):
            profile = entities.get(ref) if isinstance(ref, str) else None
        member = _member(ref) or _member((profile or {}).get("entityUrn"))
        connected_at = _when(item.get("createdAt"))
        if not isinstance(profile, dict) or member is None or connected_at is None:
            unnamed += 1
            continue
        slug = clean(profile.get("publicIdentifier")) or None
        connections.append(
            {
                "member": member,
                "slug": slug if slug and SLUG.fullmatch(slug) else None,
                "first_name": clean(profile.get("firstName"))[:200],
                "last_name": clean(profile.get("lastName"))[:200],
                "headline": clean(profile.get("headline"))[:1000],
                "connected_at": connected_at,
            }
        )
    if items and not connections and unnamed == len(items):
        # A page of entries Tin can't read at all: LinkedIn changed the answer.
        raise CollectionError("unsupported_contract")
    return connections, len(items), total, unnamed


async def read_connections(
    session,
    actor,
    *,
    client,
    start: int = 0,
    pages: int = PAGES_PER_READ,
    now: datetime | None = None,
    gap=GAP_SECONDS,
    sleep=asyncio.sleep,
) -> dict:
    """Up to `pages` pages of the member's connections, newest first, from `start`. `next`
    says where the next read starts, or None at the end of the list."""

    if type(start) is not int or not 0 <= start <= 100_000:
        raise CollectionError("unsupported_contract")
    await _me(client, session, actor)
    connections, total, unnamed, read, position = [], None, 0, 0, start
    for _ in range(max(1, min(pages, PAGES_PER_READ))):
        await _sleep(gap, sleep)
        payload = await _get(
            client,
            session,
            PATH,
            {
                "decorationId": DECORATION,
                "count": PAGE_SIZE,
                "q": "search",
                "sortType": "RECENTLY_ADDED",
                "start": position,
            },
            request_headers=_headers(
                session, page="d_flagship3_people_connections", referer=REFERER
            ),
        )
        found, listed, page_total, hidden = connections_page(payload)
        read += 1
        connections.extend(found)
        unnamed += hidden
        total = page_total if page_total is not None else total
        position += listed
        if listed < PAGE_SIZE or (total is not None and position >= total):
            position = None
            break
    return {
        "connections": connections,
        "pages": read,
        "next": position,
        "total": total,
        "unnamed": unnamed,
        "read_at": (now or datetime.now(UTC)).isoformat(),
    }


def _date(range_part) -> tuple[int, int]:
    if not isinstance(range_part, dict):
        return (0, 0)
    year, month = range_part.get("year"), range_part.get("month")
    return (year if type(year) is int else 0, month if type(month) is int else 0)


def profile_position(payload, member: str) -> dict[str, Any]:
    """The person's current position (the most recent one with no end), where they are and
    their headline, from LinkedIn's answer for their profile."""

    entities = _entities(payload)
    profile = entities.get(PROFILE + member)
    current = []
    for urn, item in entities.items():
        match = POSITION.match(urn)
        if not match or match[1] != member:
            continue
        dates = item.get("dateRange") if isinstance(item.get("dateRange"), dict) else {}
        if dates.get("end"):
            continue
        company = clean(item.get("companyName"))
        if not company:
            linked = item.get("company") or entities.get(item.get("*company") or "")
            company = clean((linked or {}).get("name")) if isinstance(linked, dict) else ""
        title = clean(item.get("title"))
        if company or title:
            current.append((_date(dates.get("start")), company, title))
    current.sort(key=lambda entry: entry[0], reverse=True)
    location = ""
    if isinstance(profile, dict):
        geo = profile.get("geoLocation")
        if isinstance(geo, dict):
            place = geo.get("geo") or entities.get(geo.get("*geo") or "")
            if isinstance(place, dict):
                location = clean(place.get("defaultLocalizedName"))
        location = location or clean(profile.get("locationName"))
    return {
        "member": member,
        "found": isinstance(profile, dict),
        "company": current[0][1][:300] if current else "",
        "position": current[0][2][:300] if current else "",
        "location": location[:300],
        "headline": clean((profile or {}).get("headline"))[:1000],
    }


async def read_positions(
    session,
    actor,
    *,
    client,
    members: list[str],
    now: datetime | None = None,
    gap=GAP_SECONDS,
    sleep=asyncio.sleep,
) -> dict:
    """Each person's current company, position and location, for up to PROFILES_PER_READ."""

    if not 0 < len(members) <= PROFILES_PER_READ or not all(
        isinstance(m, str) and MEMBER_ID.fullmatch(m) for m in members
    ):
        raise CollectionError("unsupported_contract")
    await _me(client, session, actor)
    people = []
    for member in members:
        await _sleep(gap, sleep)
        payload = await _get(
            client,
            session,
            "/voyager/api/identity/dash/profiles",
            {"q": "memberIdentity", "memberIdentity": member, "decorationId": FULL_PROFILE},
            request_headers=_headers(
                session, page="d_flagship3_profile_view_base", referer=f"{ORIGIN}/in/{member}/"
            ),
        )
        people.append(profile_position(payload, member))
    return {"people": people, "read_at": (now or datetime.now(UTC)).isoformat()}
