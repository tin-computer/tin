"""Probe what a saved LinkedIn session can do from wherever this runs.

A spike for warm reach, not product code. It reads the account, the member's own activity,
one post's reactions and comments, and one profile, and with an explicit confirmation sends
one connection request. It reports statuses, counts, LinkedIn's own type names and dates,
never names, text, profile identifiers or session values.

It uses only the standard library and httpx, so linkedin_spike.py can copy it verbatim into
the LinkedIn sandbox, which can reach www.linkedin.com and nothing else.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import sys
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

BASE = "https://www.linkedin.com"
ACTIVITY = re.compile(r"urn:li:activity:(\d{15,22})")
POST = re.compile(r"urn:li:(ugcPost|share):(\d{15,22})")
VANITY = re.compile(r"^[A-Za-z0-9_%.-]{1,200}$")
MAX_BODY = 6_000_000
INVITE = (
    "/voyager/api/voyagerRelationshipsDashMemberRelationships"
    "?action=verifyQuotaAndCreateV2&decorationId="
    "com.linkedin.voyager.dash.deco.relationships.InvitationCreationResultWithInvitee-2"
)


def _cookies(session):
    return {c["name"]: c["value"] for c in session["cookies"]}


def _client_hints(user_agent):
    match = re.search(r"(?:Chrome|CriOS)/(\d+)", user_agent)
    if not match:
        return {}
    platform = (
        "macOS" if "Mac OS X" in user_agent else "Windows" if "Windows" in user_agent else "Linux"
    )
    version = match.group(1)
    return {
        "sec-ch-ua": f'"Not:A?Brand";v="99", "Google Chrome";v="{version}", '
        f'"Chromium";v="{version}"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{platform}"',
    }


def voyager_headers(session, *, write=False, page="d_flagship3_profile_view_base", referer=None):
    cookies = _cookies(session)
    context = session["browser_context"]
    headers = {
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "csrf-token": cookies["JSESSIONID"].strip('"'),
        "cookie": "; ".join(f"{c['name']}={c['value']}" for c in session["cookies"]),
        "priority": "u=1, i",
        "referer": referer or BASE + "/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": session["user_agent"],
        "accept-language": context["accept_language"],
        "x-li-lang": context["li_lang"],
        "x-li-track": json.dumps(context["li_track"], separators=(",", ":")),
        "x-restli-protocol-version": "2.0.0",
        "x-li-page-instance": f"urn:li:page:{page};"
        + base64.b64encode(secrets.token_bytes(16)).decode(),
        **_client_hints(session["user_agent"]),
    }
    if write:
        headers["content-type"] = "application/json; charset=UTF-8"
        headers["origin"] = BASE
    return headers


def document_headers(session):
    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "cookie": "; ".join(f"{c['name']}={c['value']}" for c in session["cookies"]),
        "accept-language": session["browser_context"]["accept_language"],
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": session["user_agent"],
        **_client_hints(session["user_agent"]),
    }


async def fetch(client, method, path, headers, body=None):
    """One request, never following redirects; returns status, location, type and bytes."""
    try:
        async with client.stream(
            method,
            BASE + path,
            headers=headers,
            content=None if body is None else json.dumps(body).encode(),
            follow_redirects=False,
        ) as response:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BODY:
                    break
            return {
                "status": response.status_code,
                "location": response.headers.get("location", ""),
                "type": response.headers.get("content-type", "").split(";")[0],
                "restli_id": bool(response.headers.get("x-restli-id")),
                "body": bytes(data),
            }
    except httpx.HTTPError as error:
        return {"status": 0, "error": type(error).__name__, "body": b""}


def verdict(result):
    """What LinkedIn said, in a word: ok, checkpoint, signed_out, limited, blocked, failed."""
    status = result["status"]
    location = result.get("location", "").lower()
    if 300 <= status < 400:
        if "checkpoint" in location or "challenge" in location:
            return "checkpoint"
        return "signed_out" if "login" in location or "authwall" in location else "redirect"
    if status == 200:
        if result.get("type") != "application/json" and any(
            marker in result["body"][:200_000].lower()
            for marker in (b"/checkpoint/", b"captcha", b"unusual activity")
        ):
            return "checkpoint_page"
        return "ok"
    return {
        0: "no_answer",
        401: "signed_out",
        403: "blocked",
        429: "limited",
        999: "blocked",
    }.get(status, "failed")


def parsed(result):
    try:
        return json.loads(result["body"])
    except (ValueError, UnicodeDecodeError):
        return None


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def shape(payload):
    """LinkedIn's own type names and counts; no values that could name a person."""
    types, profiles = {}, 0
    for item in walk(payload):
        kind = item.get("$type")
        if isinstance(kind, str):
            name = kind.rsplit(".", 1)[-1]
            types[name] = types.get(name, 0) + 1
        if "publicIdentifier" in item and ("firstName" in item or "headline" in item):
            profiles += 1
    elements = payload.get("data", {}).get("*elements") if isinstance(payload, dict) else None
    if elements is None and isinstance(payload, dict):
        elements = (payload.get("data") or {}).get("elements") or payload.get("elements")
    return {
        "elements": len(elements) if isinstance(elements, list) else None,
        "profiles_with_names": profiles,
        "types": dict(sorted(types.items(), key=lambda pair: -pair[1])[:12]),
    }


