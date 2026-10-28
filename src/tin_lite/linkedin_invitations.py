"""Connection requests from a member's LinkedIn, sent with their saved session (#450).

Two calls on www.linkedin.com, each after the same /me check the readers make:
- a relationship read for a few people at a time: connected, invited (pending), or neither;
- one connection request, sent only after a fresh relationship read says neither, without a
  note.

The relationship read is a GET of the person's top card. The request is the single POST this
module makes, to one fixed path. An answer that might mean the request went out is reported
as uncertain, so it is never sent again; a later relationship read settles it.

This module runs in the LinkedIn sandbox image, so it imports only the standard library,
httpx, pydantic and the collection and session modules copied beside it.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from typing import Literal
from urllib.parse import quote

from pydantic import Field, field_validator

from tin_lite.connection_collection import ClosedModel, CollectionError
from tin_lite.linkedin_engagement import MEMBER_ID, ORIGIN, _headers, _me
from tin_lite.linkedin_session import _get

TOP_CARD = "com.linkedin.voyager.dash.deco.identity.profile.TopCardSupplementary-120"
INVITE = (
    "/voyager/api/voyagerRelationshipsDashMemberRelationships?action=verifyQuotaAndCreateV2"
    "&decorationId=com.linkedin.voyager.dash.deco.relationships.InvitationCreationResultWithInvitee-2"
)
# People one relationship read looks at, with a person's pause between them.
RELATIONSHIPS_PER_READ = 20
GAP_SECONDS = (4.0, 11.0)
INVITATION = re.compile(r"urn:li:fsd_invitation:[0-9A-Za-z_-]{1,80}")
_RANDOM = secrets.SystemRandom()

State = Literal["connected", "invited", "none", "unknown"]
# What one request came to:
# - sent: LinkedIn accepted it;
# - connected / invited: the fresh read found them connected or already invited, so nothing
#   was sent;
# - unknown_person: LinkedIn didn't show this person's relationship, so nothing was sent;
# - refused: LinkedIn refused this request (it went nowhere);
# - limit: LinkedIn said the account has sent enough for now;
# - uncertain: the request may have gone out; never send it again.
Outcome = Literal["sent", "connected", "invited", "unknown_person", "refused", "limit", "uncertain"]


class Relationship(ClosedModel):
    member: str
    state: State

    @field_validator("member")
    @classmethod
    def member_valid(cls, value):
        if not MEMBER_ID.fullmatch(value):
            raise ValueError("member")
        return value


class RelationshipsResult(ClosedModel):
    people: list[Relationship] = Field(max_length=RELATIONSHIPS_PER_READ)


class InviteResult(ClosedModel):
    member: str
    outcome: Outcome
    invitation: str | None = Field(default=None, max_length=120)

    @field_validator("member")
    @classmethod
    def member_valid(cls, value):
        if not MEMBER_ID.fullmatch(value):
            raise ValueError("member")
        return value


def _walk(value):
    if isinstance(value, dict):
        yield value
        for inner in value.values():
            yield from _walk(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _walk(inner)


def relationship_state(payload, member: str) -> State:
    """The relationship on a person's top card: connected, invited, none, or unknown.

    LinkedIn returns the member's relationship as a `MemberRelationship` whose
    `memberRelationshipUnion` names a connection, or `noConnection` holding either an
    invitation or `noInvitation`. Anything else is unknown, and nothing is sent.
    """

    if not isinstance(payload, dict):
        return "unknown"
    wanted = f"urn:li:fsd_memberRelationship:{member}"
    for item in _walk(payload):
        if item.get("entityUrn") != wanted:
            continue
        union = item.get("memberRelationshipUnion") or item.get("memberRelationshipData")
        if not isinstance(union, dict):
            return "unknown"
        keys = set(union)
        if keys & {"connection", "*connection"}:
            return "connected"
        if "noConnection" in keys:
            found = keys | {key for node in _walk(union["noConnection"]) for key in node}
            if "noInvitation" in found:
                return "none"
            if found & {"invitation", "*invitation"}:
                return "invited"
        return "unknown"
    return "unknown"


async def _sleep(gap, sleep):
    await sleep(_RANDOM.uniform(*gap))


async def _relationship(client, session, member: str) -> State:
    payload = await _get(
        client,
        session,
        "/voyager/api/identity/dash/profiles",
        {"q": "memberIdentity", "memberIdentity": member, "decorationId": TOP_CARD},
        request_headers=_headers(
            session,
            page="d_flagship3_profile_view_base",
            referer=f"{ORIGIN}/in/{quote(member)}/",
        ),
    )
    return relationship_state(payload, member)


async def read_relationships(
    session, actor, *, client, members: list[str], gap=GAP_SECONDS, sleep=asyncio.sleep
) -> dict:
    """Where the member stands with each of up to RELATIONSHIPS_PER_READ people."""

    if not 0 < len(members) <= RELATIONSHIPS_PER_READ or not all(
        MEMBER_ID.fullmatch(m) for m in members
    ):
        raise CollectionError("unsupported_contract")
    await _me(client, session, actor)
    people = []
    for member in members:
        await _sleep(gap, sleep)
        people.append({"member": member, "state": await _relationship(client, session, member)})
    return {"people": people}


def _write_headers(session) -> dict:
    result = _headers(session, page="d_flagship3_profile_view_base", referer=f"{ORIGIN}/mynetwork/")
    result["content-type"] = "application/json; charset=UTF-8"
    result["origin"] = ORIGIN
    return result


def invitation_id(headers, body) -> str | None:
    value = headers.get("x-restli-id") or ""
    if INVITATION.fullmatch(value):
        return value
    for item in _walk(body):
        urn = item.get("entityUrn") or item.get("invitationUrn")
        if isinstance(urn, str) and INVITATION.fullmatch(urn):
            return urn
    return None


async def _post_invite(client, session, member: str) -> dict:
    """The one POST: a connection request to this member, without a note.

    Signed out, a checkpoint and a block stop sending, as for reads. A refusal LinkedIn gives
    with a 4xx means nothing went out. Anything else, including no answer, is uncertain.
    """

    body = {"invitee": {"inviteeUnion": {"memberProfile": f"urn:li:fsd_profile:{member}"}}}
    try:
        async with client.stream(
            "POST",
            ORIGIN + INVITE,
            content=json.dumps(body, separators=(",", ":")).encode(),
            headers=_write_headers(session),
            follow_redirects=False,
        ) as response:
            status = response.status_code
            if 300 <= status < 400:
                location = response.headers.get("location", "")
                raise CollectionError(
                    "challenge"
                    if any(v in location for v in ("checkpoint", "challenge"))
                    else "session_expired"
                )
            if status == 401:
                raise CollectionError("session_expired")
            if status in {403, 999}:
                raise CollectionError("access_denied")
            if status == 429:
                return {"member": member, "outcome": "limit"}
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 200_000:
                    break
            text = bytes(raw[:200_000]).decode("utf-8", "replace")
            if status in {200, 201}:
                try:
                    parsed = json.loads(text) if text else {}
                except ValueError:
                    parsed = {}
                invitation = invitation_id(response.headers, parsed)
                return {"member": member, "outcome": "sent", "invitation": invitation}
            if 400 <= status < 500:
                lowered = text.lower()
                # LinkedIn's weekly invitation limit answers with a 4xx naming it.
                if any(word in lowered for word in ("limit", "fuse", "quota")):
                    return {"member": member, "outcome": "limit"}
                return {"member": member, "outcome": "refused"}
            return {"member": member, "outcome": "uncertain"}
    except CollectionError:
        raise
    except Exception:
        return {"member": member, "outcome": "uncertain"}


async def send_invitation(
    session, actor, *, client, member: str, gap=GAP_SECONDS, sleep=asyncio.sleep
) -> dict:
    """Send one connection request, only to someone neither connected nor already invited."""

    if not MEMBER_ID.fullmatch(member):
        raise CollectionError("unsupported_contract")
    own = await _me(client, session, actor)
    if member == own:
        return {"member": member, "outcome": "refused"}
    await _sleep(gap, sleep)
    state = await _relationship(client, session, member)
    if state == "connected":
        return {"member": member, "outcome": "connected"}
    if state == "invited":
        return {"member": member, "outcome": "invited"}
    if state != "none":
        return {"member": member, "outcome": "unknown_person"}
    await _sleep(gap, sleep)
    return await _post_invite(client, session, member)
