"""A client's private X List, read through Tin's own X account, one paid page at a time.

Every client List lives on one X connection: the intel host project's
(`TIN_LITE_INTEL_HOST_PROJECT_ID`, Tin Computer in production). X keeps only the newest
connection when one account is connected to several projects, so a client project's own X
connection is never used. The List is the one an operator pinned for the client
(`intel_projects.x_list_id`, `tin-lite intel-x-list`), copied onto each run at prepare; a
client's member can't name another. Before any page, one receipted lookup
(`XConnection.list_owner`) confirms Tin's account owns the List. The read goes through
`XConnection.list_posts` and its member check, unchanged, as the member who connected X on the
host, and touches nothing on the account but that List. Pages come newest
first, 100 posts each, until the oldest post is before the window or X has no more;
X_MAX_PAGES is a runaway guard (Databasin's List took 5).

Each page is a paid read, billed to the client's run like any priced provider call: the page's
ceiling (100 posts at the run's pinned `x_read_pricing`) is reserved, then its usage receipt
(`external_usage_v1`, key `intel:<run>:x:<list>:<n>`) is recorded, then X is asked, and the
page is charged for the posts X returned:

- a completed page is reused with its cursor, and the read goes on from it;
- a page started but never confirmed (X's answer was lost, the worker stopped) may have been
  bought: it is never bought again and never counted as zero; its charge stays pending until
  the run settles, and X stops with `x_uncertain`;
- X refusing before anything was sold (a rate limit, a reconnect, a List it won't serve) is
  recorded on the receipt and charged nothing, and X stops with a named reason;
- no room left in the run's budget or the project's limits stops X before X is asked.

The ownership lookup has its own receipt and isn't priced. The rest of the day goes on whatever
X does.

Plain retweets are dropped unless the saved workflow keeps them: on Databasin's first day they
were a quarter of the volume, arrived truncated, and one in 113 was worth reading. A quote post
keeps the quoted text. Links come from the post's `urls`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from tin_lite.billing_contracts import BillingError, object_value, receipt_charge
from tin_lite.connection_records import record_call
from tin_lite.integrations import (
    IntegrationAuthorizationError,
    IntegrationInputError,
    IntegrationNotConfiguredError,
    IntegrationProviderError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
)
from tin_lite.intel_fetch.items import Item, Window, iso, parse_iso

X_MAX_PAGES = 20
PAGE_SIZE = 100
# X's per-post read price, for the call record's estimate; the charge uses the run's pinned
# x_read_pricing (service_pricing.X_READ_PRICING).
X_POST_USD = 0.005
CAPABILITY = "x.lists.manage"
# A page is a priced provider call: its receipt is the usage receipt billing settles from.
OPERATION = "external_usage_v1"
OWNER_OPERATION = "intel.x_list_owner"
SOURCE_ID = "x-list"

# Each reason's sentence lands in the client's sources.json and the analyst's `intel status`,
# both of which founders can read: plain words only, never Tin's plumbing (the host project,
# connections, operator setup). Operators read the `reason` code next to it, and
# docs/daily-intelligence.md says what each code means.
_UNAVAILABLE = "Skipped: X posts weren't available today."
REASONS = {
    "x_member": _UNAVAILABLE,
    "x_reauthorize": _UNAVAILABLE,
    "x_not_connected": _UNAVAILABLE,
    "x_rate_limited": (
        "Stopped early: X limited how many posts could be read today; the posts before that "
        "are kept."
    ),
    "x_uncertain": (
        "Stopped early: one page of X posts wasn't confirmed, so it wasn't read again; the "
        "posts before it are kept."
    ),
    "x_invalid": _UNAVAILABLE,
    "x_spending": (
        "Stopped early: the run's spending limits left no room for more X posts; the posts "
        "before that are kept."
    ),
    "x_list_unavailable": _UNAVAILABLE,
    "x_list_no_members": (
        "Failed: the X List for this brief has no accounts in it, so there were no posts to read."
    ),
    "x_list_no_posts": "Failed: X returned no posts at all for this brief's X List today.",
    "x_list_not_owned": _UNAVAILABLE,
    "x_list_not_enrolled": _UNAVAILABLE,
    "x_no_list": "Skipped: no X List is set up for this brief.",
    "x_no_host": _UNAVAILABLE,
    "x_not_enrolled": _UNAVAILABLE,
}


def page_key(run_id, list_id: str, page: int) -> str:
    return f"intel:{run_id}:x:{list_id}:{page}"


def owner_key(run_id, list_id: str) -> str:
    return f"intel:{run_id}:x:{list_id}:owner"


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


SKIPPED = frozenset(
    {
        "x_member",
        "x_reauthorize",
        "x_not_connected",
        "x_invalid",
        "x_spending",
        "x_list_unavailable",
        "x_list_not_owned",
        "x_list_not_enrolled",
        "x_no_list",
        "x_no_host",
        "x_not_enrolled",
    }
)
# X answered a List read with errors and no posts (an unknown or private List): nothing was
# sold. Any other unreadable answer may have been, and stays uncertain.
NO_POSTS_ANSWER = "X returned an incomplete result"


def not_saved() -> dict[str, Any]:
    """The X source's status on a run whose saved brief names no List: skipped, said so, so the
    judge's `intel status` and sources.json show X was not read rather than leave it out."""
    return skipped("x_no_list")


