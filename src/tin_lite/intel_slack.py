"""Daily intelligence in Slack: the world summary, and the posting both summaries share.

Messages are posted through Tin's own Slack app: the `messaging.slack` connection of the
operator-configured host project (TIN_LITE_INTEL_HOST_PROJECT_ID). The world summary goes to
TIN_LITE_INTEL_WORLD_SLACK_CHANNEL; a client's brief goes to the channel an operator set for it
(`intel_projects`, `tin-lite intel-channel`). With either unset, nothing posts and the run says
so.

Slack documents no idempotency key for chat.postMessage, so each message is recorded as
`sending`, and committed, before Slack is called (`intel_slack_posts`, unique by post key and
channel). A row left `sending` may have been posted: it becomes `unknown` and is never sent
again. A refused post is recorded as failed and may be tried again for a few hours; posting
never fails the run.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from tin_lite.integrations import (
    IntegrationDeliveryUnknownError,
    IntegrationError,
    IntegrationRateLimitedError,
)
from tin_lite.intel_brief import LINK, MAX_WORLD_ITEMS, default_link

logger = logging.getLogger(__name__)
RETRY_HOURS = 6
POST_TRIES = 3
SECTION_LIMIT = 2900
TEXT_LIMIT = 300
MAX_BLOCKS = 50
# In the order the summary shows them (intel_brief.WORLD_GROUPS): world politics last.
GROUP_TITLES = {
    "tech_ai": "🤖 *Tech and AI*",
    "business": "💼 *Business and markets*",
    "culture": "🎭 *Entertainment and culture*",
    "science_health": "🔬 *Science and health*",
    "world": "🌍 *World*",
}
SENSITIVE = "⚠️"
FALLBACK_NOTE = "Tin wrote this summary from the day's top stories"


def escape(text: Any) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


ZWSP = "\u200b"
_SCHEME = re.compile(r"://")
_DOMAIN = re.compile(r"\b(?:[\w-]+\.)+[A-Za-z][\w-]+\b")


def plain(text: Any) -> str:
    """Text Slack shows as written: escaped, and with nothing it could auto-link. Slack links
    bare URLs and domains itself, so a zero-width space breaks every "://" and the dots of
    anything shaped like a domain; only Tin's own checked links stay links."""
    value = _SCHEME.sub(":" + ZWSP + "//", escape(text))
    return _DOMAIN.sub(lambda match: match.group(0).replace(".", "." + ZWSP), value)


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def link(url: str, words: str) -> str:
    """A Slack link; the URL is checked by the caller and must not break the markup."""
    if not url.startswith(("https://", "http://")) or any(c in url for c in "<>| \n"):
        return plain(words)
    return f"<{url}|{plain(words)}>"


def mrkdwn(text: str, links: dict[str, str] | None = None) -> str:
    """Brief text as Slack mrkdwn: escaped, its checked [words](url) links as Slack links.
    `links` swaps a Google News link for its publisher URL where Tin unwrapped one."""
    links = links or {}
    out = []
    last = 0
    for match in LINK.finditer(text or ""):
        out.append(plain(text[last : match.start()]))
        url = links.get(match.group(2), match.group(2))
        out.append(link(url, match.group(1)))
        last = match.end()
    out.append(plain((text or "")[last:]))
    return "".join(out)


def section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": cut(text, SECTION_LIMIT)}}


def context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": cut(text, SECTION_LIMIT)}]}


def button(text: str, url: str, action_id: str, primary: bool = False) -> dict:
    value = {
        "type": "button",
        "text": {"type": "plain_text", "text": cut(text, 75)},
        "url": url,
        "action_id": action_id,
    }
    if primary:
        value["style"] = "primary"
    return value


def header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": cut(text, 150)}}


def day_title(day: date) -> str:
    return f"{day:%A} {day.day} {day:%B}"


