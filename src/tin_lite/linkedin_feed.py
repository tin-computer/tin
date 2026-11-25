"""Read a member's LinkedIn home feed with their saved session.

Each read starts with the same /me check the other readers make, then GETs on
www.linkedin.com, the requests LinkedIn's own web app makes:
- the feed, `GET /voyager/api/feed/updatesV2` with `q=feed`, ten updates a page, carried on by
  LinkedIn's pagination token. Each update is one post with its author, its words, its media,
  why LinkedIn showed it ("Ada likes this", "Suggested") and its counts;
- a post's first page of comments, `GET /voyager/api/feed/comments` with the post's thread;
- a post's first reactors, `GET /voyager/api/voyagerSocialDashReactions`, read as for engagers.

Several feed updates can carry one post (someone liked it, someone else reposted it); a post is
named by its thread (`activity:<id>` or `ugcPost:<id>`), the ID its comments and reactions hang
off. An update Tin can't name a post for is counted, not guessed at. A page Tin can't recognise
at all is refused as `unsupported_contract`.

Reads wait a random few seconds before every request, like a person reading. This module runs
in the LinkedIn sandbox image, so it imports only the standard library, httpx, pydantic and the
collection and session modules copied beside it.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import Field, field_validator

from tin_lite.connection_collection import ClosedModel, CollectionError
from tin_lite.linkedin_engagement import (
    ACTIVITY,
    DEGREE,
    GAP_SECONDS,
    MEMBER_ID,
    REACTION,
    REACTIONS_PAGE,
    Reactor,
    _headers,
    _index,
    _me,
    _sleep,
    reactions_page,
)
from tin_lite.linkedin_session import _get

ORIGIN = "https://www.linkedin.com"
FEED_PATH = "/voyager/api/feed/updatesV2"
COMMENTS_PATH = "/voyager/api/feed/comments"
REACTIONS_PATH = "/voyager/api/voyagerSocialDashReactions"
FEED_PAGE = 10
# Feed pages per read: thirty updates, so the answer stays well inside the sandbox's 400 KB.
FEED_PAGES_PER_READ = 3
COMMENTS_PAGE = 10
# Reactor pages per post: a runaway guard on the founder's `reactors` input.
MAX_REACTOR_PAGES = 10
# Requests per detail read (comments and reactor pages together), with pauses: at most about
# three and a half minutes of waiting, inside the sandbox's five.
DETAIL_REQUESTS = 24
THREAD = re.compile(r"(activity|ugcPost):(\d{15,22})")
COMPANY = re.compile(r"urn:li:(?:fs_|fsd_)?(?:miniCompany|company):(\d{1,20})")
TEXT_MAX = 3500
COMMENT_MAX = 3000
IMAGES_MAX = 4
# Refusals that end the whole read: the account needs its owner, or LinkedIn objected. Anything
# else on one post (a server error, a timeout, an answer Tin can't read) skips that post.
STOPS = frozenset(
    {"session_expired", "account_changed", "challenge", "rate_limited", "access_denied"}
)


class Author(ClosedModel):
    kind: Literal["person", "company", "other"]
    # A person's LinkedIn profile ID, or a company's number.
    id: str | None = Field(default=None, max_length=100)
    name: str = Field(max_length=300)
    headline: str = Field(max_length=1000)
    url: str | None = Field(default=None, max_length=500)
    degree: Literal["1st", "2nd", "3rd"] | None = None


class Media(ClosedModel):
    kind: str = Field(max_length=60)
    title: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=1000)
    images: list[str] = Field(default_factory=list, max_length=IMAGES_MAX)


class Reshared(ClosedModel):
    thread: str | None = None
    author: Author | None = None
    text: str = Field(max_length=TEXT_MAX)


class Counts(ClosedModel):
    reactions: int | None = None
    comments: int | None = None
    reposts: int | None = None
    by_reaction: dict[str, int] = Field(default_factory=dict, max_length=12)


class FeedPost(ClosedModel):
    thread: str
    # Where the post sat in the member's feed, from the top, in this read.
    position: int = Field(ge=0)
    activity_urn: str | None = None
    url: str = Field(max_length=200)
    share_url: str | None = Field(default=None, max_length=500)
    published_at: str = Field(max_length=40)
    author: Author
    # Why LinkedIn showed it: "Ada Yilmaz likes this", "Suggested", or nothing for a post the
    # member follows directly.
    reason: str | None = Field(default=None, max_length=300)
    promoted: bool
    text: str = Field(max_length=TEXT_MAX)
    media: Media | None = None
    reshared: Reshared | None = None
    counts: Counts

    @field_validator("thread")
    @classmethod
    def thread_valid(cls, value):
        if not THREAD.fullmatch(value):
            raise ValueError("thread")
        return value

    @field_validator("activity_urn")
    @classmethod
    def activity_valid(cls, value):
        if value is not None and not ACTIVITY.fullmatch(value):
            raise ValueError("activity")
        return value


class FeedResult(ClosedModel):
    """What a feed read hands back to Tin; the controller checks it before using it."""

    posts: list[FeedPost] = Field(max_length=FEED_PAGE * FEED_PAGES_PER_READ)
    pages: int = Field(ge=0)
    # Updates Tin couldn't name a post for.
    skipped: int = Field(ge=0)
    # Where the next read starts, or None at the end of the feed.
    next_start: int | None = Field(default=None, ge=0)
    token: str | None = Field(default=None, max_length=500)
    read_at: str = Field(max_length=40)


class Comment(ClosedModel):
    id: str = Field(max_length=40)
    author: Author
    text: str = Field(max_length=COMMENT_MAX)
    created_at: str | None = Field(default=None, max_length=40)
    reactions: int | None = None
    replies: int | None = None
    pinned: bool = False
    edited: bool = False


class PostDetails(ClosedModel):
    thread: str
    comments: list[Comment] | None = Field(default=None, max_length=COMMENTS_PAGE)
    comments_total: int | None = None
    reactors: list[Reactor] | None = Field(
        default=None, max_length=REACTIONS_PAGE * MAX_REACTOR_PAGES
    )
    reactors_total: int | None = None
    # Why this post's details couldn't be read, when they couldn't: the read went on without it.
    unread: str | None = Field(default=None, max_length=40)

    @field_validator("thread")
    @classmethod
    def thread_valid(cls, value):
        if not THREAD.fullmatch(value):
            raise ValueError("thread")
        return value


class DetailsResult(ClosedModel):
    posts: list[PostDetails] = Field(max_length=DETAIL_REQUESTS)
    read_at: str = Field(max_length=40)


def text(value, limit: int) -> str:
    """LinkedIn's words as written: NFC, line breaks kept, other control characters dropped."""

    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFC", value)
    value = "".join(c for c in value if c in "\n\t" or unicodedata.category(c) != "Cc")
    return value.strip()[:limit]