def day(snowflake):
    return datetime.fromtimestamp((int(snowflake) >> 22) / 1000, UTC).date().isoformat()


def relationship_hints(payload):
    """Enum-like values near relationship keys: CONNECTED, DISTANCE_2, PENDING and so on."""
    hints = set()
    for item in walk(payload):
        for key, value in item.items():
            lowered = key.lower()
            if any(word in lowered for word in ("relationship", "distance", "invitation")):
                for inner in walk(value) if isinstance(value, (dict, list)) else [{"v": value}]:
                    for leaf in inner.values():
                        if isinstance(leaf, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,40}", leaf):
                            hints.add(leaf)
                        elif isinstance(leaf, str) and leaf.startswith("com.linkedin."):
                            hints.add(leaf.rsplit(".", 1)[-1])
                if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,40}", value):
                    hints.add(value)
    return sorted(hints)[:20]


def vanity(profile_url):
    parts = urlsplit(profile_url.strip())
    pieces = [p for p in parts.path.split("/") if p]
    if (
        parts.scheme != "https"
        or (parts.hostname or "").lower() not in {"linkedin.com", "www.linkedin.com"}
        or len(pieces) != 2
        or pieces[0] != "in"
        or not VANITY.fullmatch(pieces[1])
    ):
        raise ValueError("Use a https://www.linkedin.com/in/<id> profile URL.")
    return pieces[1]


def own_identity(payload):
    for item in walk(payload):
        identifier = item.get("publicIdentifier")
        if isinstance(identifier, str) and VANITY.fullmatch(identifier):
            return identifier
    return None


def exact_profile(payload, identifier):
    """The profile entity whose public identifier is exactly the one asked for."""
    for item in walk(payload):
        if str(item.get("publicIdentifier", "")).casefold() == identifier.casefold():
            urn = str(item.get("entityUrn", ""))
            if urn.startswith("urn:li:fsd_profile:"):
                return urn
    return None


async def step(report, name, coroutine):
    started = datetime.now(UTC)
    try:
        report[name] = await coroutine
    except Exception as error:  # A spike reports every failure and carries on.
        # Only the type: a message could carry a URL with someone's profile identifier.
        report[name] = {"error": type(error).__name__}
    report[name]["seconds"] = round((datetime.now(UTC) - started).total_seconds(), 2)


