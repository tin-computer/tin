# Ranking recipe: community teardown

This file holds the deterministic Python that turns the fetched community material into the
ranked opportunity ledger. It is the single source of truth the sandbox executes and the
offline checks in `tests/test_community_teardown.py` exercise, byte for byte. Nothing in this
block talks to a network or to a model; everything it needs arrives as the story and comment
records the skill already fetched, and it raises instead of guessing when data is missing.

Each supported community has a profile (`hacker_news` or `discourse`) with origin-relative
request paths and a field mapping. All stories are normalized to the same generic record, so
selection and ranking never care which community produced a thread.

```python
import re
import time

OPPORTUNITY_KINDS = frozenset(
    {"content_topic", "positioning_phrase", "product_signal", "engagement_candidate"}
)

DEFAULT_COMMUNITY = "hacker_news"
SUPPORTED_COMMUNITIES = ("hacker_news", "discourse")

COMMUNITY_LABELS = {
    "hacker_news": "Hacker News",
    "discourse": "Discourse forum",
}

PROFILE_PATHS = {
    "hacker_news": {
        "kind": "hn",
        "search_path": "/api/v1/search",
        "thread_path": "/api/v1/search_by_date",
        "thread_url": "https://news.ycombinator.com/item?id={id}",
    },
    "discourse": {
        "kind": "discourse",
        "search_path": "/search.json",
        "thread_path": "/t/{id}.json",
        "thread_url": "{base}/t/{id}",
    },
}

_HTML_TAG = re.compile(r"<[^>]+>")
_TOKEN = re.compile(r"[a-z0-9]{2,}")


def epoch_before(days, now=None):
    days = int(days)
    if not 7 <= days <= 730:
        raise ValueError("window_days must be between 7 and 730")
    stamp = now if now is not None else time.time()
    return int(stamp) - days * 86400


def resolve_community(community):
    value = str(community or "").strip().lower() or DEFAULT_COMMUNITY
    if value not in SUPPORTED_COMMUNITIES:
        raise ValueError("unsupported community; choose hacker_news or discourse")
    return value


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value):
    text = str(value or "")
    plain = _HTML_TAG.sub(" ", text)
    for entity, char in (
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
    ):
        plain = plain.replace(entity, char)
    return re.sub(r"\s+", " ", plain).strip()


def parse_story(community, hit):
    community = resolve_community(community)
    if community == "hacker_news":
        return _parse_hn_story(hit)
    return _parse_discourse_topic(hit)


def parse_comments(community, hits):
    community = resolve_community(community)
    if community == "hacker_news":
        return _parse_hn_comments(hits)
    return _parse_discourse_posts(hits)


def parse_thread(community, story_hit, comment_hits, community_base=""):
    community = resolve_community(community)
    story = parse_story(community, story_hit)
    profile = PROFILE_PATHS[community]
    story["thread_path"] = profile["thread_path"].format(id=story["id"])
    template = profile["thread_url"]
    if "{base}" in template:
        base = str(community_base or "").strip().rstrip("/")
        if not base:
            raise ValueError("community_base is required for the discourse profile")
        story["thread_url"] = template.format(base=base, id=story["id"])
    else:
        story["thread_url"] = template.format(id=story["id"])
    return {"story": story, "comments": parse_comments(community, comment_hits)}


def _parse_hn_story(hit):
    story_id = str(hit.get("objectID") or "").strip()
    if not story_id:
        raise ValueError("story hit carries no id")
    return {
        "id": story_id,
        "title": str(hit.get("title") or "").strip()[:300],
        "url": str(hit.get("url") or ""),
        "points": _number(hit.get("points")),
        "comment_count": _number(hit.get("num_comments")),
        "author": str(hit.get("author") or ""),
        "created_at": str(hit.get("created_at") or ""),
        "community": "hacker_news",
    }


def _parse_hn_comments(hits):
    comments = []
    for hit in hits or []:
        text = str(hit.get("comment_text") or "").strip()
        if not text:
            continue
        comments.append(
            {
                "text": text[:20000],
                "author": str(hit.get("author") or ""),
                "created_at": str(hit.get("created_at") or ""),
            }
        )
    return comments


def _parse_discourse_topic(hit):
    topic_id = str(hit.get("id") or hit.get("topic_id") or "").strip()
    if not topic_id:
        raise ValueError("story hit carries no id")
    posts_count = _number(hit.get("posts_count"))
    title = _text(hit.get("title")) or _text(hit.get("topic_title")) or ""
    return {
        "id": topic_id,
        "title": title[:300],
        "url": str(hit.get("url") or ""),
        "points": _number(hit.get("like_count")),
        "comment_count": max(posts_count - 1, 0),
        "author": str(hit.get("username") or ""),
        "created_at": str(hit.get("created_at") or ""),
        "community": "discourse",
    }


def _parse_discourse_posts(hits):
    posts = []
    for hit in hits or []:
        text = _text(hit.get("cooked") or hit.get("blurb") or hit.get("excerpt"))
        if not text:
            continue
        posts.append(
            {
                "text": text[:20000],
                "author": str(hit.get("username") or ""),
                "created_at": str(hit.get("created_at") or ""),
            }
        )
    return posts


def select_threads(story_records, limit=3):
    limit = int(limit)
    if limit < 1:
        raise ValueError("limit must be at least 1")
    seen = set()
    ranked = []
    for story in story_records:
        story_id = str(story.get("id") or "")
        if not story_id or _number(story.get("comment_count")) <= 0:
            continue
        if story_id in seen:
            continue
        seen.add(story_id)
        score = 2 * _number(story.get("comment_count")) + _number(story.get("points"))
        ranked.append({"id": story_id, "title": str(story.get("title") or ""), "score": score})
    ranked.sort(key=lambda row: (-row["score"], row["id"]))
    return ranked[:limit]


def validate_thread(thread):
    if not isinstance(thread, dict):
        raise ValueError("thread must be an object")
    story = thread.get("story")
    comments = thread.get("comments")
    if not isinstance(story, dict) or not str(story.get("id") or "").strip():
        raise ValueError("thread misses its story id")
    if not isinstance(comments, list):
        raise ValueError("thread comments must be a list")
    if not comments:
        raise ValueError("thread was never read")
    for comment in comments:
        text = str(comment.get("text") or "").strip()
        if not text:
            raise ValueError("comment text is empty")
        if len(text) > 20000:
            raise ValueError("comment text is too large")
    return thread


def _tokens(value):
    return set(_TOKEN.findall(str(value or "").lower()))


def rank_opportunities(threads, buyer_context, commits):
    threads = list(threads or [])
    by_id = {}
    for thread in threads:
        validate_thread(thread)
        by_id[str(thread["story"]["id"])] = thread
    tokens = _tokens(buyer_context)
    markers = set()
    rows = []
    for commit in commits or []:
        kind = commit.get("kind")
        if kind not in OPPORTUNITY_KINDS:
            raise ValueError(f"unknown opportunity kind {kind!r}")
        thread_id = str(commit.get("thread_id") or "")
        thread = by_id.get(thread_id)
        if thread is None:
            raise ValueError(
                "commit for thread "
                + thread_id
                + " was not read; only read threads may feed the ledger"
            )
        quote = str(commit.get("quote") or "").strip()
        marker = "|".join((kind, thread_id, quote[:90].lower()))
        occurrences = [thread_id]
        for extra in commit.get("extra_thread_ids") or []:
            extra = str(extra)
            if extra in by_id and extra != thread_id:
                occurrences.append(extra)
        occurrences = sorted(set(occurrences))
        fit = min(len(set(_tokens(quote)) & tokens), 4)
        score = len(occurrences) * 2 + fit
        if marker in markers:
            continue
        markers.add(marker)
        rows.append(
            {
                "kind": kind,
                "quote": quote,
                "author": str(commit.get("author") or ""),
                "thread_id": thread_id,
                "occurrences": occurrences,
                "evidence": len(occurrences),
                "fit": fit,
                "score": score,
                "url": str(thread["story"].get("thread_url") or ""),
                "title": str(thread["story"].get("title") or ""),
            }
        )
    rows.sort(key=lambda row: (-row["score"], row["thread_id"], row["quote"]))
    return rows
```