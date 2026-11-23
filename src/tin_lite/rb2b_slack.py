"""RB2B visitors in Slack: a message for each good or strong fit, and a digest of the rest.

A visitor rated 4 (strong fit) or 3 (good fit) gets their own message as soon as their run has
rated them (`rb2b_notify`). Everyone else of the day (weak fits, people who aren't a fit,
people Tin couldn't rate) goes in one digest at 18:00 in the project's time zone, covering the
day since the previous digest. A day with nobody to list posts nothing.

These posts are for Tin's managed clients. Where they go is one lookup (`destination`): the
channel Tin shares with the client in Tin's own Slack workspace, which an operator sets on the
project's RB2B hook (`tin-lite rb2b-channel`), posted through Tin's own Slack connection, the
one of the project TIN_LITE_RB2B_SLACK_PROJECT_ID names. Either unset, nothing posts.

Slack documents no idempotency key for chat.postMessage, so each message is recorded as
`sending`, and committed, before Slack is called (`rb2b_slack_posts`). A row left `sending` may
have been posted: it becomes `unknown` and is never sent again. A refused post is recorded as
failed; it never fails the run.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from tin_lite import people_records as records
from tin_lite.integrations import (
    IntegrationDeliveryUnknownError,
    IntegrationError,
    IntegrationRateLimitedError,
)

logger = logging.getLogger(__name__)
# A good or strong fit gets a message of their own; the rest wait for the digest.
POSTED = {4: ("🟢", "Strong fit"), 3: ("🟡", "Good fit")}
INTENT = {"hot": "🔥 hot", "warm": "🌤 warm", "cold": "🧊 cold"}
DIGEST_AT = time(18, 0)
DIGEST_LISTED = 15
LOOP_SECONDS = 300
TEXT = 2900
UNMATCHED = re.compile(r"no matching visit in PostHog|none clearly theirs|PostHog didn't answer")
# Links in a message are only ever these: a LinkedIn profile People rebuilt from the person's
# identifiers, and a PostHog replay of People's own shape. Anything else gets no button.
LINKEDIN = re.compile(r"https://www\.linkedin\.com/in/[A-Za-z0-9._~%-]{1,200}")


@dataclass(frozen=True)
class Destination:
    # The project whose Slack connection posts, and where.
    slack_project_id: UUID
    channel_id: str
    channel_name: str


async def destination(db, settings, project_id: UUID) -> Destination | None:
    """Where this project's visitors are posted, or None when this deployment posts no RB2B
    visitors or no operator set the project's channel."""

    poster = getattr(settings, "rb2b_slack_project_id", None)
    if poster is None:
        return None
    row = await db.pool.fetchrow(
        "SELECT slack_channel_id, slack_channel_name FROM rb2b_hooks WHERE project_id = $1",
        project_id,
    )
    if row is None or not row["slack_channel_id"]:
        return None
    return Destination(poster, row["slack_channel_id"], row["slack_channel_name"])


# The messages.


def _escape(text: Any) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _field(person: dict, key: str, part: str = "text"):
    return (person["fields"].get(key) or {}).get(part)


def _fit(person: dict) -> int | None:
    number = _field(person, "icp_fit", "number")
    return int(number) if number is not None else None


def _profile(person: dict) -> str:
    url = person.get("profile_url") or ""
    return url if LINKEDIN.fullmatch(url) else ""


def _replay(visit: dict | None) -> str:
    url = (visit or {}).get("replay_url")
    return url if isinstance(url, str) and records.REPLAY.fullmatch(url) else ""


def _who(person: dict) -> str:
    name = _escape(_field(person, "name") or "Someone")
    link = _profile(person)
    who = f"*<{link}|{name}>*" if link else f"*{name}*"
    headline = _field(person, "headline")
    return f"{who}  ·  {_escape(headline)}" if headline else who


def _account(person: dict) -> str | None:
    """The company as RB2B named it, its website and where the visit came from."""

    company = _field(person, "company")
    reason = _field(person, "company", "reason") or ""
    website = re.search(r"website (\S+)", reason)
    site = re.sub(r"^https?://(www\.)?", "", website[1]).rstrip("/") if website else ""
    bits = [_escape(company) if company else "", _escape(site), _escape(_field(person, "location"))]
    bits = [bit for bit in bits if bit]
    return "🏢 " + "  ·  ".join(bits) if bits else None


def _visit_line(visit: dict | None) -> str | None:
    summary = (visit or {}).get("summary") or ""
    summary = summary.removesuffix(" · identified by RB2B").strip()
    return f"🧭 {_escape(summary)}" if summary else None


