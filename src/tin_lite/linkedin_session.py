"""Small read-only transport preserving the published extension's request context.

Never follow redirects, write LinkedIn cookies back, sign out, or execute site scripts.
This is a candidate adapter: deployment qualification is separate from fixture coverage.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from urllib.parse import quote, urlsplit

import httpx

from tin_lite.connection_collection import CollectionError, CollectionSource, Person, profile_url

COOKIE_NAMES = frozenset({"li_at", "JSESSIONID", "bcookie", "bscookie", "lidc", "lang"})
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


def header(value, maximum):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(c) < 32 or ord(c) > 126 for c in value)
    ):
        raise CollectionError("invalid_session")
    return value


def validate_session(value):
    """Return a fresh bounded envelope; callers must encrypt it before persistence."""
    if not isinstance(value, dict) or set(value) != {"cookies", "user_agent", "browser_context"}:
        raise CollectionError("invalid_session")
    ua = header(value["user_agent"], 1024)
    raw = value["cookies"]
    if not isinstance(raw, list) or not 2 <= len(raw) <= 12:
        raise CollectionError("invalid_session")
    cookies, names = [], set()
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
        if not isinstance(cookie, dict) or set(cookie) - fields:
            raise CollectionError("invalid_session")
        name = cookie.get("name")
        if (
            name not in COOKIE_NAMES
            or name in names
            or cookie.get("domain")
            not in {".linkedin.com", "linkedin.com", ".www.linkedin.com", "www.linkedin.com"}
            or cookie.get("path") != "/"
            or type(cookie.get("secure")) is not bool
            or type(cookie.get("http_only")) is not bool
            or cookie.get("same_site") not in {"lax", "strict", "unspecified", "no_restriction"}
        ):
            raise CollectionError("invalid_session")
        secret = header(cookie.get("value"), 16384)
        if ";" in secret:
            raise CollectionError("invalid_session")
        names.add(name)
        # Cookie domain/expiry are checked at capture; the trusted HTTP jar is read-only.
        cookies.append({"name": name, "value": secret})
    if not {"li_at", "JSESSIONID"} <= names:
        raise CollectionError("invalid_session")
    context = value["browser_context"]
    if not isinstance(context, dict) or set(context) != {"accept_language", "li_lang", "li_track"}:
        raise CollectionError("invalid_session")
    lang = header(context["li_lang"], 16)
    if not re.fullmatch(r"[A-Za-z]{2,3}_[A-Za-z]{2,3}", lang):
        raise CollectionError("invalid_session")
    track = context["li_track"]
    if (
        not isinstance(track, dict)
        or not TRACK_REQUIRED <= set(track)
        or set(track) - TRACK_STRINGS - TRACK_NUMBERS
    ):
        raise CollectionError("invalid_session")
    for key, item in track.items():
        if key in TRACK_STRINGS:
            header(item, 128)
        elif type(item) not in {int, float} or not -100000 <= item <= 100000:
            raise CollectionError("invalid_session")
    return {
        "cookies": sorted(cookies, key=lambda c: c["name"]),
        "user_agent": ua,
        "browser_context": {
            "accept_language": header(context["accept_language"], 256),
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


def identity_matches(payload, actor):
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


async def read_page(session, source, keywords, page, *, client=None):
    own = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)

    async def get(path, params=None):
        async with client.stream(
            "GET",
            "https://www.linkedin.com" + path,
            params=params,
            headers=headers(session, search=params is not None),
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

    try:
        if not identity_matches(await get("/voyager/api/me"), source["actor"]):
            raise CollectionError("account_changed")
        return search_page(
            await get("/voyager/api/graphql", search_request(source, keywords, page)), page
        )
    finally:
        if own:
            await client.aclose()
