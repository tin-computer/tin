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
import json
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
RATED = {**POSTED, 2: ("🟠", "Weak fit"), 1: ("⚪", "Not a fit")}
RATED = {fit: f"{emoji} {label}" for fit, (emoji, label) in RATED.items()}
INTENT = {"hot": "🔥 hot", "warm": "🌤 warm", "cold": "🧊 cold"}
DIGEST_AT = time(18, 0)
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


def _verdict(fit: int, intent_key: str) -> str:
    emoji, label = POSTED[fit]
    intent = INTENT.get(intent_key)
    verdict = f"{emoji} *{label}*" + (f"  ·  {intent}" if intent else "")
    return verdict + f"   →   *{next_step(fit, intent_key)}*"


def _message(
    *,
    text: str,
    verdict: str,
    who: str,
    account: str | None,
    reason: str | None,
    warm: str | None,
    small: list[str],
    extra: list[dict],
    buttons: list[dict],
    project_name: str,
) -> dict:
    """The one layout of a person's or a company's message. The verdict, who, why and how to
    reach them read at full size; the account, the visit and its caveats in Slack's smaller
    context text; then any extra sections, the buttons and the footer."""

    blocks: list[dict] = [_section(verdict), _section(who)]
    if account:
        blocks.append(_context(account))
    if reason:
        blocks.append(_section(f"💬  {_escape(reason)}"))
    if warm:
        # A warm path is the most useful line after the reason, so it reads at full size.
        blocks.append(_section(_escape(warm)))
    if small:
        blocks.append(_context("\n".join(small)))
    blocks += extra
    blocks += [
        {"type": "actions", "elements": buttons},
        _context(f"{_escape(project_name)} · Tin"),
    ]
    return {"text": _cut(text, 300), "blocks": blocks}


def visitor_message(
    person: dict,
    visit: dict | None,
    *,
    people_url: str,
    project_name: str,
    warm: str | None = None,
) -> dict | None:
    """The Block Kit message for one visitor, or None when they aren't a good or strong fit.
    `warm` is an optional line on how the team can reach them."""

    fit = _fit(person)
    if fit not in POSTED:
        return None
    small = []
    if visit_line := _visit_line(visit):
        small.append(visit_line)
    if UNMATCHED.search((visit or {}).get("summary") or ""):
        small.append("⚠️ PostHog didn't confirm the visit, so who it was is unchecked")
    buttons = []
    if profile := _profile(person):
        buttons.append(_button("LinkedIn", profile, "linkedin"))
    if replay := _replay(visit):
        buttons.append(_button("▶ Replay", replay, "replay"))
    buttons.append({**_button("Open in Tin", people_url, "tin"), "style": "primary"})
    name = _field(person, "name") or "A visitor"
    return _message(
        text=f"{POSTED[fit][1]}: {name}",
        verdict=_verdict(fit, _field(person, "intent") or ""),
        who=_who(person),
        account=_account(person),
        reason=_field(person, "icp_fit", "reason"),
        warm=warm,
        small=small,
        extra=[],
        buttons=buttons,
        project_name=project_name,
    )


def company_message(
    company: dict,
    people: list[dict],
    *,
    people_url: str,
    project_name: str,
    warm: str | None = None,
) -> dict | None:
    """The Block Kit message for a visit RB2B matched only to a company, or None unless the
    model judged it worth chasing and a good or strong fit. `people` are those Tin found there,
    as People holds them, each in a section of their own, their name linked to their profile;
    `warm` is an optional line on how the team can reach them."""

    fit = company.get("fit")
    if not company.get("worth") or fit not in POSTED or company.get("repeat_of"):
        return None
    name = company.get("name") or "A company"
    size = company.get("employees")
    account = [
        company.get("site"),
        company.get("industry"),
        f"{size} employees" if size else None,
        company.get("location"),
    ]
    account = [_escape(bit) for bit in account if bit]
    small = []
    if page := company.get("page"):
        came = f" · {company['came_from']}" if company.get("came_from") else ""
        small.append(f"🧭 Visited {_escape(page)}{_escape(came)}")
    small.append("⚠️ Matched to the company, not to a person: who visited is unknown")
    extra = [] if people else [_section("Tin found nobody there to contact yet.")]
    for person in people:
        # Their name links to their checked LinkedIn profile, as in a person's message.
        line = _who(person)
        if (rated := _fit(person)) in RATED:
            line += f"  ·  {RATED[rated]}"
        extra.append(_section(line))
    return _message(
        text=f"{POSTED[fit][1]}: {name}",
        verdict=_verdict(fit, company.get("intent") or ""),
        who=f"🏢 *{_escape(name)}*",
        account="  ·  ".join(account) if account else None,
        reason=company.get("reason"),
        warm=warm,
        small=small,
        extra=extra,
        buttons=[{**_button("Open in Tin", people_url, "tin"), "style": "primary"}],
        project_name=project_name,
    )