def visitor_message(
    person: dict, visit: dict | None, *, people_url: str, project_name: str
) -> dict | None:
    """The Block Kit message for one visitor, or None when they aren't a good or strong fit."""

    fit = _fit(person)
    if fit not in POSTED:
        return None
    emoji, label = POSTED[fit]
    intent_key = _field(person, "intent") or ""
    intent = INTENT.get(intent_key)
    verdict = f"{emoji} *{label}*" + (f"  ·  {intent}" if intent else "")
    verdict += f"   →   *{next_step(fit, intent_key)}*"
    # Who and why read at full size; the account and the visit in Slack's smaller context text.
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": _cut(verdict, TEXT)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _cut(_who(person), TEXT)}},
    ]
    if account := _account(person):
        blocks.append(_context(account))
    if reason := _field(person, "icp_fit", "reason"):
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _cut(f"💬  {_escape(reason)}", TEXT)},
            }
        )
    small = []
    if visit_line := _visit_line(visit):
        small.append(visit_line)
    if UNMATCHED.search((visit or {}).get("summary") or ""):
        small.append("⚠️ PostHog didn't confirm the visit, so RB2B's match is unchecked")
    if small:
        blocks.append(_context("\n".join(small)))
    buttons = []
    if profile := _profile(person):
        buttons.append(_button("LinkedIn", profile, "linkedin"))
    if replay := _replay(visit):
        buttons.append(_button("▶ Replay", replay, "replay"))
    buttons.append({**_button("Open in Tin", people_url, "tin"), "style": "primary"})
    blocks += [
        {"type": "actions", "elements": buttons},
        _context(f"{_escape(project_name)} · RB2B · Tin"),
    ]
    name = _field(person, "name") or "A visitor"
    return {"text": _cut(f"{label}: {name}", 300), "blocks": blocks}


def next_step(fit: int, intent: str) -> str:
    """What to do, from fit and intent: a strong fit showing interest now is today's."""

    if fit == 4:
        return {"hot": "reach out today", "warm": "reach out this week"}.get(
            intent, "worth a message"
        )
    return "reach out this week" if intent == "hot" else "worth a message"


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _cut(text, TEXT)}]}


def _button(text: str, url: str, action: str) -> dict:
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "url": url,
        "action_id": f"rb2b_{action}",
    }


def digest_message(people: list[dict], *, people_url: str, project_name: str) -> dict | None:
    """The day's other visitors in one message, grouped by how they rated, or None for none."""

    if not people:
        return None
    groups = {2: [], 1: [], None: []}
    for person in people:
        groups.get(_fit(person), groups[None]).append(person)
    count = len(people)
    said = f"{count} more visitor{'s' if count != 1 else ''} today"
    head = f"🌙 *{said}*"
    lines = []
    for fit, label in ((2, "🟠 Weak fit"), (1, "⚪ Not a fit"), (None, "❔ Not rated")):
        listed = groups[fit]
        if not listed:
            continue
        shown = [_short(person) for person in listed[:DIGEST_LISTED]]
        more = len(listed) - len(shown)
        tail = f", and {more} more" if more > 0 else ""
        lines.append(f"{label} ({len(listed)}): " + "; ".join(shown) + tail)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _cut("\n".join(lines), TEXT)}},
        {"type": "actions", "elements": [_button("See them in Tin", people_url, "people")]},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{_escape(project_name)} · RB2B · Tin"}],
        },
    ]
    return {"text": said, "blocks": blocks}


def _short(person: dict) -> str:
    name = _escape(_field(person, "name") or "Someone")
    headline = _field(person, "headline")
    return _cut(f"{name}, {_escape(headline)}" if headline else name, 120)


# Sending, recorded before Slack is called.


async def send(db, slack, *, project_id: UUID, key: str, place: Destination, body: dict) -> str:
    """Post one message once. Returns its outcome: sent, failed, unknown, or `already` when this
    key was recorded before (sent or not, it is never sent again)."""

    post_id = uuid4()
    recorded = await db.pool.fetchval(
        """INSERT INTO rb2b_slack_posts (id, project_id, post_key, channel_id, status)
           VALUES ($1, $2, $3, $4, 'sending')
           ON CONFLICT (project_id, post_key) DO NOTHING RETURNING id""",
        post_id,
        project_id,
        key,
        place.channel_id,
    )
    if recorded is None:
        return "already"
    status, ts, code, message = "sent", None, None, None
    try:
        ts = await slack.post(
            place.slack_project_id,
            channel_id=place.channel_id,
            text=body["text"],
            blocks=body["blocks"],
        )
    except IntegrationRateLimitedError:
        status, code, message = "failed", "rate_limited", "Slack asked Tin to wait"
    except IntegrationDeliveryUnknownError:
        status, code = "unknown", "outcome_unknown"
        message = "Slack did not confirm the post, so it may or may not be in the channel"
    except IntegrationError as exc:
        # Refused, or never sent (no connection, a channel Slack won't take): nothing posted.
        status, code, message = "failed", "not_sent", str(exc)
    except Exception:  # noqa: BLE001 - an unexpected error after recording may follow a send
        logger.warning("An RB2B Slack post ended without a known outcome.")
        status, code = "unknown", "outcome_unknown"
        message = "Tin could not tell whether Slack showed the post"
    await db.pool.execute(
        """UPDATE rb2b_slack_posts SET status = $2, message_ts = $3, error_code = $4,
               error_message = $5, settled_at = now()
           WHERE id = $1 AND status = 'sending'""",
        post_id,
        status,
        ts,
        code,
        (message or None) and message[:300],
    )
    return status


