"""Website visitors RB2B identifies, posted to each project's own webhook (`people.rb2b_visitor`).

RB2B matches a visit's IP address to a person (or only a company) and posts a flat JSON object
to the URL the founder pasted into RB2B. It sends no signature and no account ID, and it
disconnects a URL that answers anything but 200 within 15 seconds, so the URL's random token is
the credential, the endpoint keeps the payload and answers at once, and a run does the rest:

1. record: the person in People, by their LinkedIn profile (and RB2B's email when it has one),
   with name, headline, company and location wherever nothing else set them;
2. enrich: about ten minutes later, once PostHog has the visit, the visit itself from the
   project's PostHog (same host and page within -60/+120 s of RB2B's time, city as a
   tiebreak): pages, time, how it arrived (ad, search, AI assistant, social, referral, direct),
   other visits and a replay link, as a `visited` event and the person's "On the site" fields;
3. rate: fit and intent against `strategy/ICP.md` (people_icp), with web search;
4. publish: the latest visitors and how they rated, in people/rb2b-visitors.md.

Payload facts (RB2B's docs, 2026-11): spaced title-case keys ("LinkedIn URL", "First Name",
"Seen At", "Captured URL"); empty values as null or ""; "Seen At" in a loose ISO form whose
time zone isn't documented (read as UTC when it names none); the repeat flag as either
`is_repeat_visit` or `is_repeat_visitor`, sent only on repeat visits; no field marking a
company-only visit (no first name, and a /company/ LinkedIn page); a test event whose name is
"RB2B Test Payload". Captured URLs carry no query string.
"""

# HogQL sent to PostHog's read-only query API; every value in it goes through _q.
# ruff: noqa: S608
from __future__ import annotations

import hashlib
import re
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from tin_lite import people_records as records
from tin_lite.domain import RB2B_VISITOR_WORKFLOW_NAME

KEY = RB2B_VISITOR_WORKFLOW_NAME
WORKFLOW_ID = UUID("8b0d4c1e-6f2a-4e57-9d3b-5a7c2e9f1b64")
SOURCE = "rb2b"
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "visit_id": {"type": "string", "format": "uuid"},
    },
    "required": ["project_id", "visit_id"],
}
# What RB2B may post: a runaway guard well above its flat object.
MAX_BODY = 64_000
# PostHog has a visit's events a few minutes after it; RB2B posts within seconds.
ENRICH_AFTER = timedelta(minutes=10)
WINDOW = (-60, 120)  # seconds around RB2B's time
LIST_PATH = "people/rb2b-visitors.md"
PATHS = {"rb2b-visitors.md": LIST_PATH}
LIMITS = {"rb2b-visitors.md": 200_000}
LISTED = 100
FIELDS = {
    "linkedin": "LinkedIn URL",
    "first_name": "First Name",
    "last_name": "Last Name",
    "title": "Title",
    "company": "Company Name",
    "email": "Business Email",
    "website": "Website",
    "industry": "Industry",
    "employees": "Employee Count",
    "revenue": "Estimate Revenue",
    "city": "City",
    "state": "State",
    "zipcode": "Zipcode",
    "seen_at": "Seen At",
    "referrer": "Referrer",
    "url": "Captured URL",
    "tags": "Tags",
}


# The webhook's token.


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hook_url(public_url: str, token: str) -> str:
    return f"{public_url.rstrip('/')}/webhooks/rb2b/{token}"


# A payload.


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = " ".join(str(value).split())
    return text[:2000] or None