def line(value, limit: int) -> str:
    return " ".join(text(value, limit * 2).split())[:limit]


def _view(node, key: str) -> str:
    """The text of a TextViewModel field, or ""."""

    value = node.get(key) if isinstance(node, dict) else None
    return value.get("text", "") if isinstance(value, dict) else ""


def _clean_url(value) -> str | None:
    if not isinstance(value, str) or not value.startswith("https://"):
        return None
    parts = urlsplit(value)
    if parts.netloc.endswith("linkedin.com"):
        # LinkedIn's own links carry tracking parameters; the path names the page.
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:500]
    return value[:1000]


def thread_time(thread: str) -> datetime:
    """When a post was made: LinkedIn's activity and post IDs carry their creation time."""

    match = THREAD.fullmatch(thread)
    if not match:
        raise CollectionError("unsupported_contract")
    return datetime.fromtimestamp((int(match[2]) >> 22) / 1000, UTC)


def _degree(value: str) -> str | None:
    found = DEGREE.search(value or "")
    return found[1] if found else None


def _author(actor, by_urn) -> Author | None:
    if not isinstance(actor, dict):
        return None
    urn = actor.get("urn") if isinstance(actor.get("urn"), str) else ""
    name = _view(actor, "name")
    if not name:
        return None
    ident, kind = None, "other"
    attributes = (actor.get("name") or {}).get("attributes") or []
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        mini = attribute.get("*miniProfile")
        if isinstance(mini, str) and MEMBER_ID.fullmatch(mini.rsplit(":", 1)[-1]):
            ident, kind = mini.rsplit(":", 1)[-1], "person"
            break
        company = attribute.get("*miniCompany")
        if isinstance(company, str) and COMPANY.fullmatch(company):
            ident, kind = COMPANY.fullmatch(company)[1], "company"
            break
    if kind == "other" and urn.startswith("urn:li:member:"):
        kind = "person"
    elif kind == "other" and COMPANY.fullmatch(urn):
        ident, kind = COMPANY.fullmatch(urn)[1], "company"
    navigation = actor.get("navigationContext") or {}
    url = _clean_url(navigation.get("actionTarget") if isinstance(navigation, dict) else None)
    if url is None and kind == "person" and ident:
        mini = by_urn.get(f"urn:li:fs_miniProfile:{ident}") or {}
        slug = mini.get("publicIdentifier")
        if isinstance(slug, str) and slug:
            url = f"{ORIGIN}/in/{slug}/"
    return Author(
        kind=kind,
        id=ident,
        name=line(name, 300),
        headline=line(_view(actor, "description"), 1000),
        url=url,
        degree=_degree(_view(actor, "supplementaryActorInfo")) if kind == "person" else None,
    )


