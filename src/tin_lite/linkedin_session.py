"""Small read-only transport preserving the published extension's request context.

Never follow redirects, write LinkedIn cookies back, sign out, or execute site scripts.
This is a candidate adapter: deployment qualification is separate from fixture coverage.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx

from tin_lite.connection_collection import CollectionError, CollectionSource, Person, profile_url

logger = logging.getLogger(__name__)

COOKIE_NAMES = frozenset({"li_at", "JSESSIONID", "bcookie", "bscookie", "lidc", "lang"})
LOGIN_COOKIES = frozenset({"li_at", "JSESSIONID"})
# The domains accepted for www.linkedin.com's cookies, most specific first.
COOKIE_DOMAINS = (".www.linkedin.com", "www.linkedin.com", ".linkedin.com", "linkedin.com")
TRACK_STRINGS = {"clientVersion", "mpVersion", "osName", "timezone", "deviceFormFactor", "mpName"}
TRACK_NUMBERS = {"timezoneOffset", "displayDensity", "displayWidth", "displayHeight"}
TRACK_REQUIRED = {
    "clientVersion",
    "osName",
    "timezoneOffset",
    "timezone",
    "deviceFormFactor",
    "mpName",
}


def printable(value, maximum):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and all(32 <= ord(c) <= 126 for c in value)
    )


def header(value, maximum, check="header"):
    if not printable(value, maximum):
        raise refused(check)
    return value


def places(cookies):
    """Each cookie's name, domain and path for the log, never its value."""

    def clean(item):
        return "".join(c for c in str(item)[:60] if 32 <= ord(c) <= 126)

    return ", ".join(
        f"{clean(c.get('name'))}@{clean(c.get('domain'))}{clean(c.get('path'))}"
        if isinstance(c, dict)
        else "?"
        for c in cookies[:64]
    )


def refused(check, cookies=()):
    """Log which check a transferred session failed, so a founder's failed setup has a cause."""
    logger.warning("LinkedIn session refused (%s); cookies: %s", check, places(cookies) or "none")
    return CollectionError("invalid_session")


def validate_session(value):
    """Return a fresh bounded envelope; callers must encrypt it before persistence."""
    if not isinstance(value, dict) or set(value) != {"cookies", "user_agent", "browser_context"}:
        raise refused("envelope")
    ua = header(value["user_agent"], 1024, "user_agent")
    raw = value["cookies"]
    # Chrome hands over the cookies of every LinkedIn site, not only www.linkedin.com's.
    if not isinstance(raw, list) or not 2 <= len(raw) <= 64:
        raise refused("cookie_count", raw if isinstance(raw, list) else ())
    kept = {}
    fields = {
        "name",
        "value",
        "domain",
        "path",
        "secure",
        "http_only",
        "same_site",
        "expiration_date",
    }
    for cookie in raw:
        if (
            not isinstance(cookie, dict)
            or set(cookie) - fields
            or cookie.get("name") not in COOKIE_NAMES
        ):
            raise refused("cookie_shape", raw)
        name, domain = cookie["name"], cookie.get("domain")
        # Another LinkedIn site's copy, or one for part of the site, is not what Chrome
        # sends to www.linkedin.com: leave it out rather than refuse the login.
        if domain not in COOKIE_DOMAINS or cookie.get("path") != "/":
            continue
        if (
            type(cookie.get("secure")) is not bool
            or type(cookie.get("http_only")) is not bool
            or cookie.get("same_site") not in {"lax", "strict", "unspecified", "no_restriction"}
        ):
            raise refused("cookie_flags", raw)
        secret = cookie.get("value")
        if not printable(secret, 16384) or ";" in secret:
            if name in LOGIN_COOKIES:
                raise refused("login_cookie_value", raw)
            continue
        rank = COOKIE_DOMAINS.index(domain)
        if name in kept and kept[name][0] == rank:
            raise refused("duplicate_cookie", raw)
        # Two copies of one cookie: keep the one set for www.linkedin.com itself.
        if name not in kept or rank < kept[name][0]:
            kept[name] = (rank, cookie)
    if not LOGIN_COOKIES <= set(kept):
        raise refused("login_cookie_missing", raw)
    if len(kept) < len(raw):
        chosen = [cookie for _, cookie in kept.values()]
        logger.info(
            "LinkedIn session left out other sites' cookies and duplicates: %s",
            places([c for c in raw if not any(c is k for k in chosen)]),
        )
    # Cookie domain/expiry are checked at capture; the trusted HTTP jar is read-only.
    cookies = [{"name": name, "value": cookie["value"]} for name, (_, cookie) in kept.items()]
    context = value["browser_context"]
    if not isinstance(context, dict) or set(context) != {"accept_language", "li_lang", "li_track"}:
        raise refused("browser_context")
    lang = header(context["li_lang"], 16, "li_lang")
    if not re.fullmatch(r"[A-Za-z]{2,3}_[A-Za-z]{2,3}", lang):
        raise refused("li_lang")
    track = context["li_track"]
    if (
        not isinstance(track, dict)
        or not TRACK_REQUIRED <= set(track)
        or set(track) - TRACK_STRINGS - TRACK_NUMBERS
    ):
        raise refused("li_track")
    for key, item in track.items():
        if key in TRACK_STRINGS:
            header(item, 128, "li_track")
        elif type(item) not in {int, float} or not -100000 <= item <= 100000:
            raise refused("li_track")
    return {
        "cookies": sorted(cookies, key=lambda c: c["name"]),
        "user_agent": ua,
        "browser_context": {
            "accept_language": header(context["accept_language"], 256, "accept_language"),
            "li_lang": lang,
            "li_track": dict(track),
        },
    }