async def recover(db, *, older_than_seconds: int = 600) -> None:
    """Posts left `sending` by a crash may be in the channel: never sent again, now unknown."""

    await db.pool.execute(
        """UPDATE rb2b_slack_posts SET status = 'unknown', error_code = 'outcome_unknown',
               error_message = 'Tin stopped while posting, so it may or may not be in the channel',
               settled_at = now()
           WHERE status = 'sending' AND created_at < now() - make_interval(secs => $1)""",
        older_than_seconds,
    )


# The digest.


def digest_window(now: datetime, zone: ZoneInfo) -> tuple[str, datetime, datetime] | None:
    """The local date and [start, end) of the digest due now, or None before 18:00 local."""

    local = now.astimezone(zone)
    if local.time() < DIGEST_AT:
        return None
    end = datetime.combine(local.date(), DIGEST_AT, tzinfo=zone)
    return local.date().isoformat(), end - timedelta(days=1), end


async def digest_people(conn, project_id: UUID, start: datetime, end: datetime) -> list[dict]:
    """RB2B visitors seen in [start, end) whose current fit isn't good or strong."""

    rows = await conn.fetch(
        """SELECT COALESCE(r.merged_into, r.id) AS id,
               max(COALESCE(e.occurred_at, e.recorded_at)) AS seen
           FROM record_events e JOIN records r ON r.id = e.record_id
           WHERE e.project_id = $1 AND e.kind = 'visited' AND e.detail->>'source' = 'rb2b'
             AND COALESCE(e.occurred_at, e.recorded_at) >= $2
             AND COALESCE(e.occurred_at, e.recorded_at) < $3
           GROUP BY 1 ORDER BY 2""",
        project_id,
        start,
        end,
    )
    people = []
    for row in rows:
        found = await records.people(conn, project_id, only=row["id"])
        if found and _fit(found[0]) not in POSTED:
            people.append(found[0])
    return people


async def post_digests(runtime: Any, settings: Any, *, now: datetime | None = None) -> int:
    """Post every digest that is due. Returns how many were posted."""

    from tin_lite.product_urls import dashboard_url

    db = runtime.database
    now = now or datetime.now(UTC)
    await recover(db)
    hooks = await db.pool.fetch(
        """SELECT h.project_id, p.name, p.timezone FROM rb2b_hooks h
           JOIN projects p ON p.id = h.project_id
           WHERE h.slack_channel_id IS NOT NULL"""
    )
    posted = 0
    for hook in hooks:
        try:
            zone = ZoneInfo(hook["timezone"] or "UTC")
        except Exception:  # noqa: BLE001 - an unknown zone reads as UTC
            zone = ZoneInfo("UTC")
        window = digest_window(now, zone)
        if window is None:
            continue
        day, start, end = window
        key = f"digest:{day}"
        if await db.pool.fetchval(
            "SELECT 1 FROM rb2b_slack_posts WHERE project_id = $1 AND post_key = $2",
            hook["project_id"],
            key,
        ):
            continue
        place = await destination(db, settings, hook["project_id"])
        async with db.pool.acquire() as conn:
            people = await digest_people(conn, hook["project_id"], start, end)
        body = digest_message(
            people,
            people_url=f"{dashboard_url(settings)}/people?project={hook['project_id']}",
            project_name=hook["name"],
        )
        if body is None or place is None:
            continue
        if (
            await send(
                db,
                runtime.integrations.slack,
                project_id=hook["project_id"],
                key=key,
                place=place,
                body=body,
            )
            == "sent"
        ):
            posted += 1
    return posted


async def digest_loop(runtime: Any, settings: Any) -> None:
    while True:
        try:
            await post_digests(runtime, settings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad pass never stops the next
            logger.exception("RB2B digests could not be posted this time.")
        await asyncio.sleep(LOOP_SECONDS)