def _media(content) -> Media | None:
    if not isinstance(content, dict) or not isinstance(content.get("$type"), str):
        return None
    name = content["$type"].rsplit(".", 1)[-1]
    kind = {
        "ImageComponent": "image",
        "LinkedInVideoComponent": "video",
        "ExternalVideoComponent": "video",
        "ArticleComponent": "article",
        "DocumentComponent": "document",
        "PollComponent": "poll",
        "EventComponent": "event",
        "JobComponent": "job",
        "CelebrationComponent": "celebration",
    }.get(name, re.sub(r"Component$", "", name).lower()[:60] or "other")
    images = []
    for image in content.get("images") or []:
        for attribute in (image or {}).get("attributes") or []:
            vector = (attribute or {}).get("vectorImage") or {}
            artifacts = [a for a in vector.get("artifacts") or [] if isinstance(a, dict)]
            root = vector.get("rootUrl")
            if not artifacts or not isinstance(root, str):
                continue
            largest = max(artifacts, key=lambda a: a.get("width") or 0)
            segment = largest.get("fileIdentifyingUrlPathSegment")
            if isinstance(segment, str) and root.startswith("https://"):
                images.append((root + segment)[:1000])
    title = _view(content, "title") or ((content.get("document") or {}).get("title") or "")
    navigation = content.get("navigationContext") or {}
    url = navigation.get("actionTarget") if isinstance(navigation, dict) else None
    return Media(
        kind=kind,
        title=line(title, 500) or None,
        url=_clean_url(url),
        images=images[:IMAGES_MAX],
    )


def _counts(detail, by_urn) -> Counts:
    counts = by_urn.get(detail.get("*totalSocialActivityCounts")) or {}

    def number(key):
        value = counts.get(key)
        return value if type(value) is int and value >= 0 else None

    by_reaction = {}
    for item in counts.get("reactionTypeCounts") or []:
        if not isinstance(item, dict):
            continue
        kind, count = item.get("reactionType"), item.get("count")
        if isinstance(kind, str) and REACTION.fullmatch(kind) and type(count) is int:
            by_reaction[kind] = count
    return Counts(
        reactions=number("numLikes"),
        comments=number("numComments"),
        reposts=number("numShares"),
        by_reaction=dict(list(by_reaction.items())[:12]),
    )


def _thread(detail) -> str | None:
    thread = detail.get("threadId") if isinstance(detail, dict) else None
    return thread if isinstance(thread, str) and THREAD.fullmatch(thread) else None


def _reshared(update, by_urn) -> Reshared | None:
    inner = update.get("resharedUpdate")
    if isinstance(update.get("*resharedUpdate"), str):
        inner = by_urn.get(update["*resharedUpdate"])
    if not isinstance(inner, dict):
        return None
    detail = by_urn.get(inner.get("*socialDetail")) or {}
    return Reshared(
        thread=_thread(detail),
        author=_author(inner.get("actor"), by_urn),
        text=text(_view(inner.get("commentary") or {}, "text"), TEXT_MAX),
    )