def skipped(reason: str) -> dict[str, Any]:
    """The X source's status when the List isn't read at all, with its named reason."""
    return {
        "id": SOURCE_ID,
        "name": "X List",
        "method": "x_list",
        "status": "skipped",
        "error": REASONS[reason],
        "reason": reason,
        "pages": 0,
        "posts_read": 0,
        "retweets_dropped": 0,
        "items_in_window": 0,
        "est_supplier_usd": 0.0,
    }


def _refusal(exc: Exception) -> str:
    text = str(exc).lower()
    if "only the member" in text:
        return "x_member"
    if text.startswith("connect or reconnect"):
        return "x_not_connected"
    return "x_reauthorize"


def _list_refusal(exc: Exception) -> str | None:
    """A List X answered but wouldn't serve: unknown, private to another account, or gone.
    None when the answer doesn't say so."""
    detail = getattr(exc, "provider_error", None)
    status = getattr(detail, "status", None)
    if isinstance(exc, IntegrationAuthorizationError):
        # A 403 on the List itself; X's plan or app refusals ("client-forbidden") mean the
        # connection needs attention instead.
        if status == 403 and getattr(detail, "type", None) != "client-forbidden":
            return "x_list_unavailable"
        return None
    if isinstance(exc, IntegrationProviderError):
        return "x_list_unavailable" if status in {400, 404} else None
    if isinstance(exc, IntegrationUpstreamError) and str(exc) == NO_POSTS_ANSWER:
        return "x_list_unavailable"
    return None


def _outcome(exc: Exception) -> tuple[str, bool]:
    """A failed X call's reason, and whether X refused it before selling anything (so its
    started receipt is forgotten). Anything else may have been bought and stays uncertain."""
    if isinstance(exc, IntegrationRateLimitedError):
        return "x_rate_limited", True
    if isinstance(exc, IntegrationInputError):
        return "x_invalid", True
    if isinstance(exc, IntegrationAuthorizationError):
        # Refused before X served anything (the List, the member check, a reconnect).
        return _list_refusal(exc) or _refusal(exc), True
    if isinstance(exc, IntegrationUpstreamError) and (reason := _list_refusal(exc)):
        return reason, True
    return "x_uncertain", False


def _count(value) -> int:
    return value if type(value) is int and 0 <= value <= 10_000 else 0


async def _reserve(db, conn, *, run_id: UUID, key: str) -> None:
    """Reserve one page's ceiling on the client run before X is asked (the run's budget and
    the project's limits decide). Nothing to reserve where billing is off."""
    billing = getattr(db, "billing", None)
    if billing is None:
        return
    await billing.begin_operation(
        conn,
        run_id=run_id,
        operation_id=key,
        kind="tool",
        maximum=lambda terms: PAGE_SIZE * terms["x_read_pricing"]["post_read_nanos"],
    )


async def _charge(db, conn, *, key: str, record: dict | None = None, nanos=None, reason=None):
    """Charge a page from its own receipt at the run's pinned price, or nothing for a page X
    refused before selling. A receipt without a priceable count stays pending, never zero."""
    billing = getattr(db, "billing", None)
    if billing is None:
        return
    if record is not None:
        budget = await conn.fetchval(
            "SELECT terms FROM billing_run_budgets WHERE run_id=$1", UUID(record["run_id"])
        )
        priced = receipt_charge(object_value(budget), "tool", record) if budget else None
        if priced is None:
            return
        nanos, observation = priced
    else:
        observation = {"provider": "x", "basis": "refused_before_sale", "reason": reason}
    await billing.observe_operation(conn, operation_id=key, nanos=nanos, observation=observation)