def headers(session, *, search=False):
    """Match the published outreach client's GET headers, including the captured li-track."""
    cookies = {c["name"]: c["value"] for c in session["cookies"]}
    context = session["browser_context"]
    page = "d_flagship3_search_srp_people" if search else "d_flagship3_preload"
    result = {
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "csrf-token": cookies["JSESSIONID"].strip('"'),
        "cookie": "; ".join(f"{c['name']}={c['value']}" for c in session["cookies"]),
        "priority": "u=1, i",
        "referer": "https://www.linkedin.com/search/results/people/"
        if search
        else "https://www.linkedin.com/preload/?_bprMode=vanilla",
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
    }
    ua = session["user_agent"]
    chrome = re.search(r"Chrome/(\d+)", ua)
    if chrome:
        platform = next(
            (
                v
                for k, v in [
                    ("Android", "Android"),
                    ("iPhone", "iOS"),
                    ("iPad", "iOS"),
                    ("Windows", "Windows"),
                    ("Macintosh", "macOS"),
                    ("Linux", "Linux"),
                ]
                if k in ua
            ),
            None,
        )
        if platform:
            version = chrome[1]
            result.update(
                {
                    "sec-ch-ua": (
                        f'"Not:A?Brand";v="99", "Google Chrome";v="{version}", '
                        f'"Chromium";v="{version}"'
                    ),
                    "sec-ch-ua-mobile": "?1" if platform in {"Android", "iOS"} else "?0",
                    "sec-ch-ua-platform": f'"{platform}"',
                }
            )
    return result


def identity_profile(payload):
    if not isinstance(payload, dict):
        raise CollectionError("unsupported_identity")
    data = payload.get("data", payload)
    mini = data.get("*miniProfile") if isinstance(data, dict) else None
    rows = payload.get("included", [])
    candidates = (
        [r for r in rows if isinstance(r, dict) and r.get("entityUrn") == mini] if mini else []
    )
    if isinstance(data, dict) and isinstance(data.get("miniProfile"), dict):
        candidates.append(data["miniProfile"])
    if len(candidates) != 1 or not candidates[0].get("publicIdentifier"):
        raise CollectionError("unsupported_identity")
    return candidates[0]


MEMBER = re.compile(r"[A-Za-z0-9_-]{1,100}")


def member_id(payload) -> str | None:
    """The signed-in member's LinkedIn ID from /me (urn:li:fs_miniProfile:<ID>). Unlike a profile
    photo or a profile link, it never changes."""

    urn = identity_profile(payload).get("entityUrn")
    if not isinstance(urn, str) or not urn.startswith("urn:li:fs_miniProfile:"):
        return None
    member = urn.rsplit(":", 1)[-1]
    return member if MEMBER.fullmatch(member) else None