def day_short(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def world_message(
    brief: dict,
    stories: dict[str, dict],
    *,
    day: date,
    items: int,
    sources: int,
    story_count: int,
    high_count: int,
    stories_url: str | None,
    links: dict[str, str] | None = None,
    fallback: bool = False,
) -> dict:
    """The world summary (intel-world-brief/1) as Block Kit. Sensitivity, titles and default
    links come from the story rows; the brief gives the selection, order and one line each.
    `fallback` says Tin wrote the summary itself (the brief's source)."""
    headline = brief.get("headline") or ""
    blocks: list[dict] = [
        header(f"The world today · {day_title(day)}"),
        section(plain(headline)),
        context(
            f"{items:,} items · {sources:,} sources · {story_count:,} stories, {high_count:,} high"
        ),
        {"type": "divider"},
    ]
    any_sensitive = False
    order = list(GROUP_TITLES)
    groups = sorted(
        (g for g in brief.get("groups") or [] if g.get("group") in GROUP_TITLES),
        key=lambda g: order.index(g["group"]),
    )
    for group in groups:
        lines = []
        entries = group.get("items") or []
        if group["group"] == "world":
            # A brief stored before the cap still shows at most two world lines.
            entries = entries[:MAX_WORLD_ITEMS]
        for entry in entries:
            story = stories.get(entry.get("story"))
            if story is None:
                continue
            mark = ""
            if story.get("sensitive"):
                mark, any_sensitive = f"{SENSITIVE} ", True
            url = default_link(story, links)
            title = link(url, story["title"]) if url else plain(story["title"])
            line = mrkdwn(entry.get("line") or "", links)
            lines.append(cut(f"• {mark}{title}" + (f": {line}" if line else ""), 900))
        if lines:
            blocks.append(section(GROUP_TITLES[group["group"]] + "\n" + "\n".join(lines)))
    footer = []
    if any_sensitive:
        footer.append(f"{SENSITIVE} sensitive: never a hook for a brand")
    if stories_url:
        footer.append(link(stories_url, f"All {story_count:,} stories"))
    if fallback:
        footer.append(FALLBACK_NOTE)
    if brief.get("note"):
        footer.append(plain(brief["note"]))
    if footer:
        blocks.append(context(" · ".join(footer)))
    text = cut(f"The world today, {day_short(day)}: {headline}", TEXT_LIMIT)
    return {"text": plain(text), "blocks": blocks[:MAX_BLOCKS]}


# Posting, recorded before Slack is called.


async def send(
    db,
    slack,
    *,
    poster_project_id: UUID,
    project_id: UUID | None,
    key: str,
    channel_id: str,
    body: dict,
) -> str:
    """Post one message once. Returns sent, failed, unknown, or `already` when this key was
    recorded for this channel before. A post Slack definitely didn't take is tried again for
    RETRY_HOURS; one that may be in the channel never is."""
    if not re.fullmatch(r"[A-Z0-9]{1,32}", channel_id or ""):
        return "failed"
    recorded = await db.pool.fetchval(
        """INSERT INTO intel_slack_posts (id, project_id, post_key, channel_id, status)
           VALUES ($1, $2, $3, $4, 'sending')
           ON CONFLICT (post_key, channel_id) DO UPDATE SET status = 'sending',
               error_code = NULL, error_message = NULL, settled_at = NULL
           WHERE intel_slack_posts.status = 'failed'
             AND intel_slack_posts.created_at > now() - make_interval(hours => $5)
           RETURNING id""",
        uuid4(),
        project_id,
        key,
        channel_id,
        RETRY_HOURS,
    )
    if recorded is None:
        return "already"
    status, ts, code, message = "sent", None, None, None
    try:
        ts = await slack.post(
            poster_project_id, channel_id=channel_id, text=body["text"], blocks=body["blocks"]
        )
    except IntegrationRateLimitedError:
        status, code, message = "failed", "rate_limited", "Slack asked Tin to wait"
    except IntegrationDeliveryUnknownError:
        status, code = "unknown", "outcome_unknown"
        message = "Slack did not confirm the post, so it may or may not be in the channel"
    except IntegrationError as exc:
        status, code, message = "failed", "not_sent", str(exc)
    except Exception:  # noqa: BLE001 — an unexpected error after recording may follow a send
        logger.warning("An intel Slack post ended without a known outcome.")
        status, code = "unknown", "outcome_unknown"
        message = "Tin could not tell whether Slack showed the post"
    if ts is not None and not re.fullmatch(r"[0-9]{1,12}\.[0-9]{1,8}", str(ts)):
        ts = None
    await db.pool.execute(
        """UPDATE intel_slack_posts SET status = $2, message_ts = $3, error_code = $4,
               error_message = $5, settled_at = now()
           WHERE id = $1 AND status = 'sending'""",
        recorded,
        status,
        ts,
        code,
        (message or None) and message[:300],
    )
    return status


async def recover(db, *, older_than_seconds: int = 600) -> None:
    """Posts left `sending` by a crash may be in the channel: never sent again, now unknown."""
    await db.pool.execute(
        """UPDATE intel_slack_posts SET status = 'unknown', error_code = 'outcome_unknown',
               error_message = 'Tin stopped while posting, so it may or may not be in the channel',
               settled_at = now()
           WHERE status = 'sending' AND created_at < now() - make_interval(secs => $1)""",
        older_than_seconds,
    )


def retry_refused(outcome: str) -> None:
    """Ask Temporal to run the posting step again when Slack refused the post, unless this is
    the step's last try; the last try never raises, so Slack never fails the run."""
    from temporalio import activity
    from temporalio.exceptions import ApplicationError

    if outcome == "failed" and activity.in_activity() and activity.info().attempt < POST_TRIES:
        raise ApplicationError("Slack didn't take the post; trying again shortly.")