async def probe(session, plan, *, transport=None):
    report = {"started": datetime.now(UTC).isoformat(), "where": plan.get("where", "")}
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=False, trust_env=False, transport=transport
    ) as client:
        state = {}

        async def me():
            result = await fetch(
                client,
                "GET",
                "/voyager/api/me",
                voyager_headers(
                    session,
                    page="d_flagship3_preload",
                    referer=BASE + "/preload/?_bprMode=vanilla",
                ),
            )
            state["me"] = own_identity(parsed(result) or {})
            return {
                "status": result["status"],
                "verdict": verdict(result),
                "identity_found": bool(state["me"]),
            }

        await step(report, "me", me())
        if report["me"].get("verdict") != "ok" or not state.get("me"):
            return report

        async def activity():
            out = {}
            for kind in ("all", "shares"):
                path = f"/in/{quote(state['me'])}/recent-activity/{kind}/"
                result = await fetch(client, "GET", path, document_headers(session))
                text = result["body"].decode("utf-8", "replace")
                activities = sorted(set(ACTIVITY.findall(text)), key=int, reverse=True)
                posts = sorted({f"{k}:{n}" for k, n in POST.findall(text)})
                if kind == "shares" or "newest" not in state:
                    state["newest"] = activities[0] if activities else None
                out[kind] = {
                    "status": result["status"],
                    "verdict": verdict(result),
                    "bytes": len(result["body"]),
                    "activities": len(activities),
                    "ugc_or_share_urns": len(posts),
                    "newest_day": day(activities[0]) if activities else None,
                    "oldest_day": day(activities[-1]) if activities else None,
                }
                await asyncio.sleep(plan.get("gap", 4))
            return out

        await step(report, "activity", activity())
        post = plan.get("post") or state.get("newest")
        report["post_day"] = day(post) if post else None
        if post:
            thread = quote(f"urn:li:activity:{post}", safe="")

            async def engagers(kind, candidates):
                out = []
                for label, path in candidates:
                    result = await fetch(client, "GET", path, voyager_headers(session))
                    payload = parsed(result)
                    out.append(
                        {
                            "endpoint": label,
                            "status": result["status"],
                            "verdict": verdict(result),
                            **(shape(payload) if isinstance(payload, dict) else {}),
                        }
                    )
                    await asyncio.sleep(plan.get("gap", 4))
                    if result["status"] == 200 and isinstance(payload, dict):
                        break
                return {"tried": out}

            await step(
                report,
                "reactions",
                engagers(
                    "reactions",
                    [
                        (
                            "voyagerSocialDashReactions",
                            "/voyager/api/voyagerSocialDashReactions?decorationId=com.linkedin."
                            "voyager.dash.deco.social.ReactionsByTypeWithProfileActions-13"
                            f"&count=10&q=reactionType&start=0&threadUrn={thread}",
                        ),
                        (
                            "feed/reactions",
                            f"/voyager/api/feed/reactions?count=10&q=reactionType&start=0"
                            f"&threadUrn={thread}",
                        ),
                    ],
                ),
            )
            await step(
                report,
                "comments",
                engagers(
                    "comments",
                    [
                        (
                            "feed/comments",
                            "/voyager/api/feed/comments?count=10&q=comments"
                            f"&sortOrder=RELEVANCE&start=0&updateId=activity:{post}",
                        ),
                    ],
                ),
            )

        async def profile(url):
            identifier = vanity(url)
            result = await fetch(
                client,
                "GET",
                f"/voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity="
                f"{quote(identifier, safe='')}",
                voyager_headers(session),
            )
            payload = parsed(result) or {}
            urn = exact_profile(payload, identifier)
            state.setdefault("profiles", {})[url] = (urn, relationship_hints(payload))
            return {
                "status": result["status"],
                "verdict": verdict(result),
                "exact_match": bool(urn),
                "relationship_hints": relationship_hints(payload),
                **shape(payload),
            }

        for index, url in enumerate(plan.get("profiles", [])):
            await step(report, f"profile_{index + 1}", profile(url))
            await asyncio.sleep(plan.get("gap", 4))

        target = plan.get("invite")
        if target and plan.get("invite_confirm") == target:

            async def invite():
                if target not in state.get("profiles", {}):
                    await profile(target)
                urn, hints = state["profiles"][target]
                if not urn:
                    return {"sent": False, "reason": "profile_not_resolved"}
                if any(h in hints for h in ("CONNECTED", "DISTANCE_1", "PENDING", "SENT")):
                    return {"sent": False, "reason": "already_connected_or_pending", "hints": hints}
                result = await fetch(
                    client,
                    "POST",
                    INVITE,
                    voyager_headers(session, write=True),
                    {"invitee": {"inviteeUnion": {"memberProfile": urn}}},
                )
                payload = parsed(result)
                return {
                    "sent": result["status"] in {200, 201},
                    "status": result["status"],
                    "verdict": verdict(result),
                    "restli_id": result.get("restli_id", False),
                    **(shape(payload) if isinstance(payload, dict) else {}),
                }

            await step(report, "invite", invite())
    report["finished"] = datetime.now(UTC).isoformat()
    return report


def main():
    """Sandbox entry: read request.json beside this file, write result.json."""
    folder = sys.argv[1] if len(sys.argv) > 1 else "/run/tin-spike"
    with open(f"{folder}/request.json") as handle:
        request = json.load(handle)
    report = asyncio.run(probe(request["session"], request["plan"]))
    with open(f"{folder}/result.json", "w") as handle:
        json.dump(report, handle)


if __name__ == "__main__":
    main()
