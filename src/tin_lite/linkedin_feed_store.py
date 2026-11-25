"""Posts from a member's LinkedIn home feed, kept for the project's content work.

`social.linkedin_feed` reads the member's home feed with their saved LinkedIn session, the
first `pages` pages of ten, and keeps every post it shows: author, words, media, why LinkedIn
showed it and its counts. A post new to the project also gets its first page of comments and
its first `reactors` reactors, read once. A post seen again only has its counts refreshed.

Several members of a project can each save their own copy; a post their feeds share is kept
once (`linkedin_feed_posts`, by its thread) and each feed's sighting separately
(`linkedin_feed_sightings`: whose feed, which run, where, why).

Each run writes the posts its feed showed, with their comments and reactors, to project Files:
`social/linkedin/feed/<date>-<run>.json` for the content workflows that read them, and a
Markdown digest beside it. Nothing here rates or scores anyone.

Only counts pass through Temporal. Posts stay in Postgres and in the rendered files; where the
next feed read starts stays in its read's receipt.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any
from uuid import UUID

from tin_lite.domain import LINKEDIN_FEED_WORKFLOW_NAME
from tin_lite.linkedin_engagement import REACTIONS_PAGE
from tin_lite.linkedin_feed import DETAIL_REQUESTS, FEED_PAGES_PER_READ, MAX_REACTOR_PAGES, cost

KEY = LINKEDIN_FEED_WORKFLOW_NAME
WORKFLOW_ID = UUID("8f1c2b7e-5d43-4a9e-b6f0-3c7d9e2a1f54")
SCHEMA = "tin.social.linkedin_feed.v1"
PAGES_DEFAULT = 10
PAGES_MAX = 30
REACTORS_DEFAULT = 10
REACTORS_MAX = REACTIONS_PAGE * MAX_REACTOR_PAGES
# Feed reads per run: as many as the most pages a run may ask for.
FEED_READS = math.ceil(PAGES_MAX / FEED_PAGES_PER_READ)
# Detail reads per run: a runaway guard well above a day's new posts.
DETAIL_READS = 25
# Posts an earlier run left without details are read by a later run while this recent.
CATCH_UP = "3 days"
FOLDER = "social/linkedin/feed"
LIMITS = {"feed.json": 8_000_000, "feed.md": 1_000_000}
EXCERPT = 400
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "pages": {
            "type": "integer",
            "minimum": 1,
            "maximum": PAGES_MAX,
            "default": PAGES_DEFAULT,
            "description": "Feed pages to read, ten posts each.",
        },
        "reactors": {
            "type": "integer",
            "minimum": 0,
            "maximum": REACTORS_MAX,
            "default": REACTORS_DEFAULT,
            "description": "How many of each new post's reactors to keep; 0 keeps none.",
        },
    },
    "required": ["project_id"],
}


def options(run_input) -> tuple[int, int]:
    """The run's pages and reactors, from its pinned input."""

    run_input = run_input or {}
    pages = run_input.get("pages", PAGES_DEFAULT)
    reactors = run_input.get("reactors", REACTORS_DEFAULT)
    pages = pages if type(pages) is int else PAGES_DEFAULT
    reactors = reactors if type(reactors) is int else REACTORS_DEFAULT
    return max(1, min(pages, PAGES_MAX)), max(0, min(reactors, REACTORS_MAX))


def paths(run_id, now: datetime) -> dict[str, str]:
    stem = f"{FOLDER}/{now.date().isoformat()}-{str(run_id)[:8]}"
    return {"feed.json": f"{stem}.json", "feed.md": f"{stem}.md"}


async def record_posts(conn, *, project_id, run_id, member: str, posts: list[dict]) -> dict:
    """Keep a feed read's posts and this feed's sighting of each; refresh a known post."""

    new = shown = 0
    for post in posts:
        stored = {k: v for k, v in post.items() if k not in {"position", "reason"}}
        inserted = await conn.fetchval(
            """INSERT INTO linkedin_feed_posts (project_id, thread, post)
               VALUES ($1, $2, $3::jsonb)
               ON CONFLICT (project_id, thread) DO UPDATE
                 SET post = EXCLUDED.post, last_seen_at = now()
               RETURNING xmax = 0""",
            project_id,
            post["thread"],
            json.dumps(stored),
        )
        new += bool(inserted)
        # The same post twice in one feed (two friends liked it) keeps its first place.
        status = await conn.execute(
            """INSERT INTO linkedin_feed_sightings
                 (run_id, thread, project_id, member, position, reason)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (run_id, thread) DO NOTHING""",
            run_id,
            post["thread"],
            project_id,
            member,
            post["position"],
            post.get("reason"),
        )
        shown += status == "INSERT 0 1"
    return {"posts": shown, "new": new}


# Posts whose details Tin hasn't read: this run's in feed order, then those an earlier run
# found recently and didn't finish, newest first.
_WAITING = f"""
    FROM linkedin_feed_posts p
    LEFT JOIN linkedin_feed_sightings s
      ON s.project_id = p.project_id AND s.thread = p.thread AND s.run_id = $1
    WHERE p.project_id = $2 AND p.details_read_at IS NULL
      AND (s.run_id IS NOT NULL OR p.last_seen_at > now() - interval '{CATCH_UP}')"""