def next_step(fit: int, intent: str) -> str:
    """What to do, from fit and intent: a strong fit showing interest now is today's."""

    if fit == 4:
        return {"hot": "reach out today", "warm": "reach out this week"}.get(
            intent, "worth a message"
        )
    return "reach out this week" if intent == "hot" else "worth a message"


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _cut(text, TEXT)}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _cut(text, TEXT)}]}


def _button(text: str, url: str, action: str) -> dict:
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "url": url,
        "action_id": f"rb2b_{action}",
    }


def digest_message(
    people: list[dict], *, people_url: str, project_name: str, companies: list[dict] = ()
) -> dict | None:
    """The day's other visitors in one Slack table: a row each, weak fits first, then people
    who aren't a fit, people Tin couldn't rate, and last the companies a visit was matched to
    that didn't get their own message. None for none.

    The table stops at DIGEST_ROWS rows or TABLE_TEXT characters, whichever comes first, and
    says how many more are in Tin. The message's `text`, which notifications and clients
    without tables show, lists them as lines."""

    if not people and not companies:
        return None
    order = {2: 0, 1: 1, None: 2}
    people = sorted(people, key=lambda person: order.get(_fit(person), 2))
    count = len(people) + len(companies)
    said = f"{count} more visitor{'s' if count != 1 else ''} today"
    rows = [_person_row(person) for person in people]
    rows += [_company_row(company) for company in companies]
    header = [_bold(label) for label in ("Name", "Title", "Company", "Fit")]
    table, used = [header], sum(_cell_text(cell) for cell in header)
    for row in rows[:DIGEST_ROWS]:
        size = sum(_cell_text(cell) for cell in row)
        if used + size > TABLE_TEXT:
            break
        table.append(row)
        used += size
    more = len(rows) - (len(table) - 1)
    blocks = [
        _section(f"🌙 *{said}*"),
        _context(_counts(people, companies)),
        {"type": "table", "column_settings": [{"is_wrapped": True}] * 4, "rows": table},
    ]
    if more > 0:
        blocks.append(_context(f"And {more} more in Tin."))
    blocks += [
        {"type": "actions", "elements": [_button("See them in Tin", people_url, "people")]},
        _context(f"{_escape(project_name)} · Tin"),
    ]
    lines = [_line(row) for row in rows]
    return {"text": _cut("\n".join([said, *lines]), TEXT), "blocks": blocks}


# The digest's rows: name, title, company and fit, as Slack table cells. Table cells are raw
# text, not mrkdwn, so nothing in them is escaped.
DIGEST_ROWS = 50
TABLE_TEXT = 9000
FIT_WORDS = {2: "Weak fit", 1: "Not a fit", None: "Not rated"}
EMPTY = "—"


def _counts(people: list[dict], companies: list[dict]) -> str:
    """How the day split: '14 not a fit · 6 matched to a company only'."""

    bits = []
    for fit, words in ((2, "weak fit"), (1, "not a fit"), (None, "not rated")):
        if n := sum(1 for person in people if _fit(person) == fit):
            bits.append(f"{n} {words}")
    if companies:
        bits.append(f"{len(companies)} matched to a company only")
    return " · ".join(bits)


def _title(person: dict) -> str:
    """Their job title: RB2B's position, else the headline without its ' at Company' tail."""

    if position := _field(person, "position"):
        return position
    headline = _field(person, "headline") or ""
    company = _field(person, "company") or ""
    if company and headline.endswith(f" at {company}"):
        return headline[: -len(f" at {company}")]
    return headline


def _person_row(person: dict) -> list[dict]:
    name = _cut(_field(person, "name") or "Someone", 60)
    return [
        _link(name, link) if (link := _profile(person)) else _raw(name),
        _raw(_cut(_title(person), 80)),
        _raw(_cut(_field(person, "company") or "", 60)),
        _raw(FIT_WORDS.get(_fit(person), "Not rated")),
    ]


def _company_row(company: dict) -> list[dict]:
    """A company-only visit: nobody named, the company, and what Tin made of it when that
    isn't the usual (not worth chasing)."""

    fit = company.get("fit")
    if company.get("repeat_of"):
        said = "Visited again"
    elif fit is None and company.get("worth") is None:
        said = "Not rated"
    elif not company.get("worth"):
        said = "Company only"
    else:
        said = RATED.get(fit, "Can't tell").split(" ", 1)[-1]
    name = _cut(company.get("name") or "A company", 60)
    return [_raw("Unknown visitor"), _raw(""), _raw(name), _raw(said)]


def _raw(text: str) -> dict:
    # Slack refuses an empty cell (invalid_blocks), so a missing value reads as a dash.
    return {"type": "raw_text", "text": text or EMPTY}


def _rich(element: dict) -> dict:
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [element]}],
    }