def feed_post(update, by_urn, *, position: int) -> dict | None:
    """One feed update as a post, or None when Tin can't name the post it carries."""

    if not isinstance(update, dict):
        return None
    detail = by_urn.get(update.get("*socialDetail"))
    thread = _thread(detail)
    author = _author(update.get("actor"), by_urn)
    if thread is None or author is None:
        return None
    meta = update.get("updateMetadata") if isinstance(update.get("updateMetadata"), dict) else {}
    # The post's own activity: its thread when that is an activity, else the update's (a post
    # made as a ugcPost is shown through its activity).
    activity = f"urn:li:{thread}" if thread.startswith("activity:") else None
    if activity is None and isinstance(meta.get("urn"), str) and ACTIVITY.fullmatch(meta["urn"]):
        activity = meta["urn"]
    published = thread_time(activity.removeprefix("urn:li:") if activity else thread)
    tracking = meta.get("trackingData") if isinstance(meta.get("trackingData"), dict) else {}
    when = _view(update.get("actor") or {}, "subDescription")
    promoted = any("sponsor" in key.lower() for key in tracking) or when.startswith("Promoted")
    social = update.get("socialContent") if isinstance(update.get("socialContent"), dict) else {}
    header = update.get("header") if isinstance(update.get("header"), dict) else {}
    post = FeedPost(
        thread=thread,
        position=position,
        activity_urn=activity,
        url=f"{ORIGIN}/feed/update/{activity or 'urn:li:' + thread}/",
        share_url=_clean_url(social.get("shareUrl")),
        published_at=published.isoformat(),
        author=author,
        reason=line(_view(header, "text"), 300) or None,
        promoted=promoted,
        text=text(_view(update.get("commentary") or {}, "text"), TEXT_MAX),
        media=_media(update.get("content")),
        reshared=_reshared(update, by_urn),
        counts=_counts(detail, by_urn),
    )
    return post.model_dump()


def feed_page(payload, *, start: int) -> tuple[list[dict], str | None, int, int]:
    """One feed page: its posts, the next page's token, how many updates it held and how many
    of those Tin couldn't name a post for."""

    data, by_urn = _index(payload)
    refs = data.get("*elements")
    if not isinstance(refs, list):
        raise CollectionError("unsupported_contract")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    token = metadata.get("paginationToken")
    posts, skipped = [], 0
    for offset, ref in enumerate(refs):
        post = feed_post(by_urn.get(ref), by_urn, position=start + offset)
        if post is None:
            skipped += 1
        else:
            posts.append(post)
    token = token if isinstance(token, str) and 0 < len(token) <= 500 else None
    return posts, token, len(refs), skipped


def comments_page(payload) -> tuple[list[dict], int]:
    """A post's first page of comments, and how many it has."""

    data, by_urn = _index(payload)
    paging, refs = data.get("paging"), data.get("*elements")
    if (
        not isinstance(paging, dict)
        or type(paging.get("total")) is not int
        or not isinstance(refs, list)
        or len(refs) > COMMENTS_PAGE
    ):
        raise CollectionError("unsupported_contract")
    comments = []
    for ref in refs:
        item = by_urn.get(ref)
        if not isinstance(item, dict):
            continue
        urn = item.get("urn") if isinstance(item.get("urn"), str) else ""
        ident = re.fullmatch(r"urn:li:comment:\((?:activity|ugcPost):\d+,(\d{1,22})\)", urn)
        commenter = item.get("commenterForDashConversion") or {}
        if not ident or not isinstance(commenter, dict):
            continue
        profile = commenter.get("commenterProfileId") or item.get("commenterProfileId")
        actor = commenter.get("actorUnion") if isinstance(commenter.get("actorUnion"), dict) else {}
        company = COMPANY.fullmatch(actor.get("companyUrn") or "")
        if isinstance(profile, str) and MEMBER_ID.fullmatch(profile):
            kind, author_id = "person", profile
        elif company:
            kind, author_id = "company", company[1]
        else:
            kind, author_id = "other", None
        subtitle = commenter.get("subtitle")
        stats = (
            by_urn.get(
                (by_urn.get(item.get("*socialDetail")) or {}).get("*totalSocialActivityCounts")
            )
            or {}
        )
        created = item.get("createdTime")
        comments.append(
            Comment(
                id=ident[1],
                author=Author(
                    kind=kind,
                    id=author_id,
                    name=line(_view(commenter, "title"), 300),
                    headline=line(subtitle if isinstance(subtitle, str) else "", 1000),
                    url=_clean_url(commenter.get("navigationUrl")),
                    degree=_degree(_view(commenter, "supplementaryActorInfoV2"))
                    if kind == "person"
                    else None,
                ),
                text=text(_view(item, "commentV2"), COMMENT_MAX),
                created_at=datetime.fromtimestamp(created / 1000, UTC).isoformat()
                if type(created) is int
                else None,
                reactions=stats.get("numLikes") if type(stats.get("numLikes")) is int else None,
                replies=stats.get("numComments") if type(stats.get("numComments")) is int else None,
                pinned=item.get("pinned") is True,
                edited=item.get("edited") is True,
            ).model_dump()
        )
    return comments, paging["total"]