def seen_at(value: Any) -> datetime | None:
    """RB2B's "Seen At": ISO 8601 when it is, else the date and time it starts with (its
    documented sample, "2024-01-01T12:34:56:00.00+00.00", isn't valid ISO). No zone is UTC."""

    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        match = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)", text)
        if not match:
            return None
        try:
            parsed = datetime.fromisoformat(f"{match[1]}T{match[2]}")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def lead(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload's values under Tin's names, trimmed, with empty values as None."""

    found = {name: _text(payload.get(key)) for name, key in FIELDS.items()}
    found["at"] = seen_at(payload.get("Seen At"))
    found["repeat"] = bool(payload.get("is_repeat_visit") or payload.get("is_repeat_visitor"))
    name = " ".join(x for x in (found["first_name"], found["last_name"]) if x)
    found["name"] = name or None
    found["location"] = ", ".join(x for x in (found["city"], found["state"]) if x) or None
    return found


def kind(found: dict[str, Any]) -> str:
    """person, company (RB2B named only the company) or test (RB2B's test event)."""

    if (found["first_name"], found["last_name"]) == ("RB2B", "Test Payload") or (
        found["title"] or ""
    ).startswith("Webhook Test Payload"):
        return "test"
    if found["first_name"] or found["last_name"]:
        return "person"
    return "company"


def dedupe_key(found: dict[str, Any]) -> str:
    """One visit per profile, time and page: RB2B can post the same view twice."""

    at = found["at"].isoformat() if found["at"] else ""
    return hashlib.sha256(f"{found['linkedin']}|{at}|{found['url']}".encode()).hexdigest()


def identifiers(found: dict[str, Any]) -> dict[str, str]:
    """The person's LinkedIn profile (required) and RB2B's email when it is well formed."""

    profile = re.sub(r"^http://", "https://", (found["linkedin"] or "").split("?")[0])
    ids = records.linkedin_identifiers(profile)
    email = (found["email"] or "").casefold()
    if email and records.EMAIL.fullmatch(email):
        ids["email"] = email
    return ids


def event_key(project: UUID, ids: dict[str, str], at: datetime) -> str:
    """One visit per person and RB2B minute: the same key the RB2B Slack reader used, so a
    visit both report is kept once."""

    who = ids.get("linkedin_member") or ids["linkedin_slug"]
    digest = hashlib.sha256(f"{project}:{who}".encode()).hexdigest()[:24]
    return f"rb2b:{digest}:{at.astimezone(UTC):%Y%m%dT%H%M}"


def person_values(found: dict[str, Any]) -> list[records.Value]:
    headline = " at ".join(x for x in (found["title"], found["company"]) if x)
    company_reason = f"From RB2B, website {found['website']}" if found["website"] else "From RB2B"
    values = [
        ("name", found["name"], "From RB2B"),
        ("headline", headline or None, "From RB2B"),
        ("company", found["company"], company_reason),
        ("position", found["title"], "From RB2B"),
        ("location", found["location"], "From RB2B, where the visit came from"),
    ]
    return [records.Value(key, value, reason) for key, value, reason in values if value]


# The visit, from the project's PostHog.


def _q(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ts(value: datetime) -> str:
    return f"toDateTime({_q(value.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S'))}, 'UTC')"


def page(found: dict[str, Any]) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(found["url"] or "")
    host = (parsed.hostname or "").removeprefix("www.")
    return host, parsed.path.rstrip("/") or "/"


def match_query(found: dict[str, Any]) -> str:
    host, path = page(found)
    at = found["at"]
    return f"""SELECT properties.$session_id, min(timestamp), any(properties.$geoip_city_name),
                  any(person_id)
            FROM events
            WHERE event = '$pageview'
              AND timestamp >= {_ts(at + timedelta(seconds=WINDOW[0]))}
              AND timestamp < {_ts(at + timedelta(seconds=WINDOW[1]))}
              AND (properties.$pathname = {_q(path)} OR properties.$pathname = {_q(path + "/")})
              AND coalesce(properties.$host, domain(properties.$current_url))
                  IN ({_q(host)}, {_q("www." + host)})
              AND properties.$session_id IS NOT NULL
            GROUP BY properties.$session_id
            ORDER BY min(timestamp)
            LIMIT 20"""


def matched(found: dict[str, Any], rows: list[list[Any]]) -> list[Any] | None:
    """The one session that is theirs: the only one, or the only one from RB2B's city."""

    if len(rows) == 1:
        return rows[0]
    city = (found["city"] or "").strip().lower()
    same = [row for row in rows if city and (row[2] or "").lower() == city]
    return same[0] if len(same) == 1 else None


def session_query(session_id: str) -> str:
    return f"""SELECT timestamp, event, properties.$pathname, properties.$prev_pageview_pathname,
                   properties.$prev_pageview_duration, properties.utm_source,
                   properties.utm_medium, properties.utm_campaign, properties.utm_term,
                   properties.$referring_domain, properties.$current_url
            FROM events
            WHERE properties.$session_id = {_q(session_id)}
              AND timestamp >= now() - INTERVAL 31 DAY
            ORDER BY timestamp
            LIMIT 500"""


def replay_query(session_id: str) -> str:
    return f"""SELECT session_id, sum(active_milliseconds)
            FROM raw_session_replay_events
            WHERE session_id = {_q(session_id)}
            GROUP BY session_id
            LIMIT 1"""


def others_query(person_id: str, session_id: str) -> str:
    return f"""SELECT count(DISTINCT properties.$session_id)
            FROM events
            WHERE person_id = {_q(person_id)} AND event = '$pageview'
              AND properties.$session_id != {_q(session_id)}
              AND timestamp >= now() - INTERVAL 90 DAY
            LIMIT 1"""


def first_touch_query(person_id: str) -> str:
    return f"""SELECT properties.$initial_utm_source, properties.$initial_utm_medium,
                   properties.$initial_utm_campaign, properties.$initial_utm_term,
                   properties.$initial_referring_domain, properties.$initial_current_url,
                   properties.$initial_gclid
            FROM persons WHERE id = {_q(person_id)} LIMIT 1"""


# Click IDs ad platforms add to the landing URL (fbclid is left out: Facebook adds it to organic
# shares too).
AD_CLICK_IDS = {
    "gclid": "Google Ads",
    "gbraid": "Google Ads",
    "wbraid": "Google Ads",
    "gad_source": "Google Ads",
    "msclkid": "Microsoft Ads",
    "li_fat_id": "LinkedIn Ads",
    "ttclid": "TikTok Ads",
    "twclid": "X Ads",
    "rdt_cid": "Reddit Ads",
}
PAID_MEDIUMS = {
    "cpc",
    "ppc",
    "paid",
    "paidsearch",
    "paid_search",
    "paid-search",
    "paid_social",
    "paidsocial",
    "paid-social",
    "display",
    "cpm",
    "banner",
    "retargeting",
    "ads",
    "ad",
}
SEARCH = {
    "google": "Google",
    "bing": "Bing",
    "duckduckgo": "DuckDuckGo",
    "yahoo": "Yahoo",
    "ecosia": "Ecosia",
    "search.brave": "Brave",
    "yandex": "Yandex",
    "baidu": "Baidu",
    "startpage": "Startpage",
    "kagi": "Kagi",
    "naver": "Naver",
    "googlequicksearchbox": "Google",
}
AI = {
    "chatgpt.com": "ChatGPT",
    "chat.openai.com": "ChatGPT",
    "perplexity.ai": "Perplexity",
    "claude.ai": "Claude",
    "gemini.google.com": "Gemini",
    "copilot.microsoft.com": "Copilot",
    "chat.deepseek.com": "DeepSeek",
    "grok.com": "Grok",
    "you.com": "You.com",
    "phind.com": "Phind",
}
SOCIAL = {
    "linkedin": "LinkedIn",
    "lnkd.in": "LinkedIn",
    "t.co": "X",
    "x.com": "X",
    "twitter": "X",
    "facebook": "Facebook",
    "fb.com": "Facebook",
    "instagram": "Instagram",
    "reddit": "Reddit",
    "news.ycombinator.com": "Hacker News",
    "youtube": "YouTube",
    "threads.net": "Threads",
    "bsky": "Bluesky",
    "tiktok": "TikTok",
    "slack.com": "Slack",
    "discord": "Discord",
}
# Referrers that are ad clicks themselves: Google's search partner network and ad redirects.
AD_REFERRERS = {
    "syndicatedsearch.goog": "Google Ads, search partners",
    "googleadservices.com": "Google Ads",
    "doubleclick.net": "Google Ads",
    "googlesyndication.com": "Google Ads, display",
}
LABEL = {
    "ad": "Ad",
    "organic": "Organic search",
    "ai": "AI assistant",
    "social": "Social",
    "referral": "Referral",
    "direct": "Direct",
}


def _known(host: str, table: dict[str, str]) -> str | None:
    for key, name in table.items():
        if key in host:
            return name
    return None


def channel(source, medium, campaign, term, referrer, url, gclid=None) -> dict[str, str]:
    """How a visit arrived: {kind, label, detail}; kind is ad, organic, ai, social, referral or
    direct."""

    source, medium = str(source or "").strip(), str(medium or "").strip().lower()
    host = str(referrer or "").lower().removeprefix("www.")
    host = "" if host in ("$direct", "none") else host
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(url or "")).query)
    clicked = next((name for key, name in AD_CLICK_IDS.items() if query.get(key)), None)
    if gclid and not clicked:
        clicked = "Google Ads"
    if not clicked and host:
        clicked = _known(host, AD_REFERRERS)
    if medium in PAID_MEDIUMS or clicked:
        platform = clicked or (f"{source.capitalize()} Ads" if source else "Paid")
        if source.lower() in ("google", "adwords") and not clicked:
            platform = "Google Ads"
        detail = f"keyword “{term}”" if term else (f"campaign {campaign}" if campaign else "")
        return {"kind": "ad", "label": platform, "detail": detail}
    ai = _known(host, AI) or (_known(source.lower(), AI) if source else None)
    if ai or source.lower() in ("chatgpt.com", "chatgpt", "perplexity", "claude"):
        return {"kind": "ai", "label": ai or source, "detail": ""}
    if host and _known(host, SEARCH):
        return {"kind": "organic", "label": _known(host, SEARCH) + " search", "detail": ""}
    if medium == "organic" and source:
        return {"kind": "organic", "label": source.capitalize() + " search", "detail": ""}
    social = _known(host, SOCIAL) or (_known(source.lower(), SOCIAL) if source else None)
    if social or medium in ("social", "social_media", "social-media"):
        return {"kind": "social", "label": social or source, "detail": ""}
    if source or host:
        detail = f"/ {medium}" if source and medium else ""
        return {"kind": "referral", "label": source or host, "detail": detail}
    return {"kind": "direct", "label": "no referrer", "detail": ""}


def said(how: dict[str, str]) -> str:
    """'Ad: Google Ads, keyword “x”', 'Organic search: Google', 'Direct'."""

    if how["kind"] == "direct":
        return "Direct"
    label = how["label"].removesuffix(" search") if how["kind"] == "organic" else how["label"]
    return f"{LABEL[how['kind']]}: {label}" + (f", {how['detail']}" if how["detail"] else "")


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def visit(found: dict[str, Any], info: dict[str, Any], replay_base: str) -> dict[str, Any]:
    """The visit as People keeps it, from the session's events: summary, where they stopped,
    how they arrived, how they first arrived and a replay link."""

    events = info["events"]
    if not events:
        return unmatched(found, 1)
    views = [e for e in events if e[1] == "$pageview"]
    first = views[0] if views else events[0]
    pages: list[str] = []
    for event in views:
        if not pages or pages[-1] != event[2]:
            pages.append(event[2] or "/")
    span = (_time(events[-1][0]) - _time(events[0][0])).total_seconds()
    how = channel(*first[5:11])
    trail = " → ".join(pages[:4]) + (" …" if len(pages) > 4 else "")
    bits = [f"{len(pages)} page{'s' if len(pages) != 1 else ''}"]
    if span >= 5:
        bits.append(_duration(span))
    if info["other_sessions"]:
        n = info["other_sessions"]
        bits.append(f"{n} other visit{'s' if n != 1 else ''} in 90 days")
    first_touch = info.get("first_touch")
    if first_touch and first_touch["kind"] != how["kind"]:
        bits.append(f"first came via {said(first_touch)}")
    return {
        "summary": f"{said(how)} · read {trail} ({', '.join(bits)}) · identified by RB2B"[:500],
        "stopped_at": pages[-1] if pages else page(found)[1],
        "replay_url": replay_base + info["session_id"] if info["active_ms"] else "",
        "channel": how,
        "first_touch": first_touch,
    }


def unmatched(found: dict[str, Any], sessions: int, *, why: str | None = None) -> dict[str, Any]:
    """No PostHog session to read: the landing URL RB2B saw can still carry ad tags."""

    path = page(found)[1]
    how = channel(None, None, None, None, found["referrer"], found["url"])
    lead_in = f"{said(how)} · " if how["kind"] != "direct" else ""
    why = why or (
        "no matching visit in PostHog"
        if not sessions
        else f"{sessions} PostHog visits that minute, none clearly theirs"
    )
    return {
        "summary": f"{lead_in}On {path} ({why}) · identified by RB2B"[:500],
        "stopped_at": path,
        "replay_url": "",
        "channel": how
        if how["kind"] != "direct"
        else {"kind": "unknown", "label": "", "detail": ""},
        "first_touch": None,
    }


def web_values(the_visit: dict[str, Any]) -> list[records.Value]:
    """The person's "On the site" fields from their latest visit."""

    values = [
        records.Value("web_tier", "lead", the_visit["stopped_at"]),
        records.Value("web_summary", the_visit["summary"]),
    ]
    how = the_visit["channel"]
    if how["kind"] in records.WEB_SOURCES:
        label = how["label"].removesuffix(" search") if how["kind"] == "organic" else how["label"]
        reason = (
            ""
            if how["kind"] == "direct"
            else label + (f", {how['detail']}" if how["detail"] else "")
        )
        values.append(records.Value("web_source", how["kind"], reason))
    return values


# The list in project Files.


def rating_line(fields: dict[str, Any]) -> str:
    from tin_lite.people_icp import RATINGS

    fit = (fields.get("icp_fit") or {}).get("number")
    intent = (fields.get("intent") or {}).get("text")
    if fit is None:
        return "not rated"
    label = f"{int(fit)} · {RATINGS.get(int(fit), '')}"
    return f"{label}, {intent}" if intent else label


def report(people: list[dict[str, Any]], *, now: datetime) -> str:
    def field(person, key, part="text"):
        return (person["fields"].get(key) or {}).get(part)

    lines = [
        "# Visitors RB2B identified",
        "",
        f"Updated {now.astimezone(UTC):%-d %b %Y, %H:%M} UTC. The {LISTED} most recent "
        "visitors RB2B named on your site, newest first, with how they rate against "
        "`strategy/ICP.md`. Every visitor is in People too.",
        "",
    ]
    if not people:
        lines.append("Nobody yet.")
    for person in people:
        name = field(person, "name") or "Someone"
        link = person.get("profile_url")
        who = f"[{name}]({link})" if link else name
        lines += [
            f"## {who}",
            "",
            f"- {field(person, 'headline') or 'No headline'}",
            f"- {rating_line(person['fields'])}: {field(person, 'icp_fit', 'reason') or ''}".rstrip(
                ": "
            ),
            f"- {field(person, 'web_summary') or 'No visit details yet'}",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def summary(person: dict[str, Any] | None, outcome: str, company: dict | None = None) -> str:
    """The run's one line: who visited and how they rate, or why there's no one to rate."""

    if outcome == "company":
        from tin_lite.people_icp import RATINGS

        name = (company or {}).get("name") or "the visitor's company"
        if not (company or {}).get("judged"):
            return f"RB2B named only {name}; Tin didn't judge it."[:500]
        fit = company.get("fit")
        rated = f"{fit} · {RATINGS[fit]}" if fit in RATINGS else "can't tell"
        if not company.get("worth"):
            return f"RB2B named only {name}: not worth chasing ({rated})."[:500]
        people = len(company.get("people") or [])
        found = f"{people} {'person' if people == 1 else 'people'} to contact there"
        if company.get("not_looked") == "daily_limit":
            found = "the people search was skipped for today's limit"
        return f"RB2B named only {name}: worth chasing ({rated}); {found}."[:500]
    if outcome == "forgotten":
        return "RB2B named someone you asked Tin to forget, so Tin left them out."
    if outcome == "no_profile":
        return "RB2B's visitor had no LinkedIn profile Tin could read, so Tin left them out."
    if person is None:
        return "Tin read a visitor RB2B identified."
    fields = person["fields"]
    name = (fields.get("name") or {}).get("text") or "A visitor"
    headline = (fields.get("headline") or {}).get("text")
    who = f"{name}, {headline}" if headline else name
    return f"{who}: {rating_line(fields)}."[:500]