def identity_matches(payload, actor):
    # The member ID Tin recorded when it saved the session decides when there is one: a photo or
    # a profile link can change, the account's ID can't.
    if actor.get("member"):
        return member_id(payload) == actor["member"]
    profile = identity_profile(payload)
    url = profile_url(
        "https://www.linkedin.com/in/" + quote(profile["publicIdentifier"], safe="-._~")
    )
    if actor.get("profile_url"):
        return actor["profile_url"] == url and actor["key"] == url
    # Some current layouts expose the signed-in avatar, but no profile link.
    # Match that exact marker against the authenticated /me profile only.
    markers = set()
    stack = [
        (profile.get(name), 0) for name in ("picture", "profilePicture", "avatarUrl", "pictureUrl")
    ]
    visited = 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        if visited > 100 or depth > 6:
            raise CollectionError("unsupported_identity")
        if isinstance(value, dict):
            root = value.get("rootUrl")
            if isinstance(root, str):
                for artifact in value.get("artifacts", []):
                    if isinstance(artifact, dict) and isinstance(
                        artifact.get("fileIdentifyingUrlPathSegment"), str
                    ):
                        stack.append((root + artifact["fileIdentifyingUrlPathSegment"], depth + 1))
            stack.extend((item, depth + 1) for key, item in value.items() if key != "rootUrl")
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            parsed = urlsplit(value)
            if (
                parsed.scheme == "https"
                and parsed.hostname == "media.licdn.com"
                and "/profile-displayphoto" in parsed.path
            ):
                markers.add("avatar:" + parsed.path.split("/profile-displayphoto")[0])
    return len(markers) == 1 and actor["key"] in markers


def search_request(source, keywords, page):
    source = CollectionSource.model_validate(source)
    member = source.scope(keywords=keywords)
    if not source.query_id or not 1 <= page <= 20:
        raise CollectionError("unsupported_search_contract")

    # Escape each Rest.li atom, then let httpx encode the complete query parameter.
    def atom(text):
        return quote(text, safe="-._~")

    filters = (
        f"(key:connectionOf,value:List({atom(member)})),"
        "(key:network,value:List(S)),(key:resultType,value:List(PEOPLE))"
    )
    keyword = f"keywords:{atom(keywords)}," if keywords else ""
    variables = (
        f"(start:{(page - 1) * 10},origin:FACETED_SEARCH,query:({keyword}"
        f"flagshipSearchIntent:SEARCH_SRP,queryParameters:List({filters})),count:10)"
    )
    return {"queryId": source.query_id, "variables": variables}


def search_page(payload, page):
    """Resolve only entities referenced by the search cluster, never arbitrary included rows."""
    try:
        included = {
            r["entityUrn"]: r
            for r in payload["included"]
            if isinstance(r, dict) and "entityUrn" in r
        }
        data = payload["data"]["data"]["searchDashClustersByAll"]
        paging = data["paging"]
        if (
            paging["start"] != (page - 1) * 10
            or paging["count"] != 10
            or type(paging["total"]) is not int
        ):
            raise ValueError
        rows = []
        for cluster in data["elements"]:
            for item in cluster.get("items", []):
                result = item.get("item", {})
                ref = result.get("*entityResult")
                if ref is None:
                    continue
                entity = included[ref]
                title = entity["title"]["text"]
                url = profile_url(entity["navigationUrl"])
                distance = entity.get("entityCustomTrackingInfo", {}).get("memberDistance")
                if distance != "DISTANCE_2":
                    raise CollectionError("filters_changed")
                rows.append(
                    Person(
                        profile_url=url,
                        name=title,
                        headline=entity.get("primarySubtitle", {}).get("text", ""),
                        location=entity.get("secondarySubtitle", {}).get("text", ""),
                        degree="2nd",
                    ).model_dump()
                )
        total = paging["total"]
        if (
            len(rows) > 10
            or len({p["profile_url"] for p in rows}) != len(rows)
            or (not rows and total > paging["start"])
        ):
            raise ValueError
        return {"people": rows, "next_page": paging["start"] + len(rows) < total}
    except CollectionError:
        raise
    except (KeyError, TypeError, ValueError):
        raise CollectionError("unsupported_search_contract") from None


