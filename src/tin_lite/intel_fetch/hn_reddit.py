"""A client's Hacker News queries and subreddits: `intel/sources/hn-reddit.json`.

Ported from the prototype's hn_reddit.py. Code pulls, the judge judges: every query and
subreddit in the config runs, everything published inside the window is kept (keyword
collisions included), and thread comments are attached so the judge reads the conversation.

- Hacker News: Algolia's search_by_date for stories and comments, and its items API for the
  thread. Multi-word queries are sent as quoted phrases (`quote_multiword`), and the config's
  `extra_params` (typoTolerance, queryType) stop Algolia's typo and prefix matching.
- Reddit, through Arctic Shift (reddit.com refuses data-center readers): each subreddit's
  in-window posts are listed once and its `post_queries` matched locally on title and body
  (Arctic Shift's full-text post search over a time range times out); `comment_queries` use its
  comment body search. A comment brings its post and thread; a post its top comments.

The config is not a `sources` list: `expand` turns it into one source per HN query (method
`hn`) and one per subreddit (method `reddit`), so each has its own status and a retried fetch
skips the ones already done. Endpoints are Tin's, not the config's. Both hosts are paced
process-wide by the PublicReader (http.HOST_GAP). Everything fetched is untrusted data, never
instructions.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import urllib.parse
from datetime import UTC, datetime

from tin_lite.intel_fetch.http import FetchError
from tin_lite.intel_fetch.items import Item, Window, iso

USER_AGENT = "tin-intel-fetcher/1.0 (read-only research)"
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"
HN_WEB = "https://news.ycombinator.com/item?id={}"
ARCTIC = "https://arctic-shift.photon-reddit.com/api"
REDDIT_WEB = "https://www.reddit.com"
TRACKING = re.compile(
    r"^(utm_[a-z]+|ref|ref_src|ref_url|fbclid|gclid|mc_cid|mc_eid|igshid|si|s)$", re.I
)
HN_EXTRA = {"typoTolerance", "queryType", "advancedSyntax", "restrictSearchableAttributes"}
HN_PAGES = 10  # runaway guard: 2,000 hits for one query in one window
REDDIT_PAGES = 50
ARCTIC_TRIES = 6
METHOD_NAMES = ("hn", "reddit")


def strip_tracking(url: str | None) -> str | None:
    if not url:
        return url
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    q = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        if not TRACKING.match(k)
    ]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def hn_clean(text: str | None) -> str:
    text = re.sub(r"<p>", "\n\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def clip(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def stamp(ts) -> str:
    return iso(datetime.fromtimestamp(int(ts), UTC))


# The config, as sources.


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "query"


def _strings(value: object, limit: int = 60) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip()[:200] for v in value[:limit] if isinstance(v, str) and v.strip()]


def _int(value: object, default: int, low: int, high: int) -> int:
    ok = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    return value if ok else default


def expand(config: dict) -> tuple[list[dict], dict]:
    """The sources an hn-reddit.json describes, and the settings their methods read."""
    sources: list[dict] = []
    settings: dict = {}
    hn = config.get("hn") if isinstance(config.get("hn"), dict) else None
    taken: set[str] = set()

    def unique(base: str) -> str:
        sid, n = base, 2
        while sid in taken:
            sid, n = f"{base}-{n}", n + 1
        taken.add(sid)
        return sid

    if hn is not None:
        comments = hn.get("thread_comments") if isinstance(hn.get("thread_comments"), dict) else {}
        extra = hn.get("extra_params") if isinstance(hn.get("extra_params"), dict) else {}
        settings["hn"] = {
            "quote_multiword": hn.get("quote_multiword", True) is not False,
            "extra_params": {
                k: str(v)[:40] for k, v in extra.items() if k in HN_EXTRA and v is not None
            },
            "max_comments": _int(comments.get("max_comments"), 8, 0, 30),
            "max_chars": _int(comments.get("max_chars_per_comment"), 1200, 100, 4000),
        }
        for query in hn.get("queries") or []:
            if not isinstance(query, dict):
                continue
            q = query.get("q")
            sources.append(
                {
                    "id": unique(f"hn:{_slug(str(q or ''))}"),
                    "name": f"Hacker News: {str(q)[:150]}" if q else "Hacker News",
                    "method": "hn",
                    "q": q,
                    "tags": query.get("tags", ["story", "comment"]),
                    "enabled": query.get("enabled", True),
                }
            )
    reddit = config.get("reddit") if isinstance(config.get("reddit"), dict) else None
    if reddit is not None:
        comments = (
            reddit.get("thread_comments") if isinstance(reddit.get("thread_comments"), dict) else {}
        )
        settings["reddit"] = {
            "max_comments": _int(comments.get("max_comments"), 10, 0, 30),
            "max_chars": _int(comments.get("max_chars_per_comment"), 1200, 100, 4000),
        }
        for sub in reddit.get("subreddits") or []:
            if not isinstance(sub, dict):
                continue
            name = sub.get("name")
            sources.append(
                {
                    "id": unique(f"reddit:{_slug(str(name or ''))}"),
                    "name": f"r/{str(name)[:40]}",
                    "method": "reddit",
                    "subreddit": name,
                    "all_posts": sub.get("all_posts") is True,
                    "post_queries": sub.get("post_queries", []),
                    "comment_queries": sub.get("comment_queries", []),
                    "enabled": sub.get("enabled", True),
                }
            )
    return sources, settings


def check(source: dict) -> str | None:
    """Why an expanded HN or Reddit source can't run, or None."""
    if source["method"] == "hn":
        q = source.get("q")
        if not isinstance(q, str) or not 0 < len(q.strip()) <= 200:
            return "its query is missing or longer than 200 characters"
        tags = source.get("tags")
        if not isinstance(tags, list) or not tags or set(tags) - {"story", "comment"}:
            return "its tags must be story, comment or both"
        return None
    if source["method"] == "reddit":
        if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", str(source.get("subreddit") or "")):
            return "its subreddit name is not valid"
        for name in ("post_queries", "comment_queries"):
            value = source.get(name)
            if not isinstance(value, list) or len(value) > 60:
                return f"its {name} must be a list of at most 60 phrases"
            if any(not isinstance(v, str) or not 0 < len(v.strip()) <= 200 for v in value):
                return f"its {name} must hold short phrases"
        if not (
            source.get("all_posts") or source.get("post_queries") or source.get("comment_queries")
        ):
            return "it asks for no posts and no comments"
        return None
    return "its method is not hn or reddit"