async def read_list(
    db,
    x,
    *,
    run_id: UUID,
    host_project_id: UUID,
    list_id: str,
    window: Window,
    keep_retweets: bool,
    put,
    heartbeat=None,
) -> dict[str, Any]:
    """Read the List for the window through the host project's X connection, once X confirms
    the host's account owns it, page by page under the client run's receipts; `put(conn,
    items)` stores a page's items in the page's own receipt lock. Returns the X source's
    status."""
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

    # Refused before any receipt: X not connected on the host, or not allowed Lists. The List is
    # read as whoever connected X there; the client project's own X connection is never used.
    try:
        connection = await x.connection(host_project_id, CAPABILITY)
    except IntegrationNotConfiguredError:
        return stop("x_not_connected")
    except IntegrationAuthorizationError as exc:
        return stop(_refusal(exc))
    member = connection.connected_by_clerk_user_id
    if not member:
        return stop("x_reauthorize")
    # Only a List Tin's own account owns is read, whatever ID was pinned: one receipted lookup.
    key = owner_key(run_id, list_id)
    async with db.effect_lock(key, OWNER_OPERATION) as (conn, receipt):
        if receipt is not None and receipt.status == "completed":
            owned = (receipt.result or {}).get("owned") is True
            members = (receipt.result or {}).get("member_count")
        elif receipt is not None:
            return stop("x_uncertain")
        else:
            await db.start_effect(conn, execution_key=key, operation=OWNER_OPERATION)
            try:
                found = await x.list_owner(host_project_id, member, list_id=list_id)
            except Exception as exc:  # noqa: BLE001 — sorted by what X may have sold
                reason, refused = _outcome(exc)
                if refused:
                    await db.discard_started_effect(conn, execution_key=key)
                return stop(reason)
            owned = found.get("owned") is True
            members = found.get("member_count")
            await record_call(
                db,
                connection=connection,
                capability=CAPABILITY,
                fingerprint=hashlib.sha256(f"owner|{list_id}".encode()).hexdigest(),
                execution_key=key,
                run_id=run_id,
                status="completed",
                response_summary={"owned": owned, "member_count": members},
            )
            await db.complete_effect(
                conn, execution_key=key, result={"owned": owned, "member_count": members}
            )
    if not owned:
        return stop("x_list_not_owned")
    # A List with no members has nothing to sell: no page is bought, and the gap is a failure
    # with its reason rather than a quiet day (prod 2026-11-26, two clients' empty Lists).
    if members == 0:
        return stop("x_list_no_members")
    cursor: str | None = None
    returned_total = 0
    for number in range(1, X_MAX_PAGES + 1):
        if heartbeat is not None:
            heartbeat({"x_page": number})
        key = page_key(run_id, list_id, number)
        async with db.effect_lock(key, OPERATION) as (conn, receipt):
            if receipt is not None and receipt.status == "completed":
                result = receipt.result or {}
                if result.get("refused"):
                    return stop(result["refused"])
            elif receipt is not None:
                # Started before and never confirmed: X may have sold this page already.
                return stop("x_uncertain")
            else:
                # The client's run pays for the page: its ceiling is reserved before X is asked.
                try:
                    await _reserve(db, conn, run_id=run_id, key=key)
                except BillingError as exc:
                    return stop(
                        "x_uncertain" if exc.code == "operation_already_attempted" else "x_spending"
                    )
                record = {
                    "version": 1,
                    "run_id": str(run_id),
                    "provider": "x",
                    "category": "tool",
                    "endpoint": "lists/tweets",
                    "step": f"x:{list_id}:{number}",
                    "attempted_at": iso(datetime.now(UTC)),
                    "outcome": "unconfirmed",
                    "usage": None,
                    "reported_cost_usd": None,
                }
                await db.start_effect(conn, execution_key=key, operation=OPERATION)
                await db.save_effect_progress(conn, execution_key=key, result=record)
                try:
                    page = await x.list_posts(
                        host_project_id,
                        member,
                        list_id=list_id,
                        max_results=PAGE_SIZE,
                        pagination_token=cursor,
                    )
                except Exception as exc:  # noqa: BLE001 — sorted by what X may have sold
                    reason, refused = _outcome(exc)
                    if refused:
                        # X refused before selling anything: a known outcome, charged nothing.
                        refusal = {**record, "outcome": "refused", "refused": reason}
                        await db.complete_effect(conn, execution_key=key, result=refusal)
                        await _charge(db, conn, key=key, nanos=0, reason=reason)
                    return stop(reason)
                posts = [p for p in page.get("posts") or [] if isinstance(p, dict)]
                # X sells every post it returns, protected ones it only counted included.
                returned = len(posts) + _count(page.get("left_out_protected"))
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
                    status="completed",
                    response_summary={
                        "posts": returned,
                        "est_supplier_usd": f"{X_POST_USD * returned:.3f}",
                    },
                )
                oldest = _oldest(posts)
                result = {
                    **record,
                    "outcome": "response_received",
                    "observed_at": iso(datetime.now(UTC)),
                    "usage": {"posts_returned": returned},
                    "cursor": page.get("next_token"),
                    "posts": returned,
                    "kept": len(items),
                    "oldest": iso(oldest),
                    **counts,
                }
                await db.complete_effect(conn, execution_key=key, result=result)
                await _charge(db, conn, key=key, record=result)
        status["pages"] += 1
        status["posts_read"] += int(result.get("posts_read", result.get("posts", 0)))
        status["retweets_dropped"] += int(result.get("retweets_dropped", 0))
        status["items_in_window"] += int(result.get("kept", 0))
        status["est_supplier_usd"] = round(
            status["est_supplier_usd"] + X_POST_USD * int(result.get("posts", 0)), 3
        )
        returned_total += int(result.get("posts", 0))
        cursor = result.get("cursor")
        oldest = parse_iso(result.get("oldest"))
        if not cursor or (oldest is not None and oldest < window.since):
            break
    if not returned_total:
        # Not one post, in the window or out: the List is empty or unreadable, not quiet.
        status.update(status="failed", reason="x_list_no_posts", error=REASONS["x_list_no_posts"])
    elif not status["items_in_window"]:
        status["status"] = "empty"
    return status
