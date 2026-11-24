"""A client's private X List, read through the project's X connection, one paid page at a time.

The List is read as the member who connected X (XConnection.list_posts and its member check,
unchanged): a scheduled run starts as the saved workflow's creator, so that member saves
`intel.daily`. Pages come newest first, 100 posts each, until the oldest post is before the
window or X has no more; X_MAX_PAGES is a runaway guard (Databasin's List took 5).

Each page is a paid read on Tin's X app, so it has its own receipt, recorded before X is asked
(effect `intel:<run>:x:<list>:<n>`):

- a completed page is reused with its cursor, and the read goes on from it;
- a page started but never confirmed (X's answer was lost, the worker stopped) may have been
  bought: it is never bought again, and X stops with `x_uncertain`;
- X refusing before anything was bought (a rate limit, a member mismatch, a reconnect) forgets
  the started receipt and stops with a named reason; the rest of the day goes on.

Plain retweets are dropped unless the saved workflow keeps them: on Databasin's first day they
were a quarter of the volume, arrived truncated, and one in 113 was worth reading. A quote post
keeps the quoted text. Links come from the post's `urls`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from tin_lite.connection_records import record_call
from tin_lite.integrations import (
    IntegrationAuthorizationError,
    IntegrationInputError,
    IntegrationNotConfiguredError,
    IntegrationRateLimitedError,
)
from tin_lite.intel_fetch.items import Item, Window, iso, parse_iso

X_MAX_PAGES = 20
PAGE_SIZE = 100
# X's per-post read price on Tin's app, for the receipt's estimate (operator-funded).
X_POST_USD = 0.005
CAPABILITY = "x.lists.manage"
OPERATION = "intel.x_list_page"
SOURCE_ID = "x-list"

REASONS = {
    "x_member": (
        "Skipped: only the member who connected X in this project can read its List. Save the "
        "daily brief as that member."
    ),
    "x_reauthorize": "Skipped: X needs to be reconnected in this project's Integrations.",
    "x_not_connected": "Skipped: X isn't connected in this project.",
    "x_rate_limited": "Stopped: X asked Tin to wait before reading more of the List.",
    "x_uncertain": (
        "Stopped: X didn't confirm one page of the List, so Tin didn't buy it again; the posts "
        "before it are kept."
    ),
    "x_invalid": "Skipped: X refused the List ID.",
}


def page_key(run_id, list_id: str, page: int) -> str:
    return f"intel:{run_id}:x:{list_id}:{page}"


def list_url(list_id: str) -> str:
    return f"https://x.com/i/lists/{list_id}"


def is_retweet(post: dict) -> bool:
    return bool(post.get("reposted_by")) or (post.get("text") or "").startswith("RT @")


def to_item(post: dict, list_id: str, fetched_at: str) -> Item | None:
    """One post as a contract item (the prototype's x.py to_item)."""
    author = post.get("author") or {}
    text = post.get("text") or ""
    quoted = post.get("quoted")
    if isinstance(quoted, dict):
        who = (quoted.get("author") or {}).get("username") or (quoted.get("author") or {}).get("id")
        text += f"\n\n[quoting @{who}]: {quoted.get('text') or ''}"
    links = [
        u.get("unwound_url") or u.get("expanded_url")
        for u in post.get("urls") or []
        if isinstance(u, dict) and u.get("expanded_url")
    ]
    if links:
        text += "\n\n[links] " + " ".join(links)
    if post.get("reposted_by"):
        by = (post["reposted_by"] or {}).get("username")
        text = f"[reposted by @{by}]\n\n{text}" if by else text
    metrics = post.get("public_metrics") or {}
    user = author.get("username")
    url = f"https://x.com/{user}/status/{post['id']}" if user else post.get("url")
    published = parse_iso(post.get("created_at"))
    if not url or published is None:
        return None
    return Item(
        source_type="x",
        source="@" + (user or str(author.get("id") or "unknown")),
        source_id=SOURCE_ID,
        source_url=list_url(list_id),
        url=url,
        discussion_url=None,
        publisher_site=None,
        title=None,
        author=author.get("name"),
        published_at=iso(published),
        text=text,
        engagement={
            "likes": metrics.get("like_count"),
            "reposts": metrics.get("repost_count"),
            "replies": metrics.get("reply_count"),
            "quotes": metrics.get("quote_count"),
            "bookmarks": metrics.get("bookmark_count"),
            "impressions": metrics.get("impression_count"),
        },
        fetched_at=fetched_at,
    )


def page_items(
    posts: list[dict], *, list_id: str, window: Window, keep_retweets: bool, fetched_at: str
) -> tuple[list[Item], dict[str, int]]:
    """A page's posts inside the window, retweets dropped unless kept, with counts."""
    counts = {"posts_read": 0, "retweets_dropped": 0, "out_of_window": 0}
    items: list[Item] = []
    for post in posts:
        counts["posts_read"] += 1
        created = parse_iso(post.get("created_at"))
        if created is None or not window.since <= created <= window.until:
            counts["out_of_window"] += 1
            continue
        if not keep_retweets and is_retweet(post):
            counts["retweets_dropped"] += 1
            continue
        item = to_item(post, list_id, fetched_at)
        if item is not None:
            items.append(item)
    return items, counts


def _oldest(posts: list[dict]) -> datetime | None:
    stamps = [parse_iso(p.get("created_at")) for p in posts]
    stamps = [s for s in stamps if s is not None]
    return min(stamps) if stamps else None


SKIPPED = frozenset({"x_member", "x_reauthorize", "x_not_connected", "x_invalid"})


def _refusal(exc: Exception) -> str:
    text = str(exc).lower()
    if "only the member" in text:
        return "x_member"
    if text.startswith("connect or reconnect"):
        return "x_not_connected"
    return "x_reauthorize"


async def read_list(
    db,
    x,
    *,
    run_id: UUID,
    project_id: UUID,
    member: str | None,
    list_id: str,
    window: Window,
    keep_retweets: bool,
    put,
    heartbeat=None,
) -> dict[str, Any]:
    """Read the List for the window, page by page under receipts; `put(conn, items)` stores a
    page's items in the page's own receipt lock. Returns the X source's status."""
    fetched_at = iso(datetime.now(UTC))
    status: dict[str, Any] = {
        "id": SOURCE_ID,
        "name": "X List",
        "method": "x_list",
        "status": "ok",
        "error": None,
        "reason": None,
        "pages": 0,
        "posts_read": 0,
        "retweets_dropped": 0,
        "items_in_window": 0,
        "est_supplier_usd": 0.0,
    }

    def stop(reason: str) -> dict[str, Any]:
        status["reason"] = reason
        status["error"] = REASONS[reason]
        if not status["pages"]:
            status["status"] = "skipped" if reason in SKIPPED else "failed"
        elif not status["items_in_window"]:
            status["status"] = "empty"
        return status

    # Refused before any receipt: not connected, not allowed Lists, or not this member's X.
    try:
        connection = await x.connection(project_id, CAPABILITY)
    except IntegrationNotConfiguredError:
        return stop("x_not_connected")
    except IntegrationAuthorizationError as exc:
        return stop(_refusal(exc))
    if not member or connection.connected_by_clerk_user_id != member:
        return stop("x_member")
    cursor: str | None = None
    for number in range(1, X_MAX_PAGES + 1):
        if heartbeat is not None:
            heartbeat({"x_page": number})
        key = page_key(run_id, list_id, number)
        async with db.effect_lock(key, OPERATION) as (conn, receipt):
            if receipt is not None and receipt.status == "completed":
                result = receipt.result or {}
            elif receipt is not None:
                # Started before and never confirmed: X may have sold this page already.
                return stop("x_uncertain")
            else:
                await db.start_effect(conn, execution_key=key, operation=OPERATION)
                try:
                    page = await x.list_posts(
                        project_id,
                        member,
                        list_id=list_id,
                        max_results=PAGE_SIZE,
                        pagination_token=cursor,
                    )
                except IntegrationRateLimitedError:
                    await db.discard_started_effect(conn, execution_key=key)
                    return stop("x_rate_limited")
                except IntegrationInputError:
                    await db.discard_started_effect(conn, execution_key=key)
                    return stop("x_invalid")
                except IntegrationAuthorizationError as exc:
                    # Refused before X served anything (the member check, a reconnect).
                    await db.discard_started_effect(conn, execution_key=key)
                    return stop(_refusal(exc))
                except Exception:  # noqa: BLE001 — an unknown outcome may have been bought
                    return stop("x_uncertain")
                posts = [p for p in page.get("posts") or [] if isinstance(p, dict)]
                items, counts = page_items(
                    posts,
                    list_id=list_id,
                    window=window,
                    keep_retweets=keep_retweets,
                    fetched_at=fetched_at,
                )
                await put(conn, items)
                await record_call(
                    db,
                    connection=connection,
                    capability=CAPABILITY,
                    fingerprint=hashlib.sha256(f"{list_id}|{cursor or ''}".encode()).hexdigest(),
                    execution_key=key,
                    run_id=run_id,
                    status="ok",
                    response_summary={
                        "posts": len(posts),
                        "est_supplier_usd": f"{X_POST_USD * len(posts):.3f}",
                    },
                )
                oldest = _oldest(posts)
                result = {
                    "cursor": page.get("next_token"),
                    "posts": len(posts),
                    "kept": len(items),
                    "oldest": iso(oldest),
                    **counts,
                }
                await db.complete_effect(conn, execution_key=key, result=result)
        status["pages"] += 1
        status["posts_read"] += int(result.get("posts_read", result.get("posts", 0)))
        status["retweets_dropped"] += int(result.get("retweets_dropped", 0))
        status["items_in_window"] += int(result.get("kept", 0))
        status["est_supplier_usd"] = round(
            status["est_supplier_usd"] + X_POST_USD * int(result.get("posts", 0)), 3
        )
        cursor = result.get("cursor")
        oldest = parse_iso(result.get("oldest"))
        if not cursor or (oldest is not None and oldest < window.since):
            break
    if not status["items_in_window"]:
        status["status"] = "empty"
    return status