def resolved_source(payload, source, keywords):
    """Use only the selected profile and its explicitly linked relationship evidence."""
    from urllib.parse import urlencode

    expected = profile_url(source["friend_url"])
    rows, stack = [], [(payload, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > 12 or len(rows) > 2000:
            raise CollectionError("browser_preparation_required")
        if isinstance(value, dict):
            rows.append(value)
            stack.extend(
                (item, depth + 1) for item in value.values() if isinstance(item, (dict, list))
            )
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value if isinstance(item, (dict, list)))
    profiles = {}
    for row in rows:
        identifier, urn = row.get("publicIdentifier"), row.get("entityUrn", "")
        if (
            isinstance(identifier, str)
            and isinstance(urn, str)
            and re.fullmatch(r"urn:li:fsd_profile:[A-Za-z0-9_-]{1,256}", urn)
        ):
            if (
                profile_url("https://www.linkedin.com/in/" + quote(identifier, safe="-._~"))
                == expected
            ):
                profiles[urn] = row
    if len(profiles) != 1:
        raise CollectionError("browser_preparation_required")
    urn, profile = next(iter(profiles.items()))
    relationship = profile.get("memberRelationship")
    if not isinstance(relationship, dict):
        ref = profile.get("*memberRelationship")
        matches = [row for row in rows if ref and row.get("entityUrn") == ref]
        relationship = matches[0] if len(matches) == 1 else {}
    distance = relationship.get("distance", relationship.get("memberDistance"))
    if distance not in {"DISTANCE_1", "DISTANCE_2", "DISTANCE_3", "OUT_OF_NETWORK"}:
        raise CollectionError("browser_preparation_required")
    if distance != "DISTANCE_1":
        raise CollectionError("friend_not_connected")
    # A relationship alone does not establish that the friend exposes their list.
    # Require a connections-view link within this exact profile's own evidence.
    values, visible = [profile], False
    while values:
        value = values.pop()
        if isinstance(value, dict):
            values.extend(value.values())
        elif isinstance(value, list):
            values.extend(value)
        elif isinstance(value, str) and value.startswith("https://www.linkedin.com/"):
            url = urlsplit(value)
            if url.path.rstrip("/") == "/search/results/people":
                links = parse_qs(url.query).get("connectionOf", [])
                try:
                    visible |= len(links) == 1 and json.loads(links[0]) == [urn.rsplit(":", 1)[1]]
                except ValueError:
                    pass
    if not visible:
        raise CollectionError("browser_preparation_required")
    name = " ".join(
        value
        for value in (profile.get("firstName", ""), profile.get("lastName", ""))
        if isinstance(value, str)
    ).strip()
    result = CollectionSource(
        friend_url=expected,
        friend_name=name[:200],
        actor=source["actor"],
        first_degree=True,
        collection_url="https://www.linkedin.com/search/results/people/?"
        + urlencode(
            {
                "connectionOf": json.dumps([urn.rsplit(":", 1)[1]]),
                "network": '["S"]',
                "keywords": keywords,
            }
        ),
        query_id=source["query_id"],
    )
    result.scope(keywords=keywords)
    return result.model_dump()


async def _get(client, session, path, params=None, *, request_headers=None):
    async with client.stream(
        "GET",
        "https://www.linkedin.com" + path,
        params=params,
        headers=request_headers or headers(session, search=params is not None),
        follow_redirects=False,
    ) as response:
        if response.status_code == 429:
            raise CollectionError("rate_limited")
        if response.status_code == 403:
            raise CollectionError("access_denied")
        if response.status_code == 401:
            raise CollectionError("session_expired")
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            raise CollectionError(
                "challenge"
                if any(v in location for v in ("checkpoint", "challenge"))
                else "session_expired"
            )
        if 400 <= response.status_code < 500 or response.status_code == 999:
            raise CollectionError("access_denied")
        if response.status_code != 200:
            raise CollectionError("cloud_failed")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 2_000_000:
                raise CollectionError("unsupported_search_contract")
        try:
            return json.loads(body)
        except ValueError:
            raise CollectionError("unsupported_search_contract") from None


async def check_account(session, actor, *, client=None):
    """One bounded, read-only identity check before marking saved access ready. Returns the
    member's LinkedIn ID, which Tin records with the session."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False)
    try:
        async with asyncio.timeout(10):
            me = await _get(client, session, "/voyager/api/me")
            if not identity_matches(me, actor):
                raise CollectionError("account_changed")
            return member_id(me)
    except (httpx.HTTPError, TimeoutError):
        raise CollectionError("cloud_unavailable") from None
    finally:
        if own:
            await client.aclose()


async def read_page(session, source, keywords, page, *, client=None, document=False):
    own = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)

    async def get(path, params=None):
        return await _get(client, session, path, params)

    try:
        if not identity_matches(await get("/voyager/api/me"), source["actor"]):
            raise CollectionError("account_changed")
        if document:
            from tin_lite.linkedin_document import read_document_page

            return await read_document_page(client, session, source, keywords, page)
        resolved = None
        if "collection_url" not in source:
            if not source.get("query_id"):
                raise CollectionError("browser_preparation_required")
            friend = profile_url(source["friend_url"])
            identifier = unquote(friend.rsplit("/", 1)[1])
            resolved = resolved_source(
                await get(
                    "/voyager/api/identity/dash/profiles",
                    {"q": "memberIdentity", "memberIdentity": identifier},
                ),
                source,
                keywords,
            )
            source = resolved
        result = search_page(
            await get("/voyager/api/graphql", search_request(source, keywords, page)), page
        )
        if resolved is not None:
            result["source"] = resolved
        return result
    finally:
        if own:
            await client.aclose()