async def need_details(conn, *, project_id, run_id, reactors: int) -> list[dict]:
    """The next posts whose comments and reactors Tin hasn't read, as many as one detail read
    takes: this run's in feed order first, then any an earlier run left unread in the last
    few days. Promoted posts and posts with nothing to read are settled without a read."""

    rows = await conn.fetch(
        f"""SELECT p.thread, p.post {_WAITING}
            ORDER BY s.position NULLS LAST, p.last_seen_at DESC, p.thread""",
        run_id,
        project_id,
    )
    batch, spent, settled = [], 0, []
    for row in rows:
        post = json.loads(row["post"]) if isinstance(row["post"], str) else row["post"]
        counts = post.get("counts") or {}
        wanted = {
            "thread": row["thread"],
            "comments": (counts.get("comments") or 0) > 0,
            "reactor_pages": math.ceil(
                min(reactors, counts.get("reactions") or 0) / REACTIONS_PAGE
            ),
        }
        if post.get("promoted") or not cost(wanted):
            settled.append(row["thread"])
            continue
        if spent + cost(wanted) > DETAIL_REQUESTS:
            break
        batch.append(wanted)
        spent += cost(wanted)
    if settled:
        await conn.execute(
            """UPDATE linkedin_feed_posts SET details_read_at = now()
               WHERE project_id = $1 AND thread = ANY($2::text[])""",
            project_id,
            settled,
        )
    return batch


async def record_details(conn, *, project_id, details: list[dict]) -> int:
    """Keep each post's comments and reactors, and settle it either way: a post whose details
    couldn't be read isn't asked for again. Returns how many were read."""

    for item in details:
        await conn.execute(
            """UPDATE linkedin_feed_posts
               SET comments = $3::jsonb, comments_total = $4,
                   reactors = $5::jsonb, reactors_total = $6, details_read_at = now()
               WHERE project_id = $1 AND thread = $2""",
            project_id,
            item["thread"],
            json.dumps(item.get("comments")) if item.get("comments") is not None else None,
            item.get("comments_total"),
            json.dumps(item.get("reactors")) if item.get("reactors") is not None else None,
            item.get("reactors_total"),
        )
    return sum(not item.get("unread") for item in details)


async def details_left(conn, *, project_id, run_id) -> bool:
    return bool(await conn.fetchval(f"SELECT EXISTS (SELECT 1 {_WAITING})", run_id, project_id))


async def run_posts(conn, *, project_id, run_id) -> list[dict]:
    """Every post this run's feed showed, in feed order, with what Tin read about it."""

    rows = await conn.fetch(
        """SELECT s.position, s.reason, s.seen_at, p.post, p.comments, p.comments_total,
                  p.reactors, p.reactors_total, p.first_seen_at
           FROM linkedin_feed_sightings s
           JOIN linkedin_feed_posts p USING (project_id, thread)
           WHERE s.run_id = $1 AND p.project_id = $2
           ORDER BY s.position""",
        run_id,
        project_id,
    )

    def loaded(value):
        return json.loads(value) if isinstance(value, str) else value

    return [
        {
            **loaded(row["post"]),
            "position": row["position"],
            "reason": row["reason"],
            "first_seen_at": row["first_seen_at"].isoformat(),
            "comments": loaded(row["comments"]),
            "comments_total": row["comments_total"],
            "reactors": loaded(row["reactors"]),
            "reactors_total": row["reactors_total"],
        }
        for row in rows
    ]


def sentence(notes: dict) -> str:
    """The run's one line: what it read."""

    posts, new, detailed = notes.get("posts", 0), notes.get("new", 0), notes.get("detailed", 0)
    text = f"{posts} post{'s' if posts != 1 else ''} read from your LinkedIn feed"
    if new != posts:
        text += f", {new} new"
    text += f"; comments and reactors read for {detailed}"
    if notes.get("failed"):
        text += f" ({notes['failed']} couldn't be read)"
    return text + "."


def _count(value) -> str:
    return "?" if value is None else f"{value:,}"


def render(posts: list[dict], *, run_id, notes: dict, now: datetime) -> dict[str, str]:
    """The run's files: every post as JSON, and a digest to read."""

    document = {
        "schema": SCHEMA,
        "run_id": str(run_id),
        "read_at": now.isoformat(),
        "posts": posts,
    }
    day = f"{now.day} {now.strftime('%B %Y')}"
    lines = [f"# Your LinkedIn feed, {day}", ""]
    summary = sentence(notes)
    if notes.get("notice"):
        summary = f"{notes['notice']} {summary}"
    lines += [summary, ""]
    if not posts:
        lines += ["LinkedIn showed no posts this time.", ""]
    for post in posts:
        author = post["author"]
        who = author["name"] + (f", {author['headline']}" if author.get("headline") else "")
        lines += [f"## {post['position'] + 1}. {who}", ""]
        facts = [f"[Open on LinkedIn]({post['url']})", post["published_at"][:10]]
        if post.get("reason"):
            facts.append(post["reason"])
        if post.get("promoted"):
            facts.append("Promoted")
        counts = post.get("counts") or {}
        facts.append(
            f"{_count(counts.get('reactions'))} reactions, {_count(counts.get('comments'))} "
            f"comments, {_count(counts.get('reposts'))} reposts"
        )
        lines += [" · ".join(facts), ""]
        body = post.get("text") or ""
        if body:
            excerpt = body[:EXCERPT] + ("…" if len(body) > EXCERPT else "")
            lines += ["\n".join("> " + part for part in excerpt.splitlines() or [""]), ""]
        if post.get("reshared"):
            original = post["reshared"]
            name = (original.get("author") or {}).get("name") or "someone"
            lines += [f"Reposts {name}: {(original.get('text') or '')[:200]}", ""]
        if post.get("comments"):
            top = post["comments"][:3]
            lines += [
                f"- {c['author']['name']}: {' '.join(c['text'].split())[:200]}" for c in top
            ] + [""]
    return {
        "feed.json": json.dumps(document, ensure_ascii=False, indent=1),
        "feed.md": "\n".join(lines).rstrip() + "\n",
    }
