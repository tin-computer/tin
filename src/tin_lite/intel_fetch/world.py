"""The world feed's fetch methods, ported from the prototype's world.py.

Big world, tech, business and culture news, not filtered for any client: wires and major
outlets, Google News front pages, Techmeme, the Hacker News front page, Wikipedia's most viewed
pages and Google Trends. One item per source entry; nothing is merged by meaning. Feeds, HTML,
URLs and dates are read with the helpers every intel fetcher shares (feeds.py), so an article
cleans to the same URL here and in a client's fetch, and a Google News item has the same shape
on both sides: `source` is the outlet, `source_id` the feed. Google News links stay Google News
links here: unwrapping happens after the judge, only for links a reader sees (gnews.py).
tests/test_intel_fetch_world.py holds the methods to recorded output.

Methods (a source's "method"):
  rss, atom          RSS 2.0 or Atom feed.
  google_news_rss    Google News RSS (topic, top-stories or search feed).
  google_trends_rss  Google Trends daily trending searches RSS for one geo.
  techmeme_river     Techmeme's river page (headline rows with the publisher link).
  hn_front_page      Hacker News: Firebase top and best ids, Algolia's front_page tag and stories
                     above a points floor, all inside the window, with top comments.
  wikimedia_top      Wikimedia pageviews "top" for each UTC day in the window.
  gdelt_doc          GDELT DOC 2.0 artlist JSON (one call per 5 s).
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import urllib.parse
from datetime import UTC, datetime, timedelta

from tin_lite.intel_fetch import register_groups
from tin_lite.intel_fetch.feeds import clean_url, parse_date, parse_feed, parse_xml, strip_html
from tin_lite.intel_fetch.http import FetchError, PublicReader
from tin_lite.intel_fetch.items import Item, Window, iso

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
HEADERS = {"Accept": "*/*", "Accept-Language": "en-US,en;q=0.8"}
DEFAULT_MAX_TEXT = 6000


def reader(**kwargs) -> PublicReader:
    return PublicReader(user_agent=UA, **kwargs)


def item(
    src: dict,
    url: str | None,
    title: str | None,
    published: datetime | None,
    text: str,
    engagement: dict | None = None,
    author: str | None = None,
    discussion_url: str | None = None,
    fetched_at: str | None = None,
    source: str | None = None,
) -> Item:
    return Item(
        source_type="world",
        source=source or src["name"],
        source_id=src["id"],
        source_url=src.get("url"),
        url=clean_url(url),
        discussion_url=discussion_url,
        publisher_site=None,
        title=title or None,
        author=author or None,
        published_at=iso(published),
        text=text or "",
        engagement=engagement or {},
        fetched_at=fetched_at,
    )


def _in(win: Window, d: datetime) -> bool:
    return win.since <= d <= win.until


async def _get(ctx: dict, url: str, *, retries: int | None = None, **kwargs) -> bytes:
    response = await ctx["reader"].fetch(url, retries=retries, headers=dict(HEADERS), **kwargs)
    ctx["http"] = response.status
    return response.body


async def _json(ctx: dict, url: str, **kwargs):
    body = await _get(ctx, url, **kwargs)
    try:
        return json.loads(body)
    except ValueError as exc:
        raise FetchError("the answer was not JSON") from exc


async def m_rss(src: dict, win: Window, ctx: dict) -> list[Item]:
    entries = parse_feed(await _get(ctx, src["url"]))
    ctx["seen"] = len(entries)
    out = []
    for entry in entries:
        published = parse_date(entry["date"])
        if not entry["link"]:
            continue
        if published and not _in(win, published):
            continue
        if not published and not src.get("keep_undated"):
            continue
        out.append(
            item(
                src,
                entry["link"],
                entry["title"],
                published,
                entry["text"][: ctx["max_text"]],
                author=entry["author"],
                fetched_at=ctx["now"],
            )
        )
    return out


async def m_google_news_rss(src: dict, win: Window, ctx: dict) -> list[Item]:
    """A Google News feed's entries, shaped as a client's are: `source` is the outlet (the
    feed's name when Google gives none), `author` empty, `publisher_site` the outlet's site."""
    entries = parse_feed(await _get(ctx, src["url"]))
    ctx["seen"] = len(entries)
    out = []
    for entry in entries:
        published = parse_date(entry["date"])
        if not entry["link"] or not published or not _in(win, published):
            continue
        title = entry["title"] or ""
        outlet = entry["source_name"] or ""
        if outlet and title.endswith(" - " + outlet):
            title = title[: -len(outlet) - 3]
        site = entry["source_site"] or ""
        # Google News descriptions list the story cluster: "<headline> <outlet> <headline> ...".
        text = f"Outlet: {outlet} ({site})\nGoogle News cluster: {entry['text']}"
        found = item(
            src,
            entry["link"],
            title,
            published,
            text[: ctx["max_text"]],
            fetched_at=ctx["now"],
            source=outlet or None,
        )
        found["publisher_site"] = site[:300] or None
        out.append(found)
    return out


async def m_google_trends_rss(src: dict, win: Window, ctx: dict) -> list[Item]:
    body = await _get(ctx, src["url"])
    ns = {"ht": "https://trends.google.com/trending/rss"}
    root = parse_xml(body)
    geo = src.get("geo", "")
    out = []
    entries = list(root.iter("item"))
    ctx["seen"] = len(entries)
    for it in entries:
        q = (it.findtext("title") or "").strip()
        dt = parse_date(it.findtext("pubDate"))
        if not q or not dt or not _in(win, dt):
            continue
        traffic = it.findtext("ht:approx_traffic", namespaces=ns)
        news = []
        for n in it.findall("ht:news_item", ns):
            news.append(
                f"- {html.unescape(n.findtext('ht:news_item_title', namespaces=ns) or '')} "
                f"({n.findtext('ht:news_item_source', namespaces=ns) or ''}) "
                f"{n.findtext('ht:news_item_url', namespaces=ns) or ''}"
            )
        url = "https://trends.google.com/trends/explore?" + urllib.parse.urlencode(
            {"q": q, "geo": geo, "date": "now 1-d"}
        )
        text = f'Trending search in {geo}: "{q}", approx {traffic} searches.\nNews:\n' + "\n".join(
            news
        )
        out.append(
            item(
                src,
                url,
                f"Trending in {geo}: {q}",
                dt,
                text[: ctx["max_text"]],
                engagement={"approx_traffic": traffic} if traffic else {},
                fetched_at=ctx["now"],
            )
        )
    return out


_TECHMEME = re.compile(
    r'<H2>([^<]+)</H2>|<tr class="ritem"><td>([^<&]+)&nbsp;.*?pml="([^"]+)".*?<cite>(.*?)</cite>'
    r'&nbsp;\s*<a href="([^"]+)">(.*?)</a></td></tr>',
    re.S,
)


async def m_techmeme_river(src: dict, win: Window, ctx: dict) -> list[Item]:
    s = (await _get(ctx, src["url"])).decode("utf-8", "replace")
    out, day = [], None
    for m in _TECHMEME.finditer(s):
        if m.group(1):
            day = m.group(1).strip()
            continue
        if not day:
            continue
        ctx["seen"] = ctx.get("seen", 0) + 1
        try:
            local_dt = datetime.strptime(day + " " + m.group(2).strip(), "%B %d, %Y %I:%M %p")  # noqa: DTZ007
        except ValueError:
            continue
        # Techmeme prints US Eastern time; November is EST (UTC-5). Config can override.
        dt = (local_dt + timedelta(hours=src.get("utc_offset_hours", 5))).replace(tzinfo=UTC)
        if not _in(win, dt):
            continue
        cite = strip_html(m.group(4)).rstrip(":").strip()
        headline = strip_html(m.group(6))
        pml = m.group(3)
        disc = f"https://www.techmeme.com/{pml[:6]}/p{pml[7:]}#a{pml}" if "p" in pml else None
        out.append(
            item(
                src,
                html.unescape(m.group(5)),
                headline,
                dt,
                f"{headline} ({cite})",
                author=cite,
                discussion_url=disc,
                fetched_at=ctx["now"],
            )
        )
    return out


async def m_hn_front_page(src: dict, win: Window, ctx: dict) -> list[Item]:
    t0, t1 = int(win.since.timestamp()), int(win.until.timestamp())
    fb = "https://hacker-news.firebaseio.com/v0"
    ids: list[int] = []
    lists = {}
    for name in ("topstories", "beststories"):
        lists[name] = await _json(ctx, f"{fb}/{name}.json")
        ids += lists[name][: src.get(f"{name}_limit", 150)]
    alg = "https://hn.algolia.com/api/v1"
    fp = await _json(
        ctx,
        f"{alg}/search?tags=front_page&hitsPerPage=1000&numericFilters="
        + urllib.parse.quote(f"created_at_i>={t0},created_at_i<={t1}"),
    )
    floor = src.get("points_floor", 100)
    hi = await _json(
        ctx,
        f"{alg}/search_by_date?tags=story&hitsPerPage=1000&numericFilters="
        + urllib.parse.quote(f"created_at_i>={t0},created_at_i<={t1},points>={floor}"),
    )
    top_rank = {
        i: n + 1 for n, i in enumerate(lists["topstories"][: src.get("topstories_limit", 150)])
    }
    fp_ids = {int(h["objectID"]) for h in fp["hits"]}
    all_ids = list(dict.fromkeys(ids + [int(h["objectID"]) for h in fp["hits"] + hi["hits"]]))

    async def get(i):
        try:
            return await ctx["reader"].json(f"{fb}/item/{i}.json", headers=dict(HEADERS))
        except FetchError:
            return None

    fetched = await asyncio.gather(*(get(i) for i in all_ids))
    stories = [
        s
        for s in fetched
        if isinstance(s, dict) and s.get("type") == "story" and t0 <= s.get("time", 0) <= t1
    ]
    ctx["seen"] = len(stories)
    # What was on the front page: ranked now, Algolia's front_page tag, or above the floor.
    stories = [
        s
        for s in stories
        if s["id"] in top_rank or s["id"] in fp_ids or (s.get("score") or 0) >= floor
    ]
    ncom = src.get("comments_per_story", 4)
    pairs = [(s["id"], k) for s in stories for k in (s.get("kids") or [])[:ncom]]
    coms = await asyncio.gather(*(get(k) for _, k in pairs))
    cmap: dict[int, list[str]] = {}
    for (sid, _), c in zip(pairs, coms, strict=True):
        if isinstance(c, dict) and not c.get("deleted") and not c.get("dead") and c.get("text"):
            cmap.setdefault(sid, []).append(f"{c.get('by')}: {strip_html(c['text'])[:1200]}")
    out = []
    for s in stories:
        hn_url = f"https://news.ycombinator.com/item?id={s['id']}"
        parts = []
        if s.get("text"):
            parts.append(strip_html(s["text"]))
        rank = top_rank.get(s["id"])
        parts.append(
            f"[HN: {s.get('score')} points, {s.get('descendants')} comments"
            + (f", rank {rank} at fetch" if rank else "")
            + (", front_page tag" if s["id"] in fp_ids else "")
            + "]"
        )
        parts.extend(cmap.get(s["id"], []))
        out.append(
            item(
                src,
                s.get("url") or hn_url,
                s.get("title"),
                datetime.fromtimestamp(s["time"], UTC),
                "\n\n".join(parts)[: ctx["max_text"]],
                author=s.get("by"),
                discussion_url=hn_url,
                engagement={"points": s.get("score") or 0, "comments": s.get("descendants") or 0},
                fetched_at=ctx["now"],
            )
        )
    ctx["http"] = 200
    return out


async def m_wikimedia_top(src: dict, win: Window, ctx: dict) -> list[Item]:
    exclude = re.compile(
        src.get(
            "exclude_regex",
            r"^(Main_Page|Special:|Wikipedia:|Portal:|Help:|Template:|Category:|File:)",
        )
    )
    limit = src.get("limit", 50)
    project = src["project"]
    day = win.since.replace(hour=0, minute=0, second=0, microsecond=0)
    per: dict[str, list] = {}
    last_status = 200
    while day <= win.until:
        day_end = day + timedelta(days=1)
        if day_end <= win.until and day_end >= win.since:
            u = (
                f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{project}/all-access/"
                f"{day:%Y/%m/%d}"
            )
            response = await ctx["reader"].get(u, headers=dict(HEADERS))
            if response.status == 200:
                try:
                    data = json.loads(response.body)
                    arts = [
                        a for a in data["items"][0]["articles"] if not exclude.search(a["article"])
                    ][:limit]
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise FetchError("Wikimedia's answer had no article list") from exc
                for a in arts:
                    per.setdefault(a["article"], []).append((day, a["views"], a["rank"]))
            elif response.status:
                last_status = response.status  # the day isn't published yet
            else:
                raise FetchError(response.error or "Wikimedia did not answer")
        day = day_end
    ctx["http"] = last_status
    ctx["seen"] = len(per)
    out = []
    host = project + ".org"
    for art, days in per.items():
        last = max(d[0] for d in days)
        text = f"Most-viewed on {host}: " + "; ".join(
            f"{d:%Y-%m-%d} {v:,} views (rank {r})" for d, v, r in sorted(days)
        )
        out.append(
            item(
                src,
                f"https://{host}/wiki/{urllib.parse.quote(art)}",
                art.replace("_", " "),
                last + timedelta(days=1),
                text,
                engagement={"views": max(v for _, v, _ in days)},
                fetched_at=ctx["now"],
            )
        )
    return out


async def m_gdelt_doc(src: dict, win: Window, ctx: dict) -> list[Item]:
    body = await _get(ctx, src["url"])
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise FetchError(
            "GDELT returned non-JSON (rate limit): " + body[:120].decode("utf-8", "replace")
        ) from exc
    out = []
    articles = data.get("articles", [])
    ctx["seen"] = len(articles)
    for a in articles:
        dt = parse_date(
            re.sub(
                r"^(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)Z$",
                r"\1-\2-\3T\4:\5:\6+00:00",
                a.get("seendate", ""),
            )
        )
        if not dt or not _in(win, dt):
            continue
        out.append(
            item(
                src,
                a.get("url"),
                a.get("title"),
                dt,
                f"{a.get('domain')} ({a.get('sourcecountry')})",
                author=a.get("domain"),
                fetched_at=ctx["now"],
            )
        )
    return out


METHODS = {
    "rss": m_rss,
    "atom": m_rss,
    "google_news_rss": m_google_news_rss,
    "google_trends_rss": m_google_trends_rss,
    "techmeme_river": m_techmeme_river,
    "hn_front_page": m_hn_front_page,
    "wikimedia_top": m_wikimedia_top,
    "gdelt_doc": m_gdelt_doc,
}
# The world fetch's groups: each one an activity that can retry on its own.
GROUP_METHODS = {
    "gnews": {"google_news_rss"},
    "feeds": {"rss", "atom"},
    "special": {
        "google_trends_rss",
        "techmeme_river",
        "hn_front_page",
        "wikimedia_top",
        "gdelt_doc",
    },
}
GROUP_NAMES = tuple(GROUP_METHODS)


def settings(config: dict, now: datetime) -> dict:
    return {
        "now": iso(now),
        "max_text": int(config.get("max_text_chars", DEFAULT_MAX_TEXT)),
        # Google News feeds wait their paced turn (1.5 s each, and 429 pauses) behind each other.
        "method_timeouts": {"google_news_rss": 900, "hn_front_page": 600},
    }


register_groups("world", GROUP_METHODS, METHODS)
