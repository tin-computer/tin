"""Podscan reads for `managed.podscan`: podcast search, guest appearances and charts.

Podscan indexes about four million podcasts with transcripts and the people in each episode
(hosts, guests with their company and occupation, sponsors). Tin holds one Podscan key and a
flat subscription, so these reads are free to runs; a package's `max_calls` is the runaway
guard. Every response is projected to what a guest-booking report needs: no transcripts, no
audio URLs, and descriptions cut short. API reference: https://podscan.fm/docs/rest-api

Data caveats a caller must carry into its report:
- One person often has several entity records (one per episode, sometimes misspelled), so
  merge people by company before counting appearances.
- `audience_size` is Podscan's estimate and disagrees with other sources; compare it only
  between shows in the same language and niche.
- `listed_email` comes from the feed or Podscan's enrichment and is often a placeholder or a
  hosting provider's address. It is not a pitch address until the show's own page says so.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from tin_lite.connection_records import ServiceArgumentError, fit_records, text

PROVIDER = "managed.podscan"
CAPABILITY = "podcasts.read"
ORIGIN = "https://podscan.fm/api/v1"
# Under the gateway's 60-second per-call limit.
SECONDS = 30
MAX_BYTES = 8_000_000
# A sweep runs every topic's reads inside one gateway call, so all of them share its limit.
SWEEP_SECONDS = 45
SWEEP_CONCURRENCY = 8
SWEEP_TOPICS = 10
# A show is active when it posted within this many days; topic episodes are read this far back.
ACTIVE_DAYS = 120
EPISODE_DAYS = 180
# A publisher with this many shows in one sweep is a feed network, not a show to pitch.
NETWORK_SHOWS = 4

EPISODE_FIELDS = ("transcription", "title", "description")
PODCAST_FIELDS = ("name", "description", "website", "publisher_name")
PEOPLE_FIELDS = ("name", "company", "occupation", "industry")
ROLES = ("guest", "host", "sponsor", "mention")
ID = {
    "podcast_id": re.compile(r"pd_[a-z0-9]{8,40}\Z"),
    "entity_id": re.compile(r"en_[a-z0-9]{8,40}\Z"),
}
CATEGORY = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
TAG = re.compile(r"<[^>]{0,200}>")
# Diarization labels Podscan uses when it could not name a speaker.
SPEAKER_LABEL = re.compile(r"SPEAKER_\d+\Z")


@dataclass(frozen=True)
class Operation:
    arguments: frozenset[str]


OPERATIONS = {
    "episodes.search": Operation(
        frozenset(
            {
                "query",
                "since",
                "before",
                "language",
                "region",
                "has_guests",
                "min_audience",
                "search_fields",
                "order_by",
                "per_page",
                "page",
            }
        )
    ),
    "podcasts.search": Operation(
        frozenset(
            {
                "query",
                "language",
                "region",
                "has_guests",
                "min_audience",
                "active_since",
                "search_fields",
                "order_by",
                "per_page",
                "page",
            }
        )
    ),
    "podcasts.get": Operation(frozenset({"podcast_id"})),
    "podcasts.episodes": Operation(frozenset({"podcast_id", "per_page", "page"})),
    "people.search": Operation(frozenset({"query", "search_fields", "type", "per_page", "page"})),
    "people.appearances": Operation(
        frozenset({"entity_id", "role", "since", "before", "per_page", "page"})
    ),
    "charts.top": Operation(frozenset({"platform", "country", "category", "limit"})),
    "podcasts.sweep": Operation(
        frozenset({"topics", "language", "region", "active_since", "limit", "today"})
    ),
}


# ---------------------------------------------------------------- arguments


def _integer(args, name, default, low, high):
    value = args.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise ServiceArgumentError(f"{name} must be an integer from {low} to {high}")
    return value


def _query(args):
    value = args.get("query")
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or not value.isprintable()
    ):
        raise ServiceArgumentError("query must be 1-200 printable characters")
    return value.strip()


def _day(args, name):
    value = args.get(name)
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value, str) else None
    except ValueError:
        pass
    raise ServiceArgumentError(f"{name} must be a date as YYYY-MM-DD")


def _code(args, name, pattern, example):
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ServiceArgumentError(f"{name} must be a code such as {example}")
    return value.lower()


def _flag(args, name):
    value = args.get(name)
    if value is not None and type(value) is not bool:
        raise ServiceArgumentError(f"{name} must be true or false")
    return value


def _fields(args, allowed):
    value = args.get("search_fields")
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or len(set(value)) != len(value)
        or set(value) - set(allowed)
    ):
        raise ServiceArgumentError("search_fields must be a list drawn from " + ", ".join(allowed))
    return [name for name in allowed if name in value]


def _choice(args, name, default, allowed):
    value = args.get(name, default)
    if value not in allowed:
        raise ServiceArgumentError(f"{name} must be one of {', '.join(allowed)}")
    return value


def _id(args, name):
    value = args.get(name)
    if not isinstance(value, str) or not ID[name].fullmatch(value):
        prefix = name[:2]
        raise ServiceArgumentError(f"{name} must be a Podscan id such as {prefix}_abc123xyz")
    return value


def request_for(operation: str, args: dict) -> dict[str, Any]:
    """The normalized request: `path`, query `params` (Podscan's names) and `echo` (ours)."""
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise ServiceArgumentError("unknown operation")
    extra = set(args) - spec.arguments
    if extra:
        raise ServiceArgumentError(f"unsupported argument {sorted(extra)[0]}")
    if operation == "podcasts.sweep":
        return _sweep_request(args)
    params: dict[str, Any] = {}
    echo: dict[str, Any] = {}
    paged = operation not in {"podcasts.get", "charts.top"}
    if paged:
        echo["per_page"] = _integer(args, "per_page", 20, 1, 50)
        echo["page"] = _integer(args, "page", 1, 1, 20)
        params.update(per_page=echo["per_page"], page=echo["page"])
    if operation in {"episodes.search", "podcasts.search", "people.search"}:
        echo["query"] = params["query"] = _query(args)
    if operation in {"episodes.search", "podcasts.search"}:
        language = _code(args, "language", r"[A-Za-z]{2,3}", "en or tr")
        region = _code(args, "region", r"[A-Za-z]{2}", "us or tr")
        has_guests = _flag(args, "has_guests")
        minimum = args.get("min_audience")
        if minimum is not None:
            minimum = _integer(args, "min_audience", 0, 0, 10_000_000)
        echo.update(language=language, region=region, has_guests=has_guests, min_audience=minimum)
        if has_guests is not None:
            params["has_guests"] = "true" if has_guests else "false"
    if operation == "episodes.search":
        fields = _fields(args, EPISODE_FIELDS)
        since, before = _day(args, "since"), _day(args, "before")
        order = _choice(args, "order_by", "best_match", ("best_match", "posted_at"))
        echo.update(search_fields=fields, since=since, before=before, order_by=order)
        params.update(
            show_full_podcast="true",
            show_only_fully_processed="true",
            order_by=order,
            order_dir="desc",
        )
        if echo["language"]:
            params["podcast_language"] = echo["language"]
        if echo["region"]:
            params["podcast_region"] = echo["region"].upper()
        if echo["min_audience"] is not None:
            params["min_podcast_audience_size"] = echo["min_audience"]
        if fields:
            params["search_fields"] = ",".join(fields)
        if since:
            params["since"] = f"{since} 00:00:00"
        if before:
            params["before"] = f"{before} 23:59:59"
        return {"path": "/episodes/search", "params": params, "echo": echo}
    if operation == "podcasts.search":
        fields = _fields(args, PODCAST_FIELDS)
        active_since = _day(args, "active_since")
        order = _choice(
            args,
            "order_by",
            "best_match",
            ("best_match", "audience_size", "rating", "last_posted_at"),
        )
        echo.update(search_fields=fields, active_since=active_since, order_by=order)
        params.update(order_by=order, order_dir="desc")
        if echo["language"]:
            params["language"] = echo["language"]
        if echo["region"]:
            params["region"] = echo["region"].upper()
        if echo["min_audience"] is not None:
            params["min_audience_size"] = echo["min_audience"]
        if fields:
            params["search_fields"] = ",".join(fields)
        if active_since:
            params["min_last_episode_posted_at"] = active_since
        return {"path": "/podcasts/search", "params": params, "echo": echo}
    if operation == "podcasts.get":
        echo["podcast_id"] = _id(args, "podcast_id")
        return {"path": f"/podcasts/{echo['podcast_id']}", "params": params, "echo": echo}
    if operation == "podcasts.episodes":
        echo["podcast_id"] = _id(args, "podcast_id")
        params.update(order_by="posted_at", order_dir="desc")
        return {
            "path": f"/podcasts/{echo['podcast_id']}/episodes",
            "params": params,
            "echo": echo,
        }
    if operation == "people.search":
        fields = _fields(args, PEOPLE_FIELDS)
        kind = _choice(args, "type", "person", ("person", "organization"))
        echo.update(search_fields=fields, type=kind)
        params["type"] = kind
        if fields:
            params["search_fields"] = ",".join(fields)
        return {"path": "/entities/search", "params": params, "echo": echo}
    if operation == "people.appearances":
        echo["entity_id"] = _id(args, "entity_id")
        echo["role"] = params["role"] = _choice(args, "role", "guest", ROLES)
        since, before = _day(args, "since"), _day(args, "before")
        echo.update(since=since, before=before)
        if since:
            params["from"] = since
        if before:
            params["to"] = before
        return {
            "path": f"/entities/{echo['entity_id']}/appearances",
            "params": params,
            "echo": echo,
        }
    # charts.top
    platform = _choice(args, "platform", "apple", ("apple", "spotify"))
    country = _code(args, "country", r"[A-Za-z]{2}", "us or tr")
    category = args.get("category", "all")
    if not isinstance(category, str) or not CATEGORY.fullmatch(category):
        raise ServiceArgumentError("category must be a chart slug such as technology")
    if country is None:
        raise ServiceArgumentError("country is required")
    limit = _integer(args, "limit", 50, 1, 200 if platform == "apple" else 50)
    echo.update(platform=platform, country=country, category=category, limit=limit)
    return {
        "path": f"/charts/{platform}/{country}/{category}/top",
        "params": {"limit": limit},
        "echo": echo,
    }


def _sweep_request(args: dict) -> dict[str, Any]:
    """Four reads a topic: the best matches (two pages), the biggest matches, and the shows
    whose recent episodes match. Topic words in a show's name or description find the niche
    shows; the biggest-first read finds the shows a whole field listens to."""
    topics = args.get("topics")
    if not isinstance(topics, list) or not 1 <= len(topics) <= SWEEP_TOPICS:
        raise ServiceArgumentError(f"topics must be a list of 1-{SWEEP_TOPICS} search phrases")
    clean: dict[str, str] = {}
    for topic in topics:
        if (
            not isinstance(topic, str)
            or not topic.strip()
            or len(topic) > 80
            or not topic.isprintable()
        ):
            raise ServiceArgumentError("each topic must be 1-80 printable characters")
        clean.setdefault(topic.strip().casefold(), topic.strip())
    today = _day(args, "today") or date.today().isoformat()
    active_since = (
        _day(args, "active_since")
        or (date.fromisoformat(today) - timedelta(days=ACTIVE_DAYS)).isoformat()
    )
    episodes_since = (date.fromisoformat(today) - timedelta(days=EPISODE_DAYS)).isoformat()
    language = _code(args, "language", r"[A-Za-z]{2,3}", "en or tr") or "en"
    region = _code(args, "region", r"[A-Za-z]{2}", "us or tr")
    echo = {
        "topics": list(clean.values()),
        "language": language,
        "region": region,
        "active_since": active_since,
        "limit": _integer(args, "limit", 100, 10, 100),
    }
    shows = {
        "search_fields": "name,description",
        "min_last_episode_posted_at": active_since,
        "per_page": 50,
        "language": language,
        "order_dir": "desc",
        **({"region": region.upper()} if region else {}),
    }
    reads = []
    for topic in echo["topics"]:
        reads += [
            {
                "topic": topic,
                "kind": "shows",
                "path": "/podcasts/search",
                "params": {**shows, "query": topic, "order_by": "best_match", "page": page},
            }
            for page in (1, 2)
        ]
        reads.append(
            {
                "topic": topic,
                "kind": "shows",
                "path": "/podcasts/search",
                "params": {**shows, "query": topic, "order_by": "audience_size", "page": 1},
            }
        )
        reads.append(
            {
                "topic": topic,
                "kind": "episodes",
                "path": "/episodes/search",
                "params": {
                    "query": topic,
                    "search_fields": "title,description",
                    "since": f"{episodes_since} 00:00:00",
                    "per_page": 50,
                    "page": 1,
                    "order_by": "best_match",
                    "order_dir": "desc",
                    "show_full_podcast": "true",
                    "podcast_language": language,
                    **({"podcast_region": region.upper()} if region else {}),
                },
            }
        )
    return {"reads": reads, "echo": echo}


# ---------------------------------------------------------------- projections


def _plain(value: Any, limit: int) -> str | None:
    """Provider text without markup or search highlighting, collapsed and cut short."""
    if not isinstance(value, str):
        return None
    value = html.unescape(TAG.sub(" ", value.replace("\x00", "")))
    return re.sub(r"\s+", " ", value).strip()[:limit] or None


def _count(value: Any) -> int | None:
    """Podscan sends rating counts as numeric strings."""
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdigit() and len(value) < 12:
        return int(value)
    return None


def _rating(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if 0 <= number <= 5 else None


def _day_of(value: Any) -> str | None:
    return value[:10] if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value) else None


def _list(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def podcast_record(podcast: dict, *, full: bool = False) -> dict[str, Any]:
    reach = podcast.get("reach") if isinstance(podcast.get("reach"), dict) else {}
    itunes = reach.get("itunes") if isinstance(reach.get("itunes"), dict) else {}
    spotify = reach.get("spotify") if isinstance(reach.get("spotify"), dict) else {}
    summary = podcast.get("podcast_summary")
    summary = summary if isinstance(summary, dict) else {}
    duration = podcast.get("avg_episode_duration")
    record = {
        "podcast_id": text(podcast.get("podcast_id"), 48),
        "name": _plain(podcast.get("podcast_name"), 200),
        "url": text(podcast.get("podcast_url"), 500),
        "publisher": _plain(podcast.get("publisher_name"), 200),
        "language": text(podcast.get("language"), 8),
        "region": text(podcast.get("region"), 8),
        "has_guests": podcast.get("podcast_has_guests")
        if type(podcast.get("podcast_has_guests")) is bool
        else None,
        "reach_score": _count(podcast.get("podcast_reach_score")),
        "audience_size": _count(reach.get("audience_size")),
        "apple_ratings": _count(itunes.get("itunes_rating_count")),
        "apple_rating": _rating(itunes.get("itunes_rating_average")),
        "spotify_ratings": _count(spotify.get("spotify_rating_count")),
        "release_frequency": text(podcast.get("podcast_release_frequency"), 40),
        "episode_count": _count(podcast.get("episode_count")),
        "last_posted": _day_of(podcast.get("last_posted_at")),
        "is_active": podcast.get("is_active") if type(podcast.get("is_active")) is bool else None,
    }
    if not full:
        return record
    links = []
    for link in _list(reach.get("social_links"))[:6]:
        url = text(link.get("url"), 300)
        if url:
            links.append({"platform": text(str(link.get("platform")), 20), "url": url})
    record.update(
        description=_plain(podcast.get("podcast_description"), 600),
        style=_plain(summary.get("style"), 300),
        categories=[
            name
            for name in (
                text(item.get("category_name"), 60)
                for item in _list(podcast.get("podcast_categories"))
            )
            if name
        ][:6],
        has_sponsors=podcast.get("podcast_has_sponsors")
        if type(podcast.get("podcast_has_sponsors")) is bool
        else None,
        avg_episode_minutes=round(duration / 60) if type(duration) is int else None,
        rss_url=text(podcast.get("rss_url"), 500),
        apple_id=text(podcast.get("podcast_itunes_id"), 20),
        website=text(reach.get("website"), 500),
        social_links=links,
        # Unverified: feed or enrichment data, often a placeholder. See the module docstring.
        listed_email=text(reach.get("email"), 200),
    )
    return record


def _name(value: Any) -> str | None:
    name = _plain(value, 120)
    return None if name is None or SPEAKER_LABEL.fullmatch(name) else name


def _person(item: dict, prefix: str) -> dict[str, Any] | None:
    name = _name(item.get(f"{prefix}_name"))
    if not name:
        return None
    person = {"name": name}
    for field in ("company", "occupation", "industry"):
        value = _plain(item.get(f"{prefix}_{field}"), 120)
        if value:
            person[field] = value
    return person


def episode_record(episode: dict) -> dict[str, Any]:
    metadata = episode.get("metadata") if isinstance(episode.get("metadata"), dict) else {}
    highlight = episode.get("_search_highlight")
    highlight = highlight if isinstance(highlight, dict) else {}
    duration = episode.get("episode_duration")
    confidence = metadata.get("is_branded_confidence_score")
    record = {
        "episode_id": text(episode.get("episode_id"), 48),
        "title": _plain(episode.get("episode_title"), 300),
        "posted": _day_of(episode.get("posted_at")),
        "url": text(episode.get("episode_permalink") or episode.get("episode_url"), 500),
        "minutes": round(duration / 60) if type(duration) is int else None,
        "guests": [
            person
            for item in _list(metadata.get("guests"))[:8]
            if (person := _person(item, "guest"))
        ],
        "hosts": [
            name
            for name in (_name(item.get("host_name")) for item in _list(metadata.get("hosts"))[:4])
            if name
        ],
        "sponsors": [
            name
            for name in (
                _plain(item.get("sponsor_name"), 120)
                for item in _list(metadata.get("sponsors"))[:6]
            )
            if name
        ],
        # Podscan's judgement that the episode is paid or branded content.
        "is_branded": metadata.get("is_branded")
        if type(metadata.get("is_branded")) is bool
        else None,
        "branded_confidence": round(confidence, 2)
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        else None,
        "summary": _plain(metadata.get("summary_short"), 300)
        or _plain(episode.get("episode_description"), 300),
    }
    match = next(
        (
            _plain(highlight.get(field), 300)
            for field in ("transcription", "description", "title")
            if _plain(highlight.get(field), 300)
        ),
        None,
    )
    if match:
        record["match"] = match
    podcast = episode.get("podcast")
    if isinstance(podcast, dict) and podcast.get("podcast_id"):
        record["podcast"] = podcast_record(podcast)
    return record


def person_record(entity: dict) -> dict[str, Any]:
    counts = entity.get("appearances") if isinstance(entity.get("appearances"), dict) else {}
    return {
        "entity_id": text(entity.get("entity_id"), 48),
        "name": _plain(entity.get("entity_name"), 120),
        "type": text(entity.get("entity_type"), 20),
        "company": _plain(entity.get("company"), 120),
        "occupation": _plain(entity.get("occupation"), 120),
        "industry": _plain(entity.get("industry"), 120),
        "guest_appearances": _count(counts.get("guests_count")),
        "host_appearances": _count(counts.get("hosts_count")),
    }


def chart_record(item: dict) -> dict[str, Any]:
    return {
        "rank": _count(item.get("rank")),
        "name": _plain(item.get("name"), 200),
        "publisher": _plain(item.get("publisher"), 200),
        "movement": text(str(item.get("movement")), 20)
        if item.get("movement") is not None
        else None,
        "podcast_id": text(item.get("podcast_id"), 48),
    }


def result(operation: str, request: dict, payload: dict, *, max_response_bytes: int) -> dict:
    """Project one Podscan answer and keep the leading records that fit the binding."""
    echo = request["echo"]
    if operation == "podcasts.get":
        podcast = payload.get("podcast") if isinstance(payload.get("podcast"), dict) else payload
        return {"status": "ok", **podcast_record(podcast, full=True)}
    if operation == "charts.top":
        items = _list(payload.get("podcasts"))
        if not items and isinstance(payload.get("data"), dict):
            items = _list(payload["data"].get("shows"))
        records = [chart_record(item) for item in items]
        page = fit_records(
            records,
            max_response_bytes=max_response_bytes,
            has_more=False,
            envelope={"status": "ok", **echo},
            cursor_field="rank",
        )
        page.pop("next_cursor")
        return page
    if operation == "episodes.search" or operation == "podcasts.episodes":
        records = [episode_record(item) for item in _list(payload.get("episodes"))]
    elif operation == "podcasts.search":
        records = [podcast_record(item) for item in _list(payload.get("podcasts"))]
    elif operation == "people.search":
        records = [person_record(item) for item in _list(payload.get("entities"))]
    else:  # people.appearances
        records = []
        for item in _list(payload.get("appearances")):
            episode = item.get("episode")
            if isinstance(episode, dict):
                records.append({"role": text(item.get("role"), 20), **episode_record(episode)})
        entity = payload.get("entity")
        if isinstance(entity, dict):
            echo = {**echo, "person": person_record(entity)}
    pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
    last = _count(pagination.get("last_page"))
    total = _count(pagination.get("total"))
    upstream_more = last is not None and echo["page"] < last
    envelope = {"status": "ok", **{k: v for k, v in echo.items() if v is not None}}
    envelope["total"] = total
    page = fit_records(
        records,
        max_response_bytes=max_response_bytes,
        has_more=upstream_more,
        envelope=envelope,
        cursor_field="episode_id"
        if "episode" in operation or "appearances" in operation
        else "name",
    )
    page.pop("next_cursor")
    # Pages are numbered upstream. When Tin cut records to fit the bound, ask for a smaller
    # per_page instead; the next page would skip the ones left out.
    page["next_page"] = echo["page"] + 1 if upstream_more and not page["truncated"] else None
    return page


def _key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").casefold()).strip()


def sweep_result(
    request: dict,
    answers: list[tuple[dict, dict | None]],
    failed: list[dict],
    *,
    max_response_bytes: int,
) -> dict:
    """Merge every topic's reads into one ranked list of active shows.

    One show often has several feeds (a duplicate, a video feed); they merge by name and keep
    the biggest. A show ranks higher the more topics found it, the more recent episodes
    matched, and the higher its reach score. Feed networks (one publisher, many near-identical
    shows) sink. The caller reads the leading shows and judges fit; the rank only orders them.
    """
    echo = request["echo"]
    shows: dict[str, dict] = {}
    for read, payload in answers:
        if read["kind"] == "episodes":
            found = [
                (item.get("podcast"), item)
                for item in _list((payload or {}).get("episodes"))
                if isinstance(item.get("podcast"), dict)
            ]
        else:
            found = [(item, None) for item in _list((payload or {}).get("podcasts"))]
        for podcast, item in found:
            record = podcast_record(podcast)
            if not record["podcast_id"] or not record["name"]:
                continue
            if (record["last_posted"] or "") < echo["active_since"]:
                continue
            show = shows.get(_key(record["name"]))
            if show is None:
                summary = podcast.get("podcast_summary")
                summary = summary if isinstance(summary, dict) else {}
                duration = podcast.get("avg_episode_duration")
                show = shows[_key(record["name"])] = {
                    **record,
                    "avg_episode_minutes": round(duration / 60) if type(duration) is int else None,
                    "style": _plain(summary.get("style"), 80),
                    "about": _plain(summary.get("summary_long"), 200)
                    or _plain(podcast.get("podcast_description"), 200),
                    "other_feeds": [],
                    "topics": [],
                    "episode_matches": 0,
                    "matching_episode": None,
                }
            elif record["podcast_id"] != show["podcast_id"]:
                if (record["reach_score"] or 0) > (show["reach_score"] or 0):
                    feeds = [show["podcast_id"], *show["other_feeds"]]
                    show.update({k: v for k, v in record.items() if v is not None})
                    show["other_feeds"] = feeds
                elif record["podcast_id"] not in show["other_feeds"]:
                    show["other_feeds"].append(record["podcast_id"])
            if read["topic"] not in show["topics"]:
                show["topics"].append(read["topic"])
            if item is not None:
                show["episode_matches"] += 1
                if show["matching_episode"] is None:
                    show["matching_episode"] = {
                        "title": _plain(item.get("episode_title"), 120),
                        "posted": _day_of(item.get("posted_at")),
                    }
    publishers = Counter(_key(show["publisher"]) for show in shows.values() if show["publisher"])
    topic_words = [_key(topic) for topic in echo["topics"]]
    for show in shows.values():
        network = publishers[_key(show["publisher"])] if show["publisher"] else 0
        # A topic in the show's name or the opening of its summary marks a show about the
        # field, not one that mentioned it once.
        opening = _key(f"{show['name']} {(show.get('about') or '')[:120]}")
        named = any(
            words and re.search(rf"\b{re.escape(words)}\b", opening) for words in topic_words
        )
        show["score"] = round(
            len(show["topics"]) * 2
            + min(show["episode_matches"], 2) * 0.5
            + (show["reach_score"] or 0) / 25
            + (3 if named else 0)
            - (4 if network >= NETWORK_SHOWS else 0),
            2,
        )
        if network >= NETWORK_SHOWS:
            show["feed_network"] = network
        show["other_feeds"] = show["other_feeds"][:4]
        if not show["other_feeds"]:
            del show["other_feeds"]
        if show["matching_episode"] is None:
            del show["matching_episode"]
        # `has_guests` is wrong on most small interview shows, so a sweep never offers it.
        # The show's page comes from `podcasts.get` for the finalists.
        for field in ("is_active", "has_guests", "region", "language", "apple_rating", "url"):
            show.pop(field, None)
        for field in [name for name, value in show.items() if value is None]:
            del show[field]
    ranked = sorted(shows.values(), key=lambda show: (-show["score"], show["name"]))
    envelope = {
        "status": "ok",
        **{k: v for k, v in echo.items() if v is not None},
        "shows_found": len(ranked),
        "reads": len(answers) + len(failed),
        "failed_reads": failed,
    }
    page = fit_records(
        ranked[: echo["limit"]],
        max_response_bytes=max_response_bytes,
        has_more=len(ranked) > echo["limit"],
        envelope=envelope,
        cursor_field="name",
    )
    page.pop("next_cursor")
    return page