async def read_feed(
    session,
    actor,
    *,
    client,
    start: int = 0,
    token: str | None = None,
    pages: int = FEED_PAGES_PER_READ,
    gap=GAP_SECONDS,
    sleep=asyncio.sleep,
    now=None,
) -> dict:
    """Up to `pages` pages of the member's feed from `start`; next_start and token say where
    the next read carries on."""

    if not isinstance(start, int) or start < 0 or start % FEED_PAGE:
        raise CollectionError("unsupported_contract")
    await _me(client, session, actor)
    posts, skipped, read, position, more = [], 0, 0, start, True
    for _ in range(max(1, min(pages, FEED_PAGES_PER_READ))):
        await _sleep(gap, sleep)
        params = {"count": str(FEED_PAGE), "q": "feed", "start": str(position)}
        if token:
            params["paginationToken"] = token
        payload = await _get(
            client,
            session,
            FEED_PATH,
            params,
            request_headers=_headers(session, page="d_flagship3_feed", referer=f"{ORIGIN}/feed/"),
        )
        found, token, seen, unnamed = feed_page(payload, start=position)
        posts.extend(found)
        skipped += unnamed
        read += 1
        position += FEED_PAGE
        if not seen or not token:
            more = False
            break
    return {
        "posts": posts,
        "pages": read,
        "skipped": skipped,
        "next_start": position if more else None,
        "token": token if more else None,
        "read_at": (now or datetime.now(UTC)).isoformat(),
    }


def cost(post: dict) -> int:
    """The requests a post's details take: a comments page and its reactor pages."""

    return int(bool(post.get("comments"))) + max(
        0, min(post.get("reactor_pages", 0), MAX_REACTOR_PAGES)
    )


async def read_details(
    session,
    actor,
    *,
    client,
    posts: list[dict],
    gap=GAP_SECONDS,
    sleep=asyncio.sleep,
    now=None,
) -> dict:
    """For each post: its first page of comments when it has any, and its first reactors when
    `reactor_pages` asks for them."""

    if sum(cost(p) for p in posts) > DETAIL_REQUESTS or len(posts) > DETAIL_REQUESTS:
        raise CollectionError("unsupported_contract")
    for post in posts:
        if not isinstance(post.get("thread"), str) or not THREAD.fullmatch(post["thread"]):
            raise CollectionError("unsupported_contract")
    profile_id = await _me(client, session, actor)
    found = []
    for post in posts:
        try:
            item = await _post_details(
                session, client, post, profile_id=profile_id, gap=gap, sleep=sleep
            )
        except CollectionError as error:
            if error.code in STOPS:
                raise
            item = {"thread": post["thread"], "unread": error.code}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            item = {"thread": post["thread"], "unread": "cloud_failed"}
        found.append(PostDetails.model_validate(item).model_dump())
    return {"posts": found, "read_at": (now or datetime.now(UTC)).isoformat()}


async def _post_details(session, client, post: dict, *, profile_id: str, gap, sleep) -> dict:
    """One post's first comments and first reactors."""

    thread = post["thread"]
    referer = f"{ORIGIN}/feed/update/urn:li:{thread}/"
    item = {"thread": thread}
    if post.get("comments"):
        await _sleep(gap, sleep)
        payload = await _get(
            client,
            session,
            COMMENTS_PATH,
            {
                "count": str(COMMENTS_PAGE),
                "q": "comments",
                "sortOrder": "RELEVANCE",
                "start": "0",
                "updateId": thread,
            },
            request_headers=_headers(session, page="d_flagship3_detail_base", referer=referer),
        )
        item["comments"], item["comments_total"] = comments_page(payload)
    pages = max(0, min(post.get("reactor_pages", 0), MAX_REACTOR_PAGES))
    if pages:
        people, total, position = [], None, 0
        for _ in range(pages):
            await _sleep(gap, sleep)
            payload = await _get(
                client,
                session,
                REACTIONS_PATH,
                {
                    "count": str(REACTIONS_PAGE),
                    "q": "reactionType",
                    "start": str(position),
                    "threadUrn": f"urn:li:{thread}",
                },
                request_headers=_headers(session, page="d_flagship3_detail_base", referer=referer),
            )
            page, total = reactions_page(payload, start=position, profile_id=profile_id)
            people.extend(page)
            position += REACTIONS_PAGE
            if position >= total:
                break
        item["reactors"], item["reactors_total"] = people, total
    # Checked here so a post LinkedIn answered oddly is skipped, not the whole read.
    return PostDetails.model_validate(item).model_dump()