# Hacker News.


async def _hn_json(ctx: dict, url: str):
    return await ctx["reader"].json(url, headers={"Accept": "application/json"}, retries=3)


async def _hn_tree(ctx: dict, story_id) -> dict | None:
    trees = ctx.setdefault("hn_trees", {})
    if story_id not in trees:
        try:
            trees[story_id] = await _hn_json(ctx, HN_ITEM.format(id=int(story_id)))
        except (FetchError, ValueError, TypeError):
            trees[story_id] = None
    return trees[story_id]


def _flatten(node: dict, depth: int = 0, parent=None, out=None) -> list:
    out = [] if out is None else out
    for child in node.get("children") or []:
        if child.get("type") == "comment" and child.get("text"):
            out.append((depth, child, parent))
        _flatten(child, depth + 1, child, out)
    return out


async def _hn_thread(ctx: dict, story_id, focus_id=None) -> tuple[str, dict | None]:
    tree = await _hn_tree(ctx, story_id)
    if not tree:
        return "", None
    opts = ctx.get("hn") or {}
    most, chars = opts.get("max_comments", 8), opts.get("max_chars", 1200)
    flat = _flatten(tree)
    lines = []
    if focus_id is not None:
        # The matched comment's parent and direct replies, then the thread's longest top ones.
        for _depth, comment, parent in flat:
            if str(comment.get("id")) == str(focus_id):
                if parent is not None and parent.get("text"):
                    lines.append(
                        f"[parent] {parent.get('author')}: {clip(hn_clean(parent['text']), chars)}"
                    )
                for reply in (comment.get("children") or [])[:most]:
                    if reply.get("text"):
                        lines.append(
                            f"[reply] {reply.get('author')}: {clip(hn_clean(reply['text']), chars)}"
                        )
                break
    top = [c for d, c, _p in flat if d <= 1 and str(c.get("id")) != str(focus_id)]
    top.sort(key=lambda c: -len(c.get("text") or ""))
    for comment in top[: most if focus_id is None else max(2, most // 3)]:
        lines.append(f"{comment.get('author')}: {clip(hn_clean(comment['text']), chars)}")
    return "\n\n".join(lines), tree


async def _hn_search(ctx: dict, q: str, tag: str, window: Window) -> tuple[list[dict], str]:
    opts = ctx.get("hn") or {}
    phrase = (
        f'"{q}"' if opts.get("quote_multiword", True) and " " in q and not q.startswith('"') else q
    )
    hits: list[dict] = []
    for page in range(HN_PAGES):
        params = {
            "query": phrase,
            "tags": tag,
            "hitsPerPage": 200,
            "page": page,
            "numericFilters": f"created_at_i>={int(window.since.timestamp())},"
            f"created_at_i<{int(window.until.timestamp())}",
            **(opts.get("extra_params") or {}),
        }
        try:
            body = await _hn_json(ctx, HN_SEARCH + "?" + urllib.parse.urlencode(params))
        except FetchError:
            if page == 0:
                raise
            break  # keep the pages already read
        ctx["http"] = 200
        if not isinstance(body, dict):
            raise FetchError("Algolia's answer was not the expected JSON")
        hits.extend(h for h in body.get("hits") or [] if isinstance(h, dict))
        if page + 1 >= (body.get("nbPages") or 0) or not body.get("hits"):
            break
    return hits, HN_SEARCH + "?" + urllib.parse.urlencode({"query": q, "tags": tag})


async def _hn_story(ctx: dict, hit: dict, search_url: str) -> Item:
    sid = hit["objectID"]
    discussion = HN_WEB.format(sid)
    own = hn_clean(hit.get("story_text") or "")
    thread, _tree = await _hn_thread(ctx, sid)
    engagement = {
        k: v for k, v in (("points", hit.get("points")), ("comments", hit.get("num_comments")))
    }
    return Item(
        source_type="hn",
        source="Hacker News",
        source_url=search_url,
        url=strip_tracking(hit.get("url")) or discussion,
        discussion_url=discussion,
        title=hit.get("title"),
        author=hit.get("author"),
        published_at=stamp(hit["created_at_i"]),
        text=own + ("\n\n--- comments ---\n\n" + thread if thread else ""),
        engagement={k: v for k, v in engagement.items() if v is not None},
        fetched_at=ctx["fetched_at"],
    )


async def _hn_comment(ctx: dict, hit: dict, search_url: str) -> Item:
    cid, sid = hit["objectID"], hit.get("story_id")
    thread, tree = await _hn_thread(ctx, sid, focus_id=cid) if sid else ("", None)
    own = hn_clean(hit.get("comment_text") or "")
    head = f'Comment in HN thread "{hit.get("story_title") or ""}"'
    if hit.get("story_url"):
        head += f" ({hit['story_url']})"
    text = f"{head}\n\n{hit.get('author')}: {own}"
    if thread:
        text += "\n\n--- thread context ---\n\n" + thread
    engagement = (
        {"story_points": tree.get("points")} if tree and tree.get("points") is not None else {}
    )
    return Item(
        source_type="hn",
        source="Hacker News (comment)",
        source_url=search_url,
        url=HN_WEB.format(cid),
        discussion_url=HN_WEB.format(sid) if sid else None,
        title=f"Comment on: {hit.get('story_title')}" if hit.get("story_title") else None,
        author=hit.get("author"),
        published_at=stamp(hit["created_at_i"]),
        text=text,
        engagement=engagement,
        fetched_at=ctx["fetched_at"],
    )


async def m_hn(src: dict, window: Window, ctx: dict) -> list[Item]:
    items: list[Item] = []
    seen: set[str] = set()
    for tag in src.get("tags") or ["story", "comment"]:
        hits, search_url = await _hn_search(ctx, src["q"], tag, window)
        ctx["seen"] += len(hits)
        for hit in hits:
            if "objectID" not in hit or "created_at_i" not in hit:
                continue
            build = _hn_story if tag == "story" else _hn_comment
            item = await build(ctx, hit, search_url)
            if item["url"] not in seen:
                seen.add(item["url"])
                items.append(item)
    return items


# Reddit, through Arctic Shift.


def _reset(headers: dict) -> float:
    try:
        return min(max(float(headers.get("x-ratelimit-reset") or 30) + 1, 2), 65)
    except ValueError:
        return 30.0


async def _arctic(ctx: dict, path: str, params: dict) -> dict:
    """One Arctic Shift call. A spent per-minute budget comes back as "Too many requests" (as
    HTTP 200 with an error body, or a 422 or 429), with x-ratelimit-reset; an expensive search
    as a "Timeout" error. Both are waited out a few times; anything else fails the source."""
    url = f"{ARCTIC}/{path}?" + urllib.parse.urlencode(params)
    sleep = ctx.get("sleep") or asyncio.sleep
    last = "no answer"
    for attempt in range(ARCTIC_TRIES):
        response = await ctx["reader"].get(url, headers={"Accept": "application/json"}, retries=1)
        if response.status == 200:
            try:
                body = json.loads(response.body)
            except ValueError:
                body, error = None, "the answer was not JSON"
            else:
                error = str(body.get("error") or "") if isinstance(body, dict) else ""
            if not error:
                ctx["http"] = 200
                return body if isinstance(body, dict) else {}
        else:
            error = response.error or f"HTTP {response.status}"
            ctx["http"] = response.status or None
        last = error[:200]
        lowered = error.lower()
        if "too many" in lowered or response.status == 429:
            await sleep(_reset(response.headers))
            continue
        if "timeout" in lowered or "not json" in lowered:
            if attempt >= 2:
                break
            await sleep(10 * (attempt + 1))
            continue
        if response.status == 200 or response.status in {400, 401, 403, 404, 422}:
            break
        if attempt >= 3:
            break
        await sleep(4 + 4 * attempt)
    raise FetchError(f"Arctic Shift: {last}"[:300])


async def _reddit_page(ctx: dict, path: str, params: dict, window: Window) -> tuple[list, str]:
    """Every in-window row of a search, oldest first, paginated by created_utc."""
    rows: list[dict] = []
    after = str(int(window.since.timestamp()))
    before = str(int(window.until.timestamp()))
    for _ in range(REDDIT_PAGES):
        body = await _arctic(
            ctx, path, {**params, "after": after, "before": before, "sort": "asc", "limit": 100}
        )
        data = [row for row in body.get("data") or [] if isinstance(row, dict)]
        rows.extend(data)
        if len(data) < 100:
            break
        newest = str(int(data[-1]["created_utc"]))
        after = newest if newest != after else str(int(newest) + 1)
    return rows, f"{ARCTIC}/{path}?" + urllib.parse.urlencode(params)


async def _reddit_tree(ctx: dict, link_id: str) -> list:
    trees = ctx.setdefault("reddit_trees", {})
    if link_id in trees:
        return trees[link_id]
    flat: list = []
    try:
        body = await _arctic(ctx, "comments/tree", {"link_id": f"t3_{link_id}", "limit": 500})
    except FetchError:
        body = {}

    def walk(nodes, depth, parent):
        for node in nodes or []:
            if not isinstance(node, dict) or node.get("kind") != "t1":
                continue
            data = node.get("data") or {}
            flat.append((depth, data, parent))
            replies = data.get("replies")
            if isinstance(replies, dict):
                walk((replies.get("data") or {}).get("children"), depth + 1, data)

    walk(body.get("data") or [], 0, None)
    trees[link_id] = flat
    return flat


async def _reddit_post(ctx: dict, post_id: str) -> dict | None:
    posts = ctx.setdefault("reddit_posts", {})
    if post_id not in posts:
        try:
            body = await _arctic(ctx, "posts/ids", {"ids": post_id})
            posts[post_id] = (body.get("data") or [None])[0]
        except FetchError:
            posts[post_id] = None
    return posts[post_id]


def _ok_body(comment: dict) -> bool:
    body = (comment.get("body") or "").strip()
    return bool(body) and body not in ("[deleted]", "[removed]")


async def _reddit_thread(ctx: dict, link_id: str, focus_id: str | None = None) -> str:
    opts = ctx.get("reddit") or {}
    most, chars = opts.get("max_comments", 10), opts.get("max_chars", 1200)
    flat = await _reddit_tree(ctx, link_id)
    lines = []
    if focus_id:
        for _depth, comment, parent in flat:
            if comment.get("id") == focus_id:
                if parent is not None and _ok_body(parent):
                    lines.append(f"[parent] {parent.get('author')}: {clip(parent['body'], chars)}")
                replies = comment.get("replies")
                kids = (
                    ((replies.get("data") or {}).get("children") or [])
                    if isinstance(replies, dict)
                    else []
                )
                for kid in kids[:most]:
                    data = kid.get("data") or {}
                    if _ok_body(data):
                        lines.append(f"[reply] {data.get('author')}: {clip(data['body'], chars)}")
                break
    top = [
        c
        for d, c, _p in flat
        if d <= 1 and _ok_body(c) and c.get("id") != focus_id and c.get("author") != "AutoModerator"
    ]
    top.sort(key=lambda c: (-(c.get("score") or 0), -len(c.get("body") or "")))
    for comment in top[: most if not focus_id else max(2, most // 3)]:
        lines.append(
            f"{comment.get('author')} ({comment.get('score')} pts): {clip(comment['body'], chars)}"
        )
    return "\n\n".join(lines)


async def _post_item(ctx: dict, post: dict, source_url: str) -> Item:
    perma = REDDIT_WEB + (post.get("permalink") or f"/r/{post['subreddit']}/comments/{post['id']}/")
    own = (post.get("selftext") or "").strip()
    if own in ("[deleted]", "[removed]"):
        # Arctic Shift captures posts within seconds and never refreshes them, so a post held by
        # automod or moderators keeps only its title even if it is approved later.
        why = post.get("removed_by_category") or own.strip("[]")
        own = (
            f"[title only: post body was {own} when captured ({why}); judge from the title and "
            "any comments]"
        )
    thread = await _reddit_thread(ctx, post["id"]) if (post.get("num_comments") or 0) > 0 else ""
    url = perma if post.get("is_self", True) else (strip_tracking(post.get("url")) or perma)
    return Item(
        source_type="reddit",
        source=f"r/{post['subreddit']}",
        source_url=source_url,
        url=url,
        discussion_url=perma,
        title=post.get("title"),
        author=post.get("author"),
        published_at=stamp(post["created_utc"]),
        text=own + ("\n\n--- comments ---\n\n" + thread if thread else ""),
        engagement={"score": post.get("score"), "comments": post.get("num_comments")},
        fetched_at=ctx["fetched_at"],
    )


async def _comment_item(ctx: dict, comment: dict, source_url: str) -> Item:
    link = (comment.get("link_id") or "").replace("t3_", "")
    post = await _reddit_post(ctx, link) if link else None
    sub = comment["subreddit"]
    perma = REDDIT_WEB + (
        post.get("permalink") if post and post.get("permalink") else f"/r/{sub}/comments/{link}/"
    )
    head = f'Comment in r/{sub} thread "{(post or {}).get("title") or ""}"'
    thread = await _reddit_thread(ctx, link, focus_id=comment["id"]) if link else ""
    post_text = clip((post or {}).get("selftext") or "", 1500)
    text = f"{head}\n\n{comment.get('author')}: {comment.get('body') or ''}"
    if post_text:
        text += f"\n\n--- post text ---\n\n{post_text}"
    if thread:
        text += "\n\n--- thread context ---\n\n" + thread
    engagement = {"score": comment.get("score")}
    if post:
        engagement["thread_comments"] = post.get("num_comments")
    return Item(
        source_type="reddit",
        source=f"r/{sub} (comment)",
        source_url=source_url,
        url=f"{REDDIT_WEB}/r/{sub}/comments/{link}/comment/{comment['id']}/",
        discussion_url=perma,
        title=f"Comment on: {post.get('title')}" if post else None,
        author=comment.get("author"),
        published_at=stamp(comment["created_utc"]),
        text=text,
        engagement=engagement,
        fetched_at=ctx["fetched_at"],
    )


def term_re(q: str) -> re.Pattern:
    """Literal, case-insensitive, whole-word: meaning-level collisions are the judge's job."""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(q) + r"(?![A-Za-z0-9])", re.I)


async def m_reddit(src: dict, window: Window, ctx: dict) -> list[Item]:
    sub = src["subreddit"]
    jobs: list[tuple[str, list[dict], str]] = []
    if src.get("all_posts") or src.get("post_queries"):
        rows, public = await _reddit_page(ctx, "posts/search", {"subreddit": sub}, window)
        ctx["seen"] += len(rows)
        if src.get("all_posts"):
            jobs.append(("posts", rows, public))
        else:
            patterns = [term_re(q) for q in src.get("post_queries") or []]
            hit = [
                r
                for r in rows
                if any(
                    p.search((r.get("title") or "") + "\n" + (r.get("selftext") or ""))
                    for p in patterns
                )
            ]
            jobs.append(("posts", hit, public))
    errors = []
    for q in src.get("comment_queries") or []:
        try:
            rows, public = await _reddit_page(
                ctx, "comments/search", {"subreddit": sub, "body": q}, window
            )
        except FetchError as exc:
            errors.append(f"comments:{q}: {exc}")
            continue
        ctx["seen"] += len(rows)
        jobs.append(("comments", rows, public))
    items: list[Item] = []
    seen: set[str] = set()
    for kind, rows, public in jobs:
        for row in rows:
            try:
                build = _post_item if kind == "posts" else _comment_item
                item = await build(ctx, row, public)
            except (KeyError, TypeError, ValueError):
                continue  # one malformed row never stops the subreddit
            if item["url"] not in seen:
                seen.add(item["url"])
                items.append(item)
    if errors and not jobs:
        # Every search this subreddit asked for failed: the source failed, not came back empty.
        raise FetchError("; ".join(errors)[:300])
    return items


# A subreddit with busy threads takes minutes at Arctic Shift's pace: its per-source deadline
# in run_sources (settings["method_timeouts"]) is longer than the default.
METHOD_TIMEOUTS = {"reddit": 900}
METHODS = {"hn": m_hn, "reddit": m_reddit}