def _bold(text: str) -> dict:
    return _rich({"type": "text", "text": text, "style": {"bold": True}})


def _link(text: str, url: str) -> dict:
    return _rich({"type": "link", "text": text, "url": url})


def _cell_text(cell: dict) -> int:
    if cell["type"] == "raw_text":
        return len(cell["text"])
    element = cell["elements"][0]["elements"][0]
    return len(element["text"]) + len(element.get("url", ""))


def _cell_words(cell: dict) -> str:
    if cell["type"] == "raw_text":
        return cell["text"]
    return cell["elements"][0]["elements"][0]["text"]


def _line(row: list[dict]) -> str:
    """A row as a line of the plain-text fallback: 'Jane Doe, CFO, Acme (Not a fit)'."""

    name, title, company, fit = (_cell_words(cell) for cell in row)
    # The message's text is mrkdwn, unlike the cells: escaped, so a name can't mention anyone.
    who = ", ".join(_escape(bit) for bit in (name, title, company) if bit != EMPTY)
    return f"{who} ({fit})"


# Sending, recorded before Slack is called.


async def send(db, slack, *, project_id: UUID, key: str, place: Destination, body: dict) -> str:
    """Post one message once. Returns its outcome: sent, failed, unknown, or `already` when this
    key was recorded before. A post Slack definitely didn't take (`failed`: rate-limited,
    refused) is tried again for RETRY_HOURS; one that may be in the channel never is."""

    post_id = uuid4()
    recorded = await db.pool.fetchval(
        """INSERT INTO rb2b_slack_posts (id, project_id, post_key, channel_id, status)
           VALUES ($1, $2, $3, $4, 'sending')
           ON CONFLICT (project_id, post_key) DO UPDATE SET status = 'sending',
               channel_id = EXCLUDED.channel_id, error_code = NULL, error_message = NULL,
               settled_at = NULL
           WHERE rb2b_slack_posts.status = 'failed'
             AND rb2b_slack_posts.created_at > now() - make_interval(hours => $5)
           RETURNING id""",
        post_id,
        project_id,
        key,
        place.channel_id,
        RETRY_HOURS,
    )
    if recorded is None:
        return "already"
    post_id = recorded
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


# How long a post Slack refused is tried again (each digest pass, each visitor run retry).
RETRY_HOURS = 6
# Tries of a run's own message, the first included: within its step's three attempts, and the
# last never raises, so a Slack refusal never fails the run.
POST_TRIES = 3


def retry_refused(outcome: str) -> None:
    """Ask Temporal to run the posting step again when Slack refused the post, unless this is
    the step's last try."""

    from temporalio import activity
    from temporalio.exceptions import ApplicationError

    if outcome == "failed" and activity.in_activity() and activity.info().attempt < POST_TRIES:
        raise ApplicationError("Slack didn't take the post; trying again shortly.")


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
    """RB2B visitors Tin recorded in [start, end) whose current fit isn't good or strong. By
    when Tin recorded the visit, not when it happened: a visit is read about ten minutes after
    it, so one just before 18:00 lands in the next digest rather than none."""

    rows = await conn.fetch(
        """SELECT COALESCE(r.merged_into, r.id) AS id,
               max(COALESCE(e.occurred_at, e.recorded_at)) AS seen
           FROM record_events e JOIN records r ON r.id = e.record_id
           WHERE e.project_id = $1 AND e.kind = 'visited' AND e.detail->>'source' = 'rb2b'
             AND e.recorded_at >= $2 AND e.recorded_at < $3
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


async def digest_companies(conn, project_id: UUID, start: datetime, end: datetime) -> list[dict]:
    """Company-only visits received in [start, end) that didn't get their own message."""

    rows = await conn.fetch(
        """SELECT company, payload FROM rb2b_visits
           WHERE project_id = $1 AND kind = 'company' AND received_at >= $2 AND received_at < $3
           ORDER BY received_at""",
        project_id,
        start,
        end,
    )
    found = []
    for row in rows:
        company = row["company"]
        company = json.loads(company) if isinstance(company, str) else dict(company or {})
        if company.get("worth") and company.get("fit") in POSTED and not company.get("repeat_of"):
            continue  # it had its own message
        if not company.get("name"):
            payload = row["payload"]
            payload = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
            company["name"] = payload.get("Company Name")
        found.append(company)
    return found


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
            """SELECT 1 FROM rb2b_slack_posts WHERE project_id = $1 AND post_key = $2
                 AND NOT (status = 'failed'
                          AND created_at > now() - make_interval(hours => $3))""",
            hook["project_id"],
            key,
            RETRY_HOURS,
        ):
            continue
        place = await destination(db, settings, hook["project_id"])
        async with db.pool.acquire() as conn:
            people = await digest_people(conn, hook["project_id"], start, end)
            companies = await digest_companies(conn, hook["project_id"], start, end)
        body = digest_message(
            people,
            companies=companies,
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
